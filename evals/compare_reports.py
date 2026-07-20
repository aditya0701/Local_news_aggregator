#!/usr/bin/env python3
"""Task G / Phase 7 regression tool.

Diffs two eval report JSON files (triage, entity_quality, or any future eval
that follows the same "timestamped report with nested numeric metrics"
shape) and prints per-metric deltas with a better/worse marking, plus which
prompt version(s) changed between the two runs. Read-only: makes no API
calls, touches no golden sets. Works across the two existing report shapes
(evals/triage/results/*.json's flat "metrics" dict, evals/entity_quality/
results/*.json's "entity_metrics"/"query_metrics" dicts) without needing to
know which kind of report it's looking at, since it just walks both JSON
trees and compares whatever numeric leaves they have in common.

Usage:
    python evals/compare_reports.py <before.json> <after.json>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Confusion-matrix leaves look like "correct->correct" or "good->dangling_reference"
# (see evals/triage's confusion_by_skip_reason and evals/entity_quality's confusion
# dicts). Diagonal entries (X->X) are agreement counts, where bigger is better;
# off-diagonal entries (X->Y, X != Y) are misclassification counts, where smaller
# is better — the opposite of the "bigger is better" default every other metric
# in these reports uses, so these need their own rule.
_CONFUSION_RE = re.compile(r"^(?P<true>.+)->(?P<predicted>.+)$")

# Fields whose value is a sample-size/count rather than a quality signal —
# a bigger golden set or more agreeing rows isn't "better," so these get no
# better/worse arrow, just a plain delta.
_NEUTRAL_LEAF_NAMES = {"total", "golden_set_size", "agree", "error_count_total"}

# Fields where a smaller number is the improvement (everything else defaults
# to "bigger is better", which covers every accuracy/precision/recall/
# agreement_rate/correctly_skipped field in both report shapes today).
_LOWER_IS_BETTER_LEAF_NAMES = {"error_count"}


def _flatten_numeric(obj, prefix: str = "") -> dict[str, float]:
    """Collect {dotted.path: value} for every int/float leaf reachable
    through nested dicts. Lists (misclassified/disagreements/rows) are
    skipped on purpose — they're per-item breakdowns, not aggregate metrics,
    and two different runs aren't guaranteed to produce comparable rows."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            out.update(_flatten_numeric(value, path))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = obj
    return out


def _flatten_versions(obj, prefix: str = "") -> dict[str, str]:
    """Collect {dotted.path: value} for every string leaf under any key
    whose own name contains 'version' — covers both report shapes'
    prompt_versions / judge_prompt_versions dicts."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if "version" in key.lower() and isinstance(value, dict):
                out.update({f"{path}.{k}": v for k, v in value.items() if isinstance(v, str)})
            elif isinstance(value, dict):
                out.update(_flatten_versions(value, path))
    return out


def _leaf_name(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _marker(path: str, delta: float) -> str:
    leaf = _leaf_name(path)
    if leaf in _NEUTRAL_LEAF_NAMES:
        return "  "
    if delta == 0:
        return "= "
    confusion_match = _CONFUSION_RE.match(leaf)
    if confusion_match:
        lower_is_better = confusion_match.group("true") != confusion_match.group("predicted")
    else:
        lower_is_better = leaf in _LOWER_IS_BETTER_LEAF_NAMES
    improved = (delta < 0) if lower_is_better else (delta > 0)
    return "better" if improved else "WORSE"


def compare_metrics(before: dict, after: dict) -> list[tuple[str, float, float]]:
    before_flat = _flatten_numeric(before)
    after_flat = _flatten_numeric(after)
    shared = sorted(set(before_flat) & set(after_flat))
    return [(path, before_flat[path], after_flat[path]) for path in shared]


def compare_versions(before: dict, after: dict) -> list[tuple[str, str, str]]:
    before_flat = _flatten_versions(before)
    after_flat = _flatten_versions(after)
    shared = sorted(set(before_flat) & set(after_flat))
    return [(path, before_flat[path], after_flat[path]) for path in shared]


def print_report(before_path: Path, after_path: Path, before: dict, after: dict) -> None:
    print(f"BEFORE: {before_path.name}  ({before.get('timestamp', '?')})")
    print(f"AFTER : {after_path.name}  ({after.get('timestamp', '?')})")

    version_rows = compare_versions(before, after)
    changed_versions = [(p, o, n) for p, o, n in version_rows if o != n]
    if changed_versions:
        print("\nPrompt version changes:")
        for path, old, new in changed_versions:
            print(f"  {path}: {old} -> {new}")
    else:
        print("\nPrompt versions: unchanged")

    metric_rows = compare_metrics(before, after)
    if not metric_rows:
        print("\nNo shared numeric metrics found between these two reports.")
        return

    print("\nMetric deltas:")
    name_width = max(len(path) for path, _, _ in metric_rows)
    for path, old, new in metric_rows:
        delta = new - old
        marker = _marker(path, delta)
        sign = "+" if delta >= 0 else ""
        print(f"  {marker:6} {path.ljust(name_width)}  {old:.4f} -> {new:.4f}  ({sign}{delta:.4f})")

    worse = [p for p, o, n in metric_rows if _marker(p, n - o) == "WORSE"]
    if worse:
        print(f"\n{len(worse)} metric(s) regressed: {', '.join(worse)}")
    else:
        print("\nNo regressions among shared metrics.")


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python evals/compare_reports.py <before.json> <after.json>", file=sys.stderr)
        sys.exit(1)

    before_path, after_path = Path(sys.argv[1]), Path(sys.argv[2])
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    print_report(before_path, after_path, before, after)


if __name__ == "__main__":
    main()
