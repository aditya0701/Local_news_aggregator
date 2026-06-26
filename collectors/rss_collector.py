import feedparser


def fetch_feed(url: str, limit: int = 10) -> list[dict]:
    parsed = feedparser.parse(url)
    return [
        {
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "summary": entry.get("summary", ""),
            "source": "rss",
        }
        for entry in parsed.entries[:limit]
    ]
