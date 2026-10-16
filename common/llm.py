"""
Tries Gemini first for screening-answer drafting; if it's out of free-tier
quota (rate-limited or daily-exhausted), automatically falls back to Groq's
free tier instead of stopping the whole run.

Requires at least one of GEMINI_API_KEY or GROQ_API_KEY in your .env file.
Having both means you effectively get two independent free quotas.
"""
import re
import time

from . import gemini
from . import groq_llm

_QUOTA_ERROR_HINTS = ("429", "quota", "rate limit", "resource_exhausted", "exhausted")


def _looks_like_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _QUOTA_ERROR_HINTS)


def draft_answer(question: str, profile_data: dict, job_context: str = "") -> str:
    try:
        return gemini.draft_answer(question, profile_data, job_context)
    except Exception as e:
        if not _looks_like_quota_error(e):
            raise
        print(f"  (Gemini unavailable — {e}. Falling back to Groq...)")
        return groq_llm.draft_answer(question, profile_data, job_context)


def _generate(system: str, prompt: str) -> str:
    """Gemini first (one retry on a transient 503/overload), Groq on any Gemini failure."""
    try:
        try:
            return gemini.generate(system, prompt)
        except Exception as e:
            if "503" not in str(e) and "unavailable" not in str(e).lower():
                raise
            time.sleep(3)
            return gemini.generate(system, prompt)
    except Exception as e:
        print(f"  (Gemini failed — {str(e)[:120]}. Trying Groq...)")
        return groq_llm.generate(system, prompt)


_CHOOSE_SYSTEM = """\
You are filling a job-application screening form for the applicant described
below. Pick the ONE option that best fits the applicant's profile and gives
them the best chance of being shortlisted, without contradicting any fact in
the profile. Reply with ONLY the option number, nothing else."""

_STRICT_RULE = ("\nIf the profile does not contain the facts needed to pick an option truthfully, "
                "reply with the single word UNSURE instead of a number.")


def choose_option(question: str, options: list[str], profile_data: dict, job_context: str = "",
                  strict: bool = False) -> int | None:
    """Returns the 0-based index of the best option, or None if the AI call failed
    (or, with strict=True, if the profile doesn't clearly answer the question)."""
    listing = "\n".join(f"{i}: {o}" for i, o in enumerate(options))
    prompt = (f"APPLICANT PROFILE:\n{profile_data}\n\nJOB: {job_context}\n\n"
              f"QUESTION: {question}\n\nOPTIONS:\n{listing}\n\nOption number:")
    system = _CHOOSE_SYSTEM + _experience_rule(profile_data) + (_STRICT_RULE if strict else "")
    try:
        reply = _generate(system, prompt)
        if strict and "unsure" in reply.lower():
            return None
        m = re.search(r"\d+", reply)
        if m and int(m.group()) < len(options):
            return int(m.group())
    except Exception as e:
        print(f"  (AI option pick failed: {str(e)[:120]})")
    return None


_BEST_EFFORT_SYSTEM = """\
You are filling a job-application screening form for the applicant described
below. Always give an answer -- never refuse and never say information is
missing. Use the profile's facts wherever they apply; where the profile is
silent, give the most reasonable short answer a candidate with this profile
would give (e.g. "Yes", a number, or one short sentence). If the question asks
for a number, reply with just the number. Keep it under 3 sentences. Reply
with the answer text only."""

_EXP_ANY = ("\nIf it asks for years of experience in anything, use the applicant's "
            "total_experience_years.")
_EXP_HONEST = ("\nYears of experience: use total_experience_years only for overall experience or for "
               "technologies in the applicant's skills_primary / work history. For any technology the "
               "profile does not list (or lists in tech_exclude_keywords), answer 0 -- or 'No' for a "
               "yes/no question. Never claim experience the profile doesn't show.")


def _experience_rule(profile_data: dict) -> str:
    return _EXP_ANY if profile_data.get("answer_any_experience_with_total", False) else _EXP_HONEST


def best_effort_answer(question: str, profile_data: dict, job_context: str = "") -> str | None:
    """Always-answer variant of draft_answer. Returns None only if every AI call failed."""
    prompt = (f"APPLICANT PROFILE:\n{profile_data}\n\nJOB: {job_context}\n\n"
              f"QUESTION: {question}\n\nAnswer:")
    try:
        text = _generate(_BEST_EFFORT_SYSTEM + _experience_rule(profile_data), prompt).strip().strip('"')
        if text and not text.startswith("[NEEDS_HUMAN_INPUT"):
            return text
    except Exception as e:
        print(f"  (AI answer failed: {str(e)[:120]})")
    return None
