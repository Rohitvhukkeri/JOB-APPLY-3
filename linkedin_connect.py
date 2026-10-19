"""
Sends LinkedIn connection requests to the people behind hiring posts that
match your skills -- the same post search and filters as
linkedin_post_mailer.py (outreach: in profile.yaml), but the post doesn't
need an email address.

    python linkedin_connect.py             send requests
    python linkedin_connect.py --dry-run   list who would get one, send nothing

  * Only people (linkedin.com/in/...), never company pages.
  * Each person gets one request, ever (connect_log.csv).
  * At most max_connections_per_run per run (default 15). LinkedIn allows
    roughly 100 invitations a week and restricts accounts that send too
    many -- the run stops as soon as LinkedIn shows its limit warning.
  * Requests go without a note: free accounts get only a few notes a month.
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common import guard
from common.profile import Profile
from naukri_apply import StopRun
from linkedin_apply import open_logged_in_context, page_has_stop_signal, li_settings
from linkedin_post_mailer import outreach_settings, search_url, read_posts, evaluate_post, author_details, \
    extract_role, _clean_role

CONNECT_LOG = "connect_log.csv"
LIMIT_PHRASES = ("weekly invitation limit", "reached the weekly limit", "invitation limit",
                 "too many invitations", "you've reached the limit")


def _profile_key(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower()


def log_connect(row: dict):
    fields = ["timestamp", "status", "name", "company", "role", "profile_url", "reason"]
    new_file = not Path(CONNECT_LOG).exists()
    with open(CONNECT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow({"timestamp": datetime.now(), **row})


def load_requested() -> set[str]:
    """Profiles already sent a request (or already connected / pending)."""
    done = set()
    if Path(CONNECT_LOG).exists():
        with open(CONNECT_LOG, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("sent", "pending", "connected"):
                    done.add(_profile_key(row.get("profile_url") or ""))
    return done


def sent_this_week() -> int:
    """Requests sent in the last 7 days, for the weekly cap."""
    if not Path(CONNECT_LOG).exists():
        return 0
    since = datetime.now() - timedelta(days=7)
    n = 0
    with open(CONNECT_LOG, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                when = datetime.fromisoformat((row.get("timestamp") or "").strip())
            except ValueError:
                continue
            n += row.get("status") == "sent" and when >= since
    return n


def _hit_limit(page) -> bool:
    text = (page.locator("body").inner_text(timeout=3000) or "").lower()
    return any(p in text for p in LIMIT_PHRASES)


def send_connect(page, url: str) -> str:
    """Returns sent / pending / connected / unavailable / needs email."""
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(4)
    stop = page_has_stop_signal(page)
    if stop:
        raise StopRun(stop)
    main = page.locator("main")
    if main.locator("button:has-text('Pending')").first.is_visible(timeout=1000):
        return "pending"

    direct = main.locator("button[aria-label^='Invite'][aria-label$='to connect']").first
    if direct.count() and direct.is_visible():
        direct.click()
    else:
        more = main.locator("button:has-text('More')").first
        if not more.count():
            return "unavailable"
        more.click()
        time.sleep(1.5)
        menu = page.locator("[role=menu]").last
        item = menu.get_by_text("Connect", exact=True).first
        if not item.count():
            connected = menu.get_by_text("Remove connection").count() > 0
            page.keyboard.press("Escape")
            return "connected" if connected else "unavailable"
        item.click()

    try:
        dialog = page.locator("[role=dialog]").last
        dialog.wait_for(timeout=8000)
    except PWTimeout:
        return "sent" if main.locator("button:has-text('Pending')").first.is_visible(timeout=2000) else "unavailable"
    if dialog.locator("input[type=email]").count():
        page.keyboard.press("Escape")
        return "needs email"
    send = dialog.locator("button:has-text('Send without a note')").first
    if not send.count():
        send = dialog.get_by_role("button", name="Send", exact=True).first
    if not send.count():
        page.keyboard.press("Escape")
        return "unavailable"
    send.click()
    time.sleep(2)
    if _hit_limit(page):
        raise StopRun("LinkedIn invitation limit reached -- try again next week")
    return "sent"


def run(dry_run: bool = False):
    profile = Profile.load()
    cfg = outreach_settings(profile)
    week_left = max(0, int(cfg["max_connections_per_week"]) - sent_this_week())
    max_n = min(int(cfg["max_connections_per_run"]), week_left)
    print(f"Up to {max_n} requests this run (weekly cap {cfg['max_connections_per_week']}, "
          f"{sent_this_week()} sent in the last 7 days).")
    pace = int(cfg["pace_seconds_between_connections"])
    done = load_requested()
    print(f"{len(done)} people already sent a request in earlier runs will be skipped.")
    queries = [f"hiring {role} {loc}" for loc in cfg["search_locations"] for role in cfg["search_roles"]]
    sent = 0
    seen_posts, seen_people = set(), set()

    with sync_playwright() as p:
        mode = li_settings(profile).get("browser_mode") or profile.data.get("browser_mode", "visible")
        browser = p.chromium.launch(headless=(mode == "headless"), slow_mo=100,
                                    args=["--start-minimized"] if mode == "minimized" else [])
        context, page = open_logged_in_context(browser)
        try:
            for query in queries:
                if sent >= max_n:
                    break
                if not guard.take("linkedin", "searches", profile):
                    raise StopRun("today's LinkedIn search budget is used up (safety: in profile.yaml)")
                print(f"\n--- Posts: {query} ---")
                try:
                    page.goto(search_url(query, cfg), wait_until="domcontentloaded")
                    page.wait_for_selector('[role="listitem"][componentkey^="update-card-focus"]', timeout=15000)
                except PWTimeout:
                    print("  (no posts found)")
                    continue
                stop = page_has_stop_signal(page)
                if stop:
                    raise StopRun(stop)

                # Collect first: visiting a profile leaves the search results page.
                targets = []
                for post in read_posts(page, int(cfg["scrolls_per_search"])):
                    url = post.get("authorUrl") or ""
                    key = _profile_key(url)
                    if "/in/" not in url or not post.get("key") or post["key"] in seen_posts:
                        continue
                    seen_posts.add(post["key"])
                    if key in done or key in seen_people:
                        continue
                    ok, reason, _, match = evaluate_post(post, profile, cfg, require_email=False)
                    if not ok:
                        continue
                    seen_people.add(key)
                    name, company = author_details(post)
                    role = _clean_role(extract_role(post.get("text") or "", profile, match.core), profile)
                    targets.append({"name": name, "company": company, "role": role,
                                    "profile_url": url.split("?")[0], "reason": match.summary()})
                print(f"{len(targets)} matching recruiter(s) to connect with.")

                for t in targets:
                    if sent >= max_n:
                        break
                    who = f"{t['name'] or t['profile_url']} ({t['role']}{' at ' + t['company'] if t['company'] else ''})"
                    if dry_run:
                        log_connect({**t, "status": "dry-run"})
                        print(f"Would connect: {who}")
                        sent += 1
                        continue
                    if not guard.take("linkedin", "views", profile):
                        raise StopRun("today's LinkedIn page-view budget is used up (safety: in profile.yaml)")
                    try:
                        status = send_connect(page, t["profile_url"])
                    except StopRun:
                        raise
                    except Exception as e:
                        status = "failed"
                        t = {**t, "reason": str(e)[:200]}
                    log_connect({**t, "status": status})
                    print(f"{status.capitalize()}: {who}")
                    done.add(_profile_key(t["profile_url"]))
                    if status == "sent":
                        sent += 1
                    guard.human_pause(pace)  # profile visits count too, not only sent requests
        except StopRun as e:
            print(f"STOPPING: {e}")
            log_connect({"status": "stopped", "reason": str(e)})
        finally:
            browser.close()

    what = "people would get a request" if dry_run else "connection requests sent"
    print(f"\nDone. {sent} {what} this run. See {CONNECT_LOG} for the full log.")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv[1:])
