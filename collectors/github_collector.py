import os
import re
from datetime import datetime, timedelta

import requests

GITHUB_API = "https://api.github.com/search/repositories"

# Repos matching these terms are excluded even if they carry a trusted topic
# tag — piracy/abuse tooling isn't appropriate to summarize for a general
# news audience, regardless of how relevant the underlying tech is.
DENYLIST = {"exploit", "cheat", "jailbreak", "crack", "nsfw"}

# Job/internship listing repos (e.g. curated internship trackers) show up under
# legitimate AI/dev topic tags but aren't news — catch them here so they never
# reach the LLM pipeline at all, instead of relying on its judgment every run.
JOB_LISTING_TERMS = {
    "internship", "internships", "hiring", "job board", "job-board",
    "career opportunities", "new-grad", "new grad",
}

# A repo created in the last week with only a handful of stars hasn't shown
# any real traction yet — it's noise, not a trending story.
MIN_STARS = 20

# SEO/listicle repos built to rank in search/trending rather than represent
# real engineering reliably pair a marketing word with a year stamp in the
# title — "Ultimate ... Guide 2026", "Top ... Tools 2026", "Proven 2026 ...
# System". Confirmed against live trending data before wiring this in: every
# repo matching both signals was one a quality check independently rejected.
_MARKETING_WORD_RE = re.compile(r"\b(top|ultimate|proven|guide)\b", re.IGNORECASE)
_YEAR_STAMP_RE = re.compile(r"\b20(2[5-9]|3[0-9])\b")


def _looks_like_listicle(blob: str) -> bool:
    return bool(_YEAR_STAMP_RE.search(blob) and _MARKETING_WORD_RE.search(blob))


def _is_denied(item: dict) -> bool:
    blob = f"{item.get('full_name', '')} {item.get('description') or ''}"
    lower = blob.lower()
    if any(term in lower for term in DENYLIST):
        return True
    if any(term in lower for term in JOB_LISTING_TERMS):
        return True
    if _looks_like_listicle(blob):
        return True
    return item.get("stargazers_count", 0) < MIN_STARS


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
