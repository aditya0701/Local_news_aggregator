# TechDrishti (टेकदृष्टि) — Automated Hindi Tech Journalism Pipeline

## Build Specification for Implementation

This document specifies a daily-run pipeline that converts English tech news
(RSS feeds) and GitHub trending repos into original, intellectually substantial
Hindi articles. Implement this as plain Python — no LangChain/LlamaIndex, no
vector database, no GPU requirement. Every design decision below was chosen
deliberately for a small, cheap, daily-batch project; do not add complexity
beyond what's specified without a concrete reason to.

---

## 1. Editorial Identity (constant across all prompts)

- Publication: टेकदृष्टि (TechDrishti) — Hindi science & tech publication
- Readers: educated, curious Hindi-speaking tech enthusiasts
- Voice: शांत, स्पष्ट, भरोसेमंद (calm, clear, trustworthy) — no hype, no clickbait
- Topics: AI, science, technology, Indian languages, education
- Philosophy: every tech event is a story with history, present tension, and
  future trajectory — not a translated press release
- Tone rule: the goal is to make the READER feel smarter, not to make the
  WRITER sound smart. No heavy tatsam vocabulary, no English-calque sentence
  structure. Clean, modern, direct Hindi.

---

## 2. Architecture Overview

```
[RSS feed item: title + summary + source_url]
        |
        v
[STAGE 0] Scrape source_url -> raw source text (free, always attempted first)
        |
        v
[STAGE 1] Sarvam 30B call: extract search queries + entities  (JSON out)
        |
        v
[CACHE CHECK] normalize each entity -> lookup in entity_cache.json
        |              |
     HIT (fresh)    MISS or STALE
        |              |
        |              v
        |      [SEARCH] swappable search_web() function
        |              |
        |              v
        |      [CACHE WRITE] compress result -> store in entity_cache.json
        |              |
        v              v
   [merge cached + freshly-searched entity context]
        |
        v
[STAGE 2] Sarvam 105B call: build editorial strategy  (JSON out, short)
        |
        v
[STAGE 3] Sarvam 105B call: write final Hindi article  (PLAIN LABELED TEXT out)
        |
        v
[Parse labeled text into fields] -> publish + commit article + commit
                                     updated entity_cache.json back to repo
```

Any stage that fails must be caught, logged, and must not crash the run.
Top-level pipeline function returns `None` on unrecoverable failure so the
caller can fall back to plain translation.

---

## 3. Component Specifications

### 3.1 Stage 0 — Source Scrape

- Input: `source_url`
- Fetch the RSS item's own article page, extract main body text via
  BeautifulSoup (generic selectors: `article p, main p`, fallback to all
  `<p>` tags).
- Cap extracted text at a few thousand characters — this is raw material for
  Stage 1, not final content.
- On failure (timeout, 404, blocked): return empty string, do not raise.
  Stage 1 can still work from title + RSS summary alone if this fails.

### 3.2 Stage 1 — Query & Entity Extraction

- **Model: Sarvam 30B** (fast, cheap, mechanical task — no deep reasoning needed)
- **System message:** use `/no_think` — this is pure extraction, not writing.
- **Input:** article title, RSS summary, scraped source text (all three,
  whatever is available).
- **Output: JSON only.**

```json
{
  "search_queries": ["query 1", "query 2", "..."],
  "entities": [
    {"name": "RocketLab", "type": "company", "ambiguous": false},
    {"name": "Iridium", "type": "material", "ambiguous": true,
     "resolved_sense": "chemical element — article discusses material supply,
                         not the satellite company"}
  ]
}
```

- **Critical disambiguation rule:** for any entity flagged `ambiguous`, the
  model must resolve WHICH sense applies in *this specific article*, using
  the article's own surrounding context (co-occurring words, what's actually
  being discussed) — not just flag that ambiguity exists. A name-only cache
  has no way to know which sense a new article means; only the article's own
  text can determine that, and Stage 1 is the one place in the pipeline that
  already has that text in front of it. `resolved_sense` must be a plain
  description of which meaning applies and why (a short context justification),
  not just a label.
- This resolved sense flows forward into: (a) the cache lookup/write key for
  this entity (see 3.3 — cache is sense-aware, not name-only), (b) the search
  query if this sense misses the cache (query must be sense-qualified, e.g.
  "iridium element material supply," never the bare ambiguous name — an
  unqualified search on an ambiguous name returns mixed/wrong-sense results),
  and (c) Stage 3's disambiguation handling in the final article.

- **Division of labor — do not let the model phrase entity-identity lookups:**
  - `entities`: the model simply lists every named entity that appears — no
    cap needed, this costs nothing until a cache miss triggers an actual
    search. For any cache miss, **code** (not the model) programmatically
    builds the lookup query (e.g. `"{entity} company OR technology"`) — this
    is mechanical and doesn't need the model's judgment.
  - `search_queries`: reserved for things that are NOT simple entity
    identity — comparisons, market context, "why now" background. **Cap at
    3.** Instruct the model explicitly not to generate a query whose only
    purpose is defining what an entity is (e.g. "what is Iridium") — that's
    already covered by the entities list + cache path.
  - This split keeps the model's job scoped to judgment calls it's actually
    suited for, and keeps real search-call volume low: 3 context queries +
    however many entities miss the cache (typically 0-3 once warm).
- Flag `type: "ambiguous"` for any entity that could plausibly mean two
  different things (a company vs. a material, a product vs. a person's name,
  etc.) — this flag is what triggers the disambiguation path downstream.

### 3.3 Entity Cache

- **File:** `data/entity_cache.json` — single flat JSON file, not SQLite.
  Reason: this repo commits daily via GitHub Actions; JSON diffs are
  human-readable in git history, SQLite diffs are not.
- **Critical constraint:** GitHub Actions runners are ephemeral. The cache
  file MUST be read at the start of each run and committed back to the repo
  at the end of each run, or caching provides zero benefit (every run starts
  blank otherwise).

**Key normalization (apply before every lookup or write):**
lowercase, strip whitespace, strip punctuation. Never use raw entity strings
as keys directly.

**Record schema:**

For unambiguous entities (the common case — names with only one meaning),
one flat record per normalized key, as before:

```json
{
  "rocketlab": {
    "canonical_name": "RocketLab",
    "entity_type": "company",
    "summary": "A launch and space systems company...",
    "last_updated": "2026-06-15"
  }
}
```

**For entities Stage 1 flags as `ambiguous`, store a LIST of senses under
the same key, never a single flat value.** A name-only cache cannot safely
hold two unrelated identities (a company and a chemical element) under one
key without one silently overwriting the other — and if that happens, a
future article about the other sense would get served the wrong identity
with no way to catch it.

```json
{
  "iridium": {
    "senses": [
      {"sense_label": "company", "summary": "A satellite communications company.",
       "last_updated": "2026-06-20"},
      {"sense_label": "element", "summary": "A rare, dense, corrosion-resistant
       metal used in aerospace alloys and electronics.",
       "last_updated": "2026-06-29"}
    ]
  }
}
```

- **Read path (unambiguous):** normalize key, check cache. If present and
  `last_updated` is within the freshness window (default: 45-60 days flat
  TTL, same for all entity types — do not build per-type TTL logic yet), use
  the cached record directly and skip search.
- **Read path (ambiguous):** use Stage 1's `resolved_sense` for this article
  to find the matching entry in `senses`. If that specific sense exists and
  is fresh, use it — skip search. If only a *different* sense is cached
  (e.g. cache has "company" but this article resolved to "element"), that
  counts as a miss for this sense specifically — search using a
  sense-qualified query and add a new sense entry; never overwrite the
  existing sense.
- **Write path:** compress fresh search results down to this schema before
  storing — never store raw scraped text. For unambiguous entities, overwrite
  the existing record. For ambiguous entities, append the new sense to the
  `senses` list rather than replacing the whole record. Every individual
  summary should stay roughly one paragraph, permanently.
- **Failure handling:** if search fails for an entity, write nothing to the
  cache for it. A failed lookup is not an identity record.
- Keep this file completely separate from any future "published article
  archive" store — different purpose, do not merge.

### 3.4 Search Layer (swappable, decided)

- Interface: `search_web(queries: list[str]) -> dict[str, str]` (query ->
  extracted context text).
- **Primary backend: Tavily API.** Free tier = 1,000 API credits/month, no
  card required, 1 credit per basic search. Returns already-extracted page
  content directly — no separate BeautifulSoup scrape step needed for search
  results (this only removes the scrape step for *search* results; Stage 0's
  source-article scrape is unrelated and still needed).
  - At 3-5 queries/article × 1 article/day, monthly usage is ~90-150 credits
    before caching, dropping further once the entity cache is warm. This
    comfortably fits the free tier.
- **Second-tier backend: Exa API.** Used only when Tavily fails for a given
  query. Free tier reported around 1,000 requests/month by most current
  sources, though at least one source describes it as a one-time trial
  credit rather than an ongoing monthly allowance — confirm at signup.
  Functionally overlaps with Tavily (both return AI-ready extracted content);
  Exa's semantic/neural-search strength isn't specifically needed for this
  pipeline's entity/context lookups, so its value here is redundancy against
  Tavily-specific failure (quota exhaustion, outage, bad key), not a new
  capability.
- **Third-tier / final fallback: `duckduckgo-search` library.** Free, no key,
  used only if both Tavily and Exa fail. Since it's a last resort and not a
  primary path, its known reliability issues (unofficial scraping, shared-IP
  rate-limiting risk on cloud runners) matter far less here. **Output-richness
  caveat:** DDG's result field is a plain search-snippet (one-liner), not
  extracted page content like Tavily/Exa return — only in this fallback
  branch, additionally scrape the top result's own page via BeautifulSoup to
  bring context richness up to par before merging it in.
- **Do not use a self-hosted or public SearXNG instance.** Public instances
  rate-limit API-style usage in practice (they inherit CAPTCHA/blocking from
  the upstream engines they proxy). Self-hosting avoids that but requires
  running a persistent server — which conflicts with this pipeline's
  ephemeral, once-a-day GitHub Actions runner model. Not worth the added
  infrastructure for what Tavily's free tier already covers.
- Only call this function for entities/queries that missed the cache — never
  search cache hits.
- Every call wrapped in try/except at both the Tavily and fallback level;
  total failure for a given query returns no context for that query rather
  than raising.

### 3.5 Stage 2 — Editorial Strategy

- **Model: Sarvam 105B** (quality-sensitive reasoning step — bigger context
  window matters here since this call ingests all research at once)
- **Input:** source text (Stage 0) + entity list with cached/freshly-searched
  context (Stage 3.3/3.4 merged) + article title/summary.
- **Output: JSON only, short fields — no long prose in this call.**

```json
{
  "core_narrative": "the real story and underlying tension, one paragraph",
  "key_facts_and_quotes": "the facts/figures/statements that must appear",
  "disambiguation_targets": "which terms need inline explanation and how",
  "category": "acquisition | model_release | ban_regulation | repo_analysis | general",
  "planned_length": "short guidance, e.g. '4 paragraphs, moderate complexity'"
}
```

- This is a planning/decision step, not a writing step — keep every field
  short. This is what makes the JSON safe to generate reliably: nothing in
  this schema is long-form prose.
- `category` here determines how Stage 3's STRATEGIC_ANALYSIS section should
  be framed (see 3.6) — it does not require a separate prompt template per
  category, just a conditional instruction line in Stage 3's prompt.

### 3.6 Stage 3 — Final Article Writing

- **Model: Sarvam 105B**
- **Input:** Stage 2's strategy JSON + the same facts/context Stage 2 saw.
- **Output: PLAIN LABELED TEXT — explicitly NOT JSON.** Long free-flowing
  Hindi prose inside JSON string fields is a known failure point (quote
  escaping, truncation) even at 105B scale. Labeled plain text parses just as
  easily and removes this risk entirely.

Required output shape:

```
TITLE: <one-line, insightful Hindi headline>
CONCEPT_BOX: <standalone 1-3 sentence explainer of the single hardest
              concept/name in this article, for a newcomer>
LEDE: <opening — break the news, set tone, introduce entities>
DEEP_DIVE_AND_CONTEXT: <core of the article — mechanics, numbers, who said
                        what, woven into narrative prose>
STRATEGIC_ANALYSIS: <connect to the broader ecosystem; see category framing
                     below>
CONCLUSION_AND_SIGNIFICANCE: <what this means for the reader/developer;
                              strong closing line>
```

**Category-conditioned instruction for STRATEGIC_ANALYSIS** (insert the
matching line into the prompt based on Stage 2's `category` field):

- `acquisition`: frame as probable/possible market impact — always hedge
  with "हो सकता है" / "संभावना है," never state predictions as fact.
- `model_release`: translate benchmark numbers into what they mean in
  practice, not just what the number is.
- `ban_regulation`: separate immediate impact from broader, more speculative
  implications; hedge the latter explicitly.
- `repo_analysis`: explain real-world developer/industry impact using a
  simple analogy for the core technical mechanism.
- `general`: no special framing beyond the base instructions.

**Rules applied inline in this prompt, all carried over from earlier design:**

- Never let an ambiguous or unfamiliar term pass without a seamless inline
  explanation (the "Iridium rule") — even if `disambiguation_targets` from
  Stage 2 didn't flag it, the CONCEPT_BOX + inline treatment are a second
  safety net.
- Transliteration rule: Hindi transliteration + original English in
  parentheses — e.g., "ओपन सोर्स (Open Source)."
- Do not translate or closely paraphrase source sentences — write fresh
  sentences based only on the extracted facts.
- Do not add any fact not present in the sources/research provided.
- If a fact is unclear or sources disagree, omit it — do not guess.
- Length: not hard-capped, but default to a soft anchor of roughly 3-6 well
  developed paragraphs unless the story's genuine complexity (per Stage 2's
  `planned_length`) warrants more. Avoid pure open-ended "unbounded length"
  in production — it produces inconsistent results across a daily corpus.

---

## 4. Error Handling (applies to every stage)

- Wrap every external call (scrape, search, Sarvam API call) in try/except.
- No stage failure should raise an unhandled exception up to the top level.
- Top-level pipeline function signature (conceptually):
  `run_pipeline(article) -> dict | None`
  Returns `None` on total/unrecoverable failure so the calling code can fall
  back to plain machine translation instead of publishing nothing.
- A failed cache write is silently skipped (see 3.3), not stored as an error
  record.

---

## 5. Infrastructure / Deployment

- **Runtime:** GitHub Actions scheduled workflow on a **public** repository.
  Standard Ubuntu runners are free and unlimited on public repos (6-hour max
  per individual job run — nowhere close to what this pipeline needs).
- Each run must: read `entity_cache.json`, run the pipeline, write the new
  article file, write the updated `entity_cache.json`, and **commit both back
  to the repo** — this is what makes the cache persist across ephemeral runs.
- Note: GitHub auto-disables scheduled workflows in public repos after 60
  days of no repository activity. Since every successful run commits new
  content, this should not trigger under normal daily operation — but if the
  pipeline silently stops running for an extended stretch, check this first.
- No database server, no VPS, no GPU required anywhere in this design.
- Publishing surface (if a live site is wanted): a static site (GitHub Pages
  or Netlify free tier), rebuilt from the committed article files on each run.

---

## 6. Explicitly Out of Scope for This Version

Do not build these now — they are deliberate future additions, not oversights:

- **Vector-embedding RAG over a published-article archive** (semantic
  "have we covered something like this before" search). Not needed until
  there's a meaningful archive to search, and even then, brute-force numpy
  cosine similarity is sufficient until article count reaches the tens of
  thousands — no FAISS/Chroma/vector DB needed at that point either, unless a
  live, concurrent-user-facing search feature is added to the public site.
- **LangChain / LlamaIndex** or any other orchestration framework — the
  3-call structure here is simple enough that a framework adds abstraction
  overhead without saving meaningful engineering effort, and makes debugging
  Sarvam's small-model quirks harder, not easier.
- **Per-entity-type cache TTLs** — start with one flat TTL for everything;
  refine only if a specific category is observed going stale awkwardly fast.
- **True agentic function-calling** (letting Sarvam itself decide when/what
  to search in a live loop) — this task is a fixed, bounded "give me 3-5
  queries, then stop," not an open-ended search loop, so the simpler
  orchestrated (non-agentic) design is the correct fit, not a compromise.