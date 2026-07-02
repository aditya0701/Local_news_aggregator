import re

import requests
from bs4 import BeautifulSoup

_TIMEOUT = 10
_MAX_CHARS = 3000
_HEADERS = {
    "User-Agent": "TechDrishti-Crawler/1.0 (+https://github.com/aditya0701/Local_news_aggregator)"
}


def scrape_source(url: str) -> str:
    """Fetch an article URL and return its main body text, capped at _MAX_CHARS."""
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paras = soup.select("article p, main p")
        if not paras:
            paras = soup.find_all("p")
        text = " ".join(p.get_text(" ", strip=True) for p in paras)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:_MAX_CHARS]
    except Exception:
        return ""
