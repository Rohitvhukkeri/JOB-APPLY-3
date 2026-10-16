"""
Indeed (in.indeed.com) assisted apply: finds "Easily apply" jobs that match
your resume and opens each one's Indeed application -- already filled in from
your Indeed profile -- in its own browser tab. You review and click Submit.

Why not submit automatically: Indeed's apply pages are guarded by Cloudflare
and Google reCAPTCHA, which stall or flag automated submits. A real click from
you passes them; everything before that click is done for you.

    python indeed_apply.py

  * Log in once first: python login_capture.py indeed
  * Runs in your installed Google Chrome with its own profile
    (browser_profiles/indeed) -- Playwright's test browser gets blocked.
  * Same filters as Naukri / LinkedIn: target_roles + resume skills, city,
    excluded tech / companies, and the resume skill match (matching: in
    profile.yaml). Daily budgets and cool-downs from common/guard.py.
  * At most indeed_settings.max_tabs_per_run tabs per run (default 10); each
    job is opened once, ever (indeed_opened.csv).
"""
import csv
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common import guard, job_match
from common.profile import Profile
from naukri_apply import StopRun, log_row, passes_filters, safe_evaluate, _job_key, _mentions, load_already_applied
from linkedin_apply import title_experience_ok  # seniority words / "8+ yrs" in the title

SESSION_FILE = "session_indeed.json"
PROFILE_DIR = Path("browser_profiles/indeed")
OPENED_FILE = "indeed_opened.csv"
SOURCE = "indeed"

DEFAULT_SETTINGS = {
    "location": "Bengaluru, Karnataka",
    "days": 7,                  # only jobs posted in the last N days
    "max_pages_per_term": 1,    # results are newest-first
    "max_tabs_per_run": 10,
    "pace_seconds_between_actions": 12,
}
# Non-developer roles that still mention your stack ("MERN Stack Trainer").
NON_DEV_TITLE_WORDS = ["trainer", "training", "faculty", "instructor", "teacher", "mentor", "intern", "internship"]
BLOCK_TEXT = ("additional verification required", "just a moment", "verify you are human")


def settings(profile: Profile) -> dict:
    return {**DEFAULT_SETTINGS, **(profile.data.get("indeed_settings") or {})}


def load_opened() -> set[str]:
    if not Path(OPENED_FILE).exists():
        return set()
    with open(OPENED_FILE, newline="", encoding="utf-8") as f:
        return {row["jk"] for row in csv.DictReader(f)}


def log_opened(card: dict, url: str):
    new_file = not Path(OPENED_FILE).exists()
    with open(OPENED_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "jk", "title", "company", "apply_url"])
        w.writerow([datetime.now(), card["jk"], card["title"], card["company"], url])


def open_browser(p):
    if not Path(SESSION_FILE).exists():
        raise SystemExit(f"{SESSION_FILE} not found. Run: python login_capture.py indeed")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR.resolve()), channel="chrome", headless=False,
        args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"],
        viewport={"width": 1366, "height": 850})
    ctx.add_cookies(json.loads(Path(SESSION_FILE).read_text(encoding="utf-8"))["cookies"])
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def check_blocked(page):
    text = (page.title() + " " + (safe_evaluate(page, "() => document.body.innerText.slice(0, 400)", default="") or "")).lower()
    hit = next((t for t in BLOCK_TEXT if t in text), None)
    if hit:
        guard.pause("indeed", f"Cloudflare / verification page ({hit})")
        raise StopRun(f"Indeed verification page ({hit})")
    if "secure.indeed.com/auth" in page.url:
        raise StopRun("Indeed session expired -- run: python login_capture.py indeed")


def search_url(term: str, cfg: dict, page_no: int) -> str:
    params = {"q": term, "l": cfg["location"], "fromage": cfg["days"], "sort": "date"}
    if page_no > 1:
        params["start"] = (page_no - 1) * 10
    return "https://in.indeed.com/jobs?" + urlencode(params)


def easy_apply_cards(page) -> list[dict]:
    return safe_evaluate(page, """
        () => [...document.querySelectorAll('.job_seen_beacon')].map(c => {
            const a = c.querySelector('a[data-jk]');
            return {
                jk: a?.getAttribute('data-jk') || '',
                title: (a?.innerText || c.querySelector('h2')?.innerText || '').trim(),
                company: (c.querySelector('[data-testid="company-name"]')?.innerText || '').trim(),
                location: (c.querySelector('[data-testid="text-location"]')?.innerText || '').trim(),
                easy: /easily apply/i.test(c.innerText),
                applied: /\\bapplied\\b/i.test(c.innerText),
            };
        }).filter(c => c.jk)
    """, default=[]) or []


def read_pane(page, jk: str) -> tuple[str, str | None]:
    """Clicks the job in the results (like a person) and returns its
    description and the Indeed Apply link from the side pane."""
    page.locator(f'a[data-jk="{jk}"]').first.click()
    time.sleep(4)
    data = safe_evaluate(page, """
        () => {
            const pane = document.querySelector('#jobsearch-ViewjobPaneWrapper, .jobsearch-RightPane') || document;
            const link = pane.querySelector('a[href*="smartapply.indeed.com"]');
            return {desc: pane.querySelector('#jobDescriptionText')?.innerText || pane.innerText || '',
                    apply: link ? link.href : null};
        }
    """, default={}) or {}
    return data.get("desc", ""), data.get("apply")


def run():
    profile = Profile.load()
    guard.refuse_if_paused("indeed")
    cfg = settings(profile)
    opened_before = load_opened()
    already_applied = load_already_applied()
    terms = job_match.search_terms(profile)
    opened = 0
    seen = set()
    print(f"{len(opened_before)} Indeed jobs opened in earlier runs will be skipped.")

    with sync_playwright() as p:
        ctx, page = open_browser(p)
        try:
            page.goto("https://in.indeed.com/", wait_until="domcontentloaded")
            time.sleep(3)
            check_blocked(page)
            try:
                page.get_by_role("button", name="Accept All Cookies").click(timeout=3000)
            except Exception:
                pass

            for term in terms:
                for page_no in range(1, int(cfg["max_pages_per_term"]) + 1):
                    if opened >= int(cfg["max_tabs_per_run"]):
                        break
                    if not guard.take("indeed", "searches", profile):
                        raise StopRun("today's Indeed search budget is used up (safety: in profile.yaml)")
                    print(f"\n--- Indeed: {term}, page {page_no} ---")
                    page.goto(search_url(term, cfg, page_no), wait_until="domcontentloaded")
                    time.sleep(5)
                    check_blocked(page)
                    cards = [c for c in easy_apply_cards(page) if c["jk"] not in seen]
                    seen.update(c["jk"] for c in cards)
                    easy = [c for c in cards if c["easy"]]
                    print(f"{len(cards)} new jobs, {len(easy)} with Easily apply.")

                    for card in easy:
                        if opened >= int(cfg["max_tabs_per_run"]):
                            break
                        label = f"{card['title']} @ {card['company']}"
                        if card["applied"] or card["jk"] in opened_before or _job_key(card) in already_applied:
                            print(f"Skipped: {label} — already applied / opened")
                            continue
                        ok, reason = passes_filters(card, profile, term)
                        if ok:
                            ok, reason = title_experience_ok(card["title"], profile)
                        if ok and any(_mentions(card["title"], w) for w in NON_DEV_TITLE_WORDS):
                            ok, reason = False, "not a developer role (trainer / intern)"
                        if not ok:
                            print(f"Skipped: {label} — {reason}")
                            continue
                        if not guard.take("indeed", "views", profile):
                            raise StopRun("today's Indeed job-view budget is used up (safety: in profile.yaml)")
                        desc, apply_url = read_pane(page, card["jk"])
                        check_blocked(page)
                        match = job_match.score_job(card["title"], desc, profile)
                        if not match.ok:
                            print(f"Skipped: {label} — {match.reason}")
                            log_row([datetime.now(), SOURCE, card["title"], card["company"], "skipped", match.reason])
                            guard.human_pause(cfg["pace_seconds_between_actions"])
                            continue
                        if not apply_url:
                            print(f"Skipped: {label} — no Indeed Apply link")
                            continue
                        tab = ctx.new_page()
                        tab.goto(apply_url, wait_until="domcontentloaded")
                        opened += 1
                        log_opened(card, apply_url)
                        log_row([datetime.now(), SOURCE, card["title"], card["company"], "opened",
                                 f"Indeed application opened in a tab -- submit by hand | {match.summary()}"])
                        print(f"OPENED ({opened}): {label} — {match.summary()}")
                        page.bring_to_front()
                        guard.human_pause(cfg["pace_seconds_between_actions"])
        except StopRun as e:
            print(f"STOPPING: {e}")
            log_row([datetime.now(), SOURCE, "-", "-", "stopped", str(e)])
        except PWTimeout as e:
            print(f"STOPPING: page timed out ({str(e)[:120]})")

        if opened:
            print(f"\n{opened} Indeed application(s) are open in Chrome tabs, filled in from your Indeed profile.")
            print("Check each one, answer anything blank, and click Submit. Close Chrome when you're done.")
            try:
                page.close()
                while ctx.pages:
                    ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                pass
        ctx.close()
    print(f"\nDone. {opened} Indeed applications opened for you this run. See {OPENED_FILE}.")


if __name__ == "__main__":
    run()
