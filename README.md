# Naukri Auto-Apply

A personal automation tool that searches Naukri.com for jobs matching your
profile, applies to them, and answers screening questions -- using your own
browser session and a free AI provider (Gemini and/or Groq) to draft
free-text answers strictly from facts you provide.

Also includes a resume refresh script (remove + re-upload, to keep your
profile showing as recently active) and an optional mobile-friendly status
dashboard.

## Read this before you use it

**This automates actions against Naukri's terms of service.** Naukri (like
most job platforms) prohibits automated applying, and can detect and
suspend accounts for bot-like activity. This tool tries to reduce that risk
(pacing between actions, stopping instantly on any CAPTCHA or rate-limit
warning, using a real logged-in browser session rather than headless
scraping) but it cannot eliminate the risk. You are running this against
your own account, at your own discretion, and the consequences are yours
to weigh.

**The script never sees or stores your password.** You log into Naukri by
hand in a real browser window; the script only reuses the resulting
session cookies.

**Datacenter IPs get blocked.** Naukri's infrastructure blocks traffic
from cloud-hosting IP ranges (AWS, Oracle Cloud, GCP, etc.) at the network
level. Run this from a residential IP -- your own computer, phone, or a
device on your home network -- not a rented cloud server.

## What it does

- Searches Naukri for your configured target roles, paginating through
  results
- Filters out jobs that don't match your actual experience range, excluded
  companies, or role keywords
- Applies to matching listings and answers the post-apply screening chat:
  - Recurring fields (experience, notice period, CTC, location, shift
    preference) are filled from your profile -- no AI call needed
  - Open-text questions are drafted by a free-tier LLM (Gemini, with an
    automatic fallback to Groq if Gemini's quota is exhausted), strictly
    from facts in your profile -- it will refuse to invent anything and
    ask you directly instead
  - Radio/checkbox questions are auto-answered when your profile clearly
    covers it (e.g. "willing to relocate?"), and pause to ask you in the
    terminal otherwise
  - Sensitive personal fields (date of birth, PAN, Aadhaar, etc.) are
    never guessed -- always routed to you
- Verifies each application actually went through before logging it as
  successful, rather than assuming success
- Logs every outcome (applied / skipped / stopped, with a reason) to
  `applications_log.csv`

## Requirements

- Python 3.10+
- A Naukri account with a resume already uploaded
- A free Gemini API key (and optionally a free Groq key as a fallback) --
  see below
- A residential IP (your own computer/phone/home network -- see the
  warning above)

## Setup

```bash
git clone <this-repo-url>
cd naukri-auto-apply
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Get your free API keys

**Gemini (primary):**
1. Go to <https://aistudio.google.com/apikey>
2. Sign in, click "Create API key" -- no card required for the free tier
3. Free tier is generous (roughly 10 requests/minute, 1,500/day as of
   writing) -- check current limits there, they change

**Groq (optional fallback, recommended):**
1. Go to <https://console.groq.com/keys>
2. Sign in, create a key -- also genuinely free, no card, runs open models
   like Llama
3. If Gemini's quota runs out mid-run, the script automatically switches to
   Groq instead of stopping

### Configure your secrets

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
GEMINI_API_KEY=your-real-key-here
GROQ_API_KEY=your-real-key-here          # optional but recommended
```

### Configure your profile

```bash
cp profile.example.yaml profile.yaml
```

Edit `profile.yaml` with your real details: target roles, current CTC,
notice period, work history, skills, etc. This file is the *only* source
of truth for every answer the script gives -- nothing is invented beyond
what's written here. It's gitignored, so your real details never get
committed if you fork/push this repo.

Put your resume in the project folder with the exact filename set in
`resume_file_name`.

### Capture your login session

```bash
python login_capture.py naukri
```

A real browser window opens. Log in by hand -- password, OTP, any
verification. Once you're on your logged-in Naukri homepage, go back to
the terminal and press Enter. This saves `session_naukri.json` (gitignored
-- treat it like a password, it's equivalent to being logged in).

### Run it

Start small -- set `stop_after_n_applications: 5` in `profile.yaml` for
your first run, and watch the browser window while it works:

```bash
python naukri_apply.py
```

### Resume refresh (optional)

```bash
python naukri_refresh_resume.py
```

Removes and re-uploads your resume to keep your profile showing recent
activity. Can be scheduled (cron / Windows Task Scheduler) to run a few
times a day.

## Project structure

```
naukri_apply.py           Main search + apply loop
naukri_refresh_resume.py  Resume remove/re-upload
login_capture.py          One-time manual login, saves session
schedule_daemon.py        Simple time-based scheduler (for environments
                           without reliable cron, e.g. Termux on Android)
mobile_dashboard.py       Optional Flask status dashboard, phone-friendly
profile.example.yaml      Template -- copy to profile.yaml
.env.example              Template -- copy to .env
common/
  profile.py               Loads and validates profile.yaml
  llm.py                    Dispatches to Gemini, falls back to Groq
  gemini.py / groq_llm.py   Provider-specific clients
  human_input.py            Terminal prompts for anything the AI can't answer
  learned_answers.py        Remembers past answers to recurring questions
```

## Resume-based matching (Naukri and LinkedIn)

Both scripts match jobs against the skills in your resume
(`resume_file_name`), not only against title keywords
(`common/job_match.py`, settings under `matching:` in `profile.yaml`):

- Searches cover your `target_roles` plus your core resume skills
  ("React JS", "Node JS", "MERN Stack" ...). A job found by several searches
  is opened only once.
- Any developer-looking title (e.g. "SDE 2", "Product Engineer") is opened,
  and its description is scored: core skills (React, Node, MongoDB ...)
  count 3 points, other resume skills 1 point. It needs `min_match_score`
  points and `min_core_skills` core skills.
- "5+ years" / "3-6 years" in the description is checked against
  `total_experience_years` (`max_experience_gap_years` of stretch).
- Remote jobs are included when `include_remote: true`.
- The score and matched skills are written to `applications_log.csv`.
- Set `mode: "title"` to go back to the old strict title-keyword matching.

## LinkedIn hiring-post mailer

`linkedin_post_mailer.py` (or `run_mailer.bat`) searches LinkedIn **posts**
for recruiters hiring in your cities (`outreach:` in `profile.yaml`; default
Bangalore and Pune), keeps only posts that match your resume skills and
experience and contain an email address, and emails your resume to them.

- Setup: add `SMTP_USER` and `SMTP_APP_PASSWORD` (a Gmail App Password,
  https://myaccount.google.com/apppasswords) to `.env`. Without them the
  script only saves drafts to `outreach_drafts/`.
- `python linkedin_post_mailer.py --dry-run` saves drafts without sending.
- Every address is emailed once, ever (`outreach_log.csv`), with at most
  `max_emails_per_run` emails per run.
- Skips job seekers' #OpenToWork posts, posts whose stack lines name your
  excluded tech, and posts asking for far more experience than you have.

`run_all.bat` runs Naukri, LinkedIn Easy Apply and the mailer one after
another; a failure in one never stops the next.

## Known limitations

- Naukri's page markup changes periodically; CSS selectors in
  `naukri_apply.py` may need updating when they do. If something stops
  working, compare against the real page's HTML (right-click the relevant
  element -> Inspect -> Copy outerHTML) and adjust the matching selector.
- LinkedIn Easy Apply (`linkedin_apply.py`, or double-click
  `run_linkedin.bat`) uses the same profile, filters and answer logic as
  Naukri; LinkedIn-only options live under `linkedin_settings:` in
  `profile.yaml`. If you're not logged in, a browser opens for you to log in
  and the run then starts by itself. LinkedIn's markup changes often and it
  limits Easy Apply per day, so keep `stop_after_n_applications` modest and
  check `debug_screenshots/` for any "uncertain" results.
- Radio/checkbox auto-answering only covers a small set of common
  yes/no-style questions out of the box (relocate, night shift, weekend
  work, immediately available, currently employed) -- extend the rules in
  `naukri_apply.py`'s `_auto_decide_option` for your own common questions.

## License

MIT -- use, modify, and share freely. No warranty; see the risk notice
above.
