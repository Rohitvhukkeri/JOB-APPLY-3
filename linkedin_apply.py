"""
LinkedIn Easy Apply auto-apply -- the LinkedIn twin of naukri_apply.py.

Usage:
    python linkedin_apply.py        (or double-click run_linkedin.bat)

Same rules as the Naukri script, reading the same profile.yaml:
  * Searches every target role (Easy Apply only, your city, last N days),
    page by page until the results run out.
  * Same title / location / tech / description filters as Naukri.
  * Fills every Easy Apply step: text, numbers, dropdowns, radios,
    checkboxes, city typeaheads, resume. Answers come from learned_answers
    -> profile rules -> AI -> a safe default, like the Naukri chat.
  * Marks a job "applied" only after LinkedIn confirms it was sent.
  * Stops the ENTIRE run on a CAPTCHA / security check / login wall / the
    daily Easy Apply limit. Anything else just skips that one listing.

If session_linkedin.json is missing or expired, a browser window opens on
the LinkedIn login page -- log in by hand and the run continues by itself.

LinkedIn-only settings live under `linkedin_settings:` in profile.yaml
(all optional). naukri_apply.py never reads them, and this file only
imports helpers from it -- the Naukri flow is untouched.
"""
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common.profile import Profile
from common import guard, llm
from common import learned_answers
from common import job_match
from common.human_input import ask_user
# Shared, side-effect-free helpers from the Naukri flow (imported, not modified).
from naukri_apply import (
    LOG_FILE, TRIGGER_PHRASES, StopRun, SkipJob,
    log_row, load_already_applied, _job_key, _mentions, find_excluded_tech,
    passes_filters, safe_evaluate, _save_debug_screenshot, _auto_decide_option,
    _default_option, _ai_strict_option, _SKIP_LIKE, _is_sensitive_field, _personal_detail, _fallback_text,
)

SESSION_FILE = "session_linkedin.json"
EXTERNAL_FILE = "linkedin_external_jobs.csv"
SOURCE = "linkedin"

DEFAULT_SETTINGS = {
    "search_location": "Bengaluru, Karnataka, India",
    "geo_id": "105214831",            # LinkedIn's id for Bengaluru; "" to search by text only
    "experience_levels": [2, 3, 4],   # 1 intern, 2 entry, 3 associate, 4 mid-senior, 5 director
    "max_pages_per_role": 40,         # LinkedIn shows at most 40 pages (1000 jobs) per search
    "stop_after_n_applications": 50,  # LinkedIn caps Easy Apply per day; stay well under it
    "pace_seconds_between_actions": 20,
    "unfollow_company": True,         # untick "Follow <company>" before submitting
    "browser_mode": None,             # None = use the top-level browser_mode
    # Titles that signal roles far above your experience, even without "N+ yrs".
    "title_exclude_seniority": ["principal", "staff", "architect", "director", "head of",
                                "vp", "vice president", "manager"],
    # Non-developer roles that slip through substring role matching ("sde" in "sdet").
    "title_exclude_keywords": ["sdet", "qa", "test", "tester", "testing", "quality assurance",
                               "servicenow", "service now", "pega", "appian", "outsystems", "mulesoft", "uipath"],
}

STOP_PHRASES = [
    "easy apply limit", "reached the easy apply", "reached today's limit",
    "we've restricted your account", "your account has been restricted",
    "let's do a quick security check", "unusual activity from your account",
    "verify you're a human", "complete the security check",
]
LOGGED_OUT_URL_MARKERS = ("/login", "/authwall", "/checkpoint/", "/uas/", "/signup")

SENT_PHRASES = ("application was sent", "application sent", "your application was submitted",
                "applied to ")

# New job-page layout: an "Application status" card reading "Application
# submitted" once you've applied. Scoped to that card -- job descriptions can
# contain the same words.
APPLIED_STATUS_JS = r"""
    [...document.querySelectorAll('h2, h3')].some(h => /^application status$/i.test(h.innerText.trim())
        && /application submitted|\bapplied\b/i.test(h.parentElement?.parentElement?.innerText || ''))
"""

# LinkedIn has two Easy Apply UIs: the old artdeco modal ([role=dialog]) and
# the newer SDUI one, a native <dialog open> wrapping
# [data-sdui-screen*=EasyApply]. Dialogs are position:fixed, so offsetParent
# is always null for them -- visibility is checked with getClientRects().
MODAL_JS_ROOT = """
    const isVis = el => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
    const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"], .artdeco-modal, [data-testid="dialog"]')]
        .filter(isVis);
    const modal = document.querySelector('.jobs-easy-apply-modal')
        || dialogs.find(d => d.querySelector('[data-sdui-screen*="easyapply" i], .jobs-easy-apply-content, form'))
        || dialogs.pop();
"""


def li_settings(profile: Profile) -> dict:
    return {**DEFAULT_SETTINGS, **(profile.data.get("linkedin_settings") or {})}


def log_external(card: dict):
    new_file = not Path(EXTERNAL_FILE).exists()
    with open(EXTERNAL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "title", "company", "linkedin_url"])
        w.writerow([datetime.now(), card.get("title"), card.get("company"), card.get("href")])


# ---------------------------------------------------------------- session

def is_logged_out(page) -> bool:
    url = page.url.lower()
    return "linkedin.com" in url and any(m in url for m in LOGGED_OUT_URL_MARKERS)


def login_interactively(browser):
    """Opens the LinkedIn login page and waits (up to 10 min) for YOU to log
    in by hand -- password, 2FA, any CAPTCHA. Saves the session afterwards."""
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    print("\n" + "=" * 60)
    print("LinkedIn login needed -- log in by hand in the browser window.")
    print("The run starts by itself once you reach your LinkedIn feed (10 min max).")
    print("=" * 60)
    page.wait_for_url(lambda u: "linkedin.com" in u and not any(m in u.lower() for m in LOGGED_OUT_URL_MARKERS),
                      timeout=600_000)
    page.wait_for_timeout(5000)  # let post-login cookies settle
    context.storage_state(path=SESSION_FILE)
    print(f"Session saved to {SESSION_FILE}.")
    return context, page


# LinkedIn's "unusual search traffic ... search activity has been paused" page.
# It shows inside the page (not a dialog), so it's matched on the whole page.
RESTRICTION_PHRASES = ["detected unusual search traffic", "search activity on your account has been paused",
                       "unusual activity from your account", "your account has been restricted"]
def open_logged_in_context(browser):
    guard.refuse_if_paused("linkedin")
    if Path(SESSION_FILE).exists():
        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  (couldn't load the LinkedIn feed: {e})")
        if not is_logged_out(page):
            return context, page
        print("Saved LinkedIn session has expired.")
        context.close()
    return login_interactively(browser)


def page_has_stop_signal(page) -> str | None:
    url = page.url.lower()
    if "/checkpoint/" in url:
        guard.pause("linkedin", "security checkpoint")
        return "LinkedIn security checkpoint"
    if is_logged_out(page):
        return "session expired / login wall"
    body = (safe_evaluate(page, "() => document.body ? document.body.innerText.toLowerCase() : ''",
                          default="") or "")
    restriction = next((p for p in RESTRICTION_PHRASES if p in body), None)
    if restriction:
        guard.pause("linkedin", restriction)
        return f"LinkedIn restriction: {restriction}"
    # Only look at dialogs, toasts and inline banners -- job descriptions can
    # legitimately contain words like "unusual activity" or "captcha".
    text = (safe_evaluate(page, """
        () => [...document.querySelectorAll('dialog[open], [role="dialog"], [role="alert"], .artdeco-toasts_toasts, '
                + '.artdeco-inline-feedback, .jobs-s-apply, .artdeco-modal')]
              .map(e => e.innerText).join(' ').toLowerCase()
    """, default="") or "")
    for phrase in STOP_PHRASES:
        if phrase in text:
            if "limit" in phrase:
                guard.pause_until_tomorrow("linkedin", phrase)
            else:
                guard.pause("linkedin", phrase)
            return phrase
    return None


# ---------------------------------------------------------------- search

def search_url(role: str, profile: Profile, cfg: dict, page_no: int) -> str:
    params = {"keywords": role, "f_AL": "true", "sortBy": "DD"}
    if cfg.get("search_location"):
        params["location"] = cfg["search_location"]
    if cfg.get("geo_id"):
        params["geoId"] = cfg["geo_id"]
    if cfg.get("experience_levels"):
        params["f_E"] = ",".join(str(x) for x in cfg["experience_levels"])
    days = profile.data.get("job_freshness_days")
    if days:
        params["f_TPR"] = f"r{int(days) * 86400}"
    if page_no > 1:
        params["start"] = (page_no - 1) * 25
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def enumerate_cards(page) -> list[dict]:
    """Job cards on a search page. LinkedIn only renders cards near the
    viewport, so each one is scrolled into view before reading it."""
    return safe_evaluate(page, """
        async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const items = () => {
                const li = [...document.querySelectorAll('li[data-occludable-job-id]')];
                return li.length ? li : [...document.querySelectorAll('[data-job-id]')];
            };
            for (const el of items()) { el.scrollIntoView({block: 'center'}); await sleep(150); }
            const first = el => (el?.innerText || '').trim().split('\\n')[0].trim();
            return items().map(el => {
                const id = el.getAttribute('data-occludable-job-id') || el.getAttribute('data-job-id');
                const a = el.querySelector('a[href*="/jobs/view/"]');
                const title = first(el.querySelector(
                        '.job-card-list__title--link strong, .job-card-list__title strong, a[href*="/jobs/view/"] strong'))
                    || (a?.getAttribute('aria-label') || '').replace(/ with verification$/i, '').trim()
                    || first(a);
                return {
                    jobId: id,
                    title,
                    company: first(el.querySelector('.artdeco-entity-lockup__subtitle, '
                        + '.job-card-container__primary-description, .job-card-container__company-name')),
                    location: first(el.querySelector('.job-card-container__metadata-wrapper li, '
                        + '.artdeco-entity-lockup__caption li, .job-card-container__metadata-item, '
                        + '.artdeco-entity-lockup__caption')),
                    state: (el.querySelector('.job-card-container__footer-job-state')?.innerText || '').trim(),
                    href: 'https://www.linkedin.com/jobs/view/' + id + '/',
                };
            }).filter(c => c.jobId && c.title);
        }
    """, default=[]) or []


def title_experience_ok(title: str, profile: Profile) -> tuple[bool, str]:
    """LinkedIn cards have no experience field (Naukri's do), but titles often
    say "9+ Yrs" or "5-8 years" -- apply the same seniority floor/ceiling."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|-|–|to)?\s*(\d+(?:\.\d+)?)?\s*\+?\s*(?:years?|yrs?)\b", title, re.I)
    settings = profile.data.get("linkedin_settings") or {}
    for word in settings.get("title_exclude_seniority", DEFAULT_SETTINGS["title_exclude_seniority"]):
        if _mentions(title, word):
            return False, f"title is too senior ({word})"
    for word in settings.get("title_exclude_keywords", DEFAULT_SETTINGS["title_exclude_keywords"]):
        if _mentions(title, word):
            return False, f"title excluded ({word})"
    if not m:
        return True, ""
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else (float("inf") if "+" in m.group(0) else lo)
    if lo > profile.seniority_ceiling_years or hi < profile.seniority_floor_years:
        return False, f"title asks for {m.group(0).strip()} experience"
    return True, ""


def read_description(page) -> str:
    """Job description text. Old layout: #job-details / .jobs-description.
    New layout has no stable classes -- an <h2>About the job</h2> followed by
    [data-testid=expandable-text-box]."""
    try:
        page.wait_for_selector('#job-details, .jobs-description__content, .jobs-description, '
                               '[data-testid="expandable-text-box"]', timeout=10000)
    except PWTimeout:
        pass
    return safe_evaluate(page, """
        () => {
            const d = document.querySelector('#job-details, .jobs-description__content, '
                + '.jobs-box__html-content, .jobs-description');
            if (d) return d.innerText;
            const h = [...document.querySelectorAll('h2, h3')].find(x => /^about the job$/i.test(x.innerText.trim()));
            if (h) {
                let box = h.parentElement;
                for (let i = 0; i < 4 && box && box.innerText.trim().length < 200; i++) box = box.parentElement;
                // innerText keeps line breaks (needed to spot "nice to have" blocks);
                // fall back to textContent if the collapsed view hides most of the text.
                if (box) return box.innerText.length >= 0.8 * box.textContent.length ? box.innerText : box.textContent;
            }
            const t = document.querySelector('[data-testid="expandable-text-box"]');
            return t ? t.textContent : '';
        }
    """, default="") or ""


def check_apply_state(page) -> str:
    """'applied', 'easy', 'external' or 'none'. Marks the Easy Apply control
    with data-ja-apply so it can be clicked."""
    return safe_evaluate(page, """
        () => {
            if (""" + APPLIED_STATUS_JS + """) return 'applied';
            const main = document.querySelector('main') || document.body;
            const top = main.querySelector('.jobs-s-apply, .job-details-jobs-unified-top-card__container--two-pane, '
                + '.jobs-unified-top-card, .job-details-jobs-unified-top-card') || main;
            if (/\\bapplied (\\d+|an?) \\w+ ago\\b|application submitted/i.test(top.innerText || '')
                || top.querySelector('.artdeco-inline-feedback--success, .post-apply-timeline')) return 'applied';
            const vis = el => el.offsetParent !== null;
            const ctrls = [...main.querySelectorAll('button, a[href*="apply"]')].filter(vis);
            const label = el => ((el.getAttribute('aria-label') || '') + ' ' + el.innerText).toLowerCase();
            const easy = ctrls.find(el => label(el).includes('easy apply'));
            if (easy) { easy.setAttribute('data-ja-apply', '1'); return 'easy'; }
            if (ctrls.find(el => el.innerText.trim().toLowerCase() === 'apply'
                              || label(el).startsWith('apply to'))) return 'external';
            return 'none';
        }
    """, default="none") or "none"


def click_easy_apply(page) -> bool:
    try:
        btn = page.locator('[data-ja-apply="1"]').first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
    except Exception:
        if not safe_evaluate(page, """
            () => { const b = document.querySelector('[data-ja-apply="1"]'); if (b) { b.click(); return true; } return false; }
        """, default=False):
            return False
    try:
        page.wait_for_selector('.jobs-easy-apply-modal, [data-sdui-screen*="EasyApply" i], dialog[open], [role="dialog"]',
                               timeout=10000, state="attached")
        return True
    except PWTimeout:
        return False


# ---------------------------------------------------------------- Easy Apply modal

def _dedupe_label(text: str) -> str:
    """LinkedIn labels repeat themselves for screen readers ("Q Q") and end
    with "Required" -- trim both."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"\s*(\*|Required)\s*$", "", t).strip()
    half = len(t) // 2
    if len(t) > 3 and t[:half].strip() == t[half:].strip():
        t = re.sub(r"\s*(\*|Required)\s*$", "", t[:half]).strip()
    return t


def read_modal(page, root_js: str = MODAL_JS_ROOT) -> dict | None:
    """Snapshot of the current Easy Apply step. Every control is tagged with
    a data-ja attribute so the Python side can find it again. root_js must
    define `isVis` and `modal` (the element holding the form) -- the Indeed
    script passes its own page root."""
    state = safe_evaluate(page, "() => {" + root_js + """
        if (!modal) return null;
        modal.querySelectorAll('[data-ja], [data-ja-label], [data-ja-btn]').forEach(e => {
            e.removeAttribute('data-ja'); e.removeAttribute('data-ja-label'); e.removeAttribute('data-ja-btn');
        });
        const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
        const vis = el => isVis(el) || el.type === 'radio' || el.type === 'checkbox' || el.type === 'file';
        const wrapOf = el => {
            const known = el.closest('.fb-dash-form-element, .jobs-easy-apply-form-element, '
                + '[data-test-form-element], .jobs-easy-apply-form-section__grouping');
            if (known) return known;
            // New UI has no stable classes: climb to the nearest ancestor holding the label.
            let w = el.parentElement;
            for (let i = 0; i < 4 && w && w !== modal; i++) {
                if (w.querySelector('label, legend')) return w;
                w = w.parentElement;
            }
            return el.parentElement;
        };
        const ERR_SEL = '.artdeco-inline-feedback--error, [data-test-form-element-error-messages], [role="alert"]';
        const errorOf = el => {
            let t = clean(wrapOf(el)?.querySelector(ERR_SEL)?.innerText);
            if (!t) for (const id of (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean)) {
                const d = document.getElementById(id);
                if (d && (/error/i.test(id) || el.getAttribute('aria-invalid') === 'true')) t = clean(d.innerText);
                if (t) break;
            }
            return t;
        };
        const labelOf = el => {
            let t = '';
            if (el.id) { const l = modal.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
            if (!t) t = el.getAttribute('aria-label') || '';
            if (!t) { const l = wrapOf(el)?.querySelector('label, legend'); if (l) t = l.innerText; }
            return clean(t);
        };
        const fields = [];
        let n = 0;

        modal.querySelectorAll('fieldset').forEach(fs => {
            const inputs = [...fs.querySelectorAll('input[type=radio], input[type=checkbox]')];
            if (!inputs.length || inputs.some(i => i.id === 'follow-company-checkbox')) return;
            const key = 'g' + (n++);
            const options = inputs.map((inp, i) => {
                inp.setAttribute('data-ja', key + '-' + i);
                const lbl = (inp.id && fs.querySelector(`label[for="${CSS.escape(inp.id)}"]`)) || inp.closest('label');
                if (lbl) lbl.setAttribute('data-ja-label', key + '-' + i);
                let text = clean(lbl ? lbl.innerText : '');
                if (!text) {
                    // New UI: empty <label>, option text in a sibling <p>. Climb to the
                    // largest ancestor that still holds only this one option.
                    let c = inp.parentElement;
                    while (c && c !== fs && c.parentElement && c.parentElement !== fs
                           && c.parentElement.querySelectorAll('input[type=radio], input[type=checkbox]').length === 1)
                        c = c.parentElement;
                    text = clean(c?.innerText);
                }
                return {idx: i, label: text || clean(inp.value)};
            });
            fields.push({key, kind: inputs[0].type, question: clean(fs.querySelector('legend')?.innerText || labelOf(inputs[0])),
                         options, filled: inputs.some(i => i.checked), error: clean(fs.querySelector(ERR_SEL)?.innerText)});
        });

        modal.querySelectorAll('select').forEach(sel => {
            if (!vis(sel)) return;
            const key = 'f' + (n++);
            sel.setAttribute('data-ja', key);
            const options = [...sel.options].map((o, i) => ({idx: i, label: clean(o.text)}));
            const placeholder = i => i < 0 || !options[i]?.label || /^select an option$|^select$|^choose/i.test(options[i].label);
            fields.push({key, kind: 'select', question: labelOf(sel), options,
                         filled: !placeholder(sel.selectedIndex), error: errorOf(sel)});
        });

        modal.querySelectorAll('input, textarea').forEach(el => {
            if (el.closest('fieldset') && (el.type === 'radio' || el.type === 'checkbox')) return;
            if (['hidden', 'radio', 'submit', 'button'].includes(el.type) || el.id === 'follow-company-checkbox') return;
            if (!vis(el)) return;
            const key = 'f' + (n++);
            el.setAttribute('data-ja', key);
            if (el.type === 'checkbox') {
                fields.push({key, kind: 'single_checkbox', question: labelOf(el), filled: el.checked,
                             error: errorOf(el), required: el.required || el.getAttribute('aria-required') === 'true'});
                return;
            }
            if (el.type === 'file') {
                fields.push({key, kind: 'file', question: clean(labelOf(el) + ' ' + el.id.replace(/[-_]/g, ' ')), filled: true, error: ''});
                return;
            }
            const numeric = el.type === 'number' || /numeric/i.test(el.id) || el.inputMode === 'numeric'
                || el.inputMode === 'decimal';
            const typeahead = el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete');
            // Character limit: maxlength, or the new UI's "1/20" counter under the box.
            let maxlen = el.maxLength > 0 ? el.maxLength : 0;
            if (!maxlen) {
                const m = (wrapOf(el)?.innerText || '').match(/^\\s*\\d+\\s*\\/\\s*(\\d+)\\s*$/m);
                if (m) maxlen = +m[1];
            }
            fields.push({key, kind: 'text', question: labelOf(el), numeric, typeahead, maxlen,
                         filled: !!clean(el.value),
                         error: errorOf(el) || (el.getAttribute('aria-invalid') === 'true' ? 'invalid value' : '')});
        });

        const resumeCards = modal.querySelectorAll('.jobs-document-upload-redesign-card__container, '
            + '[class*="jobs-resume-picker__resume"]');
        const resumeSelected = !!modal.querySelector('.jobs-document-upload-redesign-card__container--selected, '
            + '[class*="jobs-resume-picker__resume"][aria-checked="true"], '
            + 'input[type=radio][name*="resume" i]:checked');

        const btnName = b => clean((b.getAttribute('aria-label') || '') + ' ' + b.innerText).toLowerCase();
        const btns = [...modal.querySelectorAll('button')].filter(b => isVis(b) && !b.disabled);
        const find = re => btns.find(b => re.test(btnName(b)));
        const named = {
            submit: find(/submit application/),
            review: find(/review your application|^review\\b/),
            next: find(/continue to next step|^next\\b|\\bnext$/),
            continue_applying: find(/continue applying/),
            done: find(/^done\\b|\\bdone$/),
        };
        const buttons = [];
        for (const [k, b] of Object.entries(named)) if (b) { b.setAttribute('data-ja-btn', k); buttons.push(k); }

        const progress = modal.querySelector('progress, [role="progressbar"]');
        const text = clean(modal.innerText).toLowerCase();
        return {
            heading: clean(modal.querySelector('h3, h2')?.innerText),
            progress: progress ? (progress.getAttribute('aria-valuenow') || progress.value || '') + '' : '',
            fields, buttons, resumeCards: resumeCards.length, resumeSelected,
            errors: [...modal.querySelectorAll(ERR_SEL)].map(e => clean(e.innerText)).filter(Boolean),
            text: text.slice(0, 3000),
            isEasyApply: !!document.querySelector('.jobs-easy-apply-modal') || /easy apply|apply to /.test(text),
        };
    }""", default=None)
    if state:
        for f in state.get("fields", []):
            f["question"] = _dedupe_label(f.get("question", ""))
            for o in f.get("options") or []:
                o["label"] = _dedupe_label(o["label"])
    return state


def _is_placeholder(label: str) -> bool:
    return bool(re.match(r"^(select an option|select|choose.*|--.*)$", label.strip().lower()))


def _number_from(text: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(m.group()) if m else None


def numeric_answer(question: str, draft: str | None, profile: Profile, error: str = "") -> str:
    """Numeric boxes reject "6.48 LPA" / "Immediately" -- turn the answer
    into a bare number LinkedIn will accept."""
    d = profile.data
    q = question.lower()
    value = None
    if any(_mentions(q, w) for w in ("ctc", "cctc", "ectc", "salary", "compensation", "package", "pay")):
        lpa = float(d["expected_ctc_lpa"] if any(w in q for w in ("expect", "desired", "ectc")) else d["current_ctc_lpa"])
        if any(w in q for w in ("lakh", "lpa", "lac")):
            value = lpa
        elif any(w in q for w in ("month", "monthly")):
            value = round(lpa * 100000 / 12)
        else:
            value = round(lpa * 100000)
    elif "notice" in q:
        value = float(d.get("notice_period_days") or 0)
    elif any(w in q for w in ("year", "experience", "exp ")):
        value = job_match.experience_years_for(question, profile)  # 0 for tech you haven't used
    if value is None:
        value = _number_from(draft or "")
    if value is None:
        value = float(d.get("total_experience_years") or 0)

    err = error.lower()
    if "whole number" in err or "integer" in err:
        value = round(value)
    bounds = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", err)
    if bounds:
        value = min(max(value, float(bounds.group(1))), float(bounds.group(2)))
    floor = re.search(r"larger than\s+(-?\d+(?:\.\d+)?)", err)
    if floor and value <= float(floor.group(1)):
        value = float(floor.group(1)) + 1
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def text_answer(field: dict, profile: Profile, job_context: str, timeout_s: int) -> str | None:
    question = field["question"]
    lower_q = question.lower()
    answer = learned_answers.get_answer(question) or _personal_detail(question, profile)

    if not answer and field.get("typeahead") and any(w in lower_q for w in ("city", "location")):
        answer = profile.data.get("current_city")

    if not answer:
        library = profile.answer_library()
        for key, phrases in TRIGGER_PHRASES.items():
            if key in library and any(p in lower_q for p in phrases):
                answer = str(library[key])
                if key == "years_experience":
                    answer = f"{job_match.experience_years_for(question, profile):g}"
                break

    if not answer:
        if profile.data.get("never_skip", False):
            answer = (llm.best_effort_answer(question, profile.data, job_context)
                      or _fallback_text(question, profile))
        elif _is_sensitive_field(question):
            answer = ask_user(f"LinkedIn question (personal detail):\n{question}", timeout_seconds=timeout_s)
            if answer is None:
                raise SkipJob(f"no response for sensitive field: {question[:120]}")
            learned_answers.save_answer(question, answer)
        else:
            draft = llm.draft_answer(question, profile.data, job_context)
            if draft.startswith("[NEEDS_HUMAN_INPUT"):
                answer = ask_user(f"LinkedIn question (AI couldn't answer from your profile):\n{question}",
                                  timeout_seconds=timeout_s)
                if answer is None:
                    raise SkipJob(f"no response for: {question[:120]}")
                learned_answers.save_answer(question, answer)
            else:
                answer = draft

    # Numeric box, quantity question, or LinkedIn rejected an answer that
    # contains a number ("6.48 LPA" -> "Invalid input") -> send the bare number.
    rejected_with_number = bool(field.get("error")) and bool(re.search(r"\d", answer or "")) \
        and not re.fullmatch(r"\s*\d+(\.\d+)?\s*", answer or "")
    if field.get("numeric") or _wants_number(question) or rejected_with_number:
        answer = numeric_answer(question, answer, profile, field.get("error", ""))

    maxlen = field.get("maxlen") or 0
    if maxlen and len(answer) > maxlen:
        # Short boxes (e.g. "/20") want a number or a word, not a sentence.
        num = _number_from(answer) if maxlen <= 25 else None
        answer = (f"{num:g}" if num is not None else answer)[:maxlen].strip()
    return answer


_YES_NO_START = re.compile(r"^(do|does|are|is|have|has|can|will|would|did|were)\b", re.I)
_QUANTITY = re.compile(r"\b(how many|years?|yrs|lakhs?|lacs?|lpa|ctc|cctc|ectc|salary|notice period|in days|number of|"
                       r"team size|size of)\b", re.I)


def _wants_number(question: str) -> bool:
    """Quantity questions get a bare number even in plain text boxes -- the
    new Easy Apply UI doesn't mark numeric inputs, and "15 LPA" gets
    rejected by number-only boxes. Yes/no questions are left alone."""
    q = question.strip()
    return bool(_QUANTITY.search(q)) and not _YES_NO_START.match(q)


def choose_option(field: dict, profile: Profile, job_context: str, timeout_s: int) -> list[dict]:
    """Returns the option(s) to select for a radio / checkbox / dropdown."""
    question = field["question"]
    options = [o for o in field["options"] if o["label"] and not _is_placeholder(o["label"])]
    if not options:
        return []
    real = [o for o in options if o["label"].strip().lower() not in _SKIP_LIKE] or options
    by_label = {o["label"].strip().lower(): o for o in options}

    # Every personal_details entry named in the question, not just the first --
    # "phone" and "phone country code" both match "Phone country code".
    lower_q = question.lower()
    details = [str(v) for k, v in (profile.data.get("personal_details") or {}).items()
               if v not in (None, "") and str(k).lower() in lower_q]
    for remembered in [learned_answers.get_answer(question)] + details:
        if remembered and remembered.strip().lower() in by_label:
            return [by_label[remembered.strip().lower()]]

    auto = _auto_decide_option(question, real, profile)
    if auto:
        return [auto]

    if profile.data.get("never_skip", False):
        idx = llm.choose_option(question, [o["label"] for o in real], profile.data, job_context)
        return [real[idx] if idx is not None else _default_option(real)]

    pick = _ai_strict_option(question, real, profile, job_context)
    if pick:
        learned_answers.save_answer(question, pick["label"])
        return [pick]

    listing ="\n".join(f"  {o['idx']}: {o['label']}" for o in options)
    response = ask_user(f"LinkedIn question needs a choice:\n{question}\n\nOptions:\n{listing}\n"
                        f"Type the number of your choice:", timeout_seconds=timeout_s)
    if response is None:
        raise SkipJob(f"no response for options question: {question[:120]}")
    picks = []
    for r in (x.strip() for x in response.split(",") if x.strip()):
        match = next((o for o in options if str(o["idx"]) == r or o["label"].lower() == r.lower()), None)
        if match:
            picks.append(match)
    if not picks:
        raise SkipJob(f"couldn't match your response '{response}' to an option: {question[:120]}")
    learned_answers.save_answer(question, picks[0]["label"])
    return picks


def _click_choice(page, key: str, idx: int):
    tag = f"{key}-{idx}"
    try:
        page.locator(f'[data-ja-label="{tag}"]').first.click(timeout=3000)
        return
    except Exception:
        pass
    safe_evaluate(page, """
        (tag) => {
            const el = document.querySelector(`[data-ja-label="${tag}"]`) || document.querySelector(`[data-ja="${tag}"]`);
            if (el) el.click();
        }
    """, arg=tag)


def _fill_text(page, key: str, value: str, typeahead: bool = False):
    loc = page.locator(f'[data-ja="{key}"]').first
    if typeahead:
        loc.fill("")
        loc.type(value, delay=60)
        try:
            page.wait_for_selector('[role="listbox"] [role="option"], .basic-typeahead__selectable', timeout=4000)
            page.locator('[role="listbox"] [role="option"], .basic-typeahead__selectable').first.click(timeout=3000)
        except Exception:
            loc.press("ArrowDown")
            loc.press("Enter")
        return
    loc.fill(value)
    if not loc.input_value().strip():
        loc.click()
        page.keyboard.type(value, delay=20)


def _upload_resume(page, key: str, profile: Profile) -> bool:
    path = Path(profile.data.get("resume_file_name") or "")
    if not path.is_file():
        print(f"  (resume needed but {path} wasn't found next to this script)")
        return False
    try:
        page.locator(f'[data-ja="{key}"]').first.set_input_files(str(path.resolve()))
        page.wait_for_timeout(3000)
        print(f"  (uploaded resume {path.name})")
        return True
    except Exception as e:
        print(f"  (resume upload failed: {e})")
        return False


def fill_step(page, state: dict, profile: Profile, job_context: str, timeout_s: int, retry: bool = False):
    for field in state.get("fields", []):
        kind = field["kind"]
        question = field.get("question") or ""
        try:
            if kind == "file":
                needs_resume = any(_mentions(question, w) for w in ("resume", "cv"))
                resume_listed = state["resumeCards"] > 0 or re.search(r"\.(pdf|docx?)\b", state.get("text", ""))
                resume_error = any("resume" in e.lower() for e in state["errors"])
                if needs_resume and (not resume_listed or (resume_error and not state["resumeSelected"])):
                    _upload_resume(page, field["key"], profile)
                continue

            refill = retry and kind == "text" and _wants_number(question)
            if field["filled"] and not field.get("error") and not refill:
                continue  # prefilled by LinkedIn (email, phone, earlier answers) -- leave it

            if kind == "single_checkbox":
                # Usually "I agree to the terms" / consent boxes.
                page.locator(f'[data-ja="{field["key"]}"]').first.check(force=True, timeout=3000)
                continue

            if kind in ("radio", "checkbox", "select"):
                picks = choose_option(field, profile, job_context, timeout_s)
                if not picks:
                    print(f"  (couldn't read the options for: {question[:80]})")
                    continue
                if kind == "select":
                    loc = page.locator(f'[data-ja="{field["key"]}"]').first
                    try:
                        loc.select_option(label=picks[0]["label"], timeout=3000)
                    except Exception:
                        loc.select_option(index=picks[0]["idx"], timeout=3000)
                else:
                    for pick in picks:
                        _click_choice(page, field["key"], pick["idx"])
                print(f"  ('{picks[0]['label']}' for: {question[:80]})")
                continue

            if kind == "text":
                value = text_answer(field, profile, job_context, timeout_s)
                if value is None:
                    continue
                _fill_text(page, field["key"], value, field.get("typeahead", False))
                print(f"  ('{value[:60]}' for: {question[:80]})")
        except SkipJob:
            raise
        except Exception as e:
            print(f"  (couldn't fill '{question[:60]}': {str(e)[:120]})")

    # A resume list with nothing picked -- pick the first (most recent) one.
    if state["resumeCards"] and not state["resumeSelected"]:
        safe_evaluate(page, "() => {" + MODAL_JS_ROOT + """
            const card = modal && modal.querySelector('.jobs-document-upload-redesign-card__container, '
                + '[class*="jobs-resume-picker__resume"]');
            if (card) card.click();
        }""")


def click_modal_button(page, name: str) -> bool:
    try:
        page.locator(f'[data-ja-btn="{name}"]').first.click(timeout=5000)
        return True
    except Exception:
        return bool(safe_evaluate(page, """
            (n) => { const b = document.querySelector(`[data-ja-btn="${n}"]`); if (b) { b.click(); return true; } return false; }
        """, arg=name, default=False))


def untick_follow_company(page):
    safe_evaluate(page, """
        () => {
            const cb = document.querySelector('#follow-company-checkbox');
            if (cb && cb.checked) {
                const lbl = document.querySelector('label[for="follow-company-checkbox"]');
                (lbl || cb).click();
            }
        }
    """)


def discard_application(page):
    """Closes a half-filled Easy Apply modal so the next job starts clean."""
    try:
        for _ in range(2):
            dismissed = safe_evaluate(page, "() => {" + MODAL_JS_ROOT + """
                if (!modal) return false;
                const b = modal.querySelector('button[aria-label="Dismiss"], button[data-test-modal-close-btn]');
                if (b) { b.click(); return true; }
                return false;
            }""", default=False)
            if not dismissed:
                return
            page.wait_for_timeout(1000)
            safe_evaluate(page, """
                () => {
                    const b = document.querySelector('button[data-control-name="discard_application_confirm_btn"]')
                        || [...document.querySelectorAll('[role="alertdialog"] button, [role="dialog"] button, dialog[open] button')]
                              .find(x => /^discard$/i.test(x.innerText.trim()));
                    if (b) b.click();
                }
            """)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def confirm_sent(page, timeout_ms: int = 12000) -> bool:
    """True once LinkedIn shows "Your application was sent" (or the job page
    flips to "Applied"). Closes the post-apply dialog afterwards."""
    waited = 0
    while waited <= timeout_ms:
        text = (safe_evaluate(page, """
            () => [...document.querySelectorAll('dialog[open], [role="dialog"], .artdeco-modal, .artdeco-toasts_toasts, '
                    + '.jobs-s-apply, .artdeco-inline-feedback--success, .post-apply-timeline')]
                  .map(e => e.innerText).join(' ').toLowerCase()
        """, default="") or "")
        status_card = safe_evaluate(page, "() => " + APPLIED_STATUS_JS, default=False)
        if status_card or any(p in text for p in SENT_PHRASES) or re.search(r"\bapplied (\d+|an?) \w+ ago\b", text):
            safe_evaluate(page, """
                () => {
                    const b = [...document.querySelectorAll('dialog[open] button, [role="dialog"] button, .artdeco-modal button')]
                        .find(x => /^(done|not now)$/i.test(x.innerText.trim()))
                        || document.querySelector('dialog[open] button[aria-label="Dismiss"], [role="dialog"] button[aria-label="Dismiss"]');
                    if (b) b.click();
                }
            """)
            return True
        page.wait_for_timeout(1000)
        waited += 1000
    return False


def run_easy_apply(page, profile: Profile, cfg: dict, job_context: str, timeout_s: int) -> bool:
    """Walks the Easy Apply steps. Returns True only if LinkedIn confirmed
    the application was sent. Raises SkipJob if a step can't be completed."""
    last_sig, stuck = None, 0
    for _ in range(25):  # hard cap on steps per application
        stop = page_has_stop_signal(page)
        if stop:
            raise StopRun(f"stop signal during Easy Apply: {stop}")

        state = read_modal(page)
        if not state:
            return confirm_sent(page, 4000)
        if any(p in state["text"] for p in SENT_PHRASES) and not state["fields"]:
            return confirm_sent(page, 2000)

        if "continue_applying" in state["buttons"] and not state["fields"]:
            click_modal_button(page, "continue_applying")  # "job search safety reminder" interstitial
            page.wait_for_timeout(1500)
            continue

        sig = (state["heading"], state["progress"], tuple(f["question"] for f in state["fields"]))
        stuck = stuck + 1 if sig == last_sig else 0
        last_sig = sig
        if stuck >= 3:
            shot = _save_debug_screenshot(page, f"stuck_{job_context}")
            raise SkipJob(f"(screenshot {shot}) "f"stuck on step '{state['heading']}' -- {'; '.join(state['errors'])[:200]}")

        fill_step(page, state, profile, job_context, timeout_s, retry=stuck > 0)
        page.wait_for_timeout(800)

        buttons = state["buttons"]
        if "submit" in buttons:
            if cfg.get("unfollow_company", True):
                untick_follow_company(page)
            click_modal_button(page, "submit")
            return confirm_sent(page)
        if "review" in buttons:
            click_modal_button(page, "review")
        elif "next" in buttons:
            click_modal_button(page, "next")
        elif "done" in buttons:
            return confirm_sent(page, 3000)
        else:
            raise SkipJob(f"unrecognized Easy Apply step (heading '{state['heading']}', no Next/Review/Submit)")
        page.wait_for_timeout(1500)

    raise SkipJob("too many Easy Apply steps without reaching Submit")


# ---------------------------------------------------------------- main loop

def run():
    profile = Profile.load()
    cfg = li_settings(profile)
    human_timeout = profile.data.get("human_input_timeout_seconds", 120)
    max_apply = int(cfg["stop_after_n_applications"])
    pace = cfg.get("pace_seconds_between_actions") or profile.pace_seconds_between_actions

    applied = 0
    already_applied = load_already_applied()
    print(f"{len(already_applied)} jobs already applied to in earlier runs will be skipped.")

    with sync_playwright() as p:
        browser_mode = cfg.get("browser_mode") or profile.data.get("browser_mode", "visible")
        if browser_mode == "headless":
            browser = p.chromium.launch(headless=True, slow_mo=150)
        elif browser_mode == "minimized":
            browser = p.chromium.launch(headless=False, slow_mo=150, args=["--start-minimized"])
        else:
            browser = p.chromium.launch(headless=False, slow_mo=150)
        context, page = open_logged_in_context(browser)
        # Shared across searches, so a job found by "React JS" isn't reopened under "Node JS".
        seen_job_ids = set()
        searches = job_match.search_terms(profile)
        print(f"Searching {len(searches)} terms: {', '.join(searches)}")

        try:
            for role in searches:
                if applied >= max_apply:
                    break
                search_seen = set()  # end-of-results detection for THIS search only

                for page_no in range(1, int(cfg["max_pages_per_role"]) + 1):
                    if applied >= max_apply:
                        break

                    if not guard.take("linkedin", "searches", profile):
                        raise StopRun("today's LinkedIn search budget is used up (safety: in profile.yaml)")
                    url = search_url(role, profile, cfg, page_no)
                    print(f"\n--- LinkedIn: {role}, page {page_no} ({url}) ---")
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_selector('li[data-occludable-job-id], [data-job-id], '
                                               '.jobs-search-no-results-banner', timeout=15000)
                    except PWTimeout:
                        pass
                    except Exception as e:
                        print(f"  (navigation error, moving to the next role: {e})")
                        break
                    time.sleep(2)

                    stop = page_has_stop_signal(page)
                    if stop:
                        raise StopRun(stop)

                    cards = enumerate_cards(page)
                    if not [c for c in cards if c["jobId"] not in search_seen]:
                        print("No new listings -- last page for this search.")
                        break
                    search_seen.update(c["jobId"] for c in cards)
                    new_cards = [c for c in cards if c["jobId"] not in seen_job_ids]
                    print(f"Found {len(cards)} job cards ({len(new_cards)} not seen in earlier searches) "
                          f"on page {page_no}.")

                    for card in new_cards:
                        seen_job_ids.add(card["jobId"])
                        if applied >= max_apply:
                            break
                        label = f"{card.get('title')} @ {card.get('company')}"

                        if _job_key(card) in already_applied or card.get("state", "").lower().startswith("applied"):
                            print(f"Skipped: {label} — already applied")
                            continue

                        ok, reason = passes_filters(card, profile, role)
                        if ok:
                            ok, reason = title_experience_ok(card.get("title") or "", profile)
                        if not ok:
                            log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"), "skipped", reason])
                            print(f"Skipped: {label} — {reason}")
                            continue

                        try:
                            if not guard.take("linkedin", "views", profile):
                                raise StopRun("today's LinkedIn job-page budget is used up (safety: in profile.yaml)")
                            page.goto(card["href"], wait_until="domcontentloaded")
                            time.sleep(2)
                            stop = page_has_stop_signal(page)
                            if stop:
                                raise StopRun(stop)

                            jd_text = read_description(page)
                            if not jd_text.strip():
                                raise SkipJob("couldn't read the job description (stack filters can't run)")
                            match = job_match.score_job(card.get("title") or "", jd_text, profile)
                            if not match.ok:
                                raise SkipJob(match.reason)
                            tech = find_excluded_tech(jd_text, profile, "tech_exclude_in_description")
                            if tech:
                                raise SkipJob(f"job requires excluded tech ({tech})")

                            state = check_apply_state(page)
                            if state == "applied":
                                already_applied.add(_job_key(card))
                                print(f"Skipped: {label} — already applied on LinkedIn")
                                continue
                            if state == "external":
                                log_external(card)
                                log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"),
                                         "external", f"company-site apply, saved to {EXTERNAL_FILE}"])
                                print(f"External: {label} — saved to {EXTERNAL_FILE}")
                                continue
                            if state == "none":
                                raise SkipJob("no Easy Apply button found")

                            if not click_easy_apply(page):
                                raise SkipJob("Easy Apply didn't open")
                            time.sleep(1.5)

                            job_context = f"{card.get('title')} at {card.get('company')}"
                            if run_easy_apply(page, profile, cfg, job_context, human_timeout):
                                applied += 1
                                already_applied.add(_job_key(card))
                                log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"),
                                         "applied", match.summary()])
                                print(f"Applied: {label} ({applied} total, {match.summary()})")
                            else:
                                shot = _save_debug_screenshot(page, card.get("title"))
                                log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"), "uncertain",
                                         f"couldn't confirm submission — screenshot saved to {shot}"])
                                print(f"UNCERTAIN: {label} — couldn't confirm it was sent. Check it manually.")
                                discard_application(page)

                        except SkipJob as e:
                            discard_application(page)
                            log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"), "skipped", str(e)])
                            print(f"Skipped: {label} — {e}")
                            time.sleep(2)
                            continue
                        except StopRun:
                            raise
                        except Exception as e:
                            discard_application(page)
                            log_row([datetime.now(), SOURCE, card.get("title"), card.get("company"),
                                     "skipped", f"unexpected error: {e}"])
                            print(f"Skipped (unexpected error): {label} — {e}")
                            try:
                                if page.is_closed():
                                    page = context.new_page()
                            except Exception:
                                pass
                            continue

                        guard.human_pause(pace)

        except StopRun as e:
            print(f"STOPPING: {e}")
            log_row([datetime.now(), SOURCE, "-", "-", "stopped", str(e)])
        finally:
            try:
                context.storage_state(path=SESSION_FILE)  # keep refreshed cookies for next run
            except Exception:
                pass
            browser.close()

    print(f"\nDone. {applied} LinkedIn applications submitted this run. See {LOG_FILE} for the full log.")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(errors="replace")  # emoji in job titles never crash a redirected console
    run()
