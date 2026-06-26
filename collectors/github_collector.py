import os
from datetime import datetime, timedelta

import requests

GITHUB_API = "https://api.github.com/search/repositories"


def fetch_trending(query: str, per_page: int = 10) -> list[dict]:
    if "{date}" in query:
        since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        query = query.replace("{date}", since)

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(
        GITHUB_API,
        params={"q": query, "per_page": per_page},
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    return [
        {
            "title": item["full_name"],
            "url": item["html_url"],
            "summary": item.get("description") or "",
            "source": "github",
        }
        for item in items
    ]
