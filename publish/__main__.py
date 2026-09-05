"""Regenerate the delivery layer from the store: `python -m publish`.

The daily run does this automatically at the end of `pipeline.py`. Run it by
hand after editing `output/articles_hindi.json` directly, or to rebuild the
delivery files from scratch if they are ever deleted or out of date.
"""

import argparse
import json
from pathlib import Path

from publish.delivery import build_delivery

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", default="hindi")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    store_path = args.output_dir / f"articles_{args.language}.json"
    if not store_path.exists():
        raise SystemExit(f"No store at {store_path} -- run the pipeline first.")

    articles = json.loads(store_path.read_text(encoding="utf-8"))
    stats = build_delivery(articles, args.output_dir)
    print(
        f"[delivery] {stats['total']} articles | "
        f"{stats['articles_written']} article files written | "
        f"{stats['shards_written']}/{stats['shards']} month shards written | "
        f"{stats['pruned']} stale files pruned"
    )


if __name__ == "__main__":
    main()
