import re
from urllib.parse import quote

import feedparser

from writer.web_context import EXCLUDED_DOMAINS, fetch_page

_MAX_PAGE_CHARS = 1200
_MAX_RESULT_CHARS = 2000


def _scrape_page(url: str) -> str:
    """Thin string-only wrapper around the shared fetch_page — DDG result
    scraping doesn't need to distinguish failure reasons, just "got usable
    text or not". fetch_page already applies the same boilerplate/excluded-
    domain filtering as the Stage 0 article scrape, so this and
    writer/web_context.py's scrape_source() no longer maintain two separate
    copies of that logic.
    """
    page = fetch_page(url, _MAX_PAGE_CHARS)
    return page if isinstance(page, str) else ""


def _google_news_rss(query: str) -> str:
    """Free, no-key tier for context/recency queries (why-now, comparison) —
    these need actual recent news coverage, not a generic web search result.
    Uses feedparser, the same library already used for feed collection
    elsewhere in this pipeline (collectors/rss_collector.py).

    Deliberately does NOT scrape each entry's linked page: Google News RSS
    `link` fields are Google redirect-wrapper URLs that resolve via
    client-side JS, not a normal HTTP redirect — fetching them with
    `requests` returns Google's own consent interstitial page, not the
    publisher's article (confirmed live: every scraped "article" body was
    actually "Select 'More options' to see additional information..." junk).
    The entry titles themselves are substantive headlines on their own
    (confirmed live, e.g. "Rocket Lab Stock Surges on Iridium Deal: Is This
    the Anti-SpaceX Trade?"), so use those directly instead.
    """
    try:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
        parsed = feedparser.parse(url)
        headlines = [entry.get("title", "") for entry in parsed.entries[:5] if entry.get("title")]
        return " | ".join(headlines)[:_MAX_RESULT_CHARS]
    except Exception as e:
        print(f"[search] Google News RSS query '{query}' failed: {e}")
        return ""


def _ddg_search(query: str) -> str:
    """Universal last-resort tier — also scrapes top result page for richer text.

    Uses the `ddgs` package, not the deprecated `duckduckgo_search` name — the
    old package was returning empty results (or badly mismatched ones, e.g.
    "Zeus GPU" matching Greek mythology) for nearly every query when tested
    live; `ddgs` returned relevant results for the same queries every time.

    Wikipedia/Wikimedia domains are excluded from these organic results too,
    not just from the (now-removed) dedicated Wikipedia tier — DDG's ranking
    is non-deterministic and can surface a Wikipedia page as a top hit on any
    given call regardless (confirmed live: a "Rocket Lab" query returned text
    with Wikipedia-style inline citation markers like "[15]"/"[34]"), and
    Wikipedia is not to be used as a source here in any form.
    """
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            parts: list[str] = []
            for hit in ddgs.text(query, max_results=6):
                href = hit.get("href", "")
                if href and any(d in href.lower() for d in EXCLUDED_DOMAINS):
                    continue
                body = hit.get("body", "")
                if body:
                    parts.append(body)
                if href and len(parts) < 4:
                    page = _scrape_page(href)
                    if page:
                        parts.append(page)
                if len(parts) >= 4:
                    break
            return " ".join(parts)[:_MAX_RESULT_CHARS]
    except ImportError:
        print("[search] ddgs not installed — skipping DDG tier")
        return ""
    except Exception as e:
        print(f"[search] DDG query '{query}' failed: {e}")
        return ""


_NEGATIVE_KEYWORD_STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "and", "or", "to", "not", "is", "was", "it",
}


def _negative_keywords(resolved_sense: str) -> list[str]:
    """Pull exclusion terms out of a "not the X" style disambiguation hint.

    Stage 1's resolved_sense often names the WRONG sense explicitly (e.g.
    "AI-generated image name, not the Indian sweet") — DDG's `-term` operator
    lets that wrong sense be actively pushed out of results instead of just
    hoping the added context outweighs it in ranking.
    """
    match = re.search(r"\bnot\b(?:\s+the)?\s+(.+)", resolved_sense, re.IGNORECASE)
    if not match:
        return []
    words = re.findall(r"[A-Za-z]+", match.group(1))
    seen: list[str] = []
    for w in words:
        lw = w.lower()
        if lw not in _NEGATIVE_KEYWORD_STOPWORDS and lw not in seen:
            seen.append(lw)
        if len(seen) >= 3:
            break
    return seen


def build_identity_query(name: str, entity_type: str, resolved_sense: str | None = None) -> str:
    """Build an identity/overview search query for an entity.

    Previously this was just `'"{name}" {type} overview'`, discarding the
    disambiguation hint (resolved_sense) Stage 1 already extracts for
    ambiguous entities (e.g. "GitHub project reference, not the Harry Potter
    character") — so a search for a project literally named "Tom Riddle"
    had nothing steering it away from the fandom character it shares a name
    with. Folding resolved_sense in as extra context keywords, plus negative
    keywords for whatever sense it explicitly rules out, lets the search
    engine itself disambiguate instead of leaving that entirely to whatever
    the raw name+type happens to rank for.
    """
    query = f'"{name}" {entity_type} overview'
    if resolved_sense:
        query += f" {resolved_sense}"
        for term in _negative_keywords(resolved_sense):
            query += f" -{term}"
    return query


# Identity/overview queries ("what is X") — DDG only. Wikipedia was tried and
# deliberately removed: it's treated as too unreliable/biased a source to use
# at all, not just as a first-tier optimization to fall back from.
IDENTITY_TIERS = [_ddg_search]

# Context queries (why-now, comparison) need recent news coverage — Google
# News RSS first, DDG as a general-web fallback.
CONTEXT_TIERS = [_google_news_rss, _ddg_search]


def search_web(queries: list[str], tiers: list = None) -> dict[str, str]:
    """Search the web for each query string; return {query: combined_text}.

    Tries each tier in `tiers` in order, moving to the next only if the
    current one returns empty text. All tiers are free / keyless.
    """
    tiers = tiers or IDENTITY_TIERS
    results: dict[str, str] = {}
    for q in queries:
        text = ""
        for tier in tiers:
            text = tier(q)
            if text:
                break
        results[q] = text
    return results
