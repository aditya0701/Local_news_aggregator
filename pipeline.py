import json
from pathlib import Path

import yaml
from dotenv import load_dotenv

from collectors.github_collector import fetch_trending
from collectors.rss_collector import fetch_feed
from translator.translate import translate_item

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


def run(language: str = "hindi") -> None:
    items = collect()
    translated = [translate_item(item, language) for item in items]

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"digest_{language}.json"
    out_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(translated)} items to {out_path}")


if __name__ == "__main__":
    run()
