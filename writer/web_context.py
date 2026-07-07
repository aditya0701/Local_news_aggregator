import re
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

_TIMEOUT = 10
_MAX_CHARS = 3000
_HEADERS = {
    "User-Agent": "TechDrishti-Crawler/1.0 (+https://github.com/aditya0701/Local_news_aggregator)"
}

# Canonical exclusion/boilerplate lists — also imported by writer/search.py so
# both the Stage 0 article scrape and the DDG search-result scrape agree on
# what to skip, instead of maintaining two copies that can drift apart.
EXCLUDED_DOMAINS = ("wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org")

# When "article p, main p" finds nothing, fetch_page falls back to every <p>
# on the page — which, on sites like Yahoo Finance / Oracle Blogs, pulls in
# the cookie-consent banner text ("Accept all", "Reject all", ...) ahead of
# any real content. Confirmed live via Google News RSS-linked articles.
_BOILERPLATE_MARKERS = (
    "cookie", "accept all", "reject all",
    "subscribe to continue", "sign in to continue",
)


def _is_excluded(url: str) -> bool:
    lowered = (url or "").lower()
    return any(domain in lowered for domain in EXCLUDED_DOMAINS)


def _extract_pdf_text(content: bytes, max_chars: int) -> str:
    reader = PdfReader(BytesIO(content))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text:
            parts.append(text)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return re.sub(r"\s{2,}", " ", " ".join(parts)).strip()[:max_chars]


def fetch_page(url: str, max_chars: int = _MAX_CHARS) -> str | dict:
    """Scrape a page's body text for detail beyond a search snippet, filtering
    cookie-banner/boilerplate paragraphs and excluded domains. Returns the text as
    a plain string on success; returns a dict with an "error" key on any failure,
    so a failed fetch is never mistaken for genuine retrieved content.
    """
    if not url:
        return {"error": "no url given", "url": url}
    if _is_excluded(url):
        return {"error": "excluded domain", "url": url}
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": str(e), "url": url}

    content_type = resp.headers.get("Content-Type", "")
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            text = _extract_pdf_text(resp.content, max_chars)
        except Exception as e:
            return {"error": f"PDF parsing failed: {e}", "url": url}
        if not text:
            return {"error": "PDF had no extractable text (likely scanned/image-based)", "url": url}
        return text

    soup = BeautifulSoup(resp.text, "html.parser")
    paragraphs = soup.select("article p, main p") or soup.find_all("p")
    parts = []
    for p in paragraphs:
        text = p.get_text(" ", strip=True)
        if not text or any(marker in text.lower() for marker in _BOILERPLATE_MARKERS):
            continue
        parts.append(text)
    body = re.sub(r"\s{2,}", " ", " ".join(parts)).strip()[:max_chars]
    if not body:
        return {"error": "no readable body text found (possibly JS-rendered or empty page)", "url": url}
    return body


def scrape_source(url: str) -> str:
    """Fetch an article URL and return its main body text, capped at _MAX_CHARS.

    Thin string-only wrapper around fetch_page for callers that don't need to
    distinguish failure reasons — an empty string covers both "no url" and any
    fetch_page error dict alike.
    """
    page = fetch_page(url, _MAX_CHARS)
    return page if isinstance(page, str) else ""
