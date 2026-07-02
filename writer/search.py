import os

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": "TechDrishti-Crawler/1.0 (+https://github.com/aditya0701/Local_news_aggregator)"
}
_PAGE_TIMEOUT = 8
_MAX_PAGE_CHARS = 1200
_MAX_RESULT_CHARS = 2000


def _scrape_page(url: str) -> str:
    try:
        resp = requests.get(url, timeout=_PAGE_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paras = soup.select("article p, main p") or soup.find_all("p")
        return " ".join(p.get_text(" ", strip=True) for p in paras[:15])[:_MAX_PAGE_CHARS]
    except Exception:
        return ""


def _tavily_search(query: str) -> str:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return ""
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=key)
        resp = client.search(query, max_results=3)
        parts = [r.get("content", "") for r in resp.get("results", []) if r.get("content")]
        return " ".join(parts)[:_MAX_RESULT_CHARS]
    except ImportError:
        print("[search] tavily-python not installed — skipping Tavily tier")
        return ""
    except Exception as e:
        print(f"[search] Tavily query '{query}' failed: {e}")
        return ""


def _exa_search(query: str) -> str:
    key = os.environ.get("EXA_API_KEY", "").strip()
    if not key:
        return ""
    try:
        from exa_py import Exa

        client = Exa(api_key=key)
        resp = client.search_and_contents(query, num_results=3, text=True)
        parts = [r.text for r in resp.results if getattr(r, "text", None)]
        return " ".join(parts)[:_MAX_RESULT_CHARS]
    except ImportError:
        print("[search] exa-py not installed — skipping Exa tier")
        return ""
    except Exception as e:
        print(f"[search] Exa query '{query}' failed: {e}")
        return ""


def _ddg_search(query: str) -> str:
    """DuckDuckGo last-resort tier — also scrapes top result page for richer text."""
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            parts: list[str] = []
            for hit in ddgs.text(query, max_results=3):
                body = hit.get("body", "")
                if body:
                    parts.append(body)
                href = hit.get("href", "")
                if href and len(parts) < 4:
                    page = _scrape_page(href)
                    if page:
                        parts.append(page)
                if len(parts) >= 4:
                    break
            return " ".join(parts)[:_MAX_RESULT_CHARS]
    except ImportError:
        print("[search] duckduckgo-search not installed — skipping DDG tier")
        return ""
    except Exception as e:
        print(f"[search] DDG query '{query}' failed: {e}")
        return ""


def search_web(queries: list[str]) -> dict[str, str]:
    """Search the web for each query string; return {query: combined_text}.

    Tier order: Tavily (clean extracted text) → Exa → DuckDuckGo (with page scraping).
    Each query tries the next tier only if the current one returns empty.
    """
    results: dict[str, str] = {}
    for q in queries:
        text = _tavily_search(q) or _exa_search(q) or _ddg_search(q)
        results[q] = text
    return results
