# TechDrishti — Engineering Case Study

**A fully automated Hindi-language tech journalism pipeline.** Every day at 8 AM IST, GitHub Actions
runs a collector → clustering → 3-stage LLM writing pipeline that turns English tech news into
original Hindi articles — not translations — published to a static, serverless frontend.

- **Live site**: https://aditya0701.github.io/Local_news_aggregator/
- **Hindi README** (product-facing identity): [README.md](README.md)
- **Repo**: [aditya0701/Local_news_aggregator](https://github.com/aditya0701/Local_news_aggregator)

This document is the English engineering write-up: architecture, the specific bugs found and
fixed along the way, and — the part most project READMEs skip — actual measured quality numbers
from three hand-labeled evaluation sets, not anecdotes.

---

## What it is

Given an English tech article, TechDrishti extracts the underlying facts, researches entities it
doesn't already know about, and writes an **original Hindi article** from those facts — complete
with its own headline, a "concept box" explaining the hardest idea for a newcomer, and
category-aware editorial framing (an acquisition rumor is hedged; a benchmark release gets its
numbers translated into plain-language meaning). A generic Google-Translate fallback exists too,
but only as a degradation path when the real pipeline fails outright — the two are directly
comparable in this repo, and the difference is the entire point of this project.

Runs entirely on GitHub Actions (no server, no hosting cost), ~9–20 minutes and about $0.01 per
daily run.

---

## Architecture

```
RSS feeds (8 sources) + GitHub trending
        │
        ▼
  collectors/            ── deterministic pre-filters: job-listing keywords, listicle-pattern
        │                   regex, star-count floor. Zero model calls.
        ▼
  writer/cluster.py       ── groups same-story articles across outlets (sentence embeddings,
        │                   all-mpnet-base-v2, cosine similarity) so one story = one article
        ▼
  Stage 0 (deterministic) ── content-length gate; thin scrapes are dropped before any LLM call
        ▼
  Stage 1 (sarvam-30b)    ── cheap-model triage: publishable tech news? entity extraction?
        │                   3 calls (skip-gate → analysis → JSON extraction), not 1 — see below
        ▼
  Entity cache + search    ── 45-day TTL knowledge cache with sense disambiguation; cache misses
        │                   go to free search tiers (Google News RSS, DDG) — no paid APIs
        ▼
  Stage 2 (sarvam-105b)   ── editorial strategy: narrative, key facts, paragraph-by-paragraph
        │                   writing plan. Plans; does not write.
        ▼
  Stage 3 (sarvam-105b)   ── executes the plan into Hindi prose. Writes; does not re-plan.
        │
        ├──▶ success   → original, context-aware Hindi article
        └──▶ skip/fail → translator/translate.py (Google Translate fallback, only on failure)
        ▼
  output/articles_hindi.json  → committed by the Actions bot
        ▼
  frontend/ (vanilla HTML/CSS/JS) → GitHub Pages
```

Only Stage 1, Stage 2, and Stage 3 call an LLM. Everything upstream (collection, filtering,
clustering, the content-length gate) is deterministic and free — the design goal throughout was
to spend model tokens only once a story has already cleared every cheap filter available.

---

## Engineering decisions

The full bilingual (Hindi) engineering log with every dead end lives in [CLAUDE.md](CLAUDE.md) —
this section is the tightened English version of the highlights that mattered most.

### The token-budget discovery that shaped everything downstream

Sarvam's starter plan caps completions at 4096 tokens — but the real constraint turned out to be
**reasoning eating that budget before the model ever wrote the answer**, not the cap itself. A
single combined Stage 1 call intermittently returned `content: None` because reasoning consumed
the entire token budget with nothing left to write JSON. Fixed by splitting every stage into a
**plan-then-execute pair**: one call reasons and writes its thinking as plain text (safe, since
the visible output *is* the reasoning, nothing hidden competes with it); a second call
transcribes that plain text into strict JSON with `reasoning_effort=None` — explicitly told not
to re-think, only transcribe.

```python
# Step 1 — reasoning left on, plain-text output (not JSON) so the model's own
# thinking IS the visible output, not competing with a hidden budget
analysis = _call_sarvam(analysis_prompt, api_key, MODEL_FAST)

# Step 2 — reasoning off entirely, pure transcription into strict JSON
raw = _call_sarvam(
    extraction_prompt, api_key, MODEL_FAST,
    system="/no_think Output JSON only.",
    reasoning_effort=None,
)
```

This pattern was applied three separate times as the same failure mode kept resurfacing in
different stages (Stage 1's entity extraction, Stage 2's editorial planning, and the search-result
synthesis step) — each time confirmed via real captured `usage` data showing `completion_tokens`
hitting the cap exactly with a truncated, unparseable response, not assumed from theory.

One detail worth flagging on its own: putting `/no_think` in a system prompt does **nothing** by
itself — it's plain text the model may or may not follow. Confirmed by A/B testing the identical
prompt with and without the `reasoning_effort=None` parameter: without it, the same prompt still
burned 3000+ tokens on reasoning. The parameter is the only real control.

### Two-layer triage before the expensive model ever runs

`sarvam-30b` (fast, cheap) decides whether an item is genuine tech news and extracts its entities,
before `sarvam-105b` (the writer) ever sees it. GitHub-sourced repos get an *additional*
pre-filter layer: deterministic keyword/regex checks (job listings, listicle patterns, a 20-star
floor for week-old repos) run before any LLM call at all, and only survivors reach a batched
editorial judge call.

### Persistent entity memory with sense disambiguation

A plain translator has no memory between runs and no way to know "Claude" the AI model from
"Claude" a person sharing the name. TechDrishti's entity cache stores knowledge with a 45-day TTL,
keyed by normalized name, with ambiguous entities stored as separate `senses`:

```python
def get_entity(cache, name, resolved_sense=None):
    record = cache.get(_normalize(name))
    if "senses" in record:                      # ambiguous — needs the right sense
        for sense in record["senses"]:
            if sense["sense_label"] == resolved_sense and _is_fresh(sense):
                return sense
        return None                              # wrong/unknown sense → real miss, not a wrong answer
    return record if _is_fresh(record) else None
```

A cache hit means zero new search calls for an entity the pipeline has already researched — this
property is directly unit-tested (`evals/cache/test_cache_search_skip.py`): with the search layer
mocked, a fresh cache hit is asserted to never dispatch a search at all.

### Search: free tiers only, and two "worked in theory, broke live" bugs found by actually testing

Search costs nothing — no Tavily, no Exa (both were tried, both silently degraded to free-tier
fallback anyway once their keys expired/were never configured, so they were removed rather than
kept as dead weight). Two real, live-tested failures shaped the current design:

- **Wikipedia was tried as an identity-search tier, then removed entirely.** Its own search has
  no relevance floor — a real query for "Bolt Graphics" (an actual GPU startup) matched "Rock n'
  Bolt," an unrelated 1984 video game. Beyond just dropping the dedicated tier, DDG's own organic
  results can still surface a Wikipedia page unprompted, so the DDG scraper explicitly filters out
  `wikipedia.org`/`wikimedia.org`/etc. domains too.
- **The DDG dependency itself was broken**, not by design but by a deprecated package
  (`duckduckgo_search`). Live-tested against 6 real queries: 5 came back empty, and the 6th matched
  "Zeus GPU" (a real product) to Greek mythology. Switching to the maintained `ddgs` package fixed
  all 6/6 — a dependency bug, not an architecture problem.

### Post-processing that exists because production actually produced these bugs

`_is_meta_line()` (strips model self-commentary like "Let me...") and `_trim_to_last_sentence()`
(trims text cut off mid-sentence by the token cap, decimal-point-aware so `"GLM-5.2"` never
becomes `"GLM-5."`) were both written in direct response to failure patterns found in
`pipeline_trace.json`, a full per-stage trace kept for every article — not added speculatively.

---

## Evaluation & measurement

This is the part most "I built an AI pipeline" write-ups skip. Three golden sets were built and
hand-labeled (**by a human, not the model being evaluated** — see [Methodology](#methodology)
below), covering the three places this pipeline can silently go wrong: deciding what's worth
writing about, extracting the right entities/search queries, and not fabricating facts in the
final Hindi text.

### Methodology

For the triage golden set, the *same* deterministic-input Stage 1 function can be re-run directly
against hand-labeled examples, so accuracy/precision/recall are measured straight. For entity and
query extraction, Stage 1's output is genuinely run-to-run inconsistent on borderline cases (a
documented, accepted property of this pipeline, not something eval tooling can paper over) — so
instead of grading one frozen output, an LLM **judge** was built and *validated against human
labels*, and only once that agreement number is known does the judge get trusted to grade
anything else. The same validate-then-scale pattern was used for faithfulness-checking, sized down
appropriately: faithfulness-checking is a closed-book task (claim vs. one provided source text, no
outside judgment required), so a much smaller human validation sample was sufficient before
trusting the judge at full scale — the same logic a smaller reading-comprehension check needs less
human oversight than a subjective editorial call.

### 1. Triage quality (skip/publish decisions)

149 hand-labeled items (news, job postings, listicles, Show HN posts, borderline cases). The
skip-decision prompt originally had **no topic filter at all** — anything mentioning a tech
company passed as "tech news." Fixed with an explicit off-topic category (retail promos, non-tech
business news, entertainment news, unrelated pure-science research, multi-topic digests) built
from the 21 real misclassified examples in the baseline report, not invented cases.

| Metric | Before | After |
|---|---|---|
| Overall accuracy | 73.8% | **81.9%** |
| Skip precision | 75.6% | **86.7%** |
| Skip recall | 51.7% | **65.0%** |
| `off_topic` recall | 12.5% (3/24) | **50.0% (12/24)** |

One honestly-reported regression: `too_thin` recall dropped 1/3→0/3 (n=3, not chased further —
reporting a bad number as-is beats quietly re-tuning until it looks good).

Full reports: `evals/triage/results/20260717T195235Z.json` (before) →
`evals/triage/results/20260718T162006Z.json` (after).

### 2. Entity & search-query extraction quality (judge-validated)

293 hand-labeled entities, 56 hand-labeled search queries, judged by a `sarvam-105b` prompt
validated against those human grades.

| | Judge-vs-human agreement |
|---|---|
| Entity extraction | **74.7%** (260 `correct`: 76.5%, 33 `wrong_type`: 60.6%) |
| Search-query quality | **66.1%** (44 `good`: 75.0%, 6 `dangling_reference`: 50.0%, 6 `not_fact_seeking`: 16.7%) |

A significant harness bug was found and fixed along the way: the eval was originally checking
entities against only a 400-character source preview, while Stage 1 itself reads 2000 characters
— causing false "hallucinated" verdicts on entities that were real but just appeared past
character 400. Live-rescraping the full source text raised entity agreement from 44.4% to 74.7%
on its own.

**Root-cause investigation, not just a number**: of the entity-type mismatches, 63% turned out to
be one specific bug — the model reusing the article's own topic classification (e.g. `general`,
`model_release`) as an individual entity's type, confused between two different label sets in the
same prompt. Fixed with an explicit rule plus contrastive examples; a live re-check on 97 freshly
extracted entities (not the frozen validation set) showed the schema-violation rate drop from
**22.2% to 3.1%**.

Full reports: `evals/entity_quality/results/20260718T151414Z.json`,
`experiments/stage1_analysis_v2_live_check_20260718T162401Z.json`.

### 3. Faithfulness / hallucination rate

1107 claim-level sentences from 40 real published articles, checked against each article's actual
scraped source text — split one sentence per claim, judged `supported` / `unsupported` /
`hedged_opinion` by a two-layer checker (a free deterministic layer for claims stating a specific
number, plus an LLM judge for everything else), validated against 49 human-labeled claims first
(69.4% judge-vs-human agreement).

**Hallucination rate: 26.3%** (287 unsupported / 1091 factual claims — `hedged_opinion` claims
excluded from the denominator).

| Category | Unsupported rate |
|---|---|
| `repo_analysis` | 35.1% (107/305) — worst category |
| `general` | 25.5% (80/314) |
| `model_release` | 24.2% (57/236) |
| `ban_regulation` | 19.0% (40/210) |
| `acquisition` | 11.5% (3/26 — only 1 article in this category, small sample) |

**Three real unsupported-claim examples** (translated from the published Hindi):

1. *"Grok 4.5 is mid-pack in coding evals, scoring lower than GPT-4o and Claude 3 Opus on
   MMLU-Pro."* — the source article states real benchmark metrics, but never this specific
   comparative ranking. A fabricated competitive claim, not just a vague embellishment.
2. *"[9Router v2's decoupled rewrite is] a significant milestone for AI developers."* — the
   source README describes the architectural rewrite; it never frames it this way. Editorial
   overclaiming, not a fabricated fact per se.
3. *"Failing to fix [the Orchids vulnerability] would erode user trust and hinder adoption of
   Figma's new platform."* — the source reports the security issue itself; this consequence is
   the model's own unhedged speculation stated as fact.

Full report: `evals/faithfulness/results/20260719T162622Z-scoring.json`.

---

## Limitations, stated plainly

- **All three golden sets are single-annotator** — one person's judgment, not inter-rater
  agreement. Real disagreement was found even within the *same* human labeler on structurally
  similar cases (documented in the entity/query eval writeup) — some of the "judge disagreement"
  is genuine human labeling inconsistency, not a fixable judge bug.
- **The faithfulness judge was validated on 49 claims, not the full 1107** — a deliberate,
  disclosed tradeoff (see Methodology above), not an oversight. The validation sample also
  happened to contain zero `hedged_opinion` or `unsupported` human labels, so per-class accuracy
  on those two labels specifically is unverified, even though the overall number is real.
- **The 26.3% hallucination rate is very likely an overestimate**, concentrated in
  `repo_analysis`. Stage 3 uses search-derived `entity_context` in addition to the scraped source
  article, but this eval only checked claims against the scraped article — confirmed directly on
  one case where a claim's specific technical numbers were absent from the source page but the
  pipeline had legitimately searched for and found that information elsewhere. The full search
  material isn't recoverable for these historical articles (only a 600-character preview survives
  in the trace log) — a fix for *future* evaluation runs would be logging that material in full,
  not truncated, which wasn't implemented this round; noted here as a lesson for next time rather
  than left undiscovered.
- **Fluency and writing quality are not measured at all** — every number here is about factual/
  editorial correctness, not whether the Hindi prose reads well. That remains an entirely
  subjective, unmeasured dimension.
- **The judge validation is on one distribution of articles from a short time window** — no claim
  is made that these agreement numbers generalize to every possible future article shape.
- **Stage 1's judgment itself (skip/publish, entity typing) is documented as run-to-run
  inconsistent** on borderline cases — re-running the identical input can produce a different
  verdict. This is a known, accepted property, not something these evals attempt to eliminate.

---

## Running the evals yourself

```bash
# Full suite: pytest + triage eval + entity_quality eval (+ faithfulness once its
# golden set exists). Never wired into the daily cron — on-demand only.
./run_all_evals.sh

# Cheap smoke run
./run_all_evals.sh --limit 5

# Compare two report files (e.g. before/after a prompt change)
python evals/compare_reports.py evals/triage/results/<before>.json evals/triage/results/<after>.json
```

All eval scripts require `SARVAM_API_KEY` in `.env` and cache every judge response by
`(row id, prompt version)`, so re-running after a labeling fix or on an unchanged prompt costs
nothing extra.

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # or source .venv/bin/activate on Linux/Mac
pip install -r requirements.txt
cp .env.example .env        # fill in SARVAM_API_KEY, GH_PAT, etc.
python pipeline.py --limit 5   # cheap smoke run
```

See [README.md](README.md) for full setup/configuration details (feeds, GitHub trending queries,
design system) — this document focuses on the engineering and evaluation story.
