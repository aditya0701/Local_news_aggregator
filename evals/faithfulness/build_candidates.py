"""Builds the unlabeled candidate pool for the faithfulness golden set
(Task D / Phase 4).

Selects real published Sarvam articles from output/articles_hindi.json,
pairs each with its real English source text (live-scraped, not the trace's
truncated 400-char preview -- see the note below, same lesson Phase 3B
already learned the hard way), and splits the Hindi article into
claim-level units (one sentence per row, v1 as specced).

Which articles count as "published Sarvam articles" (not legacy translate-
fallback stubs) is only knowable from output/pipeline_trace.json's own
per-article `outcome` field -- articles_hindi.json alone doesn't record
that. Same technique as evals/triage/build_candidates.py and
evals/entity_quality/build_candidates.py: walk the current working-tree
trace plus every version reachable through `git log`, since pipeline.py
overwrites (not appends) this file every run, so old runs' articles only
survive in git history. Matched back to articles_hindi.json by URL.

Why source_text is live-scraped instead of reusing the trace's
`stage0.source_preview`: that preview is source_text[:400] -- Phase 3B
found this exact truncation caused false hallucination verdicts on real
facts that just happened to sit past character 400 (see finalize_project.md
Phase 3B writeup). A faithfulness check is even more exposed to this than
entity extraction was, since a claim can reference any fact anywhere in the
source, not just an early one. Scraped text is cached by URL
(.scraped_source_cache.json) so re-running this script doesn't re-fetch
already-scraped articles; falls back to the trace's stored preview if a
live scrape fails (a dead link, a paywall, a changed URL) rather than
dropping the article entirely.

Each claim and article title also gets a machine-translated English version
(`claim_text_en`, `article_title_en`, via writer.synthesize._translate_to_english
-- the same GoogleTranslator wrapper already used for language normalization
elsewhere in this pipeline) so the human labeler can grade without needing
to read Hindi. This is a display aid only -- `claim_text_hi` remains the
field that's actually graded and fed to the judge, since that's what was
really published; a translation error here would only affect what the
labeler sees, not what gets checked against source_text. Cached by exact
text (.translation_cache.json), since article titles repeat across every
claim row for that article.

Emits evals/faithfulness/candidates.jsonl. Then stops -- this script never
assigns a grade itself. See INSTRUCTIONS below for what happens next.
"""

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    # Windows consoles default to cp1252, which can't print the Hindi text in
    # INSTRUCTIONS below -- production runs on ubuntu-latest (UTF-8) and never
    # hits this, but local runs need it too. Same fix already documented in
    # CLAUDE.md's Stage 2 investigation for this exact class of crash.
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from writer.synthesize import _is_sentence_end, _translate_to_english  # noqa: E402
from writer.web_context import scrape_source  # noqa: E402

OUT_PATH = Path(__file__).parent / "candidates.jsonl"
TRACE_PATH = ROOT / "output" / "pipeline_trace.json"
ARTICLES_PATH = ROOT / "output" / "articles_hindi.json"
SCRAPE_CACHE_PATH = Path(__file__).parent / ".scraped_source_cache.json"
TRANSLATION_CACHE_PATH = Path(__file__).parent / ".translation_cache.json"

# The plan's own budget: ~40 articles -> ~400-500 claim labels, 1-2 days.
# Trace history currently yields 56 published-and-matched articles; capping
# at 40 keeps the labeling workload inside that stated budget instead of
# growing it by ~40% just because more happened to be available.
TARGET_ARTICLE_COUNT = 40

# The four body fields the frontend actually renders as the article, plus
# concept_box (a newcomer explainer that can itself state facts). Title is
# excluded -- a headline is a label, not a claim to fact-check on its own.
CLAIM_FIELDS = [
    "concept_box",
    "introduction_lede",
    "deep_dive_and_context",
    "strategic_analysis",
    "conclusion_and_significance",
]


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


def gather_published_traces() -> dict[str, dict]:
    """URL -> trace record, for every article Stage 3 actually completed
    (outcome == "published"), across the current trace and every version
    reachable through git log. First (most recent) version of a URL wins."""
    seen: dict[str, dict] = {}
    for raw in _git_trace_versions():
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for rec in records:
            url = rec.get("url")
            if not url or url in seen or rec.get("outcome") != "published":
                continue
            seen[url] = rec
    return seen


def select_articles(traces: dict[str, dict], articles_by_url: dict[str, dict]) -> list[dict]:
    """Match trace-published URLs to their real published article, keep only
    genuine Sarvam articles (has introduction_lede -- excludes legacy
    translate-fallback stubs), then take a category-balanced sample up to
    TARGET_ARTICLE_COUNT via round-robin so no single category (general
    dominates the raw pool at 27/56) crowds out the rarer ones (acquisition
    has only 1)."""
    matched = []
    for url, trace in traces.items():
        article = articles_by_url.get(url)
        if not article or not article.get("introduction_lede"):
            continue
        matched.append({"trace": trace, "article": article})

    by_category: dict[str, list[dict]] = defaultdict(list)
    for m in matched:
        by_category[m["article"].get("category") or "general"].append(m)

    selected: list[dict] = []
    category_cycle = list(by_category.values())
    i = 0
    while len(selected) < TARGET_ARTICLE_COUNT and any(category_cycle):
        bucket = category_cycle[i % len(category_cycle)]
        if bucket:
            selected.append(bucket.pop(0))
        i += 1
        if i > TARGET_ARTICLE_COUNT * 10:  # all buckets exhausted, safety valve
            break
    return selected


def split_into_sentences(text: str) -> list[str]:
    """One claim unit per sentence, splitting on Hindi/Latin sentence-ending
    punctuation. Reuses writer.synthesize._is_sentence_end so a "." between
    two digits (GLM-5.2, 32.8%) is never mistaken for a sentence boundary --
    the same decimal-point-aware rule already proven out in production
    rather than a fresh reimplementation that could drift from it."""
    if not text:
        return []
    sentences = []
    start = 0
    for i, ch in enumerate(text):
        if ch in "।!?" or (ch == "." and _is_sentence_end(text, i)):
            piece = text[start : i + 1].strip()
            if piece:
                sentences.append(piece)
            start = i + 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def load_scrape_cache() -> dict:
    if SCRAPE_CACHE_PATH.exists():
        return json.loads(SCRAPE_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_scrape_cache(cache: dict) -> None:
    SCRAPE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def source_text_for(url: str, trace: dict, cache: dict) -> str:
    if url in cache:
        return cache[url]
    text = scrape_source(url)
    if not text:
        # Live scrape failed (dead link, paywall, changed URL) -- fall back
        # to the trace's own stored preview rather than dropping the
        # article. Shorter than a full scrape, but still real captured text.
        text = (trace.get("stage0") or {}).get("source_preview", "")
    cache[url] = text
    return text


def load_translation_cache() -> dict:
    if TRANSLATION_CACHE_PATH.exists():
        return json.loads(TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_translation_cache(cache: dict) -> None:
    TRANSLATION_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def translate_cached(text: str, cache: dict) -> str:
    """English translation of Hindi text, for label_tool.py display only --
    the actual field that gets graded and fed to the judge is still
    claim_text_hi, this is purely a readability aid so a labeler who isn't
    fluent in Hindi can still grade claims against the (already-English)
    source_text. Cached by exact text since article titles repeat across
    every claim row for that article (~27x), and re-running this script
    shouldn't re-translate anything it already has."""
    if not text:
        return text
    if text in cache:
        return cache[text]
    translated = _translate_to_english(text)
    cache[text] = translated
    return translated


def translate_all_cached(texts: list[str], cache: dict) -> None:
    """Batch-translate every not-yet-cached string in one pass. One
    sequential _translate_to_english call per string (as translate_cached
    does) means ~1100 individual network round-trips -- slow enough that a
    first live run was still only 13% done (147 strings) several minutes
    in. deep_translator's GoogleTranslator.translate_batch() sends many
    strings per request instead of one, which is dramatically faster.
    Batched in chunks of 50 (a single oversized batch risks a request-size
    limit or one bad string failing the whole batch); saves the cache after
    every chunk so an interrupted run keeps whatever it already translated,
    same crash-safety property translate_cached already had."""
    from deep_translator import GoogleTranslator

    todo = [t for t in dict.fromkeys(texts) if t and t not in cache]
    if not todo:
        return
    translator = GoogleTranslator(source="auto", target="en")
    chunk_size = 50
    for start in range(0, len(todo), chunk_size):
        chunk = todo[start : start + chunk_size]
        try:
            results = translator.translate_batch(chunk)
        except Exception as e:
            print(f"  batch translate failed ({e}), falling back to one-by-one for this chunk")
            results = [_translate_to_english(t) for t in chunk]
        for original, translated in zip(chunk, results):
            cache[original] = translated or original
        save_translation_cache(cache)
        print(f"  translated {min(start + chunk_size, len(todo))}/{len(todo)}")


def build_rows(selected: list[dict], scrape_cache: dict, translation_cache: dict) -> list[dict]:
    # Pass 1: split every article's claims first (no translation yet), so
    # every string that needs translating is known upfront and can go
    # through translate_all_cached's batched requests in one shot instead
    # of one network round-trip per claim.
    parsed: list[dict] = []
    all_texts: list[str] = []
    for m in selected:
        article, trace = m["article"], m["trace"]
        url = article.get("url", "")
        title = article.get("title", "")
        all_texts.append(title)
        claim_index = 0
        claims = []
        for field in CLAIM_FIELDS:
            for sentence in split_into_sentences(article.get(field, "")):
                claims.append((claim_index, field, sentence))
                all_texts.append(sentence)
                claim_index += 1
        parsed.append({"article": article, "trace": trace, "url": url, "title": title, "claims": claims})

    print(f"Translating {len(set(all_texts))} unique strings (batched)...")
    translate_all_cached(all_texts, translation_cache)

    # Pass 2: assemble rows -- every translation is now a cache hit.
    rows: list[dict] = []
    for p in parsed:
        article_id = _article_id(p["url"])
        source_text = source_text_for(p["url"], p["trace"], scrape_cache)
        article_title_en = translate_cached(p["title"], translation_cache)
        for claim_index, field, sentence in p["claims"]:
            rows.append({
                "id": f"{article_id}-claim-{claim_index}",
                "article_id": article_id,
                "claim_index": claim_index,
                "field": field,
                "claim_text_hi": sentence,
                "claim_text_en": translate_cached(sentence, translation_cache),
                "source_text": source_text,
                "article_title": p["title"],
                "article_title_en": article_title_en,
                "article_url": p["url"],
                "category": p["article"].get("category"),
            })
    return rows


INSTRUCTIONS = """
================================================================================
HUMAN TASK
================================================================================
Open evals/faithfulness/candidates.jsonl and grade with
evals/faithfulness/label_tool.py -- one article's claims per screen, writes
on every click, resumable. Claims are shown in ENGLISH (machine-translated
from the published Hindi -- claim_text_en) so you don't need to read Hindi
to grade them; the original claim_text_hi is what actually gets graded and
fed to the judge, shown smaller underneath each English line in case the
translation looks off and you want to sanity-check against it.

Grading all 1107 claims isn't required -- 3-4 full articles (~80-110
claims) is enough to validate the judge (see finalize_project.md). For each
row, add "grade":
  "supported"       -- this fact appears in source_text (the real English
                        source material), even if phrased differently
  "unsupported"      -- this fact appears nowhere in source_text -- Stage 3
                        stated something the source never said
  "hedged_opinion"   -- framing/interpretation language, not a factual claim
                        (e.g. "this may be significant", editorial commentary,
                        a rhetorical question) -- nothing to fact-check here

Save your graded file as evals/faithfulness/golden_set.jsonl (one JSON
object per line, same shape as candidates.jsonl plus the "grade" field).
Once committed, treat these grades as frozen -- same rule as the other two
golden sets.
================================================================================
"""


def main() -> None:
    traces = gather_published_traces()
    articles = json.loads(ARTICLES_PATH.read_text(encoding="utf-8"))
    articles_by_url = {a.get("url"): a for a in articles}

    selected = select_articles(traces, articles_by_url)
    scrape_cache = load_scrape_cache()
    translation_cache = load_translation_cache()

    print(f"Selecting {len(selected)} articles (of {len(traces)} trace-published, category-balanced)...")
    rows = build_rows(selected, scrape_cache, translation_cache)
    save_scrape_cache(scrape_cache)
    save_translation_cache(translation_cache)

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    cats = Counter(m["article"].get("category") for m in selected)
    field_counts = Counter(r["field"] for r in rows)
    print(f"Wrote {len(rows)} candidate claim rows from {len(selected)} articles to {OUT_PATH}")
    print(f"  category mix: {dict(cats)}")
    print(f"  by field: {dict(field_counts)}")
    print(INSTRUCTIONS)


if __name__ == "__main__":
    main()
