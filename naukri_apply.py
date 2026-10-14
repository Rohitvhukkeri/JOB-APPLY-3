"""
Naukri auto-apply. Requires session_naukri.json from login_capture.py.

Usage:
    python naukri_apply.py

Stops the ENTIRE run immediately and reports if it hits a CAPTCHA, a login
prompt, or a rate-limit warning. Never attempts to solve any of those --
that's the point. Everything else (a bad selector, a question it can't
answer, a stray page error) is handled per-listing: that one listing is
skipped and logged, and the run continues.
"""
import csv
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common.profile import Profile
from common import llm
from common import learned_answers
from common import job_match
from common.human_input import ask_user

SESSION_FILE = "session_naukri.json"
LOG_FILE = "applications_log.csv"
EXTERNAL_FILE = "external_jobs.csv"

STOP_PHRASES = [
    "too many requests", "unusual activity", "verify you are human",
    "captcha", "temporarily blocked", "please try again later and reduce",
    "there was an error while processing your request",
    # Naukri's daily application cap -- nothing more can be applied today.
    "daily limit", "daily quota", "reached the maximum number of applies",
    "maximum number of applications",
]

# Questions matching any of these should NEVER be answered by the LLM --
# these are exact personal facts an LLM could easily hallucinate wrong.
# Always routed straight to you (and then remembered for next time).
SENSITIVE_FIELD_HINTS = [
    "date of birth", "dob", "pan number", "pan card", "aadhar", "aadhaar",
    "passport", "bank account", "ifsc", "father's name", "father name",
    "mother's name", "mother name", "marital status", "blood group",
    "emergency contact", "voter id", "driving licence", "driving license",
]


# Specific phrases per answer-library key, checked as whole phrases (not
# single split words) so "current ctc" and "current city" can never collide
# just because they both start with "current". This is what the old
# key.split("_")[0] logic got wrong -- it reduced both to "current" and
# whichever key came first in the dict won, regardless of what was asked.
TRIGGER_PHRASES = {
    "years_experience": ["years of experience", "year of experience", "yrs of experience", "total experience",
                         "how many years", "work experience", "relevant experience", "experience in years",
                         "years in", "yrs in", "exp in", "experience in", "experience with", "years exp",
                         "yrs exp", "overall experience"],
    "notice_period": ["notice period"],
    "current_ctc": ["current ctc", "current salary", "current compensation", "present ctc", "present salary",
                    "cctc", "last drawn"],
    "expected_ctc": ["expected ctc", "expected salary", "expected compensation", "ectc"],
    "current_city": ["current city", "current location", "which city", "current place"],
    "relocate": ["relocate", "relocation", "willing to move"],
    "night_shift": ["night shift"],
    "weekend_work": ["weekend"],
}


def log_row(row: list):
    new_file = not Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "source", "title", "company", "status", "reason"])
        w.writerow(row)


def load_already_applied() -> set[tuple[str, str]]:
    """(title, company) pairs already logged as applied in earlier runs, so
    re-runs don't waste time re-opening jobs that are already done."""
    done = set()
    if not Path(LOG_FILE).exists():
        return done
    try:
        with open(LOG_FILE, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "applied":
                    done.add(((row.get("title") or "").strip().lower(),
                              (row.get("company") or "").strip().lower()))
    except Exception as e:
        print(f"  (couldn't read {LOG_FILE} for already-applied jobs: {e})")
    return done


def _job_key(card: dict) -> tuple[str, str]:
    return ((card.get("title") or "").strip().lower(), (card.get("company") or "").strip().lower())


def _mentions(text: str, keyword: str) -> bool:
    """Whole-word match, so "java" doesn't match "javascript" and "react"
    doesn't match "reactive"."""
    kw = re.escape(keyword.lower().strip())
    return re.search(rf"(?<![a-z0-9]){kw}(?![a-z0-9])", text.lower()) is not None


def find_excluded_tech(text: str, profile: Profile, key: str = "tech_exclude_keywords") -> str | None:
    """Returns the first tech from the given profile list found in text."""
    for kw in profile.data.get(key) or []:
        if _mentions(text, kw):
            return kw
    return None


def read_naukri_match(page) -> dict[str, bool]:
    """Naukri's own "Job match score" on the job page, computed against your
    Naukri profile: {"keyskills": True, "location": True, "work experience":
    True, "early applicant": False}. Empty dict if the section isn't shown."""
    return safe_evaluate(page, """
        () => {
            const out = {};
            document.querySelectorAll('[class*="MS__details"]').forEach(d => {
                const label = (d.querySelector('span')?.innerText || '').trim().toLowerCase();
                const icon = d.querySelector('i')?.className || '';
                if (label) out[label] = /check/i.test(icon) && !/cross/i.test(icon);
            });
            return out;
        }
    """, default={}) or {}


def passes_filters(card: dict, profile: Profile, role: str, check_title: bool = True) -> tuple[bool, str]:
    title = (card.get("title") or "").lower()
    company = (card.get("company") or "").lower()

    ok, reason = job_match.location_ok(card.get("location") or "", profile)
    if not ok:
        return False, reason

    tech = find_excluded_tech(title, profile)
    if tech:
        return False, f"title mentions excluded tech ({tech})"

    if not check_title:
        pass  # caller decides on the title later (e.g. from Naukri's Keyskills match)
    elif job_match.settings(profile)["mode"] == "title":
        required_keywords = profile.data.get("role_required_keywords", {}).get(role)
        if not required_keywords:
            generic = {"engineer", "developer", "administrator", "analyst", "senior", "junior", "lead"}
            required_keywords = [w for w in role.lower().split() if w not in generic]
        if required_keywords and not any(kw.lower() in title for kw in required_keywords):
            return False, f"title doesn't match role keywords ({role})"
    else:
        # Resume mode: any dev-role title passes here; the job description is
        # scored against your resume once the job page is open.
        ok, reason = job_match.title_relevant(title, profile)
        if not ok:
            return False, reason

    for excl in profile.company_exclude:
        if excl.lower() in company:
            return False, f"company excluded ({excl})"

    if profile.company_include_only:
        if not any(inc.lower() in company for inc in profile.company_include_only):
            return False, "not in include-only list"

    exp_text = card.get("exp") or ""
    digits = [int(s) for s in exp_text.replace("Yrs", "").replace("yrs", "")
              .replace("-", " ").split() if s.isdigit()]
    if len(digits) >= 2:
        lo, hi = digits[0], digits[-1]
        if hi < profile.seniority_floor_years or lo > profile.seniority_ceiling_years:
            return False, f"experience range mismatch ({exp_text})"

    return True, ""


def safe_evaluate(page, script, arg=None, default=None):
    """Runs page.evaluate but never lets a destroyed-context or stray JS error
    crash the whole run -- returns `default` instead. Naukri's own chat widget
    and SPA navigation can tear down the page mid-script, which raises
    Playwright's 'Execution context was destroyed' error; that's expected
    occasionally, not something to crash over."""
    try:
        return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
    except Exception as e:
        print(f"  (page.evaluate failed, continuing anyway: {e})")
        return default


def enumerate_cards(page):
    """Reads real job cards off the search results page. Card wrapper is
    div.srp-jobtuple-wrapper with a data-job-id attribute -- that attribute
    is the reliable marker; filter/promo widgets on the same page don't
    have it."""
    return safe_evaluate(page, """
        () => Array.from(document.querySelectorAll('.srp-jobtuple-wrapper[data-job-id]'))
          .map((c, i) => ({
            i,
            jobId: c.getAttribute('data-job-id'),
            title: c.querySelector('a.title')?.innerText.trim(),
            href: c.querySelector('a.title')?.href,
            company: c.querySelector('a.comp-name, .comp-dtls-wrap a')?.innerText.trim(),
            exp: c.querySelector('.expwdth')?.innerText,
            location: c.querySelector('.locWdth')?.innerText,
          }))
          .filter(c => c.title && c.href)
    """, default=[]) or []


def read_key_skills(page) -> str:
    """Key-skill chips plus the job description text from the detail page,
    used to catch jobs whose title looks fine but whose stack doesn't."""
    return safe_evaluate(page, """
        () => {
            const chips = Array.from(document.querySelectorAll(
                '[class*="key-skill"] a, [class*="key-skill"] span, [class*="chip"]'))
                .map(e => e.innerText.trim()).filter(Boolean);
            const jd = document.querySelector('[class*="job-desc"], [class*="JDC"], section[class*="dang-inner-html"]');
            return chips.join(', ') + ' || ' + (jd ? jd.innerText : '');
        }
    """, default="") or ""


def open_external_in_new_tab(page, context) -> str | None:
    """Clicks "Apply on company site" so the company's page opens in a new
    tab, which is left open for you to finish by hand. Returns its URL (or
    None if no tab opened). The main tab is brought back to front so the
    run carries on."""
    try:
        with context.expect_page(timeout=10000) as new_page_info:
            page.click('#company-site-button', timeout=5000)
        new_tab = new_page_info.value
        try:
            new_tab.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        url = new_tab.url
        page.bring_to_front()
        return url
    except Exception as e:
        print(f"  (couldn't open the company site in a new tab: {e})")
        return None


def log_external(card: dict, url: str | None):
    """Keeps a list of company-site jobs to apply to by hand, so they're not
    lost once the browser closes."""
    new_file = not Path(EXTERNAL_FILE).exists()
    with open(EXTERNAL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "title", "company", "naukri_url", "company_site_url"])
        w.writerow([datetime.now(), card.get("title"), card.get("company"), card.get("href"), url or ""])


def check_apply_button(page) -> str:
    """Returns 'applied', 'external', 'native', or 'none' based on the detail page's apply button."""
    if page.query_selector('#already-applied'):
        return "applied"
    if page.query_selector('#company-site-button'):
        return "external"
    if page.query_selector('#apply-button'):
        return "native"
    return "none"


def _wait_send_enabled(page, timeout_ms: int = 4000) -> bool:
    """Polls until the Send control's wrapper no longer has Naukri's
    'disabled' class. Confirmed real markup (from a debug HTML dump you
    sent): <div id="sendMsg__..." class="send disabled"> wraps the
    clickable <div class="sendMsg">Save</div>. It starts disabled and only
    becomes clickable after Naukri's frontend registers your selection and
    re-renders -- clicking Send before that happens is a silent no-op,
    which is what was causing radio/checkbox answers to never actually save."""
    waited = 0
    step = 300
    while waited <= timeout_ms:
        enabled = safe_evaluate(page, """
            () => {
                const wrapper = document.querySelector('[id^="sendMsg__"]');
                if (!wrapper) return true;  // no wrapper found -- don't block forever on a guess
                return !wrapper.className.includes('disabled');
            }
        """, default=True)
        if enabled:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _js_click_send(page) -> bool:
    """Clicks Naukri's screening-chat Send/Save control -- a <div class="sendMsg">,
    not a <button>. A JS click bypasses the chatbot_Overlay div that sits
    visually on top of it and blocks Playwright's normal .click()."""
    return safe_evaluate(page, """
        () => {
            const el = document.querySelector('.sendMsg');
            if (!el) return false;
            el.click();
            return true;
        }
    """, default=False)


def click_native_apply(page) -> bool:
    """Clicks the Apply button and returns whether the click actually
    registered, so the caller can tell 'click failed' apart from 'clicked
    fine, just couldn't confirm success afterward' -- those need different
    handling.

    Naukri now renders two #apply-button elements (one hidden), so
    getElementById could grab the hidden one and the click was a silent
    no-op. Click the visible one with a real mouse click instead."""
    try:
        btn = page.locator('#apply-button:visible').first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
        return True
    except Exception:
        pass
    return bool(safe_evaluate(page, """
        () => {
            const btns = [...document.querySelectorAll('#apply-button')];
            const btn = btns.find(b => b.offsetParent !== null) || btns[0];
            if (btn) { btn.click(); return true; }
            return false;
        }
    """, default=False))


def page_has_stop_signal(page) -> str | None:
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return None  # page mid-navigation -- checked again on the next loop iteration
    for phrase in STOP_PHRASES:
        if phrase in text:
            return phrase
    if "login" in page.url and "naukri.com/nlogin" in page.url:
        return "session expired / login prompt"
    return None


def _save_debug_screenshot(page, job_title: str) -> str:
    """Saves a screenshot when the outcome is uncertain, so it can be looked
    at afterward instead of needing to catch it live. Also saves the page's
    HTML for the same reason — a screenshot shows what it looked like, the
    HTML shows exactly why the confirmation-text check missed it."""
    debug_dir = Path("debug_screenshots")
    debug_dir.mkdir(exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in (job_title or "unknown"))[:60]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = debug_dir / f"{safe_name}_{timestamp}.png"
    html_path = debug_dir / f"{safe_name}_{timestamp}.html"
    try:
        page.screenshot(path=str(png_path))
    except Exception as e:
        print(f"  (couldn't save debug screenshot: {e})")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"  (couldn't save debug HTML: {e})")
    return str(png_path)


def verify_applied(page) -> bool:
    """Best-effort check that the application actually went through. Confirmed
    real pattern: Naukri shows a green checkmark panel reading
    'Applied to "<job title>"' a few seconds after submit -- checking for
    that prefix specifically, plus a short wait since it isn't instant."""
    try:
        page.wait_for_timeout(3000)  # the confirmation panel takes a moment to appear
    except Exception:
        pass
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    success_phrases = [
        'applied to "', "application sent", "successfully applied",
        "you have applied", "applied successfully",
    ]
    if any(p in text for p in success_phrases):
        return True
    btn = page.query_selector('#apply-button')
    if btn:
        try:
            label = btn.inner_text().strip().lower()
            if label and label != "apply":
                return True
        except Exception:
            pass
    return False


class StopRun(Exception):
    """Something genuinely dangerous or account-risking happened -- CAPTCHA,
    login expired, rate-limit warning. Halts the entire run immediately."""


class SkipJob(Exception):
    """This one listing can't be completed -- logs it and moves to the next
    listing. Does NOT stop the run."""


def _get_options(page) -> list[dict]:
    """Reads radio/checkbox options in the chat drawer, with their visible labels."""
    return safe_evaluate(page, """
        () => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            return inputs.map((el, idx) => {
                let label = '';
                if (el.id) {
                    const lbl = root.querySelector(`label[for="${el.id}"]`);
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) {
                    const parentLabel = el.closest('label');
                    if (parentLabel) label = parentLabel.innerText.trim();
                }
                if (!label && el.nextElementSibling) {
                    label = (el.nextElementSibling.innerText || '').trim();
                }
                return {idx, label, type: el.type};
            });
        }
    """, default=[]) or []


def _click_option(page, idx: int) -> bool:
    return safe_evaluate(page, """
        (idx) => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            if (inputs[idx]) { inputs[idx].click(); return true; }
            return false;
        }
    """, arg=idx, default=False)


def _is_sensitive_field(question: str) -> bool:
    lower_q = question.lower()
    return any(hint in lower_q for hint in SENSITIVE_FIELD_HINTS)


KNOWN_CITIES = [
    "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "navi mumbai",
    "delhi", "new delhi", "noida", "gurugram", "gurgaon", "kolkata", "ahmedabad",
    "kochi", "cochin", "coimbatore", "trivandrum", "thiruvananthapuram", "jaipur",
    "indore", "chandigarh", "mysore", "mysuru", "mangalore", "nagpur", "vizag",
    "visakhapatnam", "bhubaneswar", "lucknow",
]


def _auto_decide_option(question: str, options: list[dict], profile: Profile) -> dict | None:
    """Tries to pick the correct option using known profile facts, for
    common Yes/No-style questions. Returns None (never guesses) if nothing
    in the profile clearly answers this specific question -- caller falls
    back to asking you directly rather than risk a wrong click."""
    lower_q = question.lower()
    by_label = {o["idx"]: o["label"].strip().lower() for o in options}

    def find_by_text(text: str):
        for idx, label in by_label.items():
            if label == text.lower():
                return next(o for o in options if o["idx"] == idx)
        return None

    # A relocation question that names a city is only a "Yes" for cities you
    # actually listed (or live in) -- "willing to relocate to Chennai?" must
    # not get a Yes just because relocate_cities is non-empty.
    ok_cities = [c.lower() for c in profile.data.get("relocate_cities") or []]
    ok_cities.append((profile.data.get("current_city") or "").lower())
    city_aliases = {"bengaluru": ["bangalore"], "bangalore": ["bengaluru"],
                    "hyderabad": ["hyd"], "gurugram": ["gurgaon"], "gurgaon": ["gurugram"]}
    ok_names = set(ok_cities) | {a for c in ok_cities for a in city_aliases.get(c, [])}
    ok_names.discard("")
    named = [c for c in KNOWN_CITIES if c in lower_q]
    if named:
        relocate_answer = "Yes" if all(c in ok_names for c in named) else "No"
    else:
        relocate_answer = "Yes" if profile.data.get("relocate_cities") else "No"

    rules = [
        (["relocate", "relocation", "willing to move", "residing in"], relocate_answer),
        (["night shift"], "Yes" if profile.data.get("night_shift_ok") else "No"),
        (["contract"], "Yes" if profile.data.get("contract_ok") else "No"),
        (["f2f", "face to face", "face-to-face", "in-person interview", "in person interview", "walk-in", "walkin"],
         "Yes" if profile.data.get("f2f_interview_ok") else "No"),
        (["weekend"], "Yes" if profile.data.get("weekend_ok") else "No"),
        (["immediately available", "immediate joiner"],
         "Yes" if profile.data.get("immediately_available") else "No"),
        (["currently employed", "currently working"],
         "Yes" if profile.data.get("currently_employed", bool(profile.data.get("current_employer"))) else "No"),
    ]
    for triggers, desired in rules:
        if any(t in lower_q for t in triggers):
            match = find_by_text(desired)
            if match:
                return match
    return _pick_experience_range(lower_q, options, profile)


def _parse_year_range(label: str) -> tuple[float, float] | None:
    """'2-4 years' -> (2, 4), '<2 years' -> (0, 2), '>6' / '6+' -> (6, inf),
    'No experience' -> (0, 0), '3 years' -> (3, 3)."""
    lab = label.lower().replace("to", "-").replace("–", "-")
    if "no experience" in lab or lab.strip() in ("none", "fresher", "0"):
        return (0, 0)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", lab)]
    if not nums:
        return None
    if "<" in lab or "less than" in lab or "below" in lab:
        return (0, nums[0])
    if ">" in lab or "+" in lab or "more than" in lab or "above" in lab:
        return (nums[0], float("inf"))
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], nums[0])


def _pick_experience_range(lower_q: str, options: list[dict], profile: Profile) -> dict | None:
    """Answers "how many years of experience in X?" range / yes-no questions:
    your total experience when X is on your resume (or overall experience),
    0 for tech you haven't used -- so it never overclaims."""
    if not any(w in lower_q for w in ("year", "experience", "exp ")):
        return None
    years = job_match.experience_years_for(lower_q, profile)

    labels = {o["label"].strip().lower(): o for o in options}
    if "yes" in labels and "no" in labels:
        m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs|year|yr)", lower_q)
        if m:
            return labels["yes"] if years >= float(m.group(1)) else labels["no"]
        return None

    parsed = [(o, _parse_year_range(o["label"])) for o in options]
    parsed = [(o, r) for o, r in parsed if r]
    if len(parsed) < 2:
        return None
    for o, (lo, hi) in parsed:  # prefer half-open ranges: 4 -> "4-5" over "2-4"
        if lo <= years < hi:
            return o
    for o, (lo, hi) in parsed:
        if lo <= years <= hi:
            return o
    return None


_SKIP_LIKE = ("skip this question", "skip", "prefer not to say", "prefer not to answer")


def _default_option(options: list[dict]) -> dict:
    """Last-resort pick when neither the profile nor the AI decided: "Yes"
    if offered, otherwise the first real (non-skip) option."""
    for o in options:
        if o["label"].strip().lower() in ("yes", "y"):
            return o
    real = [o for o in options if o["label"].strip().lower() not in _SKIP_LIKE]
    return (real or options)[0]


def _ai_or_default_option(question: str, options: list[dict], profile: Profile, job_context: str) -> dict:
    real = [o for o in options if o["label"].strip().lower() not in _SKIP_LIKE] or options
    idx = llm.choose_option(question, [o["label"] for o in real], profile.data, job_context)
    if idx is not None:
        return real[idx]
    return _default_option(options)


def _handle_options_question(page, question: str, profile: Profile, timeout_s: int,
                             job_context: str = "") -> bool:
    """Returns True if handled (clicked something), raises SkipJob otherwise."""
    options = _get_options(page)
    if not options:
        return False

    stored = learned_answers.get_answer(question)
    if stored:
        for opt in options:
            if opt["label"].strip().lower() == stored.strip().lower():
                _click_option(page, opt["idx"])
                print(f"  (used a remembered answer for: {question[:80]})")
                return True

    auto = _auto_decide_option(question, options, profile)
    if auto:
        _click_option(page, auto["idx"])
        learned_answers.save_answer(question, auto["label"])
        print(f"  (auto-picked '{auto['label']}' from your profile for: {question[:80]})")
        return True

    if profile.data.get("never_skip", False):
        pick = _ai_or_default_option(question, options, profile, job_context)
        _click_option(page, pick["idx"])
        learned_answers.save_answer(question, pick["label"])
        print(f"  (picked '{pick['label']}' for: {question[:80]})")
        return True

    options_text = "\n".join(f"  {o['idx']}: {o['label']}" for o in options)
    response = ask_user(
        f"Screening question needs a choice:\n{question}\n\nOptions:\n{options_text}\n"
        f"Type the number of your choice (or numbers separated by commas for multi-select):",
        timeout_seconds=timeout_s,
    )
    if response is None:
        raise SkipJob(f"no response for options question: {question[:120]}")

    chosen_raw = [r.strip() for r in response.split(",") if r.strip()]
    matches = []
    for r in chosen_raw:
        try:
            idx = int(r)
            match = next((o for o in options if o["idx"] == idx), None)
        except ValueError:
            match = next((o for o in options if o["label"].strip().lower() == r.lower()), None)
        if match:
            matches.append(match)

    if not matches:
        raise SkipJob(f"couldn't match your response '{response}' to an option: {question[:120]}")

    for match in matches:
        _click_option(page, match["idx"])
    learned_answers.save_answer(question, matches[0]["label"] if len(matches) == 1 else response)
    return True


# If the chat's last message is one of these, it's wrapping up rather than
# asking something new -- stop cleanly instead of trying to draft an answer
# into a box that may no longer exist.
COMPLETION_PHRASES = [
    "thank you", "thanks for your response", "thanks for your time",
    "we will get back", "responses have been recorded", "no further questions",
    "application submitted", "that's all", "all the information we need",
]


def _is_completion_message(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in COMPLETION_PHRASES)


def _has_answerable_input(page, timeout_ms: int = 3000) -> bool:
    """Checks for a text box or radio/checkbox to answer. Polls for up to
    timeout_ms instead of checking once -- the previous single-check version
    could wrongly conclude "nothing to answer" if the next question's input
    just hadn't rendered yet after the previous Save click, causing it to
    skip clicking Save on what was actually still a real question."""
    waited = 0
    step = 300
    while waited <= timeout_ms:
        found = safe_evaluate(page, """
            () => !!(document.querySelector('[id^="userInput"], [contenteditable="true"]') ||
                     document.querySelector('input[type=radio], input[type=checkbox]'))
        """, default=False)
        if found:
            return True
        time.sleep(step / 1000)
        waited += step
    return False


def _read_filled_text(page) -> str:
    return safe_evaluate(page, """
        () => {
            const ed = document.querySelector('[id^="userInput"], [contenteditable="true"]');
            return ed ? (ed.innerText || ed.textContent || '').trim() : '';
        }
    """, default="") or ""


def _log_action_needed(job_context: str, question: str):
    """Screening questions with a link (e.g. "please fill this Google Form")
    need you to do something by hand -- keep a to-do list of them."""
    path = Path("action_needed.csv")
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "job", "question"])
        w.writerow([datetime.now(), job_context, question])
    print(f"  (ACTION NEEDED — question has a link, saved to {path}: {question[:100]})")


def _personal_detail(question: str, profile: Profile) -> str | None:
    """Looks the question up in profile.yaml's personal_details (e.g. a
    question containing "date of birth" -> personal_details["date of birth"])."""
    lower_q = question.lower()
    for key, value in (profile.data.get("personal_details") or {}).items():
        if value not in (None, "") and str(key).lower() in lower_q:
            return str(value)
    return None


def _fallback_text(question: str, profile: Profile) -> str:
    """Used only when every AI provider failed: a plain answer that's still
    true to the profile, so the application can be submitted."""
    q = question.lower()
    years = f"{job_match.experience_years_for(question, profile):g}"
    if any(w in q for w in ("year", "experience", "exp ")):
        return years
    if "notice" in q:
        return str(profile.data.get("notice_period_days", 0))
    if any(w in q for w in ("how many", "number", "rate yourself", "rating", "scale")):
        return years if "rate" not in q else "4"
    if any(q.startswith(w) for w in ("are you", "do you", "can you", "will you", "would you",
                                       "have you", "is it ok", "are u", "r u")):
        return "Yes"
    return (f"I have {years} years of experience as a "
            f"{profile.data.get('current_title_functional', 'developer')}.")


def _click_option_label(page, idx: int) -> bool:
    """Clicks an option's <label> (some Naukri widgets ignore a direct input click)."""
    return safe_evaluate(page, """
        (idx) => {
            const root = document.querySelector('[class*="chatbot_Drawer"]') || document;
            const inputs = Array.from(root.querySelectorAll('input[type=radio], input[type=checkbox]'));
            const el = inputs[idx];
            if (!el) return false;
            const lbl = (el.id && root.querySelector(`label[for="${el.id}"]`)) || el.closest('label')
                        || el.nextElementSibling;
            (lbl || el).click();
            return true;
        }
    """, arg=idx, default=False)


def _fill_and_send(page, text: str, question: str, force: bool = False):
    """Fills the answer box, VERIFIES the text actually landed before
    clicking Send, then sends. This is the fix for answers going through
    blank: previously Send could fire even if the fill silently failed
    (a timing hiccup, or the box wasn't there), which is what produces
    Naukri's "incomplete information" rejection on the final application."""
    _fill_freetext(page, text)
    time.sleep(0.4)
    filled = _read_filled_text(page)
    if not filled:
        _fill_freetext(page, text)  # one retry -- could be a focus/timing hiccup
        time.sleep(0.6)
        filled = _read_filled_text(page)
    if not filled:
        # Second method: real keystrokes instead of execCommand.
        try:
            page.click('[id^="userInput"], [contenteditable="true"]', timeout=2000)
            page.keyboard.type(text, delay=20)
            time.sleep(0.5)
            filled = _read_filled_text(page)
        except Exception:
            pass
    if not filled and not force:
        raise SkipJob(f"couldn't confirm the answer registered before sending: {question[:120]}")
    _wait_send_enabled(page)  # same disabled-until-registered pattern as the options branch
    _js_click_send(page)
    time.sleep(2.0)  # give Naukri's backend a moment to actually save it


def answer_screening_chat(page, profile: Profile, job_context: str, timeout_s: int):
    """Handles Naukri's post-apply screening chat drawer, if it appears."""
    answers = profile.answer_library()
    never_skip = profile.data.get("never_skip", False)
    try:
        # Broadened on purpose: a pure radio/checkbox question has no text
        # box at all, so waiting only for contenteditable was causing the
        # function to give up immediately on those questions, thinking
        # there was no screening chat when there actually was one.
        # Naukri's chat drawer now takes ~6-7s to appear after Apply; the old
        # 4s wait (6s total) gave up just before it showed, so applications
        # were silently left unanswered and logged "uncertain".
        page.wait_for_selector(
            '[contenteditable="true"], [contenteditable=""], '
            'input[type=radio], input[type=checkbox], .botMsg',
            timeout=15000,
        )
    except PWTimeout:
        return  # no screening chat for this listing

    for _ in range(15):
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal during screening chat: {stop}")

        bubbles = page.query_selector_all('.botMsg')
        if not bubbles:
            break
        try:
            question = bubbles[-1].inner_text().strip()
        except Exception:
            break  # page likely navigated away mid-read; treat as chat finished
        if not question:
            break

        if _is_completion_message(question):
            time.sleep(1.5)  # let the "Applied" confirmation redirect begin before we check for it
            break  # chat is wrapping up, not asking a new question

        if not _has_answerable_input(page):
            break  # nothing to fill or click here — informational message only

        if "http" in question.lower() or "www." in question.lower():
            _log_action_needed(job_context, question)

        options = _get_options(page)
        if options:
            _handle_options_question(page, question, profile, timeout_s, job_context)
            if not _wait_send_enabled(page):
                if not never_skip:
                    raise SkipJob(f"Send button stayed disabled after selecting an option "
                                   f"(selection may not have registered): {question[:120]}")
                # Selection didn't register -- click the label too, then try
                # the other options one by one until Send enables.
                for o in [_default_option(options)] + options:
                    _click_option_label(page, o["idx"])
                    if _wait_send_enabled(page, 1500):
                        break
            _js_click_send(page)
            time.sleep(2.0)
            continue

        stored = learned_answers.get_answer(question)
        if stored:
            _fill_and_send(page, stored, question)
            print(f"  (used a remembered answer for: {question[:80]})")
            continue

        lower_q = question.lower()
        answered = False
        for key, phrases in TRIGGER_PHRASES.items():
            if key not in answers:
                continue
            if any(phrase in lower_q for phrase in phrases):
                value = str(answers[key])
                if key == "years_experience":  # 4 for your stack, 0 for tech you haven't used
                    value = f"{job_match.experience_years_for(question, profile):g}"
                _fill_and_send(page, value, question)
                answered = True
                break

        if answered:
            continue

        if never_skip:
            response = (_personal_detail(question, profile)
                        or llm.best_effort_answer(question, profile.data, job_context)
                        or _fallback_text(question, profile))
            _fill_and_send(page, response, question, force=True)
            print(f"  (answered '{response[:60]}' for: {question[:80]})")
            continue

        if _is_sensitive_field(question):
            response = ask_user(f"Screening question (personal detail):\n{question}", timeout_seconds=timeout_s)
            if response is None:
                raise SkipJob(f"no response for sensitive field: {question[:120]}")
            _fill_and_send(page, response, question)
            learned_answers.save_answer(question, response)
            continue

        draft = llm.draft_answer(question, profile.data, job_context)
        if draft.startswith("[NEEDS_HUMAN_INPUT"):
            response = ask_user(f"Screening question (AI couldn't answer from your profile):\n{question}",
                                 timeout_seconds=timeout_s)
            if response is None:
                raise SkipJob(f"no response for: {question[:120]}")
            _fill_and_send(page, response, question)
            learned_answers.save_answer(question, response)
        else:
            _fill_and_send(page, draft, question)


def _fill_freetext(page, text: str):
    safe_evaluate(page, """
        (text) => {
            const ed = document.querySelector('[id^="userInput"], [contenteditable="true"]');
            if (!ed) return;
            ed.focus();
            document.execCommand('insertText', false, text);
        }
    """, arg=text)


def run():
    profile = Profile.load()
    if not Path(SESSION_FILE).exists():
        raise SystemExit(f"{SESSION_FILE} not found. Run: python login_capture.py naukri")

    human_timeout = profile.data.get("human_input_timeout_seconds", 120)
    max_pages = profile.data.get("max_pages_per_role", 5)

    applied = 0
    already_applied = load_already_applied()
    print(f"{len(already_applied)} jobs already applied to in earlier runs will be skipped.")
    with sync_playwright() as p:
        browser_mode = profile.data.get("browser_mode", "visible")
        if browser_mode == "headless":
            browser = p.chromium.launch(headless=True, slow_mo=150)
        elif browser_mode == "minimized":
            browser = p.chromium.launch(headless=False, slow_mo=150, args=["--start-minimized"])
        else:
            browser = p.chromium.launch(headless=False, slow_mo=150)
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()
        external_opened = 0

        # Naukri caps applications per day. When the cap is hit, the Apply click
        # silently does nothing on screen -- only the apply API answers
        # 403 "Daily quota of jobs exceeded". Watch for that reply.
        quota = {"hit": False}

        def _watch_apply_quota(resp):
            try:
                if "apply-workflow" in resp.url and resp.status == 403 and "quota" in resp.text().lower():
                    quota["hit"] = True
            except Exception:
                pass

        context.on("response", _watch_apply_quota)
        # Shared across searches, so a job found by "React JS" isn't reopened under "Node JS".
        seen_job_ids = set()
        searches = job_match.search_terms(profile)
        print(f"Searching {len(searches)} terms: {', '.join(searches)}")

        for role in searches:
            if applied >= profile.stop_after_n_applications:
                break

            slug = role.lower().replace(" ", "-")
            exp_param = int(profile.total_experience_years)
            stopped_entirely = False
            search_seen = set()  # end-of-results detection for THIS search only

            for page_no in range(1, max_pages + 1):
                if applied >= profile.stop_after_n_applications:
                    break

                # Confirmed pattern: Naukri appends "-N" directly to the slug
                # for page N (page 1 has no suffix), e.g.
                # azure-data-engineer-jobs-2 for page 2.
                page_suffix = "" if page_no == 1 else f"-{page_no}"
                city = (profile.data.get("search_city") or "").strip().lower().replace(" ", "-")
                city_part = f"-in-{city}" if city else ""
                url = (
                    f"https://www.naukri.com/{slug}-jobs{city_part}{page_suffix}"
                    f"?experience={exp_param}"
                    f"&jobAge={profile.job_freshness_days}"
                )
                print(f"\n--- Searching: {role}, page {page_no} ({url}) ---")
                try:
                    page.goto(url)
                    page.wait_for_selector('.srp-jobtuple-wrapper[data-job-id]', timeout=10000)
                except PWTimeout:
                    pass
                except Exception as e:
                    print(f"  (navigation error, treating as end of results for this role: {e})")
                    break
                time.sleep(1)

                stop = page_has_stop_signal(page)
                if stop:
                    print(f"STOPPING: {stop}")
                    log_row([datetime.now(), "naukri", "-", "-", "stopped", stop])
                    stopped_entirely = True
                    break

                cards = enumerate_cards(page)
                if not [c for c in cards if c.get("jobId") not in search_seen]:
                    print("No new listings on this page -- treating as the last page for this search.")
                    break
                search_seen.update(c.get("jobId") for c in cards)
                new_cards = [c for c in cards if c.get("jobId") not in seen_job_ids]
                print(f"Found {len(cards)} job cards ({len(new_cards)} not seen in earlier searches) on page {page_no}.")

                for card in new_cards:
                    seen_job_ids.add(card.get("jobId"))
                    if applied >= profile.stop_after_n_applications:
                        break

                    if _job_key(card) in already_applied:
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} — already applied earlier")
                        continue

                    # Hard filters (your explicit exclusions: city, excluded tech in the
                    # title, company, experience range) skip right away. The softer
                    # "is this a dev title?" check waits for Naukri's own Keyskills match.
                    ok, reason = passes_filters(card, profile, role, check_title=False)
                    if not ok:
                        log_row([datetime.now(), "naukri", card.get("title"),
                                  card.get("company"), "skipped", reason])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} — {reason}")
                        continue
                    title_ok, title_reason = passes_filters(card, profile, role)

                    try:
                        page.goto(card["href"])
                        time.sleep(2)

                        stop = page_has_stop_signal(page)
                        if stop:
                            raise StopRun(f"stop signal: {stop}")

                        jd_text = read_key_skills(page)
                        chips_text, _, description = jd_text.partition(" || ")
                        key_skills = [c.strip() for c in chips_text.split(",") if c.strip()]
                        match = job_match.score_job(card.get("title") or "", description or jd_text, profile,
                                                    key_skills=key_skills)
                        # Naukri's own "Job match score": a green Keyskills tick means Naukri
                        # matched this job's key skills to YOUR profile -- then apply (or open
                        # the company site) instead of second-guessing it.
                        naukri_says = read_naukri_match(page)
                        naukri_keyskills = naukri_says.get("keyskills") is True
                        why = ("Naukri Keyskills match ✓ | " if naukri_keyskills else "") + match.summary()
                        if not naukri_keyskills:
                            if not title_ok:
                                log_row([datetime.now(), "naukri", card.get("title"),
                                          card.get("company"), "skipped", title_reason])
                                print(f"Skipped: {card.get('title')} @ {card.get('company')} — {title_reason}")
                                continue
                            if not match.ok:
                                log_row([datetime.now(), "naukri", card.get("title"),
                                          card.get("company"), "skipped", match.reason])
                                print(f"Skipped: {card.get('title')} @ {card.get('company')} — {match.reason}")
                                continue
                        # When the Key Skills match, they decide: an "Angular" mention in the
                        # description ("React or Angular") only skips the job if the Key
                        # Skills themselves name it.
                        tech_source = chips_text if (naukri_keyskills or match.source == "key skills") else jd_text
                        tech = find_excluded_tech(tech_source, profile, "tech_exclude_in_description")
                        if tech:
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "skipped", f"job requires excluded tech ({tech})"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — requires {tech}")
                            continue

                        apply_state = check_apply_button(page)
                        if apply_state == "applied":
                            already_applied.add(_job_key(card))
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — already applied on Naukri")
                            continue
                        if apply_state == "external":
                            ext_url = open_external_in_new_tab(page, context)
                            log_external(card, ext_url)
                            if ext_url:
                                external_opened += 1
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "external",
                                      f"opened company site in new tab: {ext_url or 'failed to open'}"])
                            print(f"External: {card.get('title')} @ {card.get('company')} — "
                                  f"company site opened in a new tab for you ({ext_url})")
                            continue
                        if apply_state == "none":
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "skipped", "no apply button found"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — no apply button found")
                            continue

                        clicked = click_native_apply(page)
                        if not clicked:
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "skipped", "apply button click didn't register"])
                            print(f"Skipped: {card.get('title')} @ {card.get('company')} — apply click didn't register")
                            continue
                        time.sleep(2)
                        if quota["hit"]:
                            raise StopRun("Naukri daily apply quota reached -- nothing more can be applied "
                                          "today; the next scheduled run continues tomorrow")

                        answer_screening_chat(page, profile, f"{card.get('title')} at {card.get('company')}",
                                               human_timeout)
                        time.sleep(1.5)

                        confirmed = verify_applied(page)
                        if not confirmed and check_apply_button(page) == "native":
                            # Apply button still there and still says "Apply" --
                            # the first click didn't register. Try once more.
                            print("  (Apply click didn't register — retrying once)")
                            if click_native_apply(page):
                                time.sleep(2)
                                answer_screening_chat(page, profile,
                                                       f"{card.get('title')} at {card.get('company')}",
                                                       human_timeout)
                                time.sleep(1.5)
                                confirmed = verify_applied(page)

                        if confirmed:
                            applied += 1
                            already_applied.add(_job_key(card))
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "applied", why])
                            print(f"Applied: {card.get('title')} @ {card.get('company')} ({applied} total, "
                                  f"{why})")
                        elif quota["hit"]:  # the quota reply can arrive after the first check
                            raise StopRun("Naukri daily apply quota reached -- nothing more can be applied "
                                          "today; the next scheduled run continues tomorrow")
                        else:
                            shot_path = _save_debug_screenshot(page, card.get("title"))
                            log_row([datetime.now(), "naukri", card.get("title"),
                                      card.get("company"), "uncertain",
                                      f"couldn't confirm submission — screenshot saved to {shot_path}"])
                            print(f"UNCERTAIN: {card.get('title')} @ {card.get('company')} — "
                                  f"couldn't confirm the application actually went through. Check it manually.")

                    except SkipJob as e:
                        log_row([datetime.now(), "naukri", card.get("title"),
                                  card.get("company"), "skipped", str(e)])
                        print(f"Skipped: {card.get('title')} @ {card.get('company')} — {e}")
                        continue

                    except StopRun as e:
                        print(f"STOPPING: {e}")
                        log_row([datetime.now(), "naukri", card.get("title"),
                                  card.get("company"), "stopped", str(e)])
                        browser.close()
                        return

                    except Exception as e:
                        # Anything unexpected (stray Playwright errors, torn-down
                        # page contexts, etc.) -- log it and move to the next
                        # listing instead of crashing the whole run.
                        log_row([datetime.now(), "naukri", card.get("title"),
                                  card.get("company"), "skipped", f"unexpected error: {e}"])
                        print(f"Skipped (unexpected error): {card.get('title')} @ {card.get('company')} — {e}")
                        try:
                            if page.is_closed():
                                page = context.new_page()  # recover instead of failing every later job
                        except Exception:
                            pass
                        continue

                    time.sleep(profile.pace_seconds_between_actions)

                if stopped_entirely:
                    break

            if stopped_entirely:
                break

        if external_opened and os.environ.get("JOB_APPLY_UNATTENDED"):
            # Scheduled run: nobody is there to close the tabs, so don't wait.
            print(f"\n{external_opened} company-site job(s) are listed in {EXTERNAL_FILE} to apply by hand.")
        elif external_opened:
            print(f"\n{external_opened} company-site job(s) are open in browser tabs (also listed in "
                  f"{EXTERNAL_FILE}). The browser stays open until you close it.")
            try:
                page.close()  # leave only the company-site tabs
                while browser.is_connected() and context.pages:
                    time.sleep(2)
            except Exception:
                pass
        browser.close()

    print(f"\nDone. {applied} applications submitted this run. See {LOG_FILE} for the full log.")


if __name__ == "__main__":
    run()
