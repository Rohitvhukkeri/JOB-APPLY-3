"""
Builds todo_manual.csv: matching jobs the scripts found but could not finish
on their own, newest first, so none of them is lost:

  * Naukri / LinkedIn jobs that apply on the company's own site
  * Indeed applications opened in a tab to submit by hand
  * applications logged "uncertain" (couldn't confirm it was sent)
  * screening questions that need you (forms, links -- action_needed.csv)

Jobs you've since applied to (applications_log.csv "applied") are left out.

    python manual_todo.py            last 7 days
    python manual_todo.py --days 14
"""
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

OUT = "todo_manual.csv"
FIELDS = ["found", "source", "title", "company", "what_to_do", "link"]


def _rows(path: str):
    if not Path(path).exists():
        return
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        yield from csv.DictReader(f)


def _when(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat((row.get("timestamp") or "").strip())
    except ValueError:
        return None


def _key(title: str, company: str) -> tuple[str, str]:
    return (title or "").strip().lower(), (company or "").strip().lower()


def build(days: int = 7) -> int:
    since = datetime.now() - timedelta(days=days)
    applied = {_key(r.get("title"), r.get("company")) for r in _rows("applications_log.csv")
               if r.get("status") == "applied"}
    todo, seen = [], set()

    def add(when, source, title, company, what, link):
        k = _key(title, company)
        if when is None or when < since or k in applied or (source, k) in seen:
            return
        seen.add((source, k))
        todo.append({"found": when.strftime("%Y-%m-%d %H:%M"), "source": source, "title": title,
                     "company": company, "what_to_do": what, "link": link})

    for r in _rows("external_jobs.csv"):
        add(_when(r), "naukri", r.get("title"), r.get("company"), "apply on the company site",
            r.get("company_site_url") or r.get("naukri_url"))
    for r in _rows("linkedin_external_jobs.csv"):
        add(_when(r), "linkedin", r.get("title"), r.get("company"), "apply on the company site",
            r.get("linkedin_url") or "")
    for r in _rows("indeed_opened.csv"):
        add(_when(r), "indeed", r.get("title"), r.get("company"), "finish the Indeed application",
            r.get("apply_url") or "")
    for r in _rows("applications_log.csv"):
        if r.get("status") == "uncertain":
            add(_when(r), r.get("source"), r.get("title"), r.get("company"),
                "check it was sent (screenshot in debug_screenshots/)", "")
    for r in _rows("action_needed.csv"):
        job = r.get("job") or ""
        title, _, company = job.partition(" at ")
        add(_when(r), "screening", title, company, f"answer: {r.get('question', '')[:200]}", "")

    todo.sort(key=lambda r: r["found"], reverse=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(todo)
    return len(todo)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else 7
    n = build(days)
    print(f"{n} jobs to finish by hand from the last {days} days -- see {OUT} (newest first).")
