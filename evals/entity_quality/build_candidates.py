"""Builds the unlabeled candidate pool for the entity-extraction /
GAP-query-quality golden set (Phase 3B -- new scope added mid-session, not
in the original handoff doc; see the plan file for why).

Pulls real historical Stage 1 output from output/pipeline_trace.json (the
current working-tree copy plus every version reachable through `git log`,
same approach as evals/triage/build_candidates.py), filtered to articles
Stage 1 actually decided to keep (skip: false) with entities and/or
search_queries recorded.

Flattens into one row per entity and one row per GAP query, each with
`kind: "entity"|"query"`, so they can be graded independently -- an article
with 6 entities and 2 queries becomes 8 separate candidate rows.

Emits evals/entity_quality/candidates.jsonl. Then stops -- this script never
assigns a grade itself. See INSTRUCTIONS below for what happens next.
"""

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_PATH = Path(__file__).parent / "candidates.jsonl"
TRACE_PATH = ROOT / "output" / "pipeline_trace.json"


def _article_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _git_trace_versions() -> list[str]:
    texts = []
    if TRACE_PATH.exists():
        texts.append(TRACE_PATH.read_text(encoding="utf-8"))
    try:
        commits = subprocess.run(
            ["git", "log", "--format=%H", "--", "output/pipeline_trace.json"],
            cwd=ROOT, capture_output=True, encoding="utf-8", check=True,
        ).stdout.split()
    except subprocess.CalledProcessError:
        commits = []
    for commit in commits:
        result = subprocess.run(
            ["git", "show", f"{commit}:output/pipeline_trace.json"],
            cwd=ROOT, capture_output=True, encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout:
            texts.append(result.stdout)
    return texts


def gather_articles() -> list[dict]:
    """Real articles Stage 1 actually kept (skip: false) with entities/queries
    recorded historically -- shared by build_candidates.py (grades the
    historical entities) and build_query_candidates.py (regenerates fresh
    queries from the current live prompt using the same title/context)."""
    seen_articles: set[str] = set()
    articles: list[dict] = []
    for raw in _git_trace_versions():
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for rec in records:
            url = rec.get("url")
            if not url:
                continue
            article_id = _article_id(url)
            if article_id in seen_articles:
                continue  # first (most recent) version of this article wins
            stage1 = rec.get("stage1") or {}
            if stage1.get("skip") is not False:
                continue  # only articles Stage 1 actually kept have real entities/queries
            entities = stage1.get("entities") or []
            queries = stage1.get("search_queries") or []
            if not entities and not queries:
                continue
            seen_articles.add(article_id)
            articles.append({
                "article_id": article_id,
                "title": rec.get("title", ""),
                "url": url,
                "context": (rec.get("stage0") or {}).get("source_preview", ""),
                "entities": entities,
                "queries": queries,
            })
    return articles


def build_rows() -> list[dict]:
    """Entity rows only -- GAP-query rows are built separately by
    build_query_candidates.py, which regenerates them fresh from the current
    live prompt rather than reusing historical (now-stale) ones. See that
    script's docstring for why."""
    rows: list[dict] = []
    for article in gather_articles():
        article_id = article["article_id"]
        for i, entity in enumerate(article["entities"]):
            rows.append({
                "id": f"{article_id}-entity-{i}",
                "kind": "entity",
                "article_id": article_id,
                "article_title": article["title"],
                "article_url": article["url"],
                "article_context": article["context"],
                "entity_name": entity.get("name", ""),
                "entity_type": entity.get("type", ""),
                "entity_ambiguous": entity.get("ambiguous", False),
                "entity_resolved_sense": entity.get("resolved_sense"),
            })
    return rows


INSTRUCTIONS = """
================================================================================
HUMAN TASK
================================================================================
Open evals/entity_quality/candidates.jsonl and grade a sample using
evals/entity_quality/label_tool.py -- you don't have to do all of them, pick
a representative mix across different articles.

This file has ENTITY rows only. GAP-query rows are built separately by
build_query_candidates.py (regenerated fresh from the current live prompt,
not reused from history -- see that script's docstring for why) and graded
in a second pass once you're done here.

For each ENTITY row, add "grade":
  "correct"                    -- a real named entity from the article, right type
  "wrong_type"                 -- real entity, but the type label is wrong
  "hallucinated_not_in_article"-- doesn't actually appear in the article
  "not_an_entity"               -- appears in the article but isn't a real named
                                   entity at all (a generic noun/phrase wrongly
                                   extracted as if it were one)
  "ambiguity_mislabeled"       -- flagged ambiguous when it isn't (or vice versa)

Save your graded subset as evals/entity_quality/golden_set.jsonl (one JSON
object per line, same shape as candidates.jsonl plus the "grade" field).

Once committed, treat these grades as frozen -- same rule as the triage and
faithfulness golden sets.
================================================================================
"""


def main() -> None:
    rows = build_rows()
    with OUT_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    kinds = Counter(r["kind"] for r in rows)
    n_articles = len({r["article_id"] for r in rows})
    print(f"Wrote {len(rows)} candidate rows from {n_articles} articles to {OUT_PATH}")
    print(f"  entities: {kinds.get('entity', 0)}")
    print(f"  queries : {kinds.get('query', 0)}")
    print(INSTRUCTIONS)


if __name__ == "__main__":
    main()
