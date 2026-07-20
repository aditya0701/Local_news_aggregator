"""Builds the unlabeled candidate pool for the Stage 1 triage golden set.

Pulls from three sources, deduped by URL:

1. output/pipeline_trace.json -- both the current working-tree copy and every
   version reachable through `git log` for that path. This file is
   overwritten every daily pipeline run (see CLAUDE.md), so git history is
   the only way to see more than the latest run's items. Each item here
   carries a real stage1_decision_at_the_time, since Stage 1 actually judged
   it in production.

2. A fresh, free run of the existing RSS collector (collectors/rss_collector)
   over config/feeds.yaml -- no LLM calls, just raw candidates Stage 1 has
   never seen (stage1_decision_at_the_time is null for these).

3. A one-time pull from Hacker News' public jobs RSS (hnrss.org/jobs), used
   ONLY to widen this eval pool with real job-posting examples. Production
   trace data has almost none of these: GitHub-side job postings are caught
   deterministically before Stage 1 ever runs (see
   collectors/github_collector.py's JOB_LISTING_TERMS), and RSS feeds rarely
   surface one either -- the two real trace snapshots checked while building
   this script had zero job-posting skips between them. This feed is NOT
   added to config/feeds.yaml and the daily pipeline never touches it.

Emits evals/triage/candidates.jsonl (one JSON object per line) and then
stops -- this script never assigns a true_label. See INSTRUCTIONS below for
what happens next, printed at the end of a run.
"""

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import feedparser  # noqa: E402
import yaml  # noqa: E402

from collectors.rss_collector import fetch_feed  # noqa: E402

OUT_PATH = Path(__file__).parent / "candidates.jsonl"
TRACE_PATH = ROOT / "output" / "pipeline_trace.json"
FEEDS_PATH = ROOT / "config" / "feeds.yaml"

HN_JOBS_RSS = "https://hnrss.org/jobs"
HN_JOBS_LIMIT = 20

# Pool precedence when the same URL turns up more than once: a trace-sourced
# record carries a real stage1_decision_at_the_time, so it should win over a
# fresh re-collection of the same URL that has none.
_POOL_ORDER = {"trace": 0, "fresh_rss": 1, "hn_jobs": 2}


def _item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _git_trace_versions() -> list[str]:
    texts = []
    if TRACE_PATH.exists():
        texts.append(TRACE_PATH.read_text(encoding="utf-8"))
    # encoding="utf-8" explicitly -- subprocess.run's text=True decodes with
    # the platform default (cp1252 on Windows), which chokes on the Hindi
    # text these trace files contain.
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


def _guess_source(url: str) -> str:
    return "github" if "github.com" in url else "rss"


def from_trace() -> dict[str, dict]:
    """Real items Stage 1 has actually judged, keyed by item id. Most recent
    version of a given URL wins (git log is newest-first, plus the current
    working-tree copy is checked first)."""
    items: dict[str, dict] = {}
    for raw in _git_trace_versions():
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for rec in records:
            url = rec.get("url")
            if not url:
                continue
            item_id = _item_id(url)
            if item_id in items:
                continue
            stage1 = rec.get("stage1") or {}
            if "skip" in stage1:
                decision = "skip" if stage1["skip"] else "publish"
                reason = stage1.get("skip_reason")
            else:
                # e.g. outcome == "skipped_no_content" -- dropped by the
                # Stage 0 length gate before Stage 1 ever ran.
                decision, reason = None, None
            items[item_id] = {
                "id": item_id,
                "title": rec.get("title", ""),
                "url": url,
                "source": _guess_source(url),
                "content_snippet": (rec.get("stage0") or {}).get("source_preview", ""),
                "stage1_decision_at_the_time": {"decision": decision, "reason": reason},
                "pool": "trace",
            }
    return items


def from_fresh_rss() -> dict[str, dict]:
    """A fresh, free RSS collection run over the same feeds.yaml the daily
    pipeline uses -- no LLM calls, just raw candidates Stage 1 has never
    judged. Never writes back to config/feeds.yaml."""
    items: dict[str, dict] = {}
    feeds = yaml.safe_load(FEEDS_PATH.read_text())["feeds"]
    for feed in feeds:
        print(f"  fetching {feed['name']}...")
        for entry in fetch_feed(feed["url"]):
            url = entry.get("url")
            if not url:
                continue
            item_id = _item_id(url)
            items[item_id] = {
                "id": item_id,
                "title": entry.get("title", ""),
                "url": url,
                "source": "rss",
                "content_snippet": entry.get("summary", ""),
                "stage1_decision_at_the_time": {"decision": None, "reason": None},
                "pool": "fresh_rss",
            }
    return items


def from_hn_jobs() -> dict[str, dict]:
    """A one-time pull from Hacker News' public jobs RSS -- see module
    docstring for why this exists. Not part of config/feeds.yaml."""
    items: dict[str, dict] = {}
    parsed = feedparser.parse(HN_JOBS_RSS)
    for entry in parsed.entries[:HN_JOBS_LIMIT]:
        url = entry.get("link", "")
        if not url:
            continue
        item_id = _item_id(url)
        items[item_id] = {
            "id": item_id,
            "title": entry.get("title", ""),
            "url": url,
            "source": "hn_jobs",
            # hnrss.org's summary field is boilerplate ("Article URL: ...
            # Comments URL: ...") with no real content -- the title is what
            # actually carries the signal for these ("X (YC S26) Is Hiring
            # a Backend Engineer"), same as it would be for a human or
            # Stage 1 judging one.
            "content_snippet": entry.get("title", ""),
            "stage1_decision_at_the_time": {"decision": None, "reason": None},
            "pool": "hn_jobs",
        }
    return items


INSTRUCTIONS = """
================================================================================
HUMAN TASK
================================================================================
Open evals/triage/candidates.jsonl and pick ~60 items with a deliberate mix:
  ~30 real news                     (true_label: publish)
  ~10 job postings                  (true_label: skip) -- mostly in the
                                      "hn_jobs" pool, filter candidates.jsonl
                                      by "source": "hn_jobs" to find them
  ~10 listicles / link collections  (true_label: skip)
  ~10 borderline cases               (your judgment either way)

NOTE: listicle examples are not guaranteed to be numerous here -- they depend
on what the fresh RSS pull happened to catch this run, and there is no
dedicated listicle source. If you can't find ~10 naturally, either take fewer
or note the shortfall in your labeling notes. Do not ask me to fabricate or
go find more -- widening sources further is a decision for you to make, not
mine to default into.

For each item you choose, add two fields:
  "true_label": "publish" or "skip"
  "skip_reason": "job_posting" | "listicle" | "show_hn" | "off_topic" |
                 "too_thin" | null   (null only when true_label is "publish")

The "stage1_decision_at_the_time" field already on each item is what the
production model decided historically (or null if it never saw this item) --
it's reference context, not a suggestion. Label independently of it.

Save your labeled subset as evals/triage/golden_set.jsonl (one JSON object
per line, same shape as candidates.jsonl plus the two new fields).

Budget ~1 day. Once you commit golden_set.jsonl, treat the labels as frozen
-- only fix one later if it's a genuine mistake, and note the fix in the
commit message.
================================================================================
"""


def main() -> None:
    print("Pool 1/3: reading pipeline_trace.json (working tree + git history)...")
    pool_trace = from_trace()
    print(f"  {len(pool_trace)} items")

    print("Pool 2/3: fresh RSS collection over config/feeds.yaml...")
    pool_rss = from_fresh_rss()
    print(f"  {len(pool_rss)} items")

    print("Pool 3/3: Hacker News jobs RSS (eval-pool-only, not production config)...")
    pool_jobs = from_hn_jobs()
    print(f"  {len(pool_jobs)} items")

    merged: dict[str, dict] = {}
    for pool in (pool_trace, pool_rss, pool_jobs):
        for item_id, item in pool.items():
            if item_id not in merged:
                merged[item_id] = item

    records = sorted(merged.values(), key=lambda r: (_POOL_ORDER[r["pool"]], r["title"]))
    with OUT_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            row = {k: v for k, v in r.items() if k != "pool"}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    pool_counts = Counter(r["pool"] for r in records)
    print()
    print(f"Wrote {len(records)} candidates to {OUT_PATH}")
    print(f"  from trace history : {pool_counts.get('trace', 0)}")
    print(f"  from fresh RSS pull: {pool_counts.get('fresh_rss', 0)}")
    print(f"  from HN jobs feed  : {pool_counts.get('hn_jobs', 0)}")
    print(INSTRUCTIONS)


if __name__ == "__main__":
    main()
