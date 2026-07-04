import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from collectors.github_collector import fetch_trending
from collectors.rss_collector import fetch_feed
from translator.translate import translate_item
from writer.cluster import group_by_topic
from writer.entity_cache import load_cache
from writer.github_gate import filter_notable_repos
from writer.synthesize import SKIP, get_run_traces, synthesize_article

load_dotenv()

CONFIG_DIR = Path(__file__).parent / "config"
OUTPUT_DIR = Path(__file__).parent / "output"


def collect() -> list[dict]:
    items = []

    feeds = yaml.safe_load((CONFIG_DIR / "feeds.yaml").read_text())
    for feed in feeds["feeds"]:
        items.extend(fetch_feed(feed["url"]))

    github_items = []
    queries = yaml.safe_load((CONFIG_DIR / "github.yaml").read_text())
    for query in queries["queries"]:
        github_items.extend(fetch_trending(query["search"]))

    # The same repo often matches multiple topic queries above — dedupe by URL
    # before the judgment gate so it isn't scored twice (and can't get two
    # different verdicts for the same repo in one run).
    github_items = list({item["url"]: item for item in github_items}.values())

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if api_key and github_items:
        github_items = filter_notable_repos(github_items, api_key)

    items.extend(github_items)
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
    for item in store:
        for source_url in item.get("sources", [item.get("url")]):
            if source_url:
                seen_ids.add(_article_id(source_url))

    new_items = [item for item in collect() if item.get("url") and _article_id(item["url"]) not in seen_ids]

    # Two items in the same run can share a URL (e.g. duplicate feed entries) —
    # dedupe within the batch too, not just against what's already stored.
    deduped = list({_article_id(item["url"]): item for item in new_items}.values())

    now = datetime.now(timezone.utc).isoformat()
    translated = []
    for cluster in group_by_topic(deduped):
        synthesized = synthesize_article(cluster, language)
        if synthesized is SKIP:
            continue  # actively rejected — do not fall back to translation
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

    traces = get_run_traces()
    if traces:
        trace_path = OUTPUT_DIR / "pipeline_trace.json"
        trace_path.write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Pipeline trace saved to {trace_path} ({len(traces)} articles traced)")

    cache = load_cache()
    added = sum(1 for t in traces if t.get("cache_updates"))
    new_entities = sum(len(t.get("cache_updates", [])) for t in traces)
    print(f"[cache] {len(cache)} total entities | {new_entities} new entities added this run")


if __name__ == "__main__":
    run()
