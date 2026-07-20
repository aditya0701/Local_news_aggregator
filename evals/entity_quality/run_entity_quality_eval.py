"""Judge-validation harness for the entity/GAP-query quality golden set
(Phase 3B, see the plan file's "Phase 3B" section for full rationale).

Why a judge instead of a directly re-runnable metric like the triage eval:
Stage 1's entity/query output is documented (CLAUDE.md) as run-to-run
inconsistent, so grading one specific historical extraction wouldn't stay
meaningful after a future prompt change. Instead: validate a sarvam-105b
judge against the 349 human grades already collected (293 entities, 56
queries), and once judge-vs-human agreement is established, the same judge
can re-grade any future Stage 1 output without needing fresh human labels
every time (that's --score mode, below).

Two modes:

  Validation mode (default): run the judge over evals/entity_quality/
  golden_set.jsonl, compare judge grade to human grade, report agreement
  overall and per grade class, separately for entities and queries (they
  have different label sets and different judge prompts).

      python evals/entity_quality/run_entity_quality_eval.py [--limit N]

  Scoring mode: run the judge over an UNLABELED candidates.jsonl-shaped
  file (e.g. a fresh build_candidates.py/build_query_candidates.py run
  against a Phase 8 prompt v2) and report the grade distribution. No
  accuracy numbers -- there's no ground truth for fresh output, only the
  validated judge's opinion.

      python evals/entity_quality/run_entity_quality_eval.py --score PATH

Requires SARVAM_API_KEY. Every raw judge response is cached in
evals/entity_quality/.judge_response_cache.json, keyed by (row id, judge
prompt version) -- re-running after a labeling fix or on an unchanged judge
prompt costs nothing extra; only a genuine judge-prompt-version bump or a
new row re-queries.

Writes a timestamped report to evals/entity_quality/results/<timestamp>.json.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from writer.prompts import load_prompt  # noqa: E402
from writer.synthesize import _MODEL_QUALITY, _call_sarvam, _parse_json_response  # noqa: E402
from writer.web_context import scrape_source  # noqa: E402

CANDIDATES_PATH = Path(__file__).parent / "candidates.jsonl"
GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
CACHE_PATH = Path(__file__).parent / ".judge_response_cache.json"
SCRAPE_CACHE_PATH = Path(__file__).parent / ".scraped_context_cache.json"

# candidates.jsonl's "article_context" is stage0.source_preview, which is only
# source_text[:400] (writer/synthesize.py) -- but Stage 1 itself extracts entities/queries
# from source_text[:2000]. Feeding the judge only the 400-char preview produced a flood of
# false "hallucinated_not_in_article" verdicts on entities that are real but just past
# character 400 (confirmed live: "Vistara", "The Register", "Antares Nuclear" etc.). This
# re-scrapes each article's real URL live and slices to the same 2000 chars Stage 1 saw, so
# the judge is checking against what Stage 1 actually had, not a UI preview snippet. Frozen
# golden_set.jsonl/candidates.jsonl are never touched -- this only enriches what gets sent to
# the judge at grading time.
_SCRAPE_CHARS = 2000

_ENTITY_JUDGE_PROMPT, _ENTITY_JUDGE_VERSION = load_prompt("entity_quality_judge_entity_v1")

# Query judging is a two-step call, mirroring Stage 1's own skip-gate-then-analysis split:
# a dedicated self-containment check first (query text ONLY, no article context) decides
# dangling_reference on its own; only a self-contained query goes on to the second call for
# the remaining four labels. This was added after the single-call judge showed 0/6 agreement
# on dangling_reference -- the human's actual labeling included vague "other X" comparisons
# (no named comparison target) as dangling, not just literal this/it/that.
_QUERY_SELFCONTAINED_PROMPT, _QUERY_SELFCONTAINED_VERSION = load_prompt("entity_quality_judge_query_selfcontained_v1")
_QUERY_JUDGE_PROMPT, _QUERY_JUDGE_VERSION = load_prompt("entity_quality_judge_query_v2")

_ENTITY_GRADES = {"correct", "wrong_type", "hallucinated_not_in_article", "not_an_entity", "ambiguity_mislabeled"}
_QUERY_GRADES = {"good", "hallucinated_competitor", "dangling_reference", "not_fact_seeking", "irrelevant"}


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


def load_scrape_cache() -> dict:
    if SCRAPE_CACHE_PATH.exists():
        return json.loads(SCRAPE_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_scrape_cache(cache: dict) -> None:
    SCRAPE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def article_context_for_judge(row: dict, scrape_cache: dict) -> str:
    """Real source text (source_text[:2000], matching Stage 1's own input) for the article
    this row belongs to, live-scraped and cached by URL. Falls back to the stored 400-char
    preview if the URL is unreachable/dead -- same graceful-degradation pattern used
    throughout this pipeline, not an all-or-nothing dependency."""
    url = row.get("article_url", "")
    if url in scrape_cache:
        scraped = scrape_cache[url]
    else:
        scraped = scrape_source(url)
        scrape_cache[url] = scraped
        save_scrape_cache(scrape_cache)
    return scraped[:_SCRAPE_CHARS] if scraped else row.get("article_context", "")


def _entity_names_by_article(rows: list[dict]) -> dict[str, list[str]]:
    """Article's own extracted entity names, keyed by article_id -- fed to the
    query judge the same way a human grader could see them (entity rows and
    query rows for one article sit on the same label_tool.py screen)."""
    by_article: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "entity" and r.get("entity_name"):
            by_article[r["article_id"]].append(r["entity_name"])
    return by_article


def _cached_call(cache_key: str, prompt: str, api_key: str, cache: dict, max_tokens: int = 300) -> dict | None:
    if cache_key in cache:
        return cache[cache_key]
    raw = _call_sarvam(prompt, api_key, _MODEL_QUALITY, reasoning_effort=None, max_tokens=max_tokens)
    result = _parse_json_response(raw)
    cache[cache_key] = result
    return result


def judge_row(row: dict, entity_names: list[str], api_key: str, cache: dict, scrape_cache: dict) -> dict | None:
    if row["kind"] == "entity":
        prompt = _ENTITY_JUDGE_PROMPT.format(
            article_title=row.get("article_title", ""),
            article_context=article_context_for_judge(row, scrape_cache),
            entity_name=row.get("entity_name", ""),
            entity_type=row.get("entity_type", ""),
            entity_ambiguous=row.get("entity_ambiguous", False),
            entity_resolved_sense=row.get("entity_resolved_sense") or "n/a",
        )
        return _cached_call(f"{row['id']}:{_ENTITY_JUDGE_VERSION}", prompt, api_key, cache)

    # Query judging is two calls: self-containment decides dangling_reference on its own
    # (query text only, no article context -- see the prompt file for why); only a
    # self-contained query goes on to the second call for the remaining four labels.
    sc_prompt = _QUERY_SELFCONTAINED_PROMPT.format(query_text=row.get("query_text", ""))
    sc_result = _cached_call(
        f"{row['id']}:selfcontained:{_QUERY_SELFCONTAINED_VERSION}", sc_prompt, api_key, cache, max_tokens=150
    )
    if sc_result is None or not isinstance(sc_result.get("self_contained"), bool):
        return None  # malformed self-containment response -- whole row is an error
    if sc_result["self_contained"] is False:
        return {"grade": "dangling_reference", "reason": sc_result.get("reason")}

    label_prompt = _QUERY_JUDGE_PROMPT.format(
        article_title=row.get("article_title", ""),
        article_context=article_context_for_judge(row, scrape_cache),
        entity_names=", ".join(entity_names) if entity_names else "(none extracted)",
        query_text=row.get("query_text", ""),
    )
    return _cached_call(f"{row['id']}:label:{_QUERY_JUDGE_VERSION}", label_prompt, api_key, cache)


def judge_grade(row: dict, result: dict | None) -> str:
    if result is None:
        return "error"
    grade = result.get("grade")
    valid = _ENTITY_GRADES if row["kind"] == "entity" else _QUERY_GRADES
    return grade if grade in valid else "error"


def compute_agreement(rows: list[dict], kind: str) -> dict:
    subset = [r for r in rows if r["kind"] == kind]
    total = len(subset)
    agree = sum(1 for r in subset if r["judge_grade"] == r["human_grade"])
    errors = sum(1 for r in subset if r["judge_grade"] == "error")

    valid_grades = _ENTITY_GRADES if kind == "entity" else _QUERY_GRADES
    per_class: dict = {}
    for g in sorted(valid_grades):
        class_rows = [r for r in subset if r["human_grade"] == g]
        if not class_rows:
            continue
        class_agree = sum(1 for r in class_rows if r["judge_grade"] == g)
        per_class[g] = {"total": len(class_rows), "agree": class_agree, "agreement_rate": class_agree / len(class_rows)}

    confusion = Counter((r["human_grade"], r["judge_grade"]) for r in subset)
    disagreements = [
        {"id": r["id"], "human_grade": r["human_grade"], "judge_grade": r["judge_grade"], "judge_reason": r["judge_reason"]}
        for r in subset if r["judge_grade"] != r["human_grade"]
    ]

    return {
        "total": total,
        "overall_agreement": agree / total if total else None,
        "error_count": errors,
        "per_class": per_class,
        "confusion": {f"{h}->{j}": c for (h, j), c in confusion.items()},
        "disagreements": disagreements,
    }


def print_summary(label: str, metrics: dict) -> None:
    def pct(x):
        return f"{x:.1%}" if x is not None else "n/a"

    print(f"\n{label} ({metrics['total']} rows)")
    print(f"  Overall judge-vs-human agreement: {pct(metrics['overall_agreement'])}")
    print(f"  Judge errors (unparseable): {metrics['error_count']}")
    print("  Per human-grade-class agreement:")
    for g, d in metrics["per_class"].items():
        print(f"    {g:<28} {d['agree']}/{d['total']} ({d['agreement_rate']:.1%})")


def run_validation(args) -> None:
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("SARVAM_API_KEY not set -- can't run live judge calls. Aborting.")
        return

    golden = load_jsonl(GOLDEN_PATH)
    if not golden:
        print(f"No golden set found at {GOLDEN_PATH}. Nothing to evaluate.")
        return
    golden = [r for r in golden if "grade" in r]
    if args.limit:
        golden = golden[: args.limit]

    entity_names_by_article = _entity_names_by_article(golden)
    cache = load_cache()
    scrape_cache = load_scrape_cache()

    rows = []
    for i, row in enumerate(golden, 1):
        print(f"[{i}/{len(golden)}] ({row['kind']}) {row.get('entity_name') or row.get('query_text', ''):.60}")
        entity_names = entity_names_by_article.get(row["article_id"], [])
        result = judge_row(row, entity_names, api_key, cache, scrape_cache)
        save_cache(cache)  # after every item -- a crash mid-run loses nothing
        rows.append({
            "id": row["id"],
            "kind": row["kind"],
            "human_grade": row["grade"],
            "judge_grade": judge_grade(row, result),
            "judge_reason": (result or {}).get("reason"),
        })

    entity_metrics = compute_agreement(rows, "entity")
    query_metrics = compute_agreement(rows, "query")

    report = {
        "mode": "validation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": _MODEL_QUALITY,
        "judge_prompt_versions": {
            "entity": _ENTITY_JUDGE_VERSION,
            "query_selfcontained": _QUERY_SELFCONTAINED_VERSION,
            "query_label": _QUERY_JUDGE_VERSION,
        },
        "golden_set_size": len(rows),
        "entity_metrics": entity_metrics,
        "query_metrics": query_metrics,
        "rows": rows,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nReport written to {out_path}")
    print_summary("ENTITY grades", entity_metrics)
    print_summary("QUERY grades", query_metrics)


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

    entity_names_by_article = _entity_names_by_article(unlabeled)
    cache = load_cache()
    scrape_cache = load_scrape_cache()

    rows = []
    for i, row in enumerate(unlabeled, 1):
        print(f"[{i}/{len(unlabeled)}] ({row['kind']}) {row.get('entity_name') or row.get('query_text', ''):.60}")
        entity_names = entity_names_by_article.get(row["article_id"], [])
        result = judge_row(row, entity_names, api_key, cache, scrape_cache)
        save_cache(cache)
        rows.append({
            "id": row["id"],
            "kind": row["kind"],
            "judge_grade": judge_grade(row, result),
            "judge_reason": (result or {}).get("reason"),
        })

    entity_dist = Counter(r["judge_grade"] for r in rows if r["kind"] == "entity")
    query_dist = Counter(r["judge_grade"] for r in rows if r["kind"] == "query")

    report = {
        "mode": "scoring",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge_model": _MODEL_QUALITY,
        "judge_prompt_versions": {
            "entity": _ENTITY_JUDGE_VERSION,
            "query_selfcontained": _QUERY_SELFCONTAINED_VERSION,
            "query_label": _QUERY_JUDGE_VERSION,
        },
        "scored_file": str(score_path),
        "total_rows": len(rows),
        "entity_grade_distribution": dict(entity_dist),
        "query_grade_distribution": dict(query_dist),
        "rows": rows,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-scoring.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nReport written to {out_path}")
    print(f"Entity grade distribution: {dict(entity_dist)}")
    print(f"Query grade distribution: {dict(query_dist)}")


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
