# CLAUDE CODE HANDOFF — TechDrishti Finalization

Read this entire document before writing any code. It contains the project context, what already exists, what to build, and which tasks belong to the human (Aditya), not to you.

---

## STATUS AS OF 2026-07-17 — read this first, it supersedes the plan below where they conflict

Work is in progress, picked up mid-session. Full technical plan (with rationale for every deviation from the original doc below) lives at `C:\Users\adima\.claude\plans\adaptive-gliding-robin.md` — read that for the "why," this section is just the "what's done / what's next."

### Done
- **Task A (refactor)**: complete. 6 prompts extracted to versioned files in `writer/prompts/` (loaded via `load_prompt()`, sha1-fingerprinted). `writer/synthesize.py` exposes `PROMPT_VERSIONS` dict and a public `triage_item()` wrapper. Verified byte-identical to the original inline prompts; full test suite green (208 passed, 1 skipped).
- **Task B/C (triage eval)**: complete. `evals/triage/` has `build_candidates.py`, a hand-built tkinter `label_tool.py` (one item per screen, writes on every click, resumable), `run_triage_eval.py`. Golden set: 149 items hand-labeled (all of them, not just the suggested ~60). **First real result: 73.8% accuracy, skip precision 75.6%, skip recall 51.7%.** Report at `evals/triage/results/20260717T195235Z.json`. Two concrete, evidence-backed weaknesses found for the Phase 8 iteration: (1) no subject-matter/topic filter at all — `off_topic` recall only 12.5%; (2) legitimate GitHub tool repos wrongly skipped because the skip prompt's "news event" framing doesn't recognize "a new notable open-source tool" the way Stage 2's own `repo_analysis` category does. Also confirmed some raw false positives were pre-existing scraping garbage (Verge topic-page boilerplate, a GitHub JS-render error page) feeding the model bad input, not real judgment failures — don't double-count those against Stage 1 when writing up results later.

### In progress — NEW SCOPE, added mid-session at the user's request, not in the original doc below
**Task 3B: entity-extraction / GAP-query-quality eval**, a third golden set. Why it exists and why it's designed differently from triage (an LLM judge validated against human grades, not a directly re-runnable metric, because Stage 1's entity/query output is documented as run-to-run inconsistent) is explained in the plan file's "Phase 3B" section — read that before continuing this thread of work.

Location: `evals/entity_quality/` — `build_candidates.py` (entities, from trace history), `build_query_candidates.py` (queries, regenerated live from the CURRENT prompt — the historical trace queries turned out to be stale, captured before a same-day prompt revert documented in this doc's own "External Research Agent Integration" section; grading those would've been misleading), `label_tool.py` (per-article screen, all entities+queries for one article graded together).

- **Entities: done.** All 293 graded. **260 correct, 33 wrong_type**, zero hallucinated/not-an-entity/ambiguity misses — entity extraction itself looks solid, its only real weakness so far is type-labeling.
- **Queries: done.** All 56 graded by the human. Result: **44 good, 6 not_fact_seeking, 6 dangling_reference, 0 hallucinated_competitor, 0 irrelevant**.
- **Judge-validation harness: done.** `evals/entity_quality/run_entity_quality_eval.py` built and run to completion. Final report: `evals/entity_quality/results/20260718T151414Z.json`.
  - **Entity judge-vs-human agreement: 74.7%** (correct: 76.5%, wrong_type: 60.6%). Judge uses `writer/prompts/entity_quality_judge_entity_v1.txt`, `sarvam-105b`, `reasoning_effort=None`.
  - **Query judge-vs-human agreement: 66.1%** (good: 75.0%, dangling_reference: 50.0%, not_fact_seeking: 16.7%). Query judging is a two-call design (`entity_quality_judge_query_selfcontained_v1.txt` decides `dangling_reference` on its own from the query text alone, mirroring Stage 1's own skip-gate-then-analysis split; `entity_quality_judge_query_v2.txt` grades the remaining four labels only for queries that pass self-containment).
  - **Harness bug found and fixed along the way**: `candidates.jsonl`'s `article_context` was `stage0.source_preview` -- only `source_text[:400]` -- while Stage 1 itself extracts from `source_text[:2000]`. This caused a flood of false `hallucinated_not_in_article` verdicts on entities that are real but just past character 400. Fixed by live re-scraping each article's real URL (`writer.web_context.scrape_source`, cached by URL in `.scraped_context_cache.json`) and slicing to 2000 chars to match Stage 1's actual input, without touching the frozen `golden_set.jsonl`/`candidates.jsonl` files. This alone raised entity agreement from 44.4% to 74.7%.
  - **Query judge prompt iterated twice against real failure evidence, same "wording alone often insufficient, needs contrastive examples" lesson already documented elsewhere in this project**: first pass (single call, dangling folded into the main 5-way grade) scored 0/6 on `dangling_reference` because the human's actual grading included vague "compare to other X" (no named category) as dangling, not just literal "this/it/that". Splitting into a dedicated self-containment call fixed that (6/6) but an overly broad third rule ("generic definite description") then over-fired on legitimate same-sentence coreference (e.g. "...Claude Cowork perform...that it cannot..." wrongly flagged, even though "Claude Cowork" is named two words earlier) -- fixed with explicit contrastive examples including possessive forms ("Discord's response timeline" needs no separate name for "response," only for "Discord"). A parallel bug in the second-stage prompt made `hallucinated_competitor` fire on open-ended questions that name zero competitors (e.g. "What are Manna's specific competitors?") -- fixed by requiring the label only apply when the query text itself writes down a specific invented name.
  - **Real residual finding, not a prompt bug**: some remaining query disagreements trace to genuine human-labeling inconsistency on the good/dangling_reference boundary for "X compare to other Y" patterns -- e.g. ContextVC's two nearly-identically-shaped comparison queries were graded oppositely (one `good`, one `dangling_reference`) by the same human labeler, and SynthID's "other deepfake detection tools" was graded `good` while structurally identical LocalEyes/AI-moderation comparisons were graded `dangling_reference`. This is the same run-to-run judgment variance already documented throughout `CLAUDE.md` for Stage 1 itself, just now visible on the human side of a golden set too -- not something further judge-prompt iteration can fix.
  - **New finding for Task H, entity side**: ~22% of Stage 1's extracted entities (64/293) use an `entity_type` value outside the documented schema (`company|startup|ai_model|product|person|researcher|technology|protocol|regulation|event|organization|material`) -- e.g. `general`, `model_release`, `state`, `ban_regulation`, `game`, and even full descriptive phrases like `"the specific electric vehicle model"` and `"the city, used as a location"`. This explains most of the entity `wrong_type` disagreement and is a genuine Stage 1 prompt-adherence gap, not a judge miscalibration -- candidate fix: tighten `stage1_analysis_v1.txt`/`stage1_extraction_v1.txt` to strictly enforce the enum (and consider whether `location`/`game` deserve to be added to it, since they're legitimate recurring categories the current list is simply missing).

- **New finding surfaced during query grading, 2026-07-18, flagged for Task H — "compound and-queries."** Distinct from the three failure modes already documented in `CLAUDE.md` (dangling reference, hallucinated competitor, pure-interpretive query). Stage 1 sometimes bundles a genuinely fact-seeking clause with a second, interpretive/comparative clause into one GAP line via "and" — e.g. *"What are the specific technical details of Grok 4.5's architecture, **and how does it differ from** previous Grok models or other leading LLMs?"* 7 of the 56 graded queries fit this shape (out of 36 that contain the literal word "and" — most of those 36 are harmless compound noun phrases like "settlement terms and conditions," not two questions). Because query decomposition is explicitly not built (see the "GAP query phrasing" section), the entire compound string is dispatched as a single search — same practical failure as an unnamed-category comparison query, just introduced via "and" instead of "vs./compared to." The human labeler graded these `good` on a "primary clause governs" policy (the first, factual clause is legitimate; the interpretive tail is along for the ride), so this pattern isn't visible in the raw grade counts above — it's called out here explicitly instead. Candidate Task H fix: a HARD RULE in `_STAGE1_ANALYSIS_PROMPT` requiring one ask per GAP line, no "and how does it compare/differ" tails bundled onto a factual question.

### Done (cont.)
- **Task F / Phase 6 (cache test gap-fill): complete.** `evals/cache/test_entity_cache.py` already existed on disk with the "cache-hit-avoids-search" case fully built (`TestCacheHitAvoidsSearch`, 2 tests: a fresh-hit case asserting `search_web`/`ask_concise` are never called, and a miss-case control proving the silence is actually caused by the cache hit). Verified both pass. Alongside this, extracted the entity/cache-partitioning loop inside `_synthesize_sarvam()` (the `for entity in entities: get_entity(...) ...` block) into a standalone `_partition_entities_by_cache(entities, cache)` helper in `writer/synthesize.py` — pure refactor, no behavior change, done to make the cache-hit-vs-miss split independently readable/testable rather than buried inline. Confirmed byte-identical behavior via full `tests/test_synthesize.py` + `tests/test_entity_cache.py` (119 tests) passing before and after, plus the new `evals/cache/` tests passing against the refactored code.

### Done (cont.)
- **Task G / Phase 7 (regression tooling): complete.** `evals/compare_reports.py` — walks two report JSONs recursively, diffs every shared numeric leaf, and marks each better/worse/neutral. Handles both existing report shapes (triage's flat `metrics` dict, entity_quality's `entity_metrics`/`query_metrics` dicts) generically via path-walking rather than hardcoding either schema, so it'll also work on the faithfulness report once Phase 4/5 exists. Confusion-matrix cells (`X->Y` leaf names) get their own diagonal-vs-off-diagonal rule (X->X agreement counts: bigger is better; X!=Y misclassification counts: smaller is better) — verified this mattered, not just theoretical: a naive "bigger is always better" default wrongly flagged `good->dangling_reference` dropping 9→4 (fewer good queries wrongly judged dangling — an improvement) as a regression, fixed before shipping. Also diffs `prompt_versions`/`judge_prompt_versions` string fields separately. Verified against the real entity_quality 46.4%→66.1% query-judge iteration pair: correctly surfaced the one genuine known regression in that change (`dangling_reference` agreement 100%→50%, already documented above) alongside the overall improvement, nothing else.
  - `run_all_evals.sh` — runs `pytest tests/ evals/` + triage eval + entity_quality eval + faithfulness eval (skipped with a clear message until Phase 4/5 exists), reports a pass/fail summary, exits non-zero on any failure. Supports `--limit N` forwarded to every eval script for a cheap smoke run. Smoke-tested end-to-end with `--limit 2` (real Sarvam calls, both eval scripts wrote fresh reports, pytest passed 210/211) — the 2-item throwaway reports it produced were deleted afterward, not committed.
  - **Bug found and fixed along the way**: `evals/cache/test_entity_cache.py` (Phase 6's file) and `tests/test_entity_cache.py` share a basename, which crashes pytest collection (`import file mismatch`) the instant both are collected together — exactly what `run_all_evals.sh`'s combined `pytest tests/ evals/` call does. Fixed by renaming to `evals/cache/test_cache_search_skip.py`. Not a hypothetical: reproduced the crash first, confirmed the rename fixes it, re-ran full suite clean (210 passed, 1 skipped).
  - Optional `.github/workflows/evals.yml` (manual `workflow_dispatch` only) — not built yet, genuinely optional per the spec, low priority since `run_all_evals.sh` already works standalone.

### Done (cont.)
- **Phase 8 / Task H (prompt iteration): both iterations complete, both verified live with
  real before/after numbers.** User picked all three originally-queued candidates (not just the
  1-2 the plan called for as a minimum); they collapsed into two prompt iterations since two of
  the three shared `stage1_analysis_v1.txt`. Full test suite green after both changes (210
  passed, 1 skipped). Detail below.

- **Iteration 1: `stage1_skip_v2` — off_topic recall + wrongly-skipped GitHub repos. DONE, verified.**
  Root cause: the skip prompt had zero subject-matter/topic filter — anything mentioning a tech
  company or containing a data point passed as "tech news," and a new open-source repo release
  wasn't recognized as a "launch" event the way Stage 2's own `repo_analysis` category treats it.
  Fix (`writer/prompts/stage1_skip_v2.txt`): added an explicit off-topic category with 6
  concrete sub-shapes (retail/promo posts, non-tech business/legal news, entertainment/media
  news, unrelated pure-science research, multi-topic newsletter digests, personal-reaction
  gadget anecdotes), each with a real example pulled from the actual 21 off_topic misses in the
  baseline report — not invented examples. Also added an explicit "a new GitHub repo IS a launch
  event, don't skip it just for not being a press release" clarification.
  **Real before/after, full 149-item golden set** (`evals/triage/results/20260717T195235Z.json`
  -> `20260718T162006Z.json`, diffed with the new `compare_reports.py`):
  accuracy 73.8%->81.9% (+8.0pp), skip_precision 75.6%->86.7% (+11.1pp), skip_recall
  51.7%->65.0% (+13.3pp), **off_topic recall 12.5%->50.0% (3/24->12/24)**. GitHub-repo case:
  2 of the 4 genuinely-wrongly-skipped repos (`Nanako0129/pilotfish`,
  `Neeeophytee/finding-unknowns-skills`) are now correctly published; 2 remain misclassified
  (`markfulton/claude-antigravity-agents`, `olemeyer/rocketplaneIO` — still judged "not a
  significant enough development," genuine residual judgment inconsistency, same class already
  documented under Known Issue #4 — not claimed as fully fixed). One small regression found and
  logged honestly, not chased further given n=3: `too_thin` correctly-skipped dropped 1/3->0/3
  (`Getting started with ChatGPT` flipped from skip to publish).
  Wired in: `writer/synthesize.py` now loads `stage1_skip_v2` (was `_v1`).

- **Iteration 2: `stage1_analysis_v2` — entity-type schema violations + compound and-queries.
  DONE, verified live.** Root-cause investigation before writing the fix, grounded in the real
  65 violating rows from `candidates.jsonl` (not the summary numbers alone): 41/65 (63%) turned
  out to be one single specific bug — the model reusing the article's own TYPE classification
  value (`general`, `model_release`, `ban_regulation` — a completely different enum for
  classifying the whole article) as if it were an individual entity's type (e.g.
  `"Hal Abelson (general)"`, `"Y Combinator (general)"`, real examples, not invented). Most of
  the rest were either invented descriptive-phrase types (`"the specific electric vehicle
  model"`, `"game_series"`, `"project"`) or well-known geographic place names / bare spec values
  that don't have a correct type on the list at all (`Illinois (state)`, `128GB (storage)`) —
  genuinely don't need identity-context lookup for a Hindi reader, so the fix excludes them from
  extraction entirely rather than inventing a new type category. Also confirmed
  `stage1_analysis_v1.txt`'s own type list was missing `material` — a real drift from the
  documented 12-type schema (`entity_quality_judge_entity_v1.txt` already had all 12; Stage 1
  itself only listed 11). Compound-and-query fix: added a HARD RULE (matching the file's
  existing WRONG/CORRECT style) requiring one ask per GAP line, using the real `Grok 4.5`
  example already documented in this file, explicitly distinguishing a problem "and" (bundles
  two separate questions) from a harmless one (a plain compound noun phrase like "settlement
  terms and conditions").
  **Why this needed a new live-verification script instead of reusing
  `run_entity_quality_eval.py`**: that harness validates a JUDGE against frozen human grades —
  it never calls Stage 1 live, so a Stage 1 prompt change doesn't move any number in it at all.
  `experiments/verify_stage1_analysis_v2_live.py` reuses `build_candidates.py`'s
  `gather_articles()` (same 54 real articles) and calls the real `triage_item()` live, reporting
  the schema-violation rate against the known frozen-data baseline (65/293 = 22.2%) and flagging
  any GAP query matching the compound-and shape for manual read — same "live re-run on real
  articles" technique already used in `experiments/run_gitlost_context_fix_comparison.py`.
  **Real live result** (`experiments/stage1_analysis_v2_live_check_20260718T162401Z.json`, 54
  articles, 10 skipped by Stage1 this run, 0 call failures):
  - **Entity-type schema violation rate: 22.2% (65/293, frozen baseline) -> 3.1% (3/97, live
    v2)** — the dominant TYPE-bleed bug (63% of the original violations) is effectively
    eliminated. 3 residual cases, all honestly logged, none re-introducing the TYPE-bleed
    pattern: `'Tulsa, Oklahoma' (location)` — a defensible edge case, since Tulsa is the actual
    named hub location in the Manna drone-delivery story (the rule's own carve-out for "the
    place itself is the actual subject"), but the model still invented `location` as a type
    value instead of using a real one or omitting the type; `'not interested' (feature)` and
    `'Mango' (internal code-name)` — the same descriptive-phrase-instead-of-enum-value drift,
    just far less frequent now.
  - **Compound and-queries**: the structural regex heuristic flagged 5/77 live GAP queries
    (6.5%), but a manual read of all 5 shows most are same-entity two-part factual questions
    (e.g. "What is SWE-Bench Pro and what are its specific task issues?") — the harmless shape
    the fix was explicitly designed to leave alone, matching the human's own established
    "primary clause governs" grading norm. Exactly one query is a genuine miss of the targeted
    pattern: `"What are the specific capabilities and limitations of the Claude Code
    orchestrator skill, and how does it differ from a standard model release?"` — the fix
    reduced but did not fully eliminate the comparative-tail shape, consistent with this
    project's repeated finding elsewhere that prompt wording alone rarely reaches 100% (see the
    dangling-reference and hallucinated-competitor writeups above, both of which needed a
    code-level backstop after the prompt-only fix). A code-level filter for this pattern
    (mirroring `_drop_hallucinated_comparisons`) is a reasonable next step if this residual rate
    matters enough to chase further — not built this session, logged as a candidate.
  Wired in: `writer/synthesize.py` now loads `stage1_analysis_v2` (was `_v1`).

- **Iteration 3 candidate remaining**: none — all three selected weaknesses are the two above
  (off_topic/repo_analysis was one bucket, entity-type + compound-and-queries share one prompt
  file and are being verified together).

### In progress — Phase 4 (Task D: faithfulness golden set tooling)
`evals/faithfulness/build_candidates.py` and `evals/faithfulness/label_tool.py` built, both
smoke-tested against real data. `evals/faithfulness/candidates.jsonl` generated: **1107 claim
rows from 40 real published Sarvam articles** (category mix: general 11, repo_analysis 11,
model_release 9, ban_regulation 8, acquisition 1 — acquisition is genuinely rare in the 56
trace-published articles available, only 1 exists).

- Same "walk current trace + every git-log version" technique as the other two golden sets'
  `build_candidates.py` scripts, since `output/pipeline_trace.json` is overwritten (not
  appended) every run — only 56 published-and-URL-matched articles exist across all 3 available
  trace snapshots, so 40 is close to the practical ceiling, not an arbitrary cut.
- **`source_text` is live-scraped (`writer.web_context.scrape_source`), not the trace's stored
  `stage0.source_preview`** — deliberately avoided the exact same 400-char-truncation pitfall
  Phase 3B already hit and fixed (see that section above); a faithfulness check is if anything
  more exposed to this, since a claim can reference any fact anywhere in the source. Cached by
  URL in `.scraped_source_cache.json`, falls back to the trace preview if a live scrape fails.
- Claims are split one-sentence-per-row from `concept_box`, `introduction_lede`,
  `deep_dive_and_context`, `strategic_analysis`, `conclusion_and_significance` (title excluded —
  a headline isn't a claim). The sentence splitter reuses `writer.synthesize._is_sentence_end`
  (the existing decimal-point-aware rule, e.g. `GLM-5.2`/`32.8%` never mistaken for a sentence
  boundary) instead of a fresh regex that could drift from production's own logic.
- **Workload flag for the human, resolved 2026-07-19**: the plan estimated ~400-500 claim labels
  from ~40 articles; actual sentence-level splitting produced 1107. User pushed back hard on
  hand-labeling all 1107 and asked why an LLM (with the same source_text a human would use)
  couldn't do the checking itself. Real answer, not a full concession: faithfulness-checking is
  a **closed-book reading-comprehension task** (claim vs. the specific captured `source_text`,
  no outside knowledge or web search needed by either a human or the judge), unlike triage/
  entity-typing's subjective editorial judgment calls — this makes it much more suitable for a
  validated LLM judge than those two were. Landed on the same "validate a judge on a small human
  sample, then let the judge grade everything else" pattern already proven in Phase 3B (56
  human-labeled queries validated a judge that then re-graded any future output), just sized
  smaller here: **user labels ~80-110 claims (3-4 full articles via the existing label_tool.py)
  instead of all 1107**; the validated judge grades the full 1107 for the real hallucination-rate
  number. This is NOT "let the model author its own ground truth" — the validation sample is
  still 100% human-labeled, just deliberately small, matching Phase 3B's own scale rather than
  every row.
- Bug found and fixed while building: printing the Hindi-text INSTRUCTIONS block crashed with
  `UnicodeEncodeError` on a local Windows console (cp1252) — same class of issue CLAUDE.md
  already documents from the Stage 2 investigation, not new. Fixed with the same
  `sys.stdout.reconfigure(encoding="utf-8")` guard; irrelevant on production's `ubuntu-latest`
  runners either way.
- `label_tool.py` mirrors `evals/entity_quality/label_tool.py`'s one-article-per-screen pattern
  (claims grouped by field, real scraped source text shown in a reference panel, grades written
  to `golden_set.jsonl` on every Save & Next). Smoke-tested: instantiated against the real
  1107-row/40-article pool, rendered cleanly, closed without error.

### Done (cont.)
- **Phase 5 (Task E: faithfulness eval harness): built and smoke-tested live, awaiting the
  human's small validation sample to run validation mode.** `evals/faithfulness/
  run_faithfulness_eval.py`, same validation/scoring two-mode shape as
  `run_entity_quality_eval.py`, two layers per the original plan:
  - **Deterministic layer** (`deterministic_check`, zero model calls): a claim stating a number
    (2+ digits, decimal, or %) that doesn't appear anywhere in `source_text` is confidently
    `unsupported`, no judgment call needed — free and 100% reproducible. Deliberately does NOT
    attempt named-entity matching (considered and dropped — a real entity phrased differently in
    the source, e.g. a translated name, would false-positive as unsupported with no way to catch
    the mismatch the way a judge reading both texts can; a bare number has no such ambiguity).
    Unit-verified: `"40%"` in both claim and source → supported; `"99%"` in claim only → flagged
    unsupported; a claim with no numbers → correctly deferred (`None`) to the LLM layer.
  - **LLM judge layer** (`writer/prompts/faithfulness_judge_v1.txt`, sarvam-105b,
    `reasoning_effort=None`, matching every other judge/synthesis call in this pipeline):
    closed-book — sees only the claim and its own article's `source_text`, told explicitly to
    judge against that alone, not outside knowledge. Grades `supported`/`unsupported`/
    `hedged_opinion`.
  - **Live smoke-tested end-to-end** (`--score candidates.jsonl --limit 6`, real Sarvam calls,
    real published John Deere article): 5 supported, 1 unsupported (the source never actually
    frames the settlement as a "right-to-repair movement victory" — a real, correctly-caught
    overreach by Stage 3). Judge reasoning quotes the actual source text verbatim, not vague
    hand-waving — spot-checked all 6 reasons before accepting the harness as working.
  - Bug found and fixed same as `build_candidates.py`: Hindi-text console print crashed on
    Windows cp1252, fixed with the same `sys.stdout.reconfigure(encoding="utf-8")` guard.
  - Scoring mode reports `hallucination_rate` (unsupported / (supported+unsupported),
    hedged_opinion/error excluded from the denominator per the plan), a per-category breakdown,
    and the 10 worst articles by unsupported-claim count — all per Task E's spec.

### Done (cont.)
- **Phase 5 validation + full scoring run, complete, 2026-07-19.** User graded 49 claims across
  2 articles (less than the ~80-110 suggested, but the user pushed back on doing more — accepted
  as a legitimate, honestly-small sample rather than pushing for more; same "report the real
  number, don't chase it artificially" standard used throughout this project).
  - **Validation result** (`results/20260719T160427Z.json`): 69.4% (34/49) judge-vs-human
    agreement, deterministic layer fired on 9/49 claims. Investigated all 15 disagreements
    individually rather than accepting the number at face value — found **two distinct, real
    causes, not one judge weakness**:
    1. **Entity_context blind spot (structural, not fixable retroactively)**: on the Meta/Vistara
       article, several very specific technical claims (`CXL 2.0 Type-3`, `PCIe Gen5 x16`,
       `DDR4-2400`, `256GB`, a `Transparent Page Placement (TPP)` layer, `10x` bandwidth
       reduction) were flagged `unsupported` because none of them appear in the scraped article
       text — confirmed directly by reading the real scraped `source_text`, which is a short,
       generic blog post with none of these numbers. But Stage 3 doesn't only see the scraped
       article — it also sees `entity_context` (search-derived facts gathered during synthesis,
       see CLAUDE.md's Stage 3 prompt), which this eval never captured. Checked whether it's
       recoverable from `pipeline_trace.json`: only a 600-char `entity_context_preview` survives
       (a preview, same truncation shape as the already-fixed `stage0.source_preview` issue) —
       genuinely not enough to contain those specifics, and not re-constructible after the fact
       since search results are time-sensitive/non-deterministic. This is a real, honest
       limitation of doing faithfulness-checking retroactively on historical articles, not a
       harness bug to fix.
    2. **Interpretive-vs-factual boundary, a genuine rubric-application question, not resolved**:
       several disagreements were sentences like *"this is a victory for the right-to-repair
       movement"*, *"it remains to be seen how John Deere will comply"*, *"this could set a
       precedent for other companies"* — human graded `supported`, judge graded `unsupported`/
       `hedged_opinion`. These read as textbook interpretive/speculative language per the
       rubric's own `hedged_opinion` definition. Flagged to the user rather than assumed either
       way — not yet resolved, would need either a relabel or an explicit decision that
       "reasonable characterization of the article" should count as supported.
  - **Full scoring run** (`results/20260719T162622Z-scoring.json`, all 1107 claims, real Sarvam
    calls): grade distribution `{supported: 804, unsupported: 287, hedged_opinion: 16}`,
    **hallucination rate 26.3%** (287/1091, hedged_opinion excluded from denominator per spec).
    - **Per-category**: `repo_analysis` 35.1% (107/305) — clearly the worst category and not a
      coincidence: `repo_analysis` articles are written from sparse, technical GitHub READMEs,
      which is exactly the case most likely to lean on `entity_context` for explanatory material
      — meaning this category's number is most inflated by the same structural gap found above.
      `acquisition` 11.5% (3/26, smallest sample — only 1 acquisition article total, per the
      earlier note that this category is genuinely rare in the available data), `ban_regulation`
      19.0%, `model_release` 24.2%, `general` 25.5%.
    - **Worst article, spot-checked, not assumed**: `AMRouter v2` (9Router v2 repo_analysis),
      21/28 unsupported. Read 5 of its actual judge reasons — this one is NOT primarily the
      entity_context gap: it's Stage 3 asserting specific benefits/characterizations
      ("significant milestone for AI developers," "better performance," latency/memory-footprint
      claims) that go beyond what the README literally states about a "decoupled rewrite" /
      "clean separation" — a genuine over-claiming/embellishment pattern on repo_analysis
      articles, worth its own look in a future Task H round, distinct from the entity_context
      issue.
  - **Headline number should be reported with both caveats attached, not as a clean number** —
    the true hallucination rate is very likely lower than 26.3% (the entity_context gap alone
    probably accounts for a meaningful share, concentrated in `repo_analysis`), but the honest,
    as-measured number is 26.3% and that's what's recorded here per this project's
    never-fabricate-metrics rule.
  - **Forward-looking fix identified, decided 2026-07-19: NOT implemented, documented instead.**
    `pipeline_trace.json` could log the FULL `entity_context` instead of a 600-char preview — a
    low-risk logging-only change that would let *future* articles be faithfulness-checked
    accurately against everything Stage 3 actually saw. User's call: don't make the code change
    now — write it up as a known limitation / lesson-learned in `README.en.md`'s limitations
    section instead (Task I), specifically: *"faithfulness-checking should capture the full
    search/entity_context material at synthesis time, not just a 400-600 char preview, before
    the next round of this kind of evaluation."* Correctly scoped as documentation, not more
    code, at this point in the project — noted here so Task I doesn't need to rediscover it.

### Done (cont.)
- **Phase 9 (Task I: README.en.md) and Phase 10 (Task J: final polish): complete, 2026-07-19.**
  - `README.en.md` written: what it is + live site link, architecture diagram (deterministic vs.
    model-call stages annotated), tightened English translation of CLAUDE.md's engineering
    highlights (token-budget/reasoning discovery, two-layer triage, entity cache, the two live
    search bugs, post-processing), full evaluation section with real tables for all three golden
    sets (triage before/after, entity/query judge agreement, faithfulness hallucination rate +
    per-category table + 3 real translated unsupported-claim examples), and a Limitations
    section stating plainly: single-annotator golden sets, the 49-claim faithfulness validation
    sample, the entity_context blind spot + its repo_analysis concentration (both Phase 5
    caveats, not just the headline 26.3%), fluency/style unmeasured, one-distribution judge
    validation, Stage 1's known run-to-run inconsistency. Linked prominently from the top of the
    Hindi `README.md`, which stays the product-facing identity per the spec.
  - `pipeline.py` — added `--limit N` (caps clusters synthesized, not collection/clustering
    itself, so it's a cheap *smoke run* not a faster collect). Verified: `daily.yml` calls
    `python pipeline.py` with no args, so production behavior is provably unaffected; existing
    `tests/test_pipeline.py` (5 tests) still green.
  - `.env.example` — was missing `SARVAM_MODEL_QUALITY` and `CONCISE_API_URL`/`CONCISE_API_KEY`
    (all three are real, referenced env vars — grepped every `os.environ.get(...)` call in the
    codebase to confirm, not guessed). Added with a comment noting the concise-search pair is
    optional and currently force-disabled.
  - Dead-code cleanup: grepped for `tavily`/`exa` and for prose-shaped vs. code-shaped commented
    lines across `writer/`, `collectors/`, `translator/`, `pipeline.py` — found nothing left to
    remove. The earlier Tavily/Exa removal (documented in CLAUDE.md) was already thorough.
  - `run_all_evals.sh --limit 3`: full real smoke test, all four steps (pytest, triage,
    entity_quality, faithfulness — the last one didn't exist when this script was first written)
    passed end-to-end. Throwaway 3-item reports deleted afterward, not committed.
  - **Not done, flagged honestly rather than faked**: "2-3 screenshots in README.en.md" from
    Task J's spec — no browser/screenshot tooling is available in this environment. The live
    site link is included in `README.en.md` as the closest available substitute.
  - GitHub repo description: re-read Task J's spec ("English repo description... in
    README.en.md") as content to include in the README file itself (already satisfied by its
    "What it is" section), not a live change to the repo's GitHub metadata — the actual repo
    description setting was left untouched (still Hindi, matching the product identity, same
    reasoning as leaving `README.md` itself alone).

## PROJECT STATUS: finalization complete
All of Tasks A-J are done except the two explicitly-flagged gaps above (screenshots, no tooling;
optional `.github/workflows/evals.yml`, never built, genuinely optional per spec). Every golden
set is frozen and committed-ready, every eval harness runs green, `README.en.md` carries real
measured numbers with honestly-stated limitations, and production pipeline behavior is
unchanged apart from the additive `--limit` flag.

### Working notes for whoever picks this up
- The user (Aditya) does ALL labeling personally, via the tkinter tools built for each golden set — never pre-fill a grade/label, even an "obvious" one; this has been enforced strictly all session (declined twice: once for 9 listicle articles, once for query grades) and is core to why these evals mean anything.
- `.env` has `SARVAM_API_KEY` configured locally — eval harnesses that make live calls check for it and abort cleanly if missing.
- Git status as of writing: `writer/synthesize.py` modified (Phase 1 refactor), `evals/` and `writer/prompts/` untracked (new), 4 stale `output/*.txt|json` scratch files staged for deletion (unrelated pre-existing cleanup from earlier in the session). Nothing has been committed yet this session — ask the user before committing, per standing instructions.

---

## 1. Project context (what TechDrishti is)

TechDrishti is a fully automated Hindi tech-news publication. It runs daily at 8 AM IST via GitHub Actions at zero hosting cost, publishing to GitHub Pages. The pipeline:

1. **Collectors** gather candidate stories from Google News RSS and GitHub trending, with keyword and star-count pre-filters (deterministic, no AI).
2. **Stage 0**: deterministic content-length gate; thin scrapes are dropped before any model call.
3. **Stage 1 (triage)**: a cheap model (sarvam-30b) decides in two batched calls which items are genuine publishable tech news vs job postings, listicles, Show HN posts, and other non-news. Only survivors proceed.
4. **Entity enrichment**: a persistent entity cache (TTL-based, with sense disambiguation, e.g. "Claude the model" vs "Claude the person" cached as separate senses) provides context; cache misses trigger a ddgs search.
5. **Stage 2 (synthesis)**: a stronger model (sarvam-105b) writes original Hindi articles. Planning and writing are split into separate calls because reasoning tokens were consuming the 4096-token output budget. Category-aware framing (e.g. hedged language for acquisition rumors) is applied.
6. **Post-processing**: deterministic cleanups (`_is_meta_line`, `_trim_to_last_sentence`) added in response to failure patterns found in traces.
7. **Publication** to GitHub Pages. Google Translate exists as a fallback translation path.

Per-article debugging is supported by `pipeline_trace.json` records. Typical run: 9 to 20 minutes, about $0.01 per run.

A separate deep-research agent exists in another repo. It is deliberately NOT part of this pipeline (it added 81+ minutes and 17x cost). Do not integrate it. Do not add search tiers. Search stays as-is: Google News RSS + ddgs.

## 2. What "finalization" means

The pipeline works but its quality is unproven. There are no golden datasets, no eval harnesses, no quantitative metrics. Every quality claim is currently anecdotal. Finalization = adding a measurement layer plus an English case-study README, WITHOUT changing production behavior. The daily cron must be completely unaffected: evals are run on demand, on frozen data, never as part of the daily pipeline.

## 3. Division of labor (critical, do not violate)

- **You (Claude Code)** build all tooling: candidate-extraction scripts, eval harnesses, report writers, tests, refactors needed to make pipeline stages importable by evals.
- **The human** writes every ground-truth label. You must NEVER generate, suggest, or prefill `true_label`, claim-support labels, or any field that the eval will later grade against. If the model authors the answer key, the entire eval is circular and worthless. When a step requires labeling, generate the unlabeled file, print clear instructions for the human, and stop.

## 4. Target structure to add

```
evals/
  triage/
    build_candidates.py
    golden_set.jsonl            <- human-labeled, frozen after creation
    run_triage_eval.py
    results/                    <- timestamped JSON reports, committed
  faithfulness/
    build_candidates.py
    golden_set.jsonl            <- human-labeled, frozen
    run_faithfulness_eval.py
    results/
  cache/
    test_entity_cache.py        <- plain pytest
  compare_reports.py
run_all_evals.sh
README.en.md
```

## 5. Build tasks, in order

### Task A — Refactor for testability (do this first)
Make Stage 1 triage and the Stage 2 claim-relevant paths importable as functions without running the whole pipeline (e.g. `triage_items(items) -> decisions`). Do not change behavior; the daily run must produce identical output before and after. Extract any inline prompts into versioned files (e.g. `writer/prompts/triage_v1.txt`) and load them at runtime, so eval reports can reference exact prompt versions. Add a cheap prompt-version identifier (filename or content hash) accessible to the eval scripts.

### Task B — Triage golden set tooling
`evals/triage/build_candidates.py`: walk stored `pipeline_trace.json` files and any archived collector data; emit one JSONL with `id`, `title`, `source`, `summary/content_snippet`, `stage1_decision_at_the_time`. Aim to surface a diverse pool (news, job postings, listicles, Show HN, borderline items). Then STOP and instruct the human:

> HUMAN TASK: open the JSONL, pick ~60 items with a deliberate mix (~30 real news, ~10 job postings, ~10 listicles/link collections, ~10 borderline), and add `true_label` (publish|skip) and `skip_reason` (job_posting|listicle|show_hn|off_topic|too_thin|null) to each chosen item. Save as golden_set.jsonl. Budget ~1 day. After committing, treat labels as frozen; label fixes only for genuine mistakes, noted in commit messages.

### Task C — Triage eval harness
`run_triage_eval.py`:
- Runs the real (refactored) Stage 1 path over the golden set.
- Metrics: skip-class precision and recall, overall accuracy, per-skip_reason confusion breakdown, and a full list of misclassified ids.
- Report JSON includes: metrics, timestamp, model name, prompt version id.
- Include `--limit N` and a response cache keyed by item id + prompt version, so development re-runs do not re-query unchanged items.

### Task D — Faithfulness golden set tooling
`evals/faithfulness/build_candidates.py`: select ~40 published articles from `output/articles_hindi.json`, pair each with its captured source text from traces, split the Hindi article into claim-level units (sentence-per-line is acceptable v1), emit JSONL with `article_id`, `claim_index`, `claim_text_hi`, `source_text`. Then STOP and instruct the human:

> HUMAN TASK: label every claim as supported (fact present in source/search context), unsupported (fact appears nowhere in inputs), or hedged_opinion (framing language, not a factual claim). Roughly 400-500 labels; budget 1-2 days. Keep notes on interesting unsupported claims found; they become README examples.

### Task E — Faithfulness eval harness
`run_faithfulness_eval.py`, two layers:
1. **Deterministic layer**: for claims containing numbers, dates, or named entities, normalized matching against source text. Runs first, free.
2. **LLM-as-judge layer**: for remaining claims, one grading call per claim (use the Gemini integration already present, or sarvam). Input: claim + source material only. Output: strict JSON `{"verdict": "supported|unsupported|unclear"}`. No tools, no search.
- **Judge validation mode**: run the judge over the human-labeled golden set and report judge-vs-human agreement overall and per verdict class. If agreement < ~85%, iterate the judge prompt (judge prompts are also versioned files) and re-validate. The final agreement number goes in the README regardless.
- **Pipeline scoring mode**: hallucination rate = unsupported / total factual claims, per-category breakdown, worst articles by id.
- Optional flag `--live N`: run the validated judge over the N most recent published articles (continuous monitoring mode). Off by default; never wire into the daily cron without the human explicitly deciding to.

### Task F — Entity cache pytest
`test_entity_cache.py`: TTL expiry boundaries, sense disambiguation correctness (unknown sense returns None rather than wrong sense), name normalization collisions, and cache-hit-avoids-search (mock the search layer; assert not called on fresh hit).

### Task G — Regression tooling
- `compare_reports.py`: diff two report files, print per-metric deltas with clear better/worse marking.
- `run_all_evals.sh`: triage eval + faithfulness (judge layer, frozen set) + pytest, writing fresh timestamped reports.
- Optional: `.github/workflows/evals.yml` with `workflow_dispatch` trigger only (manual), uploading reports as artifacts. Never on cron, never on push.

### Task H — The iteration story (joint task)
After C and E produce first numbers, the human picks 1-2 weaknesses the reports reveal (e.g. listicles slipping through triage, or one category with high unsupported rate). You implement prompt v2 for the relevant stage, re-run evals, and commit both before/after reports. At least two documented iterations with metric deltas are required for the README.

### Task I — README.en.md (write last)
English case study, linked prominently from the existing Hindi README (which stays as the product-facing identity). Sections: what it is + live site link; architecture diagram annotated deterministic-vs-model stages; engineering decisions (translate and tighten the existing Hindi highlights: two-call triage economics, reasoning_effort discovery, plan/write split, entity cache design, search-tier debugging); evaluation results (triage precision/recall table, hallucination rate, judge-human agreement, per-category tables, 2-3 real unsupported-claim examples from the human's labeling notes); iterations with before/after deltas and commit links; limitations stated plainly (small single-annotator golden sets, judge validated on one distribution, fluency/style unmeasured, only faithfulness measured).

### Task J — Final polish
- Fresh-clone check: install, `.env.example`, a `--limit` flag on the main pipeline for cheap smoke runs, `run_all_evals.sh` green.
- Remove dead commented-out code from the removed search tiers (the story lives in the README, not in comments).
- English repo description and 2-3 screenshots in README.en.md.

## 6. Hard constraints

- Production pipeline behavior unchanged (except the harmless `--limit` flag and the refactor in Task A, which must be output-identical).
- No new search providers, no deep-research-agent integration, no new pipeline stages, no new languages.
- Evals run on demand only. Nothing added to the daily cron.
- Golden sets are frozen after labeling. Never edit labels to improve numbers.
- Never fabricate metrics. If a number is bad, it goes in the report as-is; iteration, not adjustment, is the fix.
- Keep API spend low: cache eval model responses; the judge runs per-claim only when the deterministic layer cannot decide.

## 7. Definition of done

- Both golden sets committed, frozen, with labeling methodology described in README.en.md.
- `run_all_evals.sh` green from a fresh clone; reports written with model + prompt versions.
- Judge-human agreement number published.
- At least two before/after report pairs from documented prompt iterations.
- README.en.md live with real numbers and real failure examples.
- Daily production run demonstrably unaffected (same outputs on a test run before and after the refactor).