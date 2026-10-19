"""
Keeps every script inside limits one person could plausibly hit, so Naukri
and LinkedIn see a single person's pace -- not several bots. Shared by all
scripts and runs through guard_state.json:

  * Cool-downs: after a block / limit warning the site is paused, and every
    script for that site refuses to start until the pause ends (no retrying
    against a site that just said stop).
  * Daily budgets: search-page loads and job/profile page views per site per
    day, counted across every script and run (settings under `safety:` in
    profile.yaml).
  * Human pauses: randomised waits with an occasional longer break, instead of
    the same fixed delay between every action.
"""
import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "guard_state.json"

DEFAULT_LIMITS = {
    "naukri": {"searches": 40, "views": 150},
    "linkedin": {"searches": 25, "views": 90},
    "indeed": {"searches": 20, "views": 80},
}
COOLDOWN_HOURS = 24


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict):
    today = date.today().isoformat()
    state["usage"] = {d: u for d, u in (state.get("usage") or {}).items() if d >= today}  # drop old days
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- cool-downs

def paused(site: str) -> str | None:
    """'YYYY-MM-DD HH:MM (reason)' while the site is cooling down, else None."""
    p = (_load().get("paused") or {}).get(site)
    if not p:
        return None
    try:
        until = datetime.fromisoformat(p["until"])
    except (KeyError, ValueError):
        return None
    return f"{until:%Y-%m-%d %H:%M} ({p.get('reason', '')})" if until > datetime.now() else None


def pause(site: str, reason: str, until: datetime | None = None):
    until = until or datetime.now() + timedelta(hours=COOLDOWN_HOURS)
    state = _load()
    state.setdefault("paused", {})[site] = {"until": until.isoformat(timespec="seconds"), "reason": reason}
    _save(state)
    print(f"{site.capitalize()} automation paused until {until:%Y-%m-%d %H:%M} ({reason}). "
          f"Every {site} script skips itself until then.")


def pause_until_tomorrow(site: str, reason: str):
    """For daily caps: resume shortly after midnight."""
    pause(site, reason, datetime.combine(date.today() + timedelta(days=1), datetime.min.time()) + timedelta(minutes=5))


def refuse_if_paused(site: str):
    p = paused(site)
    if p:
        raise SystemExit(f"{site.capitalize()} automation is paused until {p}. "
                         f"(Remove it from {STATE_FILE.name} to override.)")


# ---------------------------------------------------------------- daily budgets

def _limit(site: str, kind: str, profile=None) -> int:
    cfg = ((profile.data.get("safety") or {}).get(site) or {}) if profile else {}
    return int(cfg.get(kind, DEFAULT_LIMITS[site][kind]))


def take(site: str, kind: str, profile=None, reserve: int = 0) -> bool:
    """Counts one page load of `kind` ('searches' / 'views'). False once
    today's budget is used up -- the caller should wrap up for today.
    `reserve` leaves that many loads unused for a script that runs later the
    same day (e.g. LinkedIn Easy Apply leaves room for the hiring-post pass),
    without raising the daily total."""
    state = _load()
    day = state.setdefault("usage", {}).setdefault(date.today().isoformat(), {})
    used = day.setdefault(site, {}).get(kind, 0)
    if used >= _limit(site, kind, profile) - max(0, reserve):
        return False
    day[site][kind] = used + 1
    _save(state)
    return True


def usage_today(site: str) -> dict:
    return ((_load().get("usage") or {}).get(date.today().isoformat()) or {}).get(site, {})


# ---------------------------------------------------------------- pacing

_actions = {"n": 0, "next_break": random.randint(8, 14)}


def human_pause(base_seconds: float):
    """Sleeps ~0.7x-1.6x base, and every 8-14 actions takes a 1-3 minute break."""
    _actions["n"] += 1
    if _actions["n"] >= _actions["next_break"]:
        _actions["n"], _actions["next_break"] = 0, random.randint(8, 14)
        secs = random.uniform(60, 180)
        print(f"  (short break, {secs:.0f}s)")
    else:
        secs = random.uniform(0.7, 1.6) * base_seconds
    time.sleep(secs)
