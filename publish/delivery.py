"""Builds the browser-facing delivery files from the article store.

The store (`output/articles_hindi.json`) is one growing array, so both frontend
pages had to download the entire archive to render anything -- 4.4MB to show 13
cards, and the same 4.4MB again to show one 8KB article. This splits it into:

    output/index/manifest.json   -- what shards exist, totals per category
    output/index/<YYYY-MM>.json  -- card-sized entries for one month, newest first
    output/articles/<id>.json    -- one full article

Sharding the index by month keeps the homepage's first load flat as the archive
grows (only the newest month is fetched) and means a daily run only rewrites the
current month's shard, so git isn't re-storing the whole index every morning.

Article files are deliberately FLAT rather than nested under a month directory:
`article.html` only has the id from `?id=`, so a nested path would force it to
load the index first just to learn which month to look in -- which is exactly
the whole-archive fetch this module exists to remove.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Cards clamp to 4 lines, the featured card to 6 (see .summary in index.html).
# 320 chars comfortably covers the featured card's 6 lines of Hindi; anything
# past that is never rendered, so carrying it in the index is pure page weight.
SUMMARY_CHARS = 320

# Only the first 3 tags are rendered as chips (tagsHtml in index.html).
CARD_TAGS = 3

_UNDATED_SHARD = "undated"

VALID_CATEGORIES = {"general", "model_release", "acquisition", "ban_regulation", "repo_analysis"}

# Backstop for articles already in the store. `writer.synthesize`'s own
# normalize_category() now fixes this at write time, but the July archive was
# written before that (one article has category "सामान्य", the Hindi label,
# which the frontend's category filter matches against keys and so never finds).
# Kept as a separate copy on purpose: this module reads and writes JSON and has
# no other reason to import the whole synthesis stack.
_CATEGORY_ALIASES = {
    "सामान्य": "general",
    "अधिग्रहण": "acquisition",
    "मॉडल_रिलीज": "model_release",
    "मॉडल रिलीज": "model_release",
    "प्रतिबंध_नियमन": "ban_regulation",
    "प्रतिबंध नियमन": "ban_regulation",
    "नियमन": "ban_regulation",
    "रेपो_विश्लेषण": "repo_analysis",
    "रेपो विश्लेषण": "repo_analysis",
    "रेपो": "repo_analysis",
}


def normalize_category(value: str | None) -> str:
    """Machine key for a category, tolerating Hindi labels and truncated keys.

    Mirrors writer.synthesize.normalize_category: exact key, Hindi label, then
    an unambiguous prefix (the archive holds one article filed as "repo").
    """
    text = (value or "").strip()
    if text in VALID_CATEGORIES:
        return text
    if text in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[text]
    if text:
        matches = [key for key in VALID_CATEGORIES if key.startswith(text)]
        if len(matches) == 1:
            return matches[0]
    return "general"


def _truncate(text: str, limit: int) -> str:
    """Cut to `limit` chars on a word boundary, with an ellipsis if cut."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    # Devanagari uses spaces between words like Latin script does, so a space
    # split is a safe boundary here; fall back to a hard cut if there is none.
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "\u2026"


def _shard_key(article: dict) -> str:
    """`YYYY-MM` the article belongs to, from its first_seen timestamp."""
    stamp = article.get("first_seen") or ""
    return stamp[:7] if len(stamp) >= 7 else _UNDATED_SHARD


def build_card(article: dict) -> dict:
    """The subset of an article the card grid actually renders."""
    sources = article.get("sources") or [article.get("url")]
    card = {
        "id": article.get("id"),
        "title": article.get("title"),
        "summary": _truncate(article.get("summary") or "", SUMMARY_CHARS),
        "category": normalize_category(article.get("category")),
        "tags": (article.get("tags") or [])[:CARD_TAGS],
        "image": article.get("image"),
        "source": article.get("source"),
        "feed_name": article.get("feed_name"),
        "first_seen": article.get("first_seen"),
        # index.html only ever reads sources.length (the "N स्रोत" pill), so the
        # full URL list belongs in the article file, not in every card.
        "sources_count": len([s for s in sources if s]),
    }
    return {k: v for k, v in card.items() if v not in (None, [], "")}


def _write_if_changed(path: Path, payload: str) -> bool:
    """Write only when content differs, so unchanged files don't churn."""
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.write_text(payload, encoding="utf-8")
    return True


def build_delivery(articles: list[dict], output_dir: Path) -> dict:
    """Regenerate the delivery layer from `articles` (newest first).

    Idempotent and safe to re-run: it rewrites only what changed and prunes
    article files whose id is no longer in the store.
    """
    articles_dir = output_dir / "articles"
    index_dir = output_dir / "index"
    articles_dir.mkdir(parents=True, exist_ok=True)
    index_dir.mkdir(parents=True, exist_ok=True)

    written_articles = 0
    live_ids: set[str] = set()
    shards: dict[str, list[dict]] = {}
    categories: dict[str, int] = {}

    for article in articles:
        article_id = article.get("id")
        if not article_id:
            continue  # nothing can link to it without an id
        live_ids.add(article_id)

        payload = json.dumps(article, ensure_ascii=False, indent=2)
        if _write_if_changed(articles_dir / f"{article_id}.json", payload):
            written_articles += 1

        shards.setdefault(_shard_key(article), []).append(build_card(article))
        category = normalize_category(article.get("category"))
        categories[category] = categories.get(category, 0) + 1

    # An article dropped from the store shouldn't stay reachable by direct link.
    pruned = 0
    for stale in articles_dir.glob("*.json"):
        if stale.stem not in live_ids:
            stale.unlink()
            pruned += 1

    written_shards = 0
    shard_meta = []
    for month in sorted(shards, reverse=True):  # newest month first
        cards = shards[month]
        payload = json.dumps(cards, ensure_ascii=False, indent=2)
        if _write_if_changed(index_dir / f"{month}.json", payload):
            written_shards += 1
        shard_meta.append({"month": month, "file": f"{month}.json", "count": len(cards)})

    for stale in index_dir.glob("*.json"):
        if stale.name != "manifest.json" and stale.stem not in shards:
            stale.unlink()
            pruned += 1

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(live_ids),
        "categories": categories,
        "shards": shard_meta,
    }
    _write_if_changed(index_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "total": len(live_ids),
        "articles_written": written_articles,
        "shards_written": written_shards,
        "shards": len(shard_meta),
        "pruned": pruned,
    }
