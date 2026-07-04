# Testing Thought Process

## Why these tests, and why now

Every fix documented in `prompt-design-thought-process.md` and `CLAUDE.md` was validated by
running real code against the live Sarvam API and eyeballing the result. That's real testing,
but it's manual — nothing stops the next change from silently breaking a parser that took a
whole afternoon to get right. This suite converts the parts of that manual testing that don't
require the live API into permanent, automatic regression tests.

**What's deliberately NOT tested here**: anything that calls the live Sarvam API, live web
search, or does real HTTP. Those are the parts already validated empirically and documented
with real before/after evidence in `prompt-design-thought-process.md` — faking them out in a
unit test would just be testing a mock's behavior, not the system's. What IS tested is every
pure-logic function around those calls: parsing their output, deciding what to do with it, and
the deterministic filters that run before an LLM is ever invoked.

105 tests, 6 files, `pytest.ini` + `requirements-dev.txt` at the project root, all under `tests/`.

---

## `tests/test_synthesize.py` — the core parsing/cleanup logic (37 tests)

This is the file with the most tests because `writer/synthesize.py` is where nearly every
documented bug this session actually lived — token starvation, sentence truncation, and
label-drift were all *parsing* problems, not prompt problems.

- **`TestParseJsonResponse`** — `_parse_json_response` is the single most load-bearing function
  in the pipeline; Stage 1/2/3 and the synthesis step all depend on it recovering valid JSON
  from a model that doesn't always follow instructions cleanly.
  - `test_plain_json` / `test_json_in_markdown_fence` — the two "easy" cases, confirming the
    common-path behavior isn't accidentally broken by a future edit.
  - `test_json_with_reasoning_preamble` — models sometimes prepend a sentence of reasoning
    before the JSON even with `reasoning_effort=None` requested; this confirms the
    balanced-brace scan still finds the object.
  - `test_first_balanced_object_wins_over_truncated_rewrite` — a real observed failure mode:
    `reasoning_content` sometimes contains one complete JSON object followed by a second,
    truncated rewrite attempt. This locks in "take the first valid one, don't get confused by
    the second."
  - `test_none_input` / `test_empty_string` / `test_no_json_present` / `test_unparseable_garbage`
    — every caller treats `None` from this function as "parsing failed, trigger the fallback
    path" — these confirm it fails closed (returns `None`) rather than raising or returning
    garbage.

- **`TestIsSentenceEnd`** / **`TestTrimToLastSentence`** — the decimal-point truncation bug:
  `_trim_to_last_sentence` used to treat any `.` as a sentence boundary, which truncated
  `"GLM-5.2"` to `"GLM-5."` and `"32.8%"` to `"32."` mid-number. These tests pin the fix down —
  `test_does_not_truncate_on_trailing_decimal_number` is a direct regression test for the exact
  bug found on a real article, not a hypothetical case.

- **`TestIsMetaLine`** — the model occasionally leaks self-commentary ("Let me rewrite this...")
  into article text instead of just the article itself. `test_short_ascii_fragment_is_not_meta`
  exists because the heuristic (ASCII-heavy line + "Let me/Here's/..." prefix) could easily
  misfire on a short English technical term embedded in a Hindi sentence — confirming it doesn't.

- **`TestCleanFieldText`** — the composed function (strip meta lines, then trim to last
  sentence) that Stage 3's JSON path runs on every field before it reaches the frontend.

- **`TestMatchLabel`** / **`TestParseLabeledText`** — the *fallback* parser, only used if Stage
  3's JSON output fails to parse. This existed because the model used to drift into Hindi
  labels (`शीर्षक:`) or wrap them in markdown (`**शीर्षक:**`) instead of the requested English
  labels — `test_hindi_label_synonym` and `test_markdown_wrapped_label` are regression tests for
  exactly those two drift patterns observed on real output.

- **`TestParseStage3Output`** — confirms the priority order itself: JSON first, labeled-text
  fallback second, `None` only if neither works. This is the function that actually decides
  which parser "wins," so it's tested independently of either parser's internals.

- **`TestSkipRegex`** — `_SKIP_RE` is the regex parsing Stage 1's `SKIP: yes`/`SKIP: no` line.
  Case-insensitivity and tolerance of surrounding text matter because the model is asked for an
  exact format but doesn't always deliver it byte-for-byte.

---

## `tests/test_entity_cache.py` — TTL and ambiguous-entity invariants (14 tests)

- **`TestNormalize`** — the cache key function. Confirms punctuation stripping and
  lowercasing, and — worth calling out explicitly — that it *keeps* the space between words
  (`"bolt graphics"`, not `"boltgraphics"`). Two tests initially failed here because I assumed
  the wrong key format; fixing the test (not the code) confirmed the actual behavior was
  correct all along.
- **`TestIsFresh`** — the 45-day TTL boundary. Tests both sides (10 days = fresh, 46 days =
  stale) plus a malformed-date record, since `_is_fresh` is written to fail safe (treat garbage
  as stale) rather than raise.
- **`TestGetEntity`** — the branching logic between unambiguous entities (flat record, TTL
  check only) and ambiguous ones (must match a `sense_label` AND be fresh). The ambiguous cases
  are the ones actually worth testing carefully: a sense that matches the label but is stale
  must still return `None`, not the stale data.
- **`TestSetEntity`** — the single most important invariant in this file:
  `test_ambiguous_entity_never_overwrites_existing_sense`. The cache is designed so an already-
  resolved sense is permanent — if this regressed, a wrong disambiguation could silently
  overwrite a correct one already stored under the same label.

---

## `tests/test_translate.py` — proper-noun detection for the fallback translator (19 tests)

This code only runs when Sarvam synthesis fails outright (the `translate_item` fallback path),
annotating machine-translated Hindi text with the original English brand name in parentheses.

- **`TestIsStrongCandidate`** — the heuristic deciding whether a capitalized word is confidently
  a proper noun (multi-word, has a digit, all-caps, or CamelCase) versus just a word that
  happens to be capitalized (sentence-initial "The"). Each branch of the heuristic gets its own
  test so a future tweak to one condition can't silently break another.
- **`TestHasNonInitialOccurrence`** — the secondary signal for weaker candidates: a Title-Case
  word that *only* ever appears at the very start of the text or right after a period is
  probably just normal sentence-initial capitalization, not a real proper noun.
- **`TestExtractProperNouns`** — the combined pipeline: confirms real names get through, confirms
  a stopword-only match like "The" gets fully dropped (not returned as garbage), confirms no
  duplicates.
- **`TestFindTokenSpan`** — this exists specifically because a plain substring search would be
  wrong: `test_does_not_match_substring_inside_longer_token` confirms `"AI"` doesn't falsely
  match inside the token `"OpenAI"` — a naive `str.find` would corrupt the sentence by inserting
  the annotation in the middle of an unrelated word.
- **`TestAnnotateProperNouns`** — the end-to-end behavior using a fake translator (no real API
  calls): annotation gets inserted correctly, already-annotated names are skipped, and a name
  that transliterates to itself (`"NASA"` → `"NASA"`) doesn't get a redundant `(NASA) (NASA)`.

---

## `tests/test_github_collector.py` — pre-LLM content filters (11 tests)

These are the deterministic, free filters that run *before* any LLM call — added after a job-
listing repo (an internship tracker) sailed straight through to Stage 1 with no gate at all.

- **`TestLooksLikeListicle`** — requires *both* a year stamp and a marketing word
  ("Ultimate Guide 2026") to flag as SEO-listicle spam; tests confirm neither signal alone is
  enough, since either one alone is too common in legitimate repo names.
- **`TestIsDenied`** — one test per filter layer (denylist term, job-listing term, listicle
  pattern, star floor) plus a "legitimate repo passes" control case and a "missing description
  doesn't crash" edge case, since `item.get("description")` can be `None` from the GitHub API.

---

## `tests/test_github_gate.py` — the LLM editorial judgment gate (7 tests)

- **`test_keeps_only_approved_items`** — the core verdict-parsing behavior.
- **`test_fails_open_when_call_returns_none`** — a deliberate design choice: if the judgment
  call fails outright, every item is kept rather than dropped. This test exists to protect that
  choice specifically — an API outage should never silently empty the GitHub feed.
- **`test_missing_verdict_defaults_to_kept`** — mirrors the fail-open philosophy at the
  per-item level: if the model's response omits a verdict line for one repo, that repo is kept,
  not discarded.
- **`test_tolerant_of_period_and_paren_separators`** — the model doesn't always use the exact
  `<number>: APPROVE` format requested; this confirms `1)`, `1.`, and lowercase `reject` all
  still parse.
- **`TestFilterNotableRepos`** — confirms the batching itself (`_BATCH_SIZE = 15`) actually
  splits a large input into multiple calls rather than sending one oversized prompt.

---

## `tests/test_search.py` — search-tier fallback and boilerplate filtering (10 tests)

**Why DDG correctness matters more than it might look like from the code**: `IDENTITY_TIERS`
is `[_ddg_search]` alone, and `_ddg_search` is also the second (last-resort) tier in
`CONTEXT_TIERS`. This isn't just "DDG is one option among several" — it's the tier the pipeline
actually depends on. Tavily was the paid alternative earlier in this project's life, and it's
been unusable since its free-tier quota filled up (it requires payment to raise, which hasn't
happened) — that's why it was dropped from the codebase entirely rather than kept as a paper
fallback (see `CLAUDE.md`, "Other APIs"). Google News RSS covers context/recency queries, but
every identity query and every context query that RSS doesn't answer lands on DDG. If DDG's
relevance filtering or domain exclusion silently broke, there's no paid tier left to fall back
to — which is exactly why `TestDdgSearch` exists as a real regression suite and not a
nice-to-have.

- **`TestScrapePage`** — `test_filters_boilerplate_paragraphs` is a direct regression test for
  a real bug: sites like Yahoo Finance return their cookie-consent banner as the first
  paragraphs when the primary CSS selector finds nothing and it falls back to every `<p>` on
  the page. Uses a fake `requests.get` response — no real network call.
- **`TestDdgSearch`** — `test_excludes_wikipedia_domains` locks in a deliberate, explicit
  product decision (see `CLAUDE.md`): Wikipedia is not used as a source in this project in any
  form, and DDG's own organic ranking can surface it regardless of the dedicated tier being
  removed. The fake `DDGS` class (installed via `monkeypatch.setitem(sys.modules, ...)`) means
  this test locks in the filtering behavior without ever hitting the real DDG service.
- **`TestSearchWeb`** — the tier-fallback contract itself: try each tier in order, stop at the
  first one that returns non-empty text, and don't call later tiers unnecessarily
  (`test_stops_at_first_tier_with_content` asserts on the call order, not just the result).

---

## `tests/test_pipeline.py` — article ID hashing (3 tests)

Small and deliberately so — `_article_id` is a one-line SHA1 hash, but it's the thing dedup
correctness depends on throughout `pipeline.py` (`seen_ids`, `sources[]` matching). Confirms
determinism, uniqueness across different URLs, and the expected 12-hex-char format.

---

## CI wiring (`.github/workflows/tests.yml`)

Runs `pytest -q` on every push and pull request, using `requirements-dev.txt` (which extends
`requirements.txt` with `pytest` — kept separate so the production dependency list used by the
daily pipeline run stays lean). Worth being precise about what this does and doesn't buy:

- It does **not** gate merges — this repo pushes directly to `main` with no required-status-
  check branch protection, so a failing test doesn't block anything from landing.
- What it does provide: a safety net for the case where a local test run is forgotten before
  pushing, and a visible, automatic signal (separate from the daily pipeline's own workflow)
  that the test suite exists and passes — a real artifact a reviewer can check without cloning
  the repo and running anything themselves.
