import os
from datetime import datetime, timedelta

import requests

GITHUB_API = "https://api.github.com/search/repositories"

# Repos matching these terms are excluded even if they carry a trusted topic
# tag — piracy/abuse tooling isn't appropriate to summarize for a general
# news audience, regardless of how relevant the underlying tech is.
DENYLIST = {"exploit", "cheat", "jailbreak", "crack", "nsfw"}


def _is_denied(item: dict) -> bool:
    blob = f"{item.get('full_name', '')} {item.get('description') or ''}".lower()
    return any(term in blob for term in DENYLIST)


def fetch_trending(query: str, per_page: int = 10) -> list[dict]:
    if "{date}" in query:
        since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        query = query.replace("{date}", since)

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            GITHUB_API,
            params={"q": query, "per_page": per_page},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[github] skipping query '{query}': {exc}")
        return []
    items = response.json().get("items", [])
    return [
        {
            "title": item["full_name"],
            "url": item["html_url"],
            "summary": item.get("description") or "",
            "image": f"https://opengraph.githubassets.com/1/{item['full_name']}",
            "source": "github",
        }
        for item in items
        if not _is_denied(item)
    ]
