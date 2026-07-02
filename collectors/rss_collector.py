import re

import feedparser
import requests
from bs4 import BeautifulSoup

MIN_SUMMARY_LENGTH = 40


def _clean_text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_page(url: str):
    if not url:
        return None
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (TechDrishti digest bot)"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    return BeautifulSoup(response.text, "html.parser")


def _description_from_page(soup) -> str:
    for selector in (("meta", {"property": "og:description"}), ("meta", {"name": "description"})):
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            return _clean_text(tag["content"])

    paragraph = soup.find("p")
    return _clean_text(paragraph.get_text()) if paragraph else ""


def _image_from_page(soup) -> str:
    for selector in (("meta", {"property": "og:image"}), ("meta", {"name": "twitter:image"})):
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            return tag["content"]
    return ""


def _image_from_entry(entry) -> str:
    for thumb in entry.get("media_thumbnail", []):
        if thumb.get("url"):
            return thumb["url"]
    for content in entry.get("media_content", []):
        if (content.get("medium") == "image" or (content.get("type") or "").startswith("image")) and content.get("url"):
            return content["url"]
    for link in entry.get("links", []):
        if (link.get("type") or "").startswith("image") and link.get("href"):
            return link["href"]
    return ""


def fetch_feed(url: str, limit: int = 10) -> list[dict]:
    parsed = feedparser.parse(url)
    items = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link", "")
        summary = _clean_text(entry.get("summary", ""))
        image = _image_from_entry(entry)

        if len(summary) < MIN_SUMMARY_LENGTH or not image:
            soup = _fetch_page(link)
            if soup is not None:
                if len(summary) < MIN_SUMMARY_LENGTH:
                    summary = _description_from_page(soup)
                if not image:
                    image = _image_from_page(soup)

        items.append(
            {
                "title": entry.get("title", ""),
                "url": link,
                "summary": summary,
                "image": image,
                "published": entry.get("published", ""),
                "source": "rss",
            }
        )
    return items
