"""Client for the external research agent's /api/concise endpoint.

This is a separate portfolio project (a Hugging Face Docker Space, Chainlit +
FastAPI) that can genuinely investigate a question — read multiple sources,
compare claims, reason about them — rather than just keyword-match a snippet
the way writer/search.py's DDG/Google-News tiers do. Replaces those tiers as
the primary search backend for both entity-identity/disambiguation queries
and GAP/context queries (see writer/synthesize.py Stage 1). The DDG/Google
News tiers in writer/search.py are kept as a local-dev fallback for when
CONCISE_API_URL/CONCISE_API_KEY aren't configured, not deleted.

Confirmed live (session testing, see CLAUDE.md "External research agent
integration"): on a genuinely ambiguous entity name Stage 1 correctly flagged
("Tom Riddle", resolved_sense pointing at a GitHub project, not the Harry
Potter character), the agent searched, found nothing matching, and reported
that honestly instead of confidently returning wrong material — the old DDG
path's failure mode for this exact case. On an entity Stage 1 had no reason
to flag ambiguous ("xovi"), the agent still resolved confidently to the wrong
sense (SEO software, not the real niche product) — Known Issue #12's
unanticipated-collision half is NOT fixed by this integration alone.
"""
import os

import requests

_TIMEOUT = 180

# Phrases the agent uses when it genuinely couldn't find matching material —
# confirmed live verbatim ("I could not locate any GitHub repository or
# project named..."). Caching this as if it were real identity knowledge
# would poison the 45-day entity cache with a confident-sounding "we don't
# know" — worse than not caching at all, since a plain cache miss just
# retries the search next time. Only used to decide whether to persist an
# identity answer to the cache; still passed into entity_context for the
# current run either way (an honest "not found" is harmless context, unlike
# a wrong fact).
_NOT_FOUND_MARKERS = (
    "could not locate", "could not find", "couldn't find", "couldn't locate",
    "unable to find", "unable to locate", "no information found",
    "did not find", "didn't find", "no matching", "no results found",
)


def concise_configured() -> bool:
    return bool(os.environ.get("CONCISE_API_URL", "").strip() and os.environ.get("CONCISE_API_KEY", "").strip())


def ask_concise(question: str) -> str:
    """Ask the research agent a question, return its answer text (empty string
    on any failure) — same graceful-degradation pattern as every search tier
    in writer/search.py, so a flaky external call never crashes the run.

    Prefers `research_report` over `answer` when both are present — the API
    only populates research_report for questions it classifies "complex", in
    which case it's the fuller writeup; `answer` is always populated and is
    the right field for "simple"/"ambiguous" classified questions.
    """
    url = os.environ.get("CONCISE_API_URL", "").strip()
    api_key = os.environ.get("CONCISE_API_KEY", "").strip()
    if not url or not api_key:
        return ""
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            json={"question": question},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[concise] query '{question[:60]}...' failed: {e}")
        return ""
    return (data.get("research_report") or data.get("answer") or "").strip()


def looks_like_not_found(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def build_identity_question(name: str, entity_type: str, resolved_sense: str | None = None) -> str:
    """Build a natural-language identity/overview question for the research
    agent — a plain question, not a DDG-style keyword query with dork
    operators (writer/search.py's build_identity_query uses `-term` negative
    keywords, which are meaningless to a research agent that reads and
    reasons rather than ranking a keyword index).

    Still folds in resolved_sense when Stage 1 flagged the entity ambiguous —
    confirmed live this is what let the agent correctly rule out the Harry
    Potter sense for a "Tom Riddle" GitHub project instead of defaulting to
    the more prominent unrelated sense.
    """
    if resolved_sense:
        return (
            f'What is "{name}" (a {entity_type})? Note: this specifically refers to '
            f"{resolved_sense}. Give a factual, general-purpose overview — do not confuse it "
            f"with any other entity of the same name."
        )
    return f'What is "{name}" (a {entity_type})? Give a factual, general-purpose overview.'
