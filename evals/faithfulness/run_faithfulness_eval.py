"""Judge-validation harness for the faithfulness golden set (Task E / Phase 5).

Two layers, matching the plan's own Task E design:

  1. Deterministic layer (`deterministic_check`): claims containing a number
     is the one case that's cheaply and honestly decidable without any model
     call at all -- if a claim states a specific number and that number
     never appears anywhere in the real source text, that's a real
     hallucination, no judgment call needed. Deliberately narrow (numbers
     only, not named entities -- see that function's docstring for why
     named-entity matching was tried and dropped) so this layer only ever
     fires when it's actually confident, not as a rough first guess the LLM
     layer silently overrides.

  2. LLM judge layer (sarvam-105b, reasoning_effort=None -- same as every
     other judge/synthesis call in this pipeline): everything the
     deterministic layer can't decide. This is a closed-book task -- the
     judge sees the same source_text a human grader would, nothing else --
     which is why a validated judge is trustworthy here in a way it
     wouldn't be for a subjective call like triage's "is this genuinely
     tech news."

Two modes, same shape as evals/entity_quality/run_entity_quality_eval.py:

  Validation mode (default): run both layers over golden_set.jsonl, compare
  to the human grade, report agreement overall and per class.

      python evals/faithfulness/run_faithfulness_eval.py [--limit N]

  Scoring mode: run both layers over the full (unlabeled) candidates.jsonl
  and report the hallucination rate (unsupported / total factual claims)
  plus a per-category and per-field breakdown, and the worst articles by
  unsupported-claim count. No ground truth, no accuracy number -- only the
  validated judge's opinion at full scale.

      python evals/faithfulness/run_faithfulness_eval.py --score PATH

Requires SARVAM_API_KEY. Every raw judge response is cached in
evals/faithfulness/.judge_response_cache.json, keyed by (row id, judge
prompt version) -- re-running after a labeling fix or on an unchanged judge
prompt costs nothing extra.

Writes a timestamped report to evals/faithfulness/results/<timestamp>.json.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from writer.prompts import load_prompt  # noqa: E402
from writer.synthesize import _MODEL_QUALITY, _call_sarvam, _parse_json_response  # noqa: E402

CANDIDATES_PATH = Path(__file__).parent / "candidates.jsonl"
GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
CACHE_PATH = Path(__file__).parent / ".judge_response_cache.json"

_JUDGE_PROMPT, _JUDGE_VERSION = load_prompt("faithfulness_judge_v1")

_VALID_GRADES = {"supported", "unsupported", "hedged_opinion"}

# Matches a number a claim could actually be checked against: 2+ digits (a
# bare single digit like "1" is too common in ordinary prose to be a useful
# signal -- "one thing" isn't a number claim), optionally with a decimal
# point, comma thousands-separator, or trailing %.
_NUMBER_RE = re.compile(r"\d[\d,.]*\d|\d{2,}")


def deterministic_check(claim_text: str, source_text: str) -> tuple[str, str] | None:
    """Returns (grade, reason) if confidently decidable without a model
    call, else None to defer to the LLM judge layer.

    Numbers only, not named entities -- named-entity matching was
    considered and dropped: a claim naming a real entity that just happens
    to be phrased differently in the source (a translated company name, a
    different transliteration) would false-positive as "unsupported" with
    no way to catch the mismatch the way a judge reading both texts could.
    A number is a much safer deterministic signal -- "40%" either appears
    in the source or it doesn't, with no translation/phrasing ambiguity."""
    numbers = _NUMBER_RE.findall(claim_text)
    if not numbers:
        return None
    source_flat = source_text.replace(",", "")
    missing = [n for n in numbers if n.replace(",", "") not in source_flat]
    if missing:
        return "unsupported", f"claim states {missing[0]!r}, which does not appear anywhere in source_text"
    return "supported", f"all number(s) in the claim ({', '.join(numbers)}) appear in source_text"


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


def judge_row(row: dict, api_key: str, cache: dict) -> tuple[str, str, str]:
    """Returns (grade, reason, layer) where layer is "deterministic" or "llm"."""
    det = deterministic_check(row.get("claim_text_hi", ""), row.get("source_text", ""))
    if det is not None:
        grade, reason = det
        return grade, reason, "deterministic"

    cache_key = f"{row['id']}:{_JUDGE_VERSION}"
    if cache_key in cache:
        result = cache[cache_key]
    else:
        prompt = _JUDGE_PROMPT.format(
            source_text=row.get("source_text", ""),
            field=row.get("field", ""),
            claim_text_hi=row.get("claim_text_hi", ""),
        )
        raw = _call_sarvam(prompt, api_key, _MODEL_QUALITY, reasoning_effort=None, max_tokens=200)
        result = _parse_json_response(raw)
        cache[cache_key] = result

    if result is None or result.get("grade") not in _VALID_GRADES:
        return "error", (result or {}).get("reason", "") if result else "judge call failed", "llm"
    return result["grade"], result.get("reason", ""), "llm"


def compute_agreement(rows: list[dict]) -> dict:
    total = len(rows)
    agree = sum(1 for r in rows if r["judge_grade"] == r["human_grade"])
    errors = sum(1 for r in rows if r["judge_grade"] == "error")

    per_class: dict = {}
    for g in sorted(_VALID_GRADES):
        class_rows = [r for r in rows if r["human_grade"] == g]
        if not class_rows:
            continue
        class_agree = sum(1 for r in class_rows if r["judge_grade"] == g)
        per_class[g] = {"total": len(class_rows), "agree": class_agree, "agreement_rate": class_agree / len(class_rows)}

    confusion = Counter((r["human_grade"], r["judge_grade"]) for r in rows)
    disagreements = [
        {
            "id": r["id"], "human_grade": r["human_grade"], "judge_grade": r["judge_grade"],
            "layer": r["layer"], "judge_reason": r["judge_reason"], "claim_text_hi": r["claim_text_hi"],
        }
        for r in rows if r["judge_grade"] != r["human_grade"]
    ]
    deterministic_used = sum(1 for r in rows if r["layer"] == "deterministic")

    return {
        "total": total,
        "overall_agreement": agree / total if total else None,
        "error_count": errors,
        "deterministic_layer_used": deterministic_used,
        "per_class": per_class,
        "confusion": {f"{h}->{j}": c for (h, j), c in confusion.items()},
        "disagreements": disagreements,
    }


def run_validation(args) -> None:
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("SARVAM_API_KEY not set -- can't run live judge calls. Aborting.")
        return

    golden = load_jsonl(GOLDEN_PATH)
    if not golden:
        print(f"No golden set found at {GOLDEN_PATH}. Label some claims with label_tool.py first.")
        return
    golden = [r for r in golden if "grade" in r]
    if args.limit:
        golden = golden[: args.limit]

    cache = load_cache()
    rows = []
    for i, row in enumerate(golden, 1):
        print(f"[{i}/{len(golden)}] {row['claim_text_hi'][:60]}")
        grade, reason, layer = judge_row(row, api_key, cache)
        save_cache(cache)  # after every item -- a crash mid-run loses nothing
        rows.append({
            "id": row["id"],
            "human_grade": row["grade"],
            "judge_grade": grade,
            "judge_reason": reason,
            "layer": layer,
            "claim_text_hi": row["claim_text_hi"],
        })

    metrics = compute_agreement(rows)
    report = {
        "mode": "validation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": _MODEL_QUALITY,
        "judge_prompt_version": _JUDGE_VERSION,
        "golden_set_size": len(rows),
        "metrics": metrics,
        "rows": rows,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nReport written to {out_path}")
    print(f"Overall judge-vs-human agreement: {metrics['overall_agreement']:.1%} ({metrics['total']} claims)")
    print(f"Deterministic layer used for: {metrics['deterministic_layer_used']}/{metrics['total']} claims")
    print("Per human-grade-class agreement:")
    for g, d in metrics["per_class"].items():
        print(f"  {g:<16} {d['agree']}/{d['total']} ({d['agreement_rate']:.1%})")


def run_scoring(args) -> None:
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("SARVAM_API_KEY not set -- can't run live judge calls. Aborting.")
        return

    score_path = Path(args.score)
    unlabeled = load_jsonl(score_path)
    if not unlabeled:
        print(f"No rows found at {score_path}. Nothing to score.")
        return
    if args.limit:
        unlabeled = unlabeled[: args.limit]

    cache = load_cache()
    rows = []
    for i, row in enumerate(unlabeled, 1):
        print(f"[{i}/{len(unlabeled)}] {row['claim_text_hi'][:60]}")
        grade, reason, layer = judge_row(row, api_key, cache)
        save_cache(cache)
        rows.append({
            "id": row["id"],
            "article_id": row["article_id"],
            "article_title": row.get("article_title", ""),
            "category": row.get("category"),
            "field": row.get("field"),
            "judge_grade": grade,
            "judge_reason": reason,
            "layer": layer,
        })

    factual_rows = [r for r in rows if r["judge_grade"] in ("supported", "unsupported")]
    unsupported = [r for r in rows if r["judge_grade"] == "unsupported"]
    hallucination_rate = len(unsupported) / len(factual_rows) if factual_rows else None

    by_category: dict = defaultdict(lambda: {"total": 0, "unsupported": 0})
    by_article: dict = defaultdict(lambda: {"title": "", "total": 0, "unsupported": 0})
    for r in factual_rows:
        cat = r["category"] or "unknown"
        by_category[cat]["total"] += 1
        by_article[r["article_id"]]["title"] = r["article_title"]
        by_article[r["article_id"]]["total"] += 1
        if r["judge_grade"] == "unsupported":
            by_category[cat]["unsupported"] += 1
            by_article[r["article_id"]]["unsupported"] += 1

    worst_articles = sorted(
        (
            {"article_id": aid, **stats, "rate": stats["unsupported"] / stats["total"] if stats["total"] else 0}
            for aid, stats in by_article.items()
        ),
        key=lambda a: (-a["unsupported"], -a["rate"]),
    )[:10]

    report = {
        "mode": "scoring",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": _MODEL_QUALITY,
        "judge_prompt_version": _JUDGE_VERSION,
        "scored_file": str(score_path),
        "total_rows": len(rows),
        "grade_distribution": dict(Counter(r["judge_grade"] for r in rows)),
        "hallucination_rate": hallucination_rate,
        "hallucination_rate_note": "unsupported / (supported + unsupported); hedged_opinion/error excluded from denominator",
        "by_category": dict(by_category),
        "worst_articles": worst_articles,
        "rows": rows,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-scoring.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nReport written to {out_path}")
    print(f"Grade distribution: {dict(Counter(r['judge_grade'] for r in rows))}")
    print(f"Hallucination rate: {hallucination_rate:.1%}" if hallucination_rate is not None else "Hallucination rate: n/a")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only evaluate the first N rows")
    parser.add_argument(
        "--score", type=str, default=None,
        help="Path to an unlabeled candidates.jsonl-shaped file to grade with the judge (no accuracy, no ground truth)",
    )
    args = parser.parse_args()

    if args.score:
        run_scoring(args)
    else:
        run_validation(args)


if __name__ == "__main__":
    main()
