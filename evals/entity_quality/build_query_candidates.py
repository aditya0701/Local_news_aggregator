"""Regenerates GAP search-query candidates using the CURRENT live Stage 1
prompt, replacing the historical ones that were removed from candidates.jsonl.

Why this exists instead of just reusing trace history (like entities do):
the historical queries in output/pipeline_trace.json were captured the same
day CLAUDE.md documents a prompt revert -- Stage 1's GAP-query wording went
from a "relaxed" version (allowed interpretive/comparative questions, written
while the now-disabled /api/concise deep-research agent was active) back to
the original strict "must be a fact a search can realistically return"
wording, once DDG-only search became final. The committed trace and the
committed prompt are from the same day, but the trace almost certainly
captured queries mid-testing, before the same-day revert -- confirmed by
inspecting real examples: nearly all of them were long, multi-clause
interpretive questions, not the short factual ones the current prompt asks
for. Grading those against today's prompt would be misleading.

This script re-runs the real, current triage_item() (writer/synthesize.py)
against the same 54 real articles used for entity grading -- only the prompt
differs, so it's a fair comparison on the same underlying material. Makes
live SARVAM_API_KEY calls (cheap sarvam-30b, up to 3 calls per article).

Appends "kind": "query" rows to evals/entity_quality/candidates.jsonl
(existing entity rows are left untouched). Then stops -- grade the new
queries with label_tool.py same as before.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from writer.synthesize import triage_item  # noqa: E402
from build_candidates import OUT_PATH as CANDIDATES_PATH  # noqa: E402
from build_candidates import gather_articles  # noqa: E402

INSTRUCTIONS = """
================================================================================
HUMAN TASK
================================================================================
Fresh QUERY rows have been appended to evals/entity_quality/candidates.jsonl.
Open evals/entity_quality/label_tool.py and grade them -- entities you
already graded are untouched and won't ask again.

For each QUERY row, add "grade":
  "good"                       -- a legitimate fact-seeking question grounded in the article
  "hallucinated_competitor"    -- names a competitor/product not in the article
  "dangling_reference"         -- uses "this"/"it"/etc. with no named entity to resolve it
  "not_fact_seeking"           -- asks for an opinion/judgment a search can't answer
  "irrelevant"                 -- doesn't relate to any real gap in this article

Same rule as always: once committed, treat these grades as frozen.
================================================================================
"""


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only regenerate for the first N articles")
    args = parser.parse_args()

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        print("SARVAM_API_KEY not set -- can't regenerate queries live. Aborting.")
        return

    articles = gather_articles()
    if args.limit:
        articles = articles[: args.limit]

    existing_rows = load_jsonl(CANDIDATES_PATH)
    new_query_rows: list[dict] = []

    for i, article in enumerate(articles, 1):
        print(f"[{i}/{len(articles)}] {article['title'][:60]}")
        result = triage_item(
            title=article["title"],
            summary="",
            source_text=article["context"],
            api_key=api_key,
        )
        if result is None:
            print("  (call failed, skipping)")
            continue
        if result.get("skip"):
            print(f"  (Stage 1 now skips this one: {result.get('skip_reason')} -- no queries to grade)")
            continue
        queries = result.get("search_queries") or []
        for qi, query in enumerate(queries):
            new_query_rows.append({
                "id": f"{article['article_id']}-query-v2-{qi}",
                "kind": "query",
                "article_id": article["article_id"],
                "article_title": article["title"],
                "article_url": article["url"],
                "article_context": article["context"],
                "query_text": query,
            })

    all_rows = existing_rows + new_query_rows
    with CANDIDATES_PATH.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print()
    print(f"Appended {len(new_query_rows)} fresh query rows to {CANDIDATES_PATH}")
    print(INSTRUCTIONS)


if __name__ == "__main__":
    main()
