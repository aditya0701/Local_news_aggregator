import re

import feedparser
import requests
from bs4 import BeautifulSoup

MIN_SUMMARY_LENGTH = 40


def _clean_text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_description(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (TechDrishti digest bot)"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    for selector in (("meta", {"property": "og:description"}), ("meta", {"name": "description"})):
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            return _clean_text(tag["content"])

    paragraph = soup.find("p")
    return _clean_text(paragraph.get_text()) if paragraph else ""


def fetch_feed(url: str, limit: int = 10) -> list[dict]:
    parsed = feedparser.parse(url)
    items = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link", "")
        summary = _clean_text(entry.get("summary", ""))
        if len(summary) < MIN_SUMMARY_LENGTH:
            summary = _fetch_description(link)

        items.append(
            {
                "title": entry.get("title", ""),
                "url": link,
                "summary": summary,
                "published": entry.get("published", ""),
                "source": "rss",
            }
        )
    return items
