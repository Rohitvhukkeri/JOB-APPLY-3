"""
Resume-driven job matching, shared by naukri_apply.py and linkedin_apply.py.

Instead of rejecting jobs whose TITLE doesn't contain a role keyword (which
skipped good jobs titled "SDE 2", "Product Engineer", "Software Engineer II"
...), every developer-looking job is opened and its description is scored
against the skills actually in your resume, plus the experience it asks for.

  * Resume skills are read from resume_file_name (PDF) -- falls back to the
    skills in profile.yaml if the PDF can't be read.
  * Core skills (the ones in require_in_description_any) count 3 points,
    other resume skills 1 point. A job needs min_match_score points and at
    least min_core_skills core skills.
  * "5+ years" / "3-6 years" in the description is checked against
    total_experience_years (with max_experience_gap_years of stretch).
  * Searches use your target_roles PLUS search terms built from your core
    resume skills ("React JS", "Node JS", "MERN" ...), so jobs titled by
    stack instead of role are found too.

All settings live under `matching:` in profile.yaml (all optional).
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULTS = {
    "mode": "resume",              # resume = score descriptions | title = old strict title keywords
    "skill_search": True,          # also search by your core resume skills
    "skill_search_terms": [],      # empty = built from your resume's core skills
    "min_skill_match_pct": 60,     # apply only if >= this % of the job's skills are on your resume
    "min_core_skills": 1,          # ...and it names at least one of React/Node/Next/Express/MongoDB/MERN/JS
    "min_match_score": 0,          # legacy points threshold (unused when min_skill_match_pct is set)
    # Tech NOT on your resume that should still count as a match (you're happy to take these jobs).
    "count_as_match": ["aws", "llm / genai", "microservices"],
    "max_experience_gap_years": 2,  # skip a job asking for more than total + this many years
    "include_remote": True,
    # Titles that are never developer roles in your stack, even if the description matches.
    "title_exclude_any": ["data engineer", "data scientist", "data analyst", "machine learning", "ml engineer",
                          "embedded", "firmware", "hardware", "mechanical", "electrical", "civil",
                          "network engineer", "support engineer", "site reliability", "sre",
                          "security engineer", "sdet", "qa", "tester", "sales", "marketing", "recruiter",
                          "test", "testing", "automation engineer", "mobile", "android", "ios",
                          "teamcenter", "team center", "lcnc", "low code", "low-code", "no code", "no-code",
                          "servicenow", "service now", "salesforce", "sap", "pega", "appian", "outsystems",
                          "mulesoft", "uipath", "rpa", "power apps", "power platform", "dynamics",
                          "implementation engineer", "sprinklr", "shopify", "wordpress", "magento", "drupal",
                          "part-time", "part time", "equity only", "equity-only", "unpaid", "intern", "internship",
                          "security", "appsec", "cyber", "cybersecurity", "aem", "adobe", "bpm", "newgen", "camunda", "workflow developer", "oracle", "palantir", "foundry"],
}

# Words that make a title look like a software-development role at all.
DEV_TITLE_WORDS = ["developer", "engineer", "sde", "programmer", "full stack", "fullstack", "full-stack",
                   "frontend", "front end", "front-end", "backend", "back end", "back-end", "mern",
                   "react", "node", "javascript", "web", "software", "application", "ui", "swe"]

# canonical skill -> (aliases searched for, search term or None)
SKILL_CATALOG = {
    "react":          (["react", "react.js", "reactjs", "react js"], "React JS"),
    "node":           (["node", "node.js", "nodejs", "node js"], "Node JS"),
    "next.js":        (["next.js", "nextjs", "next js"], "Next JS"),
    "express":        (["express", "express.js", "expressjs"], "Express JS"),
    "mongodb":        (["mongodb", "mongo db", "mongoose"], "MongoDB"),
    "mern":           (["mern"], "MERN Stack"),
    "javascript":     (["javascript", "es6"], "Javascript"),
    "typescript":     (["typescript"], "TypeScript"),
    "redux":          (["redux", "redux toolkit"], None),
    "html":           (["html", "html5"], None),
    "css":            (["css", "css3", "scss", "sass"], None),
    "material-ui":    (["material-ui", "material ui", "mui"], None),
    "bootstrap":      (["bootstrap"], None),
    "rest api":       (["rest api", "rest apis", "restful"], None),
    "websocket":      (["websocket", "websockets", "socket.io"], None),
    "firebase":       (["firebase", "fcm"], None),
    "payment gateway": (["razorpay", "payment gateway", "payment integration"], None),
    "sql":            (["sql", "mysql", "postgresql", "postgres"], None),
    "nosql":          (["nosql"], None),
    "git":            (["git", "github", "gitlab"], None),
    "ci/cd":          (["ci/cd", "ci cd", "continuous integration"], None),
    "heroku":         (["heroku"], None),
    "postman":        (["postman"], None),
    "ssr":            (["ssr", "server-side rendering", "server side rendering"], None),
    "system design":  (["system design"], None),
    "agile":          (["agile", "scrum"], None),
    "authentication": (["authentication", "jwt", "oauth"], None),
    "responsive":     (["responsive"], None),
    "crm":            (["crm"], None),
    "pos":            (["point of sale", "pos system", "pos systems"], None),
}


# Technologies NOT on your resume. They only matter for the match %: a job
# asking for React + Node + AWS + Docker + Kubernetes is a 40% match, not a hit.
OTHER_TECH = {
    "java": ["java", "j2ee"], "spring": ["spring", "spring boot", "springboot"], "python": ["python"],
    "django": ["django"], "flask": ["flask"], "fastapi": ["fastapi"], "angular": ["angular", "angularjs"],
    "vue": ["vue", "vue.js", "vuejs", "nuxt"], "svelte": ["svelte"], "php": ["php"], "laravel": ["laravel"],
    "ruby": ["ruby", "rails", "ruby on rails"], "golang": ["golang", "go lang"], "rust": ["rust"],
    "c#": ["c#"], ".net": [".net", "dotnet", "asp.net"], "c++": ["c++"], "kotlin": ["kotlin"],
    "swift": ["swiftui", "swift ui", "swift programming"], "flutter": ["flutter", "dart"], "react native": ["react native"],
    "android": ["android"], "ios": ["ios"], "aws": ["aws", "amazon web services", "lambda", "ec2", "s3"],
    "azure": ["azure"], "gcp": ["gcp", "google cloud"], "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"], "terraform": ["terraform"], "jenkins": ["jenkins"],
    "graphql": ["graphql"], "redis": ["redis"], "kafka": ["kafka"], "rabbitmq": ["rabbitmq"],
    "elasticsearch": ["elasticsearch", "elastic search"], "microservices": ["microservices", "microservice"],
    "jest": ["jest"], "cypress": ["cypress"], "selenium": ["selenium"], "playwright": ["playwright"],
    "tailwind": ["tailwind", "tailwindcss"], "webpack": ["webpack"], "vite": ["vite"],
    "nestjs": ["nestjs", "nest.js"], "prisma": ["prisma"], "linux": ["linux"], "devops": ["devops"],
    "machine learning": ["machine learning", "deep learning"],
    "llm / genai": ["llm", "llms", "genai", "gen ai", "generative ai", "langchain", "rag", "agentic ai",
                    "ai agents", "agentic", "openai", "gpt", "prompt engineering", "artificial intelligence", "ai/ml"],
    "aem": ["aem", "adobe experience manager"], "salesforce": ["salesforce"], "sap": ["sap"],
    "power bi": ["power bi"], "oracle": ["oracle"], "snowflake": ["snowflake"], "spark": ["apache spark", "pyspark"],
    "unity": ["unity3d", "unity engine"], "figma": ["figma"], "storybook": ["storybook"],
}
# Resume items that are process/domain words, not job skills -- left out of the %.
PCT_IGNORE = {"agile", "responsive", "crm", "pos"}

_NICE_TO_HAVE = re.compile(r"nice[\s-]to[\s-]have|good[\s-]to[\s-]have|preferred|bonus|added advantage|"
                           r"is a plus|would be a plus|plus point|desirable|optional", re.I)
_BULLET = re.compile(r"^\s*([-•*▪◦·➢➤✓✔]|\d+[.)])\s*")


def required_text(description: str) -> str:
    """The description minus "nice to have / preferred / bonus" lines and the
    bullet block under such a heading -- those skills shouldn't count against you."""
    out, skipping = [], False
    for line in (description or "").split("\n"):
        s = line.strip()
        if not s:
            skipping = False
            out.append(line)
            continue
        if _NICE_TO_HAVE.search(s):
            if len(s) > 300:  # one long paragraph: drop only the sentence with the phrase
                out.append(re.sub(r"[^.]*(" + _NICE_TO_HAVE.pattern + r")[^.]*\.?", " ", s, flags=re.I))
                continue
            skipping = len(s) < 60 or s.endswith(":")  # a heading starts a block to skip
            continue
        if skipping and (_BULLET.match(s) or len(s) < 80):
            continue
        skipping = False
        out.append(line)
    return "\n".join(out)


def settings(profile) -> dict:
    return {**DEFAULTS, **(profile.data.get("matching") or {})}


def _has(text: str, phrase: str) -> bool:
    """Whole-word / whole-phrase match ("java" never matches "javascript")."""
    kw = re.escape(phrase.lower().strip())
    return re.search(rf"(?<![a-z0-9]){kw}(?![a-z0-9])", text) is not None


@lru_cache(maxsize=4)
def _resume_text(path: str) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    except Exception as e:
        print(f"  (couldn't read resume {path} -- using profile.yaml skills instead: {e})")
        return ""


def resume_skills(profile) -> tuple[set[str], set[str]]:
    """(core, other) canonical skills found in your resume. Core = skills
    named in require_in_description_any. Tech in tech_exclude_keywords
    (e.g. Java, C++) is never counted, even if the resume mentions it."""
    path = Path(profile.data.get("resume_file_name") or "")
    text = _resume_text(str(path)) if path.is_file() else ""
    if not text:
        text = " ".join(str(s) for s in (profile.data.get("skills_primary") or []))
        text += " " + str(profile.data.get("work_history_narrative") or "")
    text = text.lower()

    found = {name for name, (aliases, _) in SKILL_CATALOG.items() if any(_has(text, a) for a in aliases)}
    core_terms = [t.lower() for t in profile.data.get("require_in_description_any") or []]
    core = {name for name in found if any(t in SKILL_CATALOG[name][0] for t in core_terms)}
    return core, found - core


def search_terms(profile) -> list[str]:
    """target_roles, then skill searches from your core resume skills -- deduplicated."""
    s = settings(profile)
    terms = list(profile.data.get("target_roles") or [])
    if s["skill_search"]:
        extra = s["skill_search_terms"]
        if not extra:
            core, _ = resume_skills(profile)
            extra = [SKILL_CATALOG[n][1] for n in SKILL_CATALOG if n in core and SKILL_CATALOG[n][1]]
        terms += extra
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def title_relevant(title: str, profile) -> tuple[bool, str]:
    """Cheap pre-check on the card title before opening the job page."""
    t = (title or "").lower()
    for word in settings(profile)["title_exclude_any"]:
        if _has(t, word):
            return False, f"not a dev role in your stack ({word})"
    if not any(_has(t, w) for w in DEV_TITLE_WORDS):
        return False, "title isn't a software-development role"
    return True, ""


def location_ok(location: str, profile) -> tuple[bool, str]:
    wanted = profile.data.get("location_include") or []
    loc = (location or "").lower()
    if not wanted or not loc or any(w.lower() in loc for w in wanted):
        return True, ""
    remote_ok = settings(profile)["include_remote"] and profile.data.get("work_mode") != "onsite_or_hybrid"
    if remote_ok and ("remote" in loc or "work from home" in loc or "wfh" in loc):
        return True, ""
    return False, f"location not in {wanted} ({location})"


_EXP_RANGE = re.compile(r"(\d{1,2})(?:\.\d)?\s*\+?\s*(?:-|–|to)\s*(\d{1,2})(?:\.\d)?\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_MIN = re.compile(r"(?:minimum\s+(?:of\s+)?|at\s+least\s+)?(\d{1,2})(?:\.\d)?\s*\+?\s*(?:years?|yrs?)", re.I)


def required_experience(text: str) -> tuple[float, float] | None:
    """First "N-M years" / "N+ years" in the description that is about
    experience. Returns (low, high); high is inf for "N+"."""
    for rx in (_EXP_RANGE, _EXP_MIN):
        for m in rx.finditer(text):
            window = text[max(0, m.start() - 60): m.end() + 60].lower()
            if "experience" not in window and "exp" not in window:
                continue
            lo = float(m.group(1))
            hi = float(m.group(2)) if rx is _EXP_RANGE else float("inf")
            if lo > 25 or hi < lo:
                continue
            return lo, hi
    return None


_GENERIC_EXPERIENCE = ["total", "overall", "relevant", "professional", "work experience", "it experience",
                       "industry", "full stack", "fullstack", "full-stack", "web development", "web developer",
                       "software development", "software developer", "software engineer", "frontend", "front end",
                       "front-end", "backend", "back end", "back-end", "programming", "coding"]
_NAMES_A_SUBJECT = re.compile(r"\b(in|with|wit|on|using|of|for)\s+[a-z0-9.#+]", re.I)


def experience_years_for(question: str, profile) -> float:
    """Years to claim for an experience question. Your total for skills in
    your resume or overall experience; 0 for tech you haven't used, unless
    answer_any_experience_with_total is true."""
    years = float(profile.data.get("total_experience_years") or 0)
    if profile.data.get("answer_any_experience_with_total", False):
        return years
    q = (question or "").lower()
    core, other = resume_skills(profile)
    excluded = [t.lower() for t in profile.data.get("tech_exclude_keywords") or []]
    if any(_has(q, t) for t in excluded):
        return 0.0  # e.g. Java / Python / Angular -- not your professional stack
    if any(_has(q, a) for name in core | other for a in SKILL_CATALOG[name][0]):
        return years
    if any(g in q for g in _GENERIC_EXPERIENCE):
        return years
    # "experience in Power Apps / AWS / Salesforce" -- a tech that isn't on your resume.
    after = q.split("experience", 1)[-1] if "experience" in q else q
    return 0.0 if _NAMES_A_SUBJECT.search(after) else years


@dataclass
class Match:
    ok: bool
    score: int = 0                       # match percentage (0-100)
    core: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    source: str = ""                     # "key skills" or "description"
    reason: str = ""

    def summary(self) -> str:
        have = ", ".join(self.core + self.other)
        miss = f" | missing: {', '.join(self.missing)}" if self.missing else ""
        return f"match {self.score}% via {self.source}: {have}{miss}"


def _resume_names(profile) -> tuple[set[str], set[str]]:
    core, other = resume_skills(profile)
    return core, other - PCT_IGNORE


def _find(text: str, aliases: list[str]) -> bool:
    return any(_has(text, a) for a in aliases)


def _skill_breakdown(text: str, profile) -> tuple[list[str], list[str], list[str]]:
    """(core skills you have, other skills you have, skills you don't have) named in text."""
    core, other = _resume_names(profile)
    have_core = [n for n in sorted(core) if _find(text, SKILL_CATALOG[n][0])]
    have_other = [n for n in sorted(other) if _find(text, SKILL_CATALOG[n][0])]
    accepted = set(settings(profile)["count_as_match"])
    found = [n for n, aliases in OTHER_TECH.items() if _find(text, aliases)]
    have_other += [n for n in found if n in accepted]
    missing = [n for n in found if n not in accepted]
    return have_core, have_other, missing


def _pct(have: int, missing: int) -> int:
    return round(100 * have / (have + missing)) if have + missing else 0


def _key_skills_match(key_skills: list[str], profile) -> tuple[int, list[str], list[str], list[str]] | None:
    """Naukri's declared Key Skills chips: each recognised chip is one vote.
    Returns (pct, matched chips, missing chips, core skills named), or None if
    fewer than 3 chips are recognisable."""
    core, other = _resume_names(profile)
    mine = core | other
    accepted = [n for n in settings(profile)["count_as_match"] if n in OTHER_TECH]
    matched, missing, seen = [], [], set()
    for chip in key_skills:
        c = chip.lower().strip()
        if not c or len(c) > 60 or c in seen:  # Naukri renders each chip twice (link + span)
            continue
        seen.add(c)
        if c in ("ai", "a.i.", "ai/ml") and "llm / genai" in accepted:  # a bare "AI" key-skill chip
            matched.append(chip.strip())
        elif any(_find(c, SKILL_CATALOG[n][0]) for n in mine) or any(_find(c, OTHER_TECH[n]) for n in accepted):
            matched.append(chip.strip())
        elif any(_find(c, aliases) for aliases in OTHER_TECH.values()):
            missing.append(chip.strip())
    if len(matched) + len(missing) < 4:  # too few recognisable chips to judge the skill set
        return None
    named_core = sorted(n for n in core if any(_find(c.lower(), SKILL_CATALOG[n][0]) for c in matched))
    return _pct(len(matched), len(missing)), matched, missing, named_core


def score_job(title: str, description: str, profile, min_score: int | None = None,
              min_core: int | None = None, key_skills: list[str] | None = None,
              min_pct: int | None = None) -> Match:
    """Applies only when >= min_skill_match_pct of the job's skills are on your
    resume -- measured on Naukri's Key Skills list OR on the description
    (whichever is higher; "nice to have" skills don't count against you).
    min_core / min_pct let the mailer use its own bars for short posts;
    min_score is accepted for backwards compatibility and ignored."""
    s = dict(settings(profile))
    if min_core is not None:
        s["min_core_skills"] = min_core
    threshold = s["min_skill_match_pct"] if min_pct is None else min_pct

    text = f"{title}\n{required_text(description)}".lower()
    core, other, missing = _skill_breakdown(text, profile)
    m = Match(ok=True, score=_pct(len(core) + len(other), len(missing)),
              core=core, other=other, missing=missing, source="description")

    ks = _key_skills_match(key_skills or [], profile)
    if ks and (ks[0] >= threshold or ks[0] > m.score):
        # Naukri's declared Key Skills are the job's real skill set -- when they
        # match, they decide, even if the description wording scores lower.
        m.score, m.source, m.missing = ks[0], "key skills", ks[2]
        m.core = sorted(set(m.core) | set(ks[3]))

    # A bare "Engineer" title (no developer / software / web / stack word) can be
    # any engineering field -- e.g. "Engineer, Membrane development". Then the
    # description itself must name two of your stack skills.
    t = (title or "").lower()
    software_title = any(_has(t, w) for w in DEV_TITLE_WORDS if w != "engineer") or \
        any(_has(t, w) for w in ("platform", "product", "api", "cloud", "it", "tech"))
    description_stack = [c for c in core if c != "javascript"] + \
        [o for o in other if o in ("typescript", "redux")]  # from the description text only
    if t and not software_title and len(description_stack) < 2:
        m.ok, m.reason = False, f"generic 'Engineer' title and the description doesn't name your stack"
        return m

    # JavaScript alone is too generic (Oracle / BPM / AEM jobs list it too):
    # the job must name React, Node, Next.js, Express, MongoDB or MERN.
    stack = [c for c in m.core if c != "javascript"]
    if len(stack) < s["min_core_skills"]:
        m.ok, m.reason = False, f"doesn't name your core stack ({', '.join(m.core) or 'none'})"
    elif m.score < threshold:
        m.ok, m.reason = False, f"only {m.score}% of the job's skills are on your resume ({m.summary()})"
    else:
        years = float(profile.data.get("total_experience_years") or 0)
        req = required_experience(description)
        if req:
            lo, hi = req
            if lo > years + s["max_experience_gap_years"]:
                m.ok, m.reason = False, f"asks for {lo:g}+ years (you have {years:g})"
            elif hi < float(profile.data.get("seniority_floor_years") or 0):
                m.ok, m.reason = False, f"junior role ({lo:g}-{hi:g} years)"
    return m
