from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": "TechDrishti-Crawler/1.0 (+https://github.com/aditya0701/Local_news_aggregator)"
}
_PAGE_TIMEOUT = 8
_MAX_PAGE_CHARS = 1200
_MAX_RESULT_CHARS = 2000

# When "article p, main p" finds nothing, _scrape_page falls back to every <p>
# on the page — which, on sites like Yahoo Finance / Oracle Blogs, pulls in
# the cookie-consent banner text ("Accept all", "Reject all", ...) ahead of
# any real content. Confirmed live via Google News RSS-linked articles.
_BOILERPLATE_MARKERS = (
    "cookie", "accept all", "reject all",
    "subscribe to continue", "sign in to continue",
)


def _scrape_page(url: str) -> str:
    try:
        resp = requests.get(url, timeout=_PAGE_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paras = soup.select("article p, main p") or soup.find_all("p")
        texts = [p.get_text(" ", strip=True) for p in paras[:15]]
        texts = [t for t in texts if not any(m in t.lower() for m in _BOILERPLATE_MARKERS)]
        return " ".join(texts)[:_MAX_PAGE_CHARS]
    except Exception:
        return ""


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


_EXCLUDED_DOMAINS = ("wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org")


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
                if href and any(d in href.lower() for d in _EXCLUDED_DOMAINS):
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
