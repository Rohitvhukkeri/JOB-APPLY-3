"""
LinkedIn hiring-post mailer.

Searches LinkedIn POSTS (not job listings) for recruiters' hiring posts that
match your resume skills in your chosen cities (Bangalore / Pune by default),
takes the email address from the post, and emails your resume to it with a
subject and body built from profile.yaml.

Usage:
    python linkedin_post_mailer.py             (or double-click run_mailer.bat)
    python linkedin_post_mailer.py --dry-run   (save drafts, send nothing)

Safety rules:
  * Each email address is contacted ONCE, ever (outreach_log.csv).
  * Only posts that are hiring, mention one of your cities, ask for/contain an
    email, and match your resume skills + experience are used. Posts whose
    headline names excluded tech (Java, Python ...) are skipped.
  * At most max_emails_per_run emails per run, with a pause between sends.
  * No Gmail credentials in .env -> drafts are saved to outreach_drafts/
    instead of failing.
  * Stops on a LinkedIn security check / login wall, like the apply scripts.

Needs in .env (Gmail: create an App Password at
https://myaccount.google.com/apppasswords -- requires 2-Step Verification):
    SMTP_USER=your.address@gmail.com
    SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx
    SMTP_HOST=smtp.gmail.com      (optional)
    SMTP_PORT=587                 (optional)

Templates and limits live under `outreach:` in profile.yaml. Uses the same
LinkedIn login (session_linkedin.json) as linkedin_apply.py; nothing in the
Naukri or LinkedIn apply flows is changed by this file.
"""
import csv
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common.profile import Profile
from common import guard, job_match
from naukri_apply import StopRun, safe_evaluate, find_excluded_tech, _mentions
from linkedin_apply import SESSION_FILE, open_logged_in_context, page_has_stop_signal, li_settings

OUTREACH_LOG = "outreach_log.csv"
DRAFTS_DIR = Path("outreach_drafts")

DEFAULT_SUBJECT = "Application for {role} | {name} | {years} yrs | MERN & Next.js | {notice}"
DEFAULT_BODY = """Dear {recruiter},

I came across your LinkedIn post about the {role} opening{company_part} and would like to apply.

I'm a Full Stack Developer with {years} years of experience building SaaS products on the MERN stack and Next.js. Your requirement for {skills} matches the stack I work with every day.

- Total experience: {years} years
- Current CTC: {current_ctc} LPA | Expected CTC: {expected_ctc}
- Notice period: {notice}
- Location: {city} (open to {relocate})

My resume is attached. I'd be glad to discuss how I can contribute to your team.

Best regards,
{name}
{phone} | {email}
{linkedin}
"""

DEFAULTS = {
    "search_roles": ["React developer", "Node.js developer", "MERN stack developer", "Full stack developer",
                     "Next.js developer", "Frontend developer", "Backend developer"],
    "search_locations": ["Bangalore", "Pune"],
    "locations": ["Bangalore", "Bengaluru", "Pune"],   # the post must mention one of these
    "date_posted": "past-week",                       # past-24h | past-week | past-month
    "scrolls_per_search": 6,
    "max_emails_per_run": 25,
    "max_connections_per_run": 15,                    # LinkedIn allows ~100 invites a week
    "max_connections_per_week": 80,                   # stays under LinkedIn's weekly invitation limit
    "connect_without_email": True,                    # matching posts with no email -> connection request to the author
    "post_min_skill_match_pct": 50,                   # posts list fewer skills than job pages, so a lower bar
    "remote_location_words": ["remote", "work from home", "wfh", "pan india", "anywhere in india"],
    "pace_seconds_between_connections": 40,
    "max_emails_per_post": 2,
    "pace_seconds_between_emails": 30,
    "min_match_score": 3,                             # posts are short: one core skill (3 pts) is enough
    "min_core_skills": 1,
    "sender_name": "",
    "resume_attachment_name": "",                     # empty = "<Your_Name>_Resume.pdf"
    "subject": DEFAULT_SUBJECT,
    "body": DEFAULT_BODY,
}

HIRING_WORDS = ["hiring", "looking for", "opening", "openings", "vacancy", "vacancies", "opportunity",
                "share your resume", "send your resume", "share your cv", "send your cv", "share resume",
                "send resume", "share cv", "send cv", "interested candidates", "apply"]
SEEKER_WORDS = ["#opentowork", "open to work", "opentowork", "i am looking for", "i'm looking for",
                "i’m looking for", "looking for a job", "looking for new opportunities", "seeking a new role",
                "seeking new opportunities", "hire me", "i am actively", "i'm actively", "my resume"]
HEADLINE_ROLE_WORDS = ["full stack", "fullstack", "full-stack", "frontend", "front end", "front-end",
                       "backend", "back end", "back-end", "web developer", "ui developer"]
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,6}\b")
SKIP_EMAIL_PARTS = ("noreply", "no-reply", "donotreply", "example.", "linkedin.com", "sentry", "wixpress")
ROLE_RX = re.compile(r"((?:[A-Za-z.+/#-]+\s+){0,4}(?:developer|engineer|programmer)s?)\b", re.I)
ROLE_STOPWORDS = {"hiring", "for", "a", "an", "the", "experienced", "skilled", "talented", "looking", "we",
                  "are", "urgent", "urgently", "immediate", "opening", "openings", "of", "our", "join", "team",
                  "as", "is", "re", "we're", "were", "role", "position", "job", "title", "with", "strong",
                  "multiple", "seeking", "need", "needed", "hire", "to", "and", "who", "can", "in"}


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def outreach_settings(profile: Profile) -> dict:
    return {**DEFAULTS, **(profile.data.get("outreach") or {})}


def load_env():
    """Reads KEY=VALUE lines from .env without overriding real env vars."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def smtp_config() -> dict | None:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
    if not user or not password:
        return None
    return {"host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            "port": int(os.environ.get("SMTP_PORT", "587")), "user": user, "password": password}


# ---------------------------------------------------------------- log

def log_outreach(row: dict):
    fields = ["timestamp", "status", "email", "recruiter", "company", "role", "post_key", "author_url", "reason"]
    new_file = not Path(OUTREACH_LOG).exists()
    with open(OUTREACH_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerow({"timestamp": datetime.now(), **{k: row.get(k, "") for k in fields if k != "timestamp"}})


def load_contacted() -> set[str]:
    done = set()
    if Path(OUTREACH_LOG).exists():
        with open(OUTREACH_LOG, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "sent":
                    done.add((row.get("email") or "").strip().lower())
    return done


# ---------------------------------------------------------------- LinkedIn posts

def search_url(query: str, cfg: dict) -> str:
    params = {"keywords": query, "sortBy": '"date_posted"', "datePosted": f'"{cfg["date_posted"]}"'}
    return "https://www.linkedin.com/search/results/content/?" + urlencode(params)


def read_posts(page, scrolls: int) -> list[dict]:
    for _ in range(scrolls):
        try:
            page.mouse.wheel(0, 5000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
    # Expand "…more" so the whole post text (and any email in it) is rendered.
    safe_evaluate(page, r"""
        () => document.querySelectorAll('[role="listitem"][componentkey^="update-card-focus"] button')
              .forEach(b => { if (/^(…|\.\.\.)?\s*more$/i.test(b.innerText.trim())) b.click(); })
    """)
    page.wait_for_timeout(1000)
    return safe_evaluate(page, r"""
        () => [...document.querySelectorAll('[role="listitem"][componentkey^="update-card-focus"]')].map(it => {
            const key = it.getAttribute('componentkey').replace('update-card-focus', '').replace(/FeedType.*$/, '');
            const body = it.querySelector('[data-testid="expandable-text-box"]');
            const author = it.querySelector('a[href*="/in/"], a[href*="/company/"]');
            const header = it.innerText.split('\n').map(s => s.trim()).filter(Boolean).slice(0, 8);
            return {
                key,
                text: body ? body.innerText : '',
                mailtos: [...it.querySelectorAll('a[href^="mailto:"]')].map(a => a.getAttribute('href').slice(7).split('?')[0]),
                authorUrl: author ? author.href.split('?')[0] : '',
                header,
            };
        })
    """, default=[]) or []


def extract_emails(post: dict) -> list[str]:
    found = list(post.get("mailtos") or []) + EMAIL_RX.findall(post.get("text") or "")
    out = []
    for e in found:
        e = e.strip().strip(".").lower()
        if EMAIL_RX.fullmatch(e) and not any(p in e for p in SKIP_EMAIL_PARTS) and e not in out:
            out.append(e)
    return out


def author_details(post: dict) -> tuple[str, str]:
    """(recruiter first name, company) from the post header lines:
    ["Feed post", "Monika Dhamdhere", "• 2nd", "Talent Acquisition at Follex ...", "1h", ...]."""
    header = [h for h in post.get("header") or [] if h.lower() != "feed post"]
    name = re.sub(r"\s*•.*$", "", header[0]).strip() if header else ""
    first = name.split()[0].strip(",.").title() if name and "/in/" in (post.get("authorUrl") or "") else ""
    company = ""
    for line in header[1:6]:
        m = re.search(r"\bat\s+(.+)$", line)
        if m:
            company = re.split(r"\s+[|•·]\s+", m.group(1))[0].strip(" .")
            break
    if not company and "/company/" in (post.get("authorUrl") or ""):
        company = name
    return first, company


_GENERIC_ROLES = {"developer", "engineer", "sr developer", "senior developer", "sr engineer", "programmer",
                  "software developer", "software engineer"}


def _clean_role(role: str, profile: Profile) -> str:
    """Strips emojis, hashtags, "X is hiring", "at Company" from a role name;
    falls back to your first target role when nothing specific is left."""
    r = re.sub(r"[^\w\s.+#/&()-]", " ", role)
    r = re.sub(r"^.*?\bhiring\b\s*(for\s+)?(an?\s+)?", "", r, flags=re.I)
    r = re.sub(r"#\w+", " ", r)
    r = re.sub(r"\s+at\s+.*$", "", r, flags=re.I)
    r = re.sub(r"\s+", " ", r).strip(" .-:|")
    if not r or r.lower().replace(".", "") in _GENERIC_ROLES and not r.lower().startswith(("software", "senior", "sr")):
        return (profile.data.get("target_roles") or ["Full Stack Developer"])[0]
    return r[:1].upper() + r[1:]


def _singular(role: str) -> str:
    return re.sub(r"\b(developer|engineer|programmer)s\b", r"\1", role, flags=re.I)


def extract_role(text: str, profile: Profile, core: list[str] | None = None) -> str:
    # Multi-role posts ("- React JS – 2–3 yrs – Pune / - Workday Consultant ...")
    # -> use the line naming your stack, not just the first role mentioned.
    aliases = [a for name in (core or []) for a in job_match.SKILL_CATALOG[name][0]]
    for line in text.split("\n"):
        clean = line.strip(" -•*·📌🔹▪️➡️🚀🚨\t")
        clean = re.sub(r"^(we('re|’re| are)\s+)?(now\s+)?(urgently\s+)?hiring\s*(alert\s*)?(for\s+)?(an?\s+)?[:|\-–!]*\s*",
                       "", clean, flags=re.I)
        # "Role: MERN Stack Developer" -> "MERN Stack Developer", not "Role Developer"
        clean = re.sub(r"^(job\s+)?(role|position|title|designation|opening)\s*[:|\-–]\s*", "", clean, flags=re.I)
        if aliases and len(clean) <= 80 and any(_mentions(clean.lower(), a) for a in aliases):
            role = re.split(r"\s+[–|-]\s+|\s*\|\s*|:\s", clean)[0].strip(" :|-–")
            requirement_line = role.lower().startswith((
                "skills", "experience", "tech", "must", "strong", "good", "hands-on", "hands on", "proficien",
                "excellent", "solid", "expert", "knowledge", "familiar", "working", "deep", "proven", "min"))
            if 3 <= len(role) <= 50 and not requirement_line:
                if not re.search(r"\b(developer|engineer|programmer|lead|architect)s?\b", role, re.I):
                    role += " Developer"
                return _singular(role[:1].upper() + role[1:])
    head = text[:400]
    for m in ROLE_RX.finditer(head):
        words = m.group(1).split()
        while words and words[0].lower().strip(":|-–!") in ROLE_STOPWORDS:
            words.pop(0)
        words = [w.strip(":|–!,") for w in words if w.strip(":|–!,")]
        if len(words) >= 2:
            return _singular(" ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words))
    return (profile.data.get("target_roles") or ["Full Stack Developer"])[0]


def evaluate_post(post: dict, profile: Profile, cfg: dict, require_email: bool = True):
    """Returns (ok, reason, emails, match)."""
    text = post.get("text") or ""
    low = text.lower()
    emails = extract_emails(post)
    if require_email and not emails:
        return False, "no email in post", [], None
    if not any(w in low for w in HIRING_WORDS):
        return False, "not a hiring post", emails, None
    _, company = author_details(post)
    for excl in profile.data.get("company_exclude") or []:
        e = excl.lower()
        if e in (company or "").lower() or e in low[:300] or any(e in m for m in emails):
            return False, f"company excluded ({excl})", emails, None
    seeker = next((w for w in SEEKER_WORDS if w in low), None)
    if seeker:
        return False, f"job seeker's post, not a recruiter ({seeker})", emails, None
    if not any(_mentions(low, loc) for loc in list(cfg["locations"]) + list(cfg["remote_location_words"] or [])):
        return False, f"no location from {cfg['locations']} (or remote)", emails, None
    tech = find_excluded_tech(text[:200], profile)
    if tech:
        return False, f"headline names excluded tech ({tech})", emails, None
    tech = find_excluded_tech(text, profile, "tech_exclude_in_description")
    if tech:
        return False, f"requires excluded tech ({tech})", emails, None
    match = job_match.score_job("", text, profile, min_score=cfg["min_match_score"], min_core=cfg["min_core_skills"],
                                min_pct=cfg.get("post_min_skill_match_pct"))
    if not match.ok:
        return False, match.reason, emails, match
    # A line naming your stack together with excluded tech ("Python, C++, SQL,
    # MongoDB", "Java with React") means the role is really that other stack.
    core_aliases = [a for name in match.core for a in job_match.SKILL_CATALOG[name][0]]
    for line in text.split("\n"):
        lower_line = line.lower()
        if any(_mentions(lower_line, a) for a in core_aliases):
            tech = find_excluded_tech(line, profile)
            if tech:
                return False, f"stack line includes excluded tech ({tech})", emails, match
    # One core skill buried in a long post isn't enough -- the headline must
    # name your stack or a generic web role, unless two+ core skills appear.
    if len(match.core) < 2:
        headline = text[:200].lower()
        stack_words = [a for name in match.core for a in job_match.SKILL_CATALOG[name][0]] + HEADLINE_ROLE_WORDS
        if not any(_mentions(headline, w) for w in stack_words):
            return False, f"role in headline isn't your stack ({extract_role(text, profile)})", emails, match
    return True, "", emails, match


# ---------------------------------------------------------------- email

def sender_name(profile: Profile, cfg: dict) -> str:
    if cfg.get("sender_name"):
        return cfg["sender_name"]
    path = Path(profile.data.get("resume_file_name") or "")
    first_line = (job_match._resume_text(str(path)) if path.is_file() else "").strip().split("\n")[0].strip()
    return first_line if 0 < len(first_line) <= 40 else "Applicant"


def compose(post: dict, match, profile: Profile, cfg: dict) -> tuple[str, str, dict]:
    d = profile.data
    pd = d.get("personal_details") or {}
    recruiter, company = author_details(post)
    company = re.sub(r"[^\w\s.&,()'-]+", "", company or "").strip(" .,!-")  # "IntVerse.io! 🚀" -> "IntVerse.io"
    pretty ={"node": "Node.js", "react": "React.js", "next.js": "Next.js", "express": "Express.js",
              "mongodb": "MongoDB", "mern": "MERN", "javascript": "JavaScript", "typescript": "TypeScript"}
    skills = [pretty.get(s, s) for s in (match.core if match else [])] or ["the MERN stack"]
    fields = SafeDict(
        name=sender_name(profile, cfg),
        role=_clean_role(extract_role(post.get("text") or "", profile, match.core if match else None), profile),
        company=company,
        company_part=f" at {company}" if company else "",
        recruiter=recruiter or "Hiring Team",
        skills=", ".join(skills[:-1]) + (" and " if len(skills) > 1 else "") + skills[-1],
        years=f"{d.get('total_experience_years', '')}",
        current_ctc=f"{d.get('current_ctc_lpa', '')}",
        expected_ctc=f"{d.get('expected_ctc_lpa', '')} LPA" + (" (negotiable)" if d.get("expected_ctc_negotiable") else ""),
        notice="Immediate Joiner" if d.get("immediately_available") else f"{d.get('notice_period_days')} days notice",
        city=d.get("current_city", ""),
        relocate=", ".join(c for c in d.get("relocate_cities") or [] if c != d.get("current_city")) or "relocation",
        phone=pd.get("phone", ""),
        email=pd.get("email", ""),
        linkedin=pd.get("linkedin", ""),
    )
    subject = cfg["subject"].format_map(fields).strip()
    body = cfg["body"].format_map(fields).strip() + "\n"
    return subject, body, fields


def check_smtp_login(smtp: dict) -> str | None:
    """Logs in once without sending. Returns None if OK, else a readable error.
    After a rejected password Gmail drops the connection while smtplib tries
    its next auth method, so a disconnect during login also means "rejected"."""
    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as s:
            s.starttls()
            s.login(smtp["user"], smtp["password"])
        return None
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected):
        return (f"Gmail rejected the login for {smtp['user']}. The App Password must be created on that same "
                f"Google account (https://myaccount.google.com/apppasswords).")
    except Exception as e:
        return f"couldn't reach {smtp['host']}:{smtp['port']} ({e})"


def send_email(smtp: dict, to: str, subject: str, body: str, resume: Path, attach_name: str, from_name: str,
               reply_to: str = ""):
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, smtp["user"]))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to and reply_to.lower() != smtp["user"].lower():
        msg["Reply-To"] = reply_to  # replies go to the address on your resume
    msg.set_content(body)
    msg.add_attachment(resume.read_bytes(), maintype="application", subtype="pdf", filename=attach_name)
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as s:
        s.starttls()
        s.login(smtp["user"], smtp["password"])
        s.send_message(msg)


def save_draft(to: str, subject: str, body: str) -> Path:
    DRAFTS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.@-]", "_", to)
    path = DRAFTS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.txt"
    path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}", encoding="utf-8")
    return path


# ---------------------------------------------------------------- main

def run(dry_run: bool = False):
    profile = Profile.load()
    cfg = outreach_settings(profile)
    load_env()

    resume = Path(profile.data.get("resume_file_name") or "")
    smtp = None if dry_run else smtp_config()
    if not dry_run and not smtp:
        print("SMTP_USER / SMTP_APP_PASSWORD not set in .env -- saving drafts to outreach_drafts/ instead of sending.")
        dry_run = True
    if not dry_run and not resume.is_file():
        print(f"Resume {resume} not found -- saving drafts instead of sending.")
        dry_run = True
    if not dry_run:
        problem = check_smtp_login(smtp)
        if problem:
            print(f"{problem}\nSaving drafts to outreach_drafts/ instead of sending.")
            dry_run = True
        else:
            print(f"Gmail login OK ({smtp['user']}) -- emails will be sent.")

    name = sender_name(profile, cfg)
    attach_name = cfg["resume_attachment_name"] or f"{name.replace(' ', '_')}_Resume.pdf"
    contacted = load_contacted()
    print(f"{len(contacted)} recruiters already emailed in earlier runs will be skipped.")
    queries = [f"hiring {role} {loc}" for loc in cfg["search_locations"] for role in cfg["search_roles"]]
    max_emails = int(cfg["max_emails_per_run"])
    sent = 0
    seen_posts = set()

    # Matching posts without an email: send the author a connection request
    # instead (same search pass, so no extra LinkedIn searches).
    import linkedin_connect as lc  # imported here: linkedin_connect imports this module
    connect_on = bool(cfg["connect_without_email"]) and not dry_run
    requested = lc.load_requested()
    connect_left = min(int(cfg["max_connections_per_run"]),
                       max(0, int(cfg["max_connections_per_week"]) - lc.sent_this_week()))
    connected = 0
    if connect_on:
        print(f"Connection requests for matching posts without an email: up to {connect_left} this run "
              f"({lc.sent_this_week()} sent in the last 7 days, weekly cap {cfg['max_connections_per_week']}).")

    with sync_playwright() as p:
        mode = li_settings(profile).get("browser_mode") or profile.data.get("browser_mode", "visible")
        browser = p.chromium.launch(headless=(mode == "headless"), slow_mo=100,
                                    args=["--start-minimized"] if mode == "minimized" else [])
        context, page = open_logged_in_context(browser)
        try:
            for query in queries:
                if sent >= max_emails:
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
                except Exception as e:
                    print(f"  (couldn't load search: {str(e)[:120]})")
                    continue
                stop = page_has_stop_signal(page)
                if stop:
                    raise StopRun(stop)

                posts = read_posts(page, int(cfg["scrolls_per_search"]))
                print(f"Found {len(posts)} posts.")
                connect_targets = []
                for post in posts:
                    if sent >= max_emails and not connect_on:
                        break
                    if not post.get("key") or post["key"] in seen_posts:
                        continue
                    seen_posts.add(post["key"])
                    try:
                        ok, reason, emails, match = evaluate_post(post, profile, cfg, require_email=False)
                        if ok and not emails:
                            url = post.get("authorUrl") or ""
                            key = lc._profile_key(url)
                            if connect_on and "/in/" in url and key not in requested:
                                requested.add(key)
                                first, company = author_details(post)
                                role = _clean_role(extract_role(post.get("text") or "", profile, match.core), profile)
                                connect_targets.append({"name": first, "company": company, "role": role,
                                                        "profile_url": url.split("?")[0],
                                                        "reason": match.summary()})
                            continue
                        if sent >= max_emails:
                            continue
                        if not ok:
                            if emails:  # only posts with an email are worth a log line
                                print(f"Skipped post ({', '.join(emails)}) — {reason}")
                                log_outreach({"status": "skipped", "email": ", ".join(emails), "reason": reason,
                                              "post_key": post["key"], "author_url": post.get("authorUrl")})
                            continue

                        subject, body, fields = compose(post, match, profile, cfg)
                        for email in emails[: int(cfg["max_emails_per_post"])]:
                            if sent >= max_emails:
                                break
                            if email in contacted:
                                print(f"Skipped {email} — already emailed earlier")
                                continue
                            row = {"email": email, "recruiter": fields["recruiter"], "company": fields["company"],
                                   "role": fields["role"], "post_key": post["key"],
                                   "author_url": post.get("authorUrl"), "reason": match.summary()}
                            if dry_run:
                                path = save_draft(email, subject, body)
                                log_outreach({**row, "status": "draft", "reason": f"{match.summary()} | {path}"})
                                print(f"Draft: {email} — {fields['role']}{fields['company_part']} ({path.name})")
                            else:
                                try:
                                    send_email(smtp, email, subject, body, resume, attach_name, name,
                                               reply_to=fields["email"])
                                except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
                                    raise StopRun(f"Gmail rejected the login -- check SMTP_USER / SMTP_APP_PASSWORD ({e})")
                                except Exception as e:
                                    log_outreach({**row, "status": "failed", "reason": str(e)[:200]})
                                    print(f"FAILED: {email} — {e}")
                                    continue
                                log_outreach({**row, "status": "sent"})
                                print(f"Sent: {email} — {fields['role']}{fields['company_part']} ({sent + 1} this run)")
                                guard.human_pause(int(cfg["pace_seconds_between_emails"]))
                            contacted.add(email)
                            sent += 1
                    except StopRun:
                        raise
                    except Exception as e:
                        print(f"  (skipped a post after an unexpected error: {str(e)[:150]})")

                # Profiles are visited after the posts are read: leaving the
                # search page would lose the rest of the results.
                for t in connect_targets:
                    if connected >= connect_left:
                        break
                    who = f"{t['name'] or t['profile_url']} ({t['role']}{' at ' + t['company'] if t['company'] else ''})"
                    if not guard.take("linkedin", "views", profile):
                        # Profile visits are out for today; keep emailing, connect tomorrow.
                        print("  (today's LinkedIn page-view budget is used up -- no more connection requests today)")
                        connect_on, connect_left = False, 0
                        break
                    try:
                        status = lc.send_connect(page, t["profile_url"])
                    except StopRun as e:
                        if "invitation limit" not in str(e):
                            raise
                        # Weekly invite cap hit: stop connecting, keep emailing.
                        print(f"  {e} -- no more connection requests this run.")
                        lc.log_connect({"status": "stopped", "reason": str(e)})
                        connect_on, connect_left = False, 0
                        break
                    except Exception as e:
                        status, t = "failed", {**t, "reason": str(e)[:200]}
                    lc.log_connect({**t, "status": status})
                    print(f"Connect {status}: {who}")
                    if status == "sent":
                        connected += 1
                    guard.human_pause(int(cfg["pace_seconds_between_connections"]))
        except StopRun as e:
            print(f"STOPPING: {e}")
            log_outreach({"status": "stopped", "reason": str(e)})
        finally:
            try:
                context.storage_state(path=SESSION_FILE)
            except Exception:
                pass
            browser.close()

    what = "drafts saved to outreach_drafts/" if dry_run else "emails sent"
    print(f"\nDone. {sent} {what} this run. See {OUTREACH_LOG} for the full log.")
    if connected:
        print(f"{connected} connection requests sent to recruiters whose posts had no email (see {lc.CONNECT_LOG}).")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # emoji in posts never crash a redirected console
    run(dry_run="--dry-run" in sys.argv[1:])
