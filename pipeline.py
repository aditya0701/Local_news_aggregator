import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from collectors.github_collector import fetch_trending
from collectors.rss_collector import fetch_feed
from translator.translate import translate_item
from writer.cluster import group_by_topic
from writer.synthesize import synthesize_article

load_dotenv()

CONFIG_DIR = Path(__file__).parent / "config"
OUTPUT_DIR = Path(__file__).parent / "output"


def collect() -> list[dict]:
    items = []

    feeds = yaml.safe_load((CONFIG_DIR / "feeds.yaml").read_text())
    for feed in feeds["feeds"]:
        items.extend(fetch_feed(feed["url"]))

    queries = yaml.safe_load((CONFIG_DIR / "github.yaml").read_text())
    for query in queries["queries"]:
        items.extend(fetch_trending(query["search"]))

    return items


def _article_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _load_store(out_path: Path) -> list[dict]:
    if not out_path.exists():
        return []
    return json.loads(out_path.read_text(encoding="utf-8"))


def run(language: str = "hindi") -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"articles_{language}.json"
    store = _load_store(out_path)
    seen_ids = {item["id"] for item in store}

    new_items = [item for item in collect() if item.get("url") and _article_id(item["url"]) not in seen_ids]

    # Two items in the same run can share a URL (e.g. duplicate feed entries) —
    # dedupe within the batch too, not just against what's already stored.
    deduped = list({_article_id(item["url"]): item for item in new_items}.values())

    now = datetime.now(timezone.utc).isoformat()
    translated = []
    for cluster in group_by_topic(deduped):
        synthesized = synthesize_article(cluster, language) if len(cluster) >= 2 else None
        if synthesized is not None:
            article_id = _article_id("|".join(sorted(synthesized["sources"])))
            synthesized["id"] = article_id
            synthesized["first_seen"] = now
            translated.append(synthesized)
            continue

        for item in cluster:
            translated_item = translate_item(item, language)
            translated_item["id"] = _article_id(item["url"])
            translated_item["first_seen"] = now
            translated.append(translated_item)

    combined = translated + store
    out_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(translated)} new items ({len(combined)} total) to {out_path}")


if __name__ == "__main__":
    run()
