"""Runs the real, current Stage 1 triage function against
evals/triage/golden_set.jsonl and reports how well it matches your labels.

Usage:
    python evals/triage/run_triage_eval.py [--limit N]

Requires SARVAM_API_KEY (same as the production pipeline) -- this makes real
API calls: 1 per item the model decides to skip (just the skip gate), up to 3
per item it decides to keep (skip gate + analysis + extraction). Every raw
result is cached in evals/triage/.response_cache.json, keyed by
(item id, prompt version) -- re-running after a labeling fix or on an
unchanged prompt version costs nothing extra for items already cached; only
a genuine prompt-version bump re-queries.

Known limitation: golden_set.jsonl only carries one merged text field
(content_snippet) per item, not production's separate summary/source_text
split (trace-derived items had scraped source text; fresh RSS items had only
the feed's short summary; the entries added for listicle coverage had only a
short manual description). This harness passes content_snippet as
source_text and an empty string as summary. That's a narrower input than
Stage 1 sees in real production, so treat these numbers as a lower bound on
real accuracy, not an exact reproduction of live conditions.

Writes a timestamped report to evals/triage/results/<timestamp>.json.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from writer.synthesize import PROMPT_VERSIONS, _MODEL_FAST, triage_item  # noqa: E402

GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
CACHE_PATH = Path(__file__).parent / ".response_cache.json"

_STAGE1_VERSION_KEYS = ["stage1_skip", "stage1_analysis", "stage1_extraction"]


def _version_key() -> str:
    return "-".join(PROMPT_VERSIONS[k] for k in _STAGE1_VERSION_KEYS)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def run_triage(item: dict, api_key: str, cache: dict, version_key: str) -> dict | None:
    cache_key = f"{item['id']}:{version_key}"
    if cache_key in cache:
        return cache[cache_key]
    result = triage_item(
        title=item.get("title", ""),
        summary="",
        source_text=item.get("content_snippet", ""),
        api_key=api_key,
    )
    cache[cache_key] = result
    return result


def predicted_label(result: dict | None) -> str:
    if result is None:
        return "error"
    return "skip" if result.get("skip") else "publish"


def compute_metrics(rows: list[dict]) -> dict:
    total = len(rows)
    correct = sum(1 for r in rows if r["predicted_label"] == r["true_label"])
    errors = sum(1 for r in rows if r["predicted_label"] == "error")

    tp = sum(1 for r in rows if r["true_label"] == "skip" and r["predicted_label"] == "skip")
    fp = sum(1 for r in rows if r["true_label"] == "publish" and r["predicted_label"] == "skip")
    fn = sum(1 for r in rows if r["true_label"] == "skip" and r["predicted_label"] == "publish")

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    by_reason: dict = defaultdict(lambda: {"total": 0, "correctly_skipped": 0})
    for r in rows:
        if r["true_label"] != "skip":
            continue
        reason = r["true_skip_reason"] or "unknown"
        by_reason[reason]["total"] += 1
        if r["predicted_label"] == "skip":
            by_reason[reason]["correctly_skipped"] += 1

    misclassified = [
        {
            "id": r["id"], "title": r["title"], "true_label": r["true_label"],
            "true_skip_reason": r["true_skip_reason"], "predicted_label": r["predicted_label"],
            "predicted_skip_reason": r["predicted_skip_reason"],
        }
        for r in rows if r["predicted_label"] != r["true_label"]
    ]

    return {
        "accuracy": correct / total if total else None,
        "skip_precision": precision,
        "skip_recall": recall,
        "error_count": errors,
        "total": total,
        "confusion_by_skip_reason": dict(by_reason),
        "misclassified": misclassified,
    }


def print_summary(metrics: dict) -> None:
    def pct(x):
        return f"{x:.1%}" if x is not None else "n/a"

    print(f"Accuracy       : {pct(metrics['accuracy'])} ({metrics['total']} items)")
    print(f"Skip precision : {pct(metrics['skip_precision'])}")
    print(f"Skip recall    : {pct(metrics['skip_recall'])}")
    print(f"Errors (call failed): {metrics['error_count']}")
    print("Per-skip-reason recall (of the ones you labeled skip, how many did the model also skip):")
    for reason, d in metrics["confusion_by_skip_reason"].items():
        rate = d["correctly_skipped"] / d["total"] if d["total"] else 0
        print(f"  {reason:<12} {d['correctly_skipped']}/{d['total']} ({rate:.1%})")
    print(f"Misclassified items: {len(metrics['misclassified'])}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only evaluate the first N golden-set items")
    args = parser.parse_args()

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("SARVAM_API_KEY not set -- can't run live triage calls. Aborting.")
        return

    golden = load_jsonl(GOLDEN_PATH)
    if not golden:
        print(f"No golden set found at {GOLDEN_PATH}. Nothing to evaluate.")
        return
    missing_labels = [r for r in golden if "true_label" not in r]
    if missing_labels:
        print(f"WARNING: {len(missing_labels)} items in golden_set.jsonl have no true_label -- skipping them.")
        golden = [r for r in golden if "true_label" in r]
    if args.limit:
        golden = golden[: args.limit]

    version_key = _version_key()
    cache = load_cache()

    rows = []
    for i, item in enumerate(golden, 1):
        print(f"[{i}/{len(golden)}] {item['title'][:60]}")
        result = run_triage(item, api_key, cache, version_key)
        save_cache(cache)  # after every item -- a crash mid-run loses nothing
        rows.append({
            "id": item["id"],
            "title": item["title"],
            "true_label": item["true_label"],
            "true_skip_reason": item.get("skip_reason"),
            "predicted_label": predicted_label(result),
            "predicted_skip_reason": (result or {}).get("skip_reason"),
        })

    metrics = compute_metrics(rows)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": _MODEL_FAST,
        "prompt_versions": {k: PROMPT_VERSIONS[k] for k in _STAGE1_VERSION_KEYS},
        "golden_set_size": len(rows),
        "metrics": metrics,
        "rows": rows,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"Report written to {out_path}")
    print_summary(metrics)


if __name__ == "__main__":
    main()
