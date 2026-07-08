import json
import os
import re

import requests
from deep_translator import GoogleTranslator
from langdetect import LangDetectException, detect

from writer.concise_search import ask_concise, build_identity_question, concise_configured, looks_like_not_found
from writer.entity_cache import get_entity, load_cache, save_cache, set_entity
from writer.search import CONTEXT_TIERS, IDENTITY_TIERS, build_identity_query, search_web
from writer.web_context import fetch_page

SARVAM_URL = "https://api.sarvam.ai/v1/chat/completions"
_MODEL_FAST = os.environ.get("SARVAM_MODEL", "sarvam-30b")
_MODEL_QUALITY = os.environ.get("SARVAM_MODEL_QUALITY", "sarvam-105b")

# Ollama fallback config (used when SARVAM_API_KEY is not set)
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b-instruct-q4_K_M"

_CATEGORY_FRAMING = {
    "acquisition": (
        "frame as probable/possible market impact — always hedge with "
        "हो सकता है / संभावना है, never state predictions as fact"
    ),
    "model_release": (
        "translate benchmark numbers into what they mean in practice, "
        "not just what the number is"
    ),
    "ban_regulation": (
        "separate immediate impact from broader, more speculative implications; "
        "hedge the latter explicitly"
    ),
    "repo_analysis": (
        "explain real-world developer/industry impact using a simple analogy "
        "for the core technical mechanism"
    ),
    "general": "no special framing beyond the base instructions",
}

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# Stage 1 is three separate calls, not one. Tried as a single combined call
# first (skip + entities + queries all at once) — that call would
# intermittently burn its whole token budget on hidden reasoning and return
# nothing at all (confirmed: 3 identical runs, 3 different outcomes). Splitting
# into 3 focused calls, each with reasoning_effort=None and a format designed
# to make the model "reason" via the visible output order (not a hidden
# channel), fixed every failure mode found — see stage1-design.md for the full
# test history and rejected alternatives.

# Step 1 — skip gate. REASON is asked for before SKIP deliberately: with
# reasoning off, the model writes strictly in the order requested, so asking
# for the verdict first meant it committed to an answer before it had any
# reasoning to base it on (tested: 0/5 correct on a real article this way,
# with the model's own stated reason contradicting its verdict). Reversing
# the order fixed it completely (10/10 correct across 2 real cases).
_STAGE1_SKIP_PROMPT = """Research assistant for टेकदृष्टि (TechDrishti), a Hindi tech publication.
Decide if the article below is genuine tech news suitable for publication, or should be skipped.

Title: {title}
Summary: {summary}
Source: {source_text}

Skip ONLY if this is:
- A job/career posting (even if about the tech sector)
- A product marketplace, directory, or "Show HN" website showcase with no news event
- A personal blog, tutorial, or documentation page — not a news article
- Content with no identifiable tech news event (announcement, launch, acquisition, regulation, research)
Do NOT skip articles ABOUT job market trends, hiring booms/crises, or industry-level employment analysis — those are real news.

Respond in EXACTLY this format, with no other text before or after — REASON comes first,
decide your verdict only after stating the reason:
REASON: <one sentence stating what the core crux of this article is>
SKIP: yes or no

"SKIP: yes" means discard this article. "SKIP: no" means keep it, it is genuine news."""

_SKIP_RE = re.compile(r"SKIP:\s*(yes|no)", re.IGNORECASE)
_SKIP_REASON_RE = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)


# Step 2 — entity + gap analysis. Only runs if Step 1 kept the article.
# Structured as TYPE -> CHECKLIST -> COVERAGE CHECK -> GAP1/2/3, mirroring the
# reason-before-verdict trick: forcing the model to commit to a story TYPE
# first gives it a category-specific checklist to check the article against,
# rather than open-endedly guessing what might be missing (the "chicken and
# egg" problem — the model can't know the finished narrative in advance, but
# it CAN check a fixed checklist against what the article already states).
#
# GAP1/GAP2/GAP3 are fixed, numbered slots, not an open list — this was a
# second, separate bug from the reasoning-starvation one: even with reasoning
# off, an earlier open-ended "QUERIES:" list caused the model to fall into a
# runaway repetition loop (one run produced 300+ near-identical lines before
# hitting the token cap). Numbered slots plus max_tokens=600 (below) bound
# this: tested across 4 real, diverse articles (an acquisition, a macro
# investment story, a dense AI-model release, a GitHub repo) — the repetition
# bug still recurred once out of 12 runs on the densest article, so the
# max_tokens cap is a required backstop, not just the prompt wording.
_STAGE1_ANALYSIS_PROMPT = """You are a researcher supporting a narrative editor at टेकदृष्टि (TechDrishti), a
Hindi tech publication. This article has already been confirmed as genuine tech news. Your job
is to find what's MISSING that the editor will need -- not to write the analysis yourself.

Think in this order:

1. TYPE -- classify what kind of tech story this is:
   - model_release: a new AI model, product, hardware, or tech launch
   - acquisition: M&A, funding, business deal, partnership
   - ban_regulation: policy/export/regulatory action
   - repo_analysis: an open-source project/tool/framework
   - general: none of the above

2. CHECKLIST -- based on that type, here is what a good analysis piece needs:
   - model_release: how the new tech's claims compare to existing competitors/alternatives;
     what is genuinely novel vs incremental; pricing/cost if relevant; what the underlying
     technology/domain even IS if it's specialized or unfamiliar to a general reader; who it's for
   - acquisition: competitive landscape of the space; what problem/gap this deal fills;
     background on the companies involved
   - ban_regulation: immediate vs long-term implications; who is directly affected
   - repo_analysis: comparison to existing tools/frameworks; real-world impact
   - general: no special checklist beyond entities

3. COVERAGE CHECK -- for each checklist item for this TYPE, decide: does the article ALREADY
   cover it, or is it a GAP the editor will need filled in from elsewhere?

4. QUERIES -- GAP1/GAP2/GAP3 are a MAXIMUM of 3 slots, NOT a required minimum and NOT a target
   to hit. Only fill a slot if there is a genuine, real gap for that specific article. If the
   article already covers everything the checklist calls for, leave ALL slots as "none" -- do
   not invent a question just to fill a slot. It is normal and expected for some or all slots
   to be "none".

   These queries go to a real autonomous research agent that can genuinely investigate a
   question -- read multiple sources, compare claims, and reason about them -- not just
   keyword-match a snippet the way an old-style search engine would. So a GAP query does NOT
   need to be watered down into a bare fact-lookup: an interpretive, comparative, or "why does
   this matter" question is fine. Ask for whatever the editor actually needs to know, in its
   sharpest, most useful form.

   The only thing still off-limits is pure future speculation that nobody -- not even a real
   researcher -- could answer today (e.g. "what will happen to this company next year", "will
   this technology replace X"). If a gap is genuinely that kind of unknowable prediction, either
   rephrase it to ask for the present-day facts/context that inform such a prediction, or leave
   the slot "none".

   GOOD (interpretive/comparative -- fine for a real research agent):
   "How does this attack's automation compare to previously documented AI-assisted cyberattacks,
   and what does that suggest about the state of autonomous cybercrime?"
   GOOD (a single entity's own background, still useful):
   "What is Langflow, and why has CVE-2025-3248 been considered a serious vulnerability in it?"
   STILL AVOID (unknowable prediction, not even a real researcher could answer this):
   "Will AI-driven ransomware attacks become the dominant form of cybercrime by 2028?"

   HARD RULE, no exceptions: each GAP query is sent to the research agent completely on its own,
   as a fresh standalone question -- it does NOT see this article, does NOT see the other GAP
   queries, and has no memory of anything you wrote above. Never refer to "this vulnerability",
   "this model", "this attack", "the tool", "it", or any other pronoun/vague reference that only
   makes sense to someone who already read GAP1 or the article -- a reader (or research agent)
   seeing ONLY that one query, with nothing else, must be able to tell exactly what it's about.
   Always spell out the actual name every time, even if that means repeating a name already used
   in an earlier GAP line.
   WRONG (GAP2 only makes sense if you already know GAP1's subject):
   GAP1: "What is GitLost and why is it considered a critical vulnerability?"
   GAP2: "How does this vulnerability compare to previously disclosed supply-chain attacks?"
   CORRECT (each line names its subject on its own):
   GAP1: "What is GitLost and why is it considered a critical vulnerability?"
   GAP2: "How does the GitLost vulnerability compare to previously disclosed supply-chain attacks?"

   HARD RULE, no exceptions: every named competitor/model/company that appears in a GAP query
   MUST already be present, character-for-character, in the ENTITIES list you just wrote above
   in this same response, or in the Source text below. Before finalizing any GAP line, re-check
   it against your own ENTITIES line: if a name in the query is not in that list, delete that
   name from the query -- do not substitute a different name from memory, even one you are
   confident is a real, currently-relevant product, and even one in the same category. You do
   not have live knowledge of which product is still the current or correct point of comparison
   today. If, after removing any unlisted name, the query would have nothing left to compare
   against, do not name any competitor at all -- ask about the article's own entity alone, or
   set that slot to "none" if there is nothing left to ask.

Respond in EXACTLY this format, in this order, with no other text before or after:
TYPE: <model_release, acquisition, ban_regulation, repo_analysis, or general>
ENTITIES: <comma-separated NAMED entities only, format "Name (type)". Types: company, startup,
ai_model, product, person, researcher, technology, protocol, regulation, event, organization.
If an entity name is ambiguous on its own, append " [ambiguous: <which sense applies here>]".>
GAP1: <query for first real gap, or "none">
GAP2: <query for second real gap, or "none">
GAP3: <query for third real gap, or "none">

Title: {title}
Summary: {summary}
Source: {source_text}"""

_STAGE1_ANALYSIS_MAX_TOKENS = 600

_MAX_GAP_SLOTS = 3
_GAP_LINE_RE = re.compile(r"^GAP(\d+):", re.MULTILINE)


def _cap_gap_lines(analysis: str, max_gaps: int = _MAX_GAP_SLOTS) -> str:
    """Hard code-level cap on GAP slots — a backstop, not just prompt wording.

    The prompt already asks for GAP1/GAP2/GAP3 as a maximum, and `max_tokens`
    already bounds the worst case, but the model has still been observed
    (documented in prompt-design-thought-process.md) to occasionally ignore
    the 3-slot instruction and keep generating GAP4, GAP5, GAP6... in a
    runaway repetition loop (300+ lines in one test run, 15 queries generated
    instead of ≤3 in the first live production run). Rather than trust the
    prompt or token cap alone, this truncates the raw analysis text the
    instant a GAP line beyond `max_gaps` appears — Step 3 (JSON extraction)
    never even sees the runaway tail, so it can't accidentally transcribe
    GAP4/GAP5/... as if they were legitimate. The existing downstream
    `search_queries[:3]` truncation in `_synthesize_sarvam` is a second,
    independent safety net at the query-usage level; this one acts earlier,
    at the analysis-text level, before Step 3 even runs.
    """
    matches = list(_GAP_LINE_RE.finditer(analysis))
    over_limit = [m for m in matches if int(m.group(1)) > max_gaps]
    if not over_limit:
        return analysis
    return analysis[: over_limit[0].start()].rstrip()


# Hard code-level backstop for a second, distinct hallucination bug — a
# comparison-shaped GAP query naming a competitor the article never actually
# mentioned. Prompt wording alone was tried twice and failed both times,
# confirmed live: the model repeatedly named "GPT-4 Turbo"/"Claude 3 Opus" as
# comparison targets for Leanstral 1.5 (a 2026 model) even when explicitly
# told not to invent unlisted names — the exact same "prompt wording isn't
# enough" lesson _cap_gap_lines() above already exists for. This filter
# doesn't try to guess which names are hallucinated (that's not generally
# knowable); it only checks whether a comparison query's named entities
# already appear somewhere in the article's own material, mirroring the
# existing "do not invent anything not already in the write-up" principle
# used elsewhere in this pipeline, enforced here in code instead of trusted
# to the model's compliance.
_COMPARISON_MARKER_RE = re.compile(r"\bvs\.?\b|\bversus\b|\bcompared? to\b|\bcomparison\b", re.IGNORECASE)
_CONNECTOR_WORDS = {
    "a", "an", "the", "of", "on", "in", "for", "with", "and", "or", "to", "by",
    "from", "at", "per", "beyond", "other", "existing", "some", "similarly-named",
}
# Generic analysis-noun vocabulary that's often the sentence-initial word of
# a GAP query ("Comparison of...", "Details on...") — capitalized only
# because it starts the sentence, not because it names a real product/company.
# Confirmed live this caused a false positive (a fine, non-hallucinating query
# — "Comparison of Leanstral's performance on PutnamBench versus other
# state-of-the-art models" — got dropped for no reason other than its first
# word being capitalized).
_GENERIC_ANALYSIS_WORDS = {
    "comparison", "comparisons", "details", "detail", "impact", "impacts",
    "analysis", "overview", "explanation", "discussion", "review",
    "differences", "similarities", "significance", "implications",
    "advantages", "disadvantages",
}

# Hyphenated generic-modifier compounds ("AI-assisted", "cloud-based") read as
# a candidate proper noun purely from being capitalized mid-sentence, the same
# false-positive class _GENERIC_ANALYSIS_WORDS exists for. Confirmed live in
# the relaxed-query experiment (see CLAUDE.md): a legitimate, non-hallucinating
# GAP query — "...compare to previously documented AI-assisted cyberattacks" —
# got dropped because "AI-assisted" doesn't literally appear in the article's
# entities/source text, even though it names no specific competitor at all.
_GENERIC_SUFFIX_RE = re.compile(
    r"^[A-Za-z]+-(assisted|based|driven|powered|enabled|related|style|like|type|specific)$",
    re.IGNORECASE,
)


def _extract_candidate_names(query: str) -> list[str]:
    """Chunk consecutive proper-noun-ish tokens (capitalized, or containing a
    digit — to catch version numbers like "5.2") into candidate name phrases."""
    phrases = []
    current: list[str] = []
    for word in query.split():
        stripped = word.strip(",.;:()")
        # Strip a trailing possessive ("Leanstral's" -> "Leanstral") -- without
        # this, the article's own entity name in possessive form fails the
        # known-entity substring match (confirmed live: "Leanstral's" doesn't
        # contain, and isn't contained in, "Leanstral 1.5", causing a false
        # positive that dropped an otherwise-fine query).
        stripped = re.sub(r"['’]s$", "", stripped)
        is_candidate = bool(stripped) and (
            stripped[0].isupper() or any(c.isdigit() for c in stripped)
        ) and stripped.lower() not in _CONNECTOR_WORDS and stripped.lower() not in _GENERIC_ANALYSIS_WORDS \
            and not _GENERIC_SUFFIX_RE.match(stripped)
        if is_candidate:
            current.append(stripped)
        else:
            if current:
                phrases.append(" ".join(current))
                current = []
    if current:
        phrases.append(" ".join(current))
    return phrases


def _query_names_unlisted_competitor(query: str, entity_names: list[str], source_text: str) -> bool:
    """True if `query` is comparison-shaped AND names something not found
    anywhere in this article's own entities/source text — see module-level
    comment above for why this exists as a code check, not just a prompt rule."""
    if not _COMPARISON_MARKER_RE.search(query):
        return False
    known_lower = {n.lower() for n in entity_names}
    source_lower = (source_text or "").lower()
    for phrase in _extract_candidate_names(query):
        phrase_lower = phrase.lower()
        if len(phrase) < 4:
            continue
        if any(phrase_lower in k or k in phrase_lower for k in known_lower):
            continue
        if phrase_lower in source_lower:
            continue
        return True
    return False


def _drop_hallucinated_comparisons(queries: list[str], entities: list[dict], source_text: str) -> list[str]:
    """Drop any GAP query naming a comparison target the article never
    mentioned, rather than send it to search at all — confirmed live this
    happens even after prompt-only fixes (see comment above)."""
    entity_names = [e.get("name", "") for e in entities if e.get("name")]
    kept = []
    for q in queries:
        if _query_names_unlisted_competitor(q, entity_names, source_text):
            print(f"[stage1] dropping hallucinated-comparison query: {q}")
            continue
        kept.append(q)
    return kept


# Dangling-reference bug (found 2026-07-08, see CLAUDE.md): a GAP query using a pointing word
# ("this", "that", "it" — grammatically these are demonstrative/anaphoric pronouns, the class of
# words that stand in for a noun already named elsewhere rather than naming it again) only makes
# sense within one conversation — but each GAP query is dispatched to the search/research
# backend as an independent, standalone question with no memory of any other GAP query or the
# article (see the HARD RULE added to _STAGE1_ANALYSIS_PROMPT above). Confirmed live on a real
# article ("GitLost: We Tricked GitHub's AI Agent into Leaking Private Repos"): GAP1 named
# GitLost explicitly, but GAP2 said "the vulnerability" and "...how does this differ from a
# standard repository access control bypass?" without ever repeating "GitLost" (or any other
# named entity) anywhere in the query text — so a backend answering GAP2 alone has nothing in
# the query itself for "this"/"the vulnerability" to resolve to.
#
# Deliberately NOT scoped to "pointing word immediately followed by a generic noun" (e.g. "this
# vulnerability") — the real GAP2 case above shows the pointing word can stand alone ("how does
# this differ") with the generic noun phrase ("the vulnerability") appearing separately in the
# same query. What actually distinguishes a fine same-query "this" (referring to something named
# earlier in that SAME query, which is normal English) from a dangling cross-query one is simply
# whether any of this article's own named entities appear ANYWHERE in the query at all.
_POINTING_WORD_RE = re.compile(r"\b(this|that|these|those|it)\b", re.IGNORECASE)


def _entity_referenced_in_query(name: str, query_lower: str) -> bool:
    """True if `query_lower` names `name`, allowing an abbreviated reference to a multi-word
    entity name — not just an exact full-string match.

    Confirmed live this matters: a real GAP query referred to the entity "GitHub Agentic
    Workflows" as just "the Agentic Workflows architecture" — dropping the "GitHub" prefix is a
    completely normal way to refer back to an already-established compound proper noun, not a
    dangling reference, but a naive exact-substring check flagged it as one anyway (a false
    positive caught by the live test in tests/test_live_gap_queries.py, not hypothetical).
    Requires at least half (rounded up) of the name's words to appear together, so a single
    generic word shared with a multi-word name ("Workflows" alone) still isn't enough on its own.

    Floored at 2 words even for a 2-word name (not "half rounded up", which would be 1) — found
    and fixed via tests/test_gap_context_fixtures.py replaying real captured Stage 1 output: a
    real GAP query about "GitHub" contained the plain English word "actions" ("...the sequence
    of actions the agent takes...") which, with the naive half-rounded-up rule, alone satisfied
    the 2-word entity name "GitHub Actions" — a false match on a generic word coincidentally
    overlapping one word of an unrelated compound name, not an actual reference to that entity.
    """
    words = name.lower().split()
    if len(words) <= 1:
        return name.lower() in query_lower if name else False
    min_len = max(2, (len(words) + 1) // 2)
    for length in range(len(words), min_len - 1, -1):
        for start in range(len(words) - length + 1):
            if " ".join(words[start : start + length]) in query_lower:
                return True
    return False


def _query_has_dangling_reference(query: str, entity_names: list[str]) -> bool:
    """True if `query` uses a pointing word ("this", "that", "it", ...) and none of this
    article's own named entities appear anywhere in the query text — meaning the pointing word
    has nothing in the query itself to resolve to once it's sent to a backend with no memory of
    the other GAP lines or the article.

    Prompt-only fix (the HARD RULE in _STAGE1_ANALYSIS_PROMPT) is not yet backed by dropping
    matches here in production the way _drop_hallucinated_comparisons does for its bug — this
    function exists so the prompt fix can be checked with a live test first (see
    tests/test_live_gap_queries.py) before deciding whether a code-level drop is warranted too.
    """
    if not _POINTING_WORD_RE.search(query):
        return False
    query_lower = query.lower()
    return not any(_entity_referenced_in_query(name, query_lower) for name in entity_names if name)


def _prepare_context_queries(queries: list[str], entities: list[dict]) -> list[tuple[str, str]]:
    """Returns (original_query, text_to_dispatch) pairs for GAP/context queries.

    Added 2026-07-08 after a live test (tests/test_live_gap_queries.py) confirmed the prompt-only
    HARD RULE fix in _STAGE1_ANALYSIS_PROMPT does not reliably prevent dangling references — a
    fresh live run reproduced the exact same bug class within minutes of the prompt fix landing
    (a query asking "...that allow it to be triggered by a public issue?" with no named entity
    anywhere in it). An earlier version of this function just dropped such queries outright
    (same fix `_drop_hallucinated_comparisons` above uses for its bug) — but unlike a
    hallucinated name, a dangling "this"/"it" isn't a fabrication to remove, it's a real,
    editorially useful question that's just missing its own antecedent. Stage 1's own checklist
    ordering reliably puts the article's central "what is X" question first (see
    _STAGE1_ANALYSIS_PROMPT), so re-attaching that first query as background context to every
    later dangling one usually supplies the missing name without needing to guess which entity
    it is, and without losing the question. Dropping is now only the last resort, for a query
    that's STILL dangling even with that context attached (i.e. neither it nor the first query
    names any of the article's own entities) — there's no reliable name left to attach at that
    point, and inventing one would be fabricating content, not fixing it.
    """
    if not queries:
        return []
    entity_names = [e.get("name", "") for e in entities if e.get("name")]
    first = queries[0]
    pairs: list[tuple[str, str]] = [(first, first)]
    for q in queries[1:]:
        dispatch_text = q
        if _query_has_dangling_reference(q, entity_names):
            dispatch_text = f"Background context: {first}\n\nQuestion: {q}"
        pairs.append((q, dispatch_text))

    kept = []
    for orig, dispatch_text in pairs:
        if _query_has_dangling_reference(dispatch_text, entity_names):
            print(f"[stage1] dropping dangling-reference query (no usable context to attach): {orig}")
            continue
        if dispatch_text != orig:
            print(f"[stage1] attached GAP1 as context to dangling-reference query: {orig}")
        kept.append((orig, dispatch_text))
    return kept


# Step 3 — transcribe Step 2's analysis into strict JSON. Reasoning off, same
# as everywhere else in this pipeline: no judgment left to make here.
_STAGE1_EXTRACTION_PROMPT = """Below is a researcher's structured write-up about a news article already confirmed as genuine
tech news. Your only job is to transcribe it into JSON -- do not re-analyze, do not invent
anything not already in the write-up.

Researcher write-up:
{analysis}

Build the JSON like this:
- "search_queries": for each GAP line that is NOT "none", add its text as a string in this
  array. Skip any GAP line that says "none" -- do not include it, do not invent a placeholder
  for it. If all three GAP lines say "none", this array must be empty: [].
- "entities": for each item listed after ENTITIES:, add one object
  {{"name": <name, without the type>, "type": <the type>}}. If marked "[ambiguous: ...]", also
  add "ambiguous": true and "resolved_sense": <the text after "ambiguous:">.

Output ONLY the JSON object, nothing else."""

_STAGE2_ANALYSIS_PROMPT = """You are the editorial director of टेकदृष्टि (TechDrishti), a Hindi science and technology publication.

Article Title: {title}
Summary: {summary}

Source Text:
{source_text}

Research Context (entity knowledge + web search results):
{entity_context}

Think through the editorial strategy for this Hindi article and write out your analysis in
plain text (not JSON). Cover exactly these five things, in order:

CORE NARRATIVE: the real story and underlying tension, one sentence.
KEY FACTS AND QUOTES: the facts/figures/statements that must appear in the article.
DISAMBIGUATION TARGETS: which terms need inline Hindi explanation for a newcomer.
CATEGORY: exactly one of acquisition|model_release|ban_regulation|repo_analysis|general.
PARAGRAPH PLAN: a numbered list, 4-6 paragraphs — use as many as the story genuinely has
  substance for (a maximum driven by real content, not a target to hit; a thin story with only
  4 paragraphs' worth of real material should stay at 4, don't pad with restated facts to reach
  6). For each paragraph, write sentences specifying exactly what to say, which
  facts/entities/numbers belong in it, and the tone — enough detail that a writer needs zero
  additional thinking to produce a full sentence paragraph from it. List out the specific
  facts, figures, comparisons, and quotes to use, not just the general theme."""

_STAGE2_EXTRACTION_PROMPT = """Below is a टेकदृष्टि editorial director's finalized analysis for a Hindi tech news article.
Transcribe it into JSON exactly as written — do not re-analyze, re-plan, or invent anything not
already present in the analysis below.

ANALYSIS:
{analysis}

Output ONLY valid JSON with exactly these keys:
{{
  "core_narrative": "<from CORE NARRATIVE above>",
  "key_facts_and_quotes": "<from KEY FACTS AND QUOTES above>",
  "disambiguation_targets": "<from DISAMBIGUATION TARGETS above>",
  "category": "<from CATEGORY above — acquisition|model_release|ban_regulation|repo_analysis|general>",
  "paragraph_plan": [
    "Para 1: <first paragraph's instruction from PARAGRAPH PLAN above>",
    "Para 2: <second paragraph's instruction>"
    ...
  ]
}}

Include exactly as many paragraph_plan entries as the PARAGRAPH PLAN section above lists (4-6) —
no more, no fewer, no invented paragraphs."""

_STAGE3_PROMPT = """You are a Hindi writer for टेकदृष्टि. The editorial team has done all the planning — your ONLY job is to execute the writing plan below exactly as instructed.

DO NOT re-plan, re-think, or restructure. Follow each paragraph instruction below, but NEVER copy the
instruction's own wording into the article — the plan tells you WHAT to cover, not what to literally
write. Turn each instruction into actual flowing Hindi prose that DOES what it says, using the source
facts and entity definitions provided.

You are writing for टेकदृष्टि, a premium Hindi tech publication — readers expect the same depth
and unhurried pacing they'd get from a serious print newspaper, not a quick blog summary. Take
the time each paragraph actually needs; do not rush to wrap a paragraph up in 2 sentences just
because the plan's instruction itself was short. A short instruction still deserves a fully
developed paragraph once you bring in the source facts, entity context, and real-world
implications around it.

Write a COMPREHENSIVE, substantial article, not a short summary — you have a generous token budget for
this, use it. Every body paragraph should be full, multi-sentence Hindi prose that genuinely builds up on
the plan and source facts — not a one-line gist of the paragraph's topic. deep_dive_and_context in particular is the main body of the article:
it should be the longest section, covering every middle paragraph from the plan in real depth.

Each individual middle paragraph inside deep_dive_and_context must be at least 5-7 substantial
sentences on its own — not 2-3. Do not treat the plan's one-line instruction as a cap on length;
expand it: bring in the specific facts/figures/quotes from SOURCE FACTS and ENTITY DEFINITIONS
that support that paragraph's point, explain the mechanism or reasoning behind it, and spell out
why it matters, before moving to the next paragraph. A paragraph that only restates the plan's
instruction in one or two sentences is unacceptably thin for this publication.

CRITICAL — instruction vs. content, do not confuse the two:
WRONG (copies the instruction itself, explains nothing): "उपकरण के पीछे की तकनीक की व्याख्या करें। वर्णन करें कि यह कैसे काम करता है।"
CORRECT (actually executes it): "यह उपकरण परफ्यूजन तकनीक का उपयोग करता है, जो आंख की धमनी के माध्यम से ऑक्सीजन युक्त तरल पहुँचाता है।"
If a sentence you're about to write contains a verb like "करें"/"दें" telling the reader what to do (व्याख्या करें, वर्णन करें, उल्लेख करें, शामिल करें), you are copying the instruction, not writing the article — rewrite it as a direct statement of fact instead.

## Editorial Quality (Very Important)

Write like a senior technology journalist editing for an elite Hindi technology newspaper. Do
NOT merely summarize the plan and source facts — produce a polished, publication-ready news
article that reads as one coherent piece, not a plan stapled to a fact list.

Follow these editorial principles:
- Report facts first; interpretation second.
- Maintain a neutral, evidence-based tone.
- Attribute opinions and judgments to their source (e.g., "कंपनी के अनुसार...", "शोधकर्ताओं का
  कहना है...", "विश्लेषकों के मुताबिक..."). Do not present opinions or predictions as settled fact.
- Never exaggerate capabilities or significance beyond what SOURCE FACTS/ENTITY DEFINITIONS
  actually support.
- Preserve important nuances. If the source material mentions limitations, human involvement,
  uncertainty, or caveats, include them — do not smooth them away for a cleaner narrative.
- Prefer precise statements over dramatic language.
- If the source material's own certainty is hedged ("according to," "researchers believe,"
  "appears to," "may," "suggests"), preserve that same level of certainty in Hindi — do not
  strengthen a hedge into a fact. This is in addition to, not a replacement for, the existing
  हो सकता है / संभावना है hedging rule below.

Writing style:
- Read like a professionally edited newspaper article, not an AI summary or technical
  documentation.
- Use varied sentence structures and natural transitions.
- Avoid repetitive constructions such as "इसके बाद... इसके बाद... इसके बाद..."
- Avoid filler adjectives like "बहुत ही", "बेहद", "चौंकाने वाला", "क्रांतिकारी", unless directly
  supported by the source material.
- Show significance through facts rather than emotional wording.

Paragraph quality:
- Every paragraph should introduce a new idea — if two paragraphs communicate the same idea,
  keep only the stronger one.
- Remove redundant explanations; if a paragraph does not improve the reader's understanding,
  omit it rather than padding for length.
- Every paragraph must answer at least one of: what happened, how it happened, why it matters,
  or what the reader should understand from it.

Technical writing:
- Explain technical concepts only when necessary for understanding the news.
- Do not overload the article with implementation details.
- Retain only details that help explain how something worked or why it matters.

Before finalizing, silently verify: no factual exaggeration; no unsupported conclusions; proper
attribution for all opinions; no repeated ideas; professional newspaper tone throughout; no
uncertainty converted into certainty. Think like an editor, not a researcher — the goal is not
to include every fact available, but to publish the article an experienced technology editor
would approve.

--- WRITING PLAN (describes what each paragraph must cover — an instruction to you, not text to reproduce) ---
Title idea: {title}
Paragraph plan:
{paragraph_plan}

--- SOURCE FACTS (use these, do not invent) ---
{source_text_block}

--- ENTITY DEFINITIONS ---
{entity_context}

Mapping the plan's paragraphs (there may be 4-6) onto the sections below:
- The FIRST paragraph in the plan -> परिचय (Intro)
- The MIDDLE paragraphs in the plan -> मुख्य लेख (Main Article) — add as many middle
  paragraphs as the plan has, each as its own separate paragraph
- The LAST paragraph in the plan -> विश्लेषण (Analysis), using the category framing

## Article Length and Section Structure

The final output must be a publication-ready technology news article, not a research report or
a JSON object — plain Hindi text with a heading before each section, written the way a human
reporter would type it: real paragraph breaks (a blank line between paragraphs), not one
run-on block and not escaped characters.

Output ONLY the following, in exactly this order, nothing else before or after (no preamble,
no markdown, no code fences, no meta-commentary):

शीर्षक: <one sharp Hindi headline based on the title idea>

मुख्य अवधारणा: <40-60 words — quick explanation of the central news point and why the reader
should care; do not repeat the intro>

परिचय: <80-120 words — establish the news immediately: what happened, who reported it, why it's
significant; no deep technical details here>

मुख्य लेख: <500-700 words — the primary reporting section, covering every middle paragraph
from the plan in real depth. This is the longest section. Cover: the core discovery/
announcement, the technical details needed to understand it, how it happened, evidence
supporting the claims, important caveats/limitations, and why it matters. Write each distinct
middle plan-paragraph as its own paragraph, separated by a blank line — do NOT merge them into
one continuous block. Avoid unnecessary implementation detail that doesn't improve reader
understanding.>

विश्लेषण: <200-350 words — editorial value, not a repeat of मुख्य लेख. Clearly separate analysis
from reported fact. Cover broader industry implications, whether this is a genuine shift or an
incremental change, limitations of the technology, and likely future impact, using the category
framing below. Never state speculation as fact — use phrasing like "यह संकेत देता है...",
"विशेषज्ञों के अनुसार...", "इसका संभावित प्रभाव..." for anything not already confirmed.>

निष्कर्ष: <50-80 words — the key takeaway and broader significance; do not repeat the intro>

Target total length across all sections: 900-1200 words (maximum 1400). If you run long, cut
secondary background, repeated explanations, and low-value technical detail first — never cut
core facts, caveats, or important context. A shorter, carefully-chosen article beats a longer
one stuffed with every available detail.

Category framing for विश्लेषण: {category_framing}

Language rules (CRITICAL):
- हर वाक्य हिंदी में — क्रिया, संयोजन, विशेषण सब हिंदी में
- CORRECT: "स्वायत्त एजेंट (Autonomous Agent) एक सरल निर्णय-चक्र पर काम करते हैं।"
- WRONG: "Individual agents बहुत simple हैं और एक loop follow करते हैं।"
- Technical terms: देवनागरी पहले, English parentheses में — मेमोरी स्टोर (Memory Store)
- कोई भी fact जो source में नहीं है वो मत लिखो
- Predictions hedge करो: हो सकता है, संभावना है
- Opinions/judgments को उनके स्रोत से attribute करो (जैसे "कंपनी के अनुसार", "शोधकर्ताओं का कहना है")
- The six section headings above (शीर्षक/मुख्य अवधारणा/परिचय/मुख्य लेख/विश्लेषण/निष्कर्ष) must
  appear exactly as given, each on its own line followed by a colon — only the content after
  each colon is free-form Hindi text"""

_SYNTHESIS_PROMPT = """You are a research assistant distilling raw web search material into clean, direct answers for an editorial team writing a tech news article.

For each ENTITY item below, write a short general-purpose overview of that entity (2-3 sentences), using ONLY the material given. This overview will be reused for OTHER future articles too, so do not shape it around today's story or mention today's news event.

For each QUESTION item below, directly answer the specific question asked (2-3 sentences), using ONLY the material given.

If the material given for an item doesn't actually contain a usable answer, say so plainly (e.g. "material did not address this") instead of guessing or inventing facts.

ENTITIES:
{identity_block}

QUESTIONS:
{context_block}

Output ONLY valid JSON in this exact shape, one entry per item listed above (same order, do not skip any):
{{
  "identity": [{{"query": "<exact ENTITY query as given>", "answer": "<synthesized overview>"}}],
  "context": [{{"query": "<exact QUESTION as given>", "answer": "<synthesized answer>"}}]
}}"""

_LABELED_SECTIONS = [
    "TITLE",
    "CONCEPT_BOX",
    "LEDE",
    "DEEP_DIVE_AND_CONTEXT",
    "STRATEGIC_ANALYSIS",
    "CONCLUSION_AND_SIGNIFICANCE",
]

# The model is asked for the English labels above but routinely drifts into
# translating them (and/or wrapping them in markdown emphasis) — observed on
# real articles even though the prompt says "EXACT labeled format". Map every
# variant seen in practice back to its canonical field so a formatting slip
# doesn't throw away an otherwise-good article.
_LABEL_SYNONYMS = {
    "TITLE": ["TITLE", "शीर्षक"],
    "CONCEPT_BOX": [
        "CONCEPT_BOX", "कॉन्सेप्ट बॉक्स", "अवधारणा बॉक्स", "कांसेप्ट बॉक्स", "मुख्य अवधारणा",
    ],
    "LEDE": ["LEDE", "लेड", "लीड", "परिचय", "इंट्रो"],
    "DEEP_DIVE_AND_CONTEXT": [
        "DEEP_DIVE_AND_CONTEXT", "डीप डाइव एंड कॉन्टेक्स्ट", "डीप डाइव और संदर्भ",
        "गहन विश्लेषण और संदर्भ", "गहन विश्लेषण", "मुख्य लेख", "मुख्य आलेख",
    ],
    "STRATEGIC_ANALYSIS": [
        "STRATEGIC_ANALYSIS", "रणनीतिक विश्लेषण", "रणनीतिक दृष्टिकोण", "विश्लेषण",
    ],
    "CONCLUSION_AND_SIGNIFICANCE": [
        "CONCLUSION_AND_SIGNIFICANCE", "निष्कर्ष और महत्व", "निष्कर्ष",
    ],
}
_LABEL_LOOKUP = {
    synonym.casefold(): canonical
    for canonical, synonyms in _LABEL_SYNONYMS.items()
    for synonym in synonyms
}
_MARKUP_CHARS = "*_#​"


def _match_label(line: str) -> tuple[str, str] | None:
    """Return (canonical_label, rest_of_line) if `line` opens a labeled section."""
    stripped = line.strip().lstrip(_MARKUP_CHARS).strip()
    colon = None
    for ch in (":", "："):
        idx = stripped.find(ch)
        if idx != -1 and (colon is None or idx < colon):
            colon = idx
    if colon is None:
        return None
    candidate = stripped[:colon].strip().strip(_MARKUP_CHARS).strip()
    canonical = _LABEL_LOOKUP.get(candidate.casefold())
    if not canonical:
        return None
    rest = stripped[colon + 1:].lstrip(_MARKUP_CHARS).strip()
    return canonical, rest

# ---------------------------------------------------------------------------
# Sarvam API helpers
# ---------------------------------------------------------------------------


_UNSET = object()


def _call_sarvam(
    prompt: str,
    api_key: str,
    model: str,
    system: str | None = None,
    reasoning_effort=_UNSET,
    max_tokens: int = 4096,
) -> str | None:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if reasoning_effort is not _UNSET:
        payload["reasoning_effort"] = reasoning_effort
    try:
        response = requests.post(
            SARVAM_URL,
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        msg = response.json()["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or None
        if not msg.get("content") and content:
            print(f"[sarvam:{model}] content=None, using reasoning_content fallback")
        return content
    except requests.RequestException as e:
        print(f"[sarvam:{model}] request failed: {e}")
        return None


def _parse_json_response(raw: str | None) -> dict | None:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Scan for the first balanced, valid JSON object — greedy search breaks
    # when reasoning_content contains a complete JSON followed by a truncated rewrite.
    pos = 0
    while True:
        start = cleaned.find("{", pos)
        if start == -1:
            break
        depth = 0
        for i, ch in enumerate(cleaned[start:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : start + i + 1])
                    except json.JSONDecodeError:
                        break
        pos = start + 1
    return None


_META_COMMENTARY = re.compile(
    r"^(let me|here'?s?\b|i'?ll\b|i will\b|i need\b|let'?s\b|note:|"
    r"here is|here are|revised|rewriting|rewrite|one more time|"
    r"please note|this is|above is)",
    re.IGNORECASE,
)


def _is_meta_line(line: str) -> bool:
    """Return True if the line looks like model self-commentary, not article text."""
    stripped = line.strip()
    if not stripped:
        return False
    # Lines that are mostly ASCII in a Hindi article are likely meta-commentary
    hindi_chars = sum(1 for c in stripped if "ऀ" <= c <= "ॿ")
    ascii_chars = sum(1 for c in stripped if c.isascii() and c.isalpha())
    if ascii_chars > 6 and hindi_chars == 0:
        if _META_COMMENTARY.match(stripped):
            return True
    return False


def _is_sentence_end(text: str, i: int) -> bool:
    if text[i] != ".":
        return True
    # A "." between two digits is a decimal point (GLM-5.2, 32.8%), not a
    # sentence boundary — treating it as one truncates the rest of the text.
    before = text[i - 1] if i > 0 else ""
    after = text[i + 1] if i + 1 < len(text) else ""
    return not (before.isdigit() and after.isdigit())


def _trim_to_last_sentence(text: str) -> str:
    """If text ends mid-sentence (no terminal punctuation), trim to last complete sentence."""
    if not text or (text[-1] in "।!?" or (text[-1] == "." and _is_sentence_end(text, len(text) - 1))):
        return text
    for punct in reversed(range(len(text))):
        if text[punct] in "।!?" or (text[punct] == "." and _is_sentence_end(text, punct)):
            return text[:punct + 1].strip()
    return text


def _clean_field_text(text: str) -> str:
    """Strip stray meta-commentary lines and trim to the last complete sentence."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not _is_meta_line(ln)]
    return _trim_to_last_sentence(" ".join(lines).strip())


def _clean_deep_dive_text(text: str) -> str:
    """Like _clean_field_text, but preserves the model's blank-line paragraph
    breaks instead of collapsing deep_dive_and_context into one run-on block —
    see CLAUDE.md "next line" investigation for why this field needs its own
    cleaner: the frontend renders it as a single <p>, splitting on \\n\\n."""
    if not text:
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    cleaned = []
    for para in paragraphs:
        lines = [ln.strip() for ln in para.splitlines()]
        lines = [ln for ln in lines if ln and not _is_meta_line(ln)]
        joined = " ".join(lines).strip()
        if joined:
            cleaned.append(joined)
    if not cleaned:
        return ""
    cleaned[-1] = _trim_to_last_sentence(cleaned[-1])
    return "\n\n".join(cleaned)


_STAGE3_FIELDS = [
    "title", "concept_box", "introduction_lede",
    "deep_dive_and_context", "strategic_analysis", "conclusion_and_significance",
]


def _parse_labeled_text(raw: str | None) -> dict | None:
    """Parse Stage 3 labeled plain-text output into a dict of article fields.

    Blank lines within a section are kept (not dropped) so that natural paragraph
    breaks the model writes survive into `fields` — DEEP_DIVE_AND_CONTEXT then
    reuses those breaks via _clean_deep_dive_text instead of needing an escaped
    "\\n\\n" the model has to remember to type, which is what the old JSON-only
    format required and which the model reliably skipped in practice.
    """
    if not raw:
        return None
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        match = _match_label(line)
        if match:
            current, rest = match
            fields[current] = [rest] if rest else []
        elif current is not None:
            stripped = line.strip()
            if stripped and _is_meta_line(stripped):
                continue
            fields[current].append(stripped)

    result = {
        k: (_clean_deep_dive_text if k == "DEEP_DIVE_AND_CONTEXT" else _clean_field_text)(
            "\n".join(v).strip()
        )
        for k, v in fields.items()
    }
    if not result.get("TITLE") or not result.get("LEDE"):
        return None

    return {
        "title": result["TITLE"],
        "concept_box": result.get("CONCEPT_BOX", ""),
        "introduction_lede": result["LEDE"],
        "deep_dive_and_context": result.get("DEEP_DIVE_AND_CONTEXT", ""),
        "strategic_analysis": result.get("STRATEGIC_ANALYSIS", ""),
        "conclusion_and_significance": result.get("CONCLUSION_AND_SIGNIFICANCE", ""),
    }


def _parse_stage3_output(raw: str | None) -> dict | None:
    """Parse Stage 3 output: labeled plain text is the primary format (see
    _STAGE3_PROMPT); JSON is kept as a fallback in case the model reverts to
    the old JSON habit despite the prompt no longer asking for it."""
    if not raw:
        return None
    parsed = _parse_labeled_text(raw)
    if parsed:
        return parsed
    parsed_json = _parse_json_response(raw)
    if parsed_json and all(isinstance(parsed_json.get(k), str) and parsed_json.get(k) for k in ("title", "introduction_lede")):
        return {
            k: _clean_deep_dive_text(parsed_json.get(k, "")) if k == "deep_dive_and_context"
            else _clean_field_text(parsed_json.get(k, ""))
            for k in _STAGE3_FIELDS
        }
    return None


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def _stage1_extract_queries(
    title: str, summary: str, source_text: str, api_key: str
) -> dict | None:
    """Three-step Stage 1: a dedicated skip gate, then (if kept) entity/gap
    analysis, then JSON extraction. All three use sarvam-30b with
    reasoning_effort=None — see the prompt definitions above for why each
    step is shaped the way it is, and stage1-design.md for the full test
    history this design is based on."""
    # 2000 chars, matching Stage 2's window — 800 was found to frequently cut off
    # before an article's actual named entities appear (confirmed on a real case:
    # the article's device/researcher/institution names all sat past char 800,
    # in what was still generic scene-setting text at that point), forcing entity
    # extraction to fall back to generic nouns ("device", "researchers") since the
    # real names were never in view. Stage 2 uses 2000 chars and reliably finds
    # the real names on the same articles, which is why that's the target here too.
    truncated_source = source_text[:2000] if source_text else "(not available)"

    skip_prompt = _STAGE1_SKIP_PROMPT.format(title=title, summary=summary[:500], source_text=truncated_source)
    skip_raw = _call_sarvam(skip_prompt, api_key, _MODEL_FAST, reasoning_effort=None)
    if not skip_raw:
        return None
    skip_match = _SKIP_RE.search(skip_raw)
    if not skip_match:
        return None
    reason_match = _SKIP_REASON_RE.search(skip_raw)
    skip_reason = reason_match.group(1).strip() if reason_match else None
    if skip_match.group(1).lower() == "yes":
        return {"skip": True, "skip_reason": skip_reason}

    analysis_prompt = _STAGE1_ANALYSIS_PROMPT.format(title=title, summary=summary[:500], source_text=truncated_source)
    analysis = _call_sarvam(
        analysis_prompt, api_key, _MODEL_FAST,
        reasoning_effort=None, max_tokens=_STAGE1_ANALYSIS_MAX_TOKENS,
    )
    if not analysis:
        return None
    analysis = _cap_gap_lines(analysis)

    extraction_prompt = _STAGE1_EXTRACTION_PROMPT.format(analysis=analysis[:2000])
    raw = _call_sarvam(
        extraction_prompt,
        api_key,
        _MODEL_FAST,
        system="/no_think Output JSON only.",
        reasoning_effort=None,
    )
    result = _parse_json_response(raw)
    if result is not None:
        result["skip"] = False
        result["skip_reason"] = skip_reason
        result["search_queries"] = _drop_hallucinated_comparisons(
            result.get("search_queries", []), result.get("entities", []), source_text
        )
        # Dangling-reference handling (pointing words like "this"/"it" with no named entity)
        # happens later, at dispatch time in _synthesize_sarvam via _prepare_context_queries —
        # that's where GAP1 can be attached as context to a later dangling query instead of
        # just dropping it, which needs this list to still be in its original GAP order.
    return result


def _stage2_editorial_strategy(
    title: str,
    summary: str,
    source_text: str,
    entity_context: str,
    api_key: str,
) -> dict | None:
    """Two-step Stage 2: an analysis call that does the real editorial thinking as
    visible plain text, then a JSON-extraction call that transcribes it.

    The original single-call version left reasoning_effort unset (Stage 2's editorial
    judgment benefits from real reasoning, unlike Stage 1's extraction tasks) — but
    reasoning and output share the same token budget, and reasoning has no separate
    allowance. Confirmed live: a real Stage 2 call hit the 4096-token cap with only
    594 chars of visible content, i.e. most of the budget went to reasoning before the
    JSON was cut off mid-object, which then failed to parse and fell through to the
    translate fallback. Splitting into an analysis call (reasoning off, but the model's
    thinking IS the visible output, so it isn't fighting anything for budget) and a
    separate reasoning-off transcription call mirrors the fix already proven for Stage 1
    (_stage1_extract_queries) — same failure mode, same cure.
    """
    analysis_prompt = _STAGE2_ANALYSIS_PROMPT.format(
        title=title,
        summary=summary[:500],
        source_text=source_text[:2000] if source_text else "(not available)",
        entity_context=entity_context[:3000],
    )
    analysis = _call_sarvam(
        analysis_prompt, api_key, _MODEL_QUALITY,
        reasoning_effort=None, max_tokens=1500,
    )
    if not analysis:
        return None

    extraction_prompt = _STAGE2_EXTRACTION_PROMPT.format(analysis=analysis[:3000])
    raw = _call_sarvam(
        extraction_prompt,
        api_key,
        _MODEL_QUALITY,
        system="/no_think Output JSON only.",
        reasoning_effort=None,
    )
    return _parse_json_response(raw)


def _stage3_write_article(
    title: str,
    source_text: str,
    entity_context: str,
    strategy: dict,
    api_key: str,
) -> dict | None:
    _HINDI_CATEGORY_MAP = {
        "सामान्य": "general", "अधिग्रहण": "acquisition",
        "मॉडल_रिलीज": "model_release", "प्रतिबंध_नियमन": "ban_regulation",
        "रेपो_विश्लेषण": "repo_analysis",
    }
    category = strategy.get("category", "general")
    category = _HINDI_CATEGORY_MAP.get(category, category)
    category_framing = _CATEGORY_FRAMING.get(category, _CATEGORY_FRAMING["general"])
    source_text_block = source_text[:2500] if source_text else "(not available)"

    # Format paragraph plan from Stage 2
    raw_plan = strategy.get("paragraph_plan", [])
    if isinstance(raw_plan, list):
        paragraph_plan = "\n".join(f"{i+1}. {p}" for i, p in enumerate(raw_plan))
    else:
        paragraph_plan = str(raw_plan)

    prompt = _STAGE3_PROMPT.format(
        title=title,
        paragraph_plan=paragraph_plan,
        source_text_block=source_text_block,
        entity_context=entity_context[:2000],
        category_framing=category_framing,
    )
    raw = _call_sarvam(
        prompt,
        api_key,
        _MODEL_QUALITY,
        system="/no_think Output the labeled Hindi sections only. No preamble, no JSON, no markdown.",
        reasoning_effort=None,
    )
    return _parse_stage3_output(raw)


def _stage3_write_article_with_retry(
    title: str,
    source_text: str,
    entity_context: str,
    strategy: dict,
    api_key: str,
) -> dict | None:
    """One retry before giving up on Stage 3. Confirmed live on a real failed
    article (a TechCrunch humanoid-robotics story): Stage 3 failed once in
    production, but re-running the *exact same* inputs succeeded immediately,
    twice — pointing to a one-off transient API hiccup (network blip /
    momentary Sarvam instability), not a deterministic problem with that
    article's content or the prompt. A single retry is cheap (only fires on
    failure, not on the common success path) and would have upgraded that
    article from the sparse translate-fallback view to a full Sarvam-written
    one."""
    article_fields = _stage3_write_article(title, source_text, entity_context, strategy, api_key)
    if article_fields:
        return article_fields
    print("  FAILED — retrying once")
    return _stage3_write_article(title, source_text, entity_context, strategy, api_key)


def _synthesize_search_results(
    identity_results: dict[str, str],
    context_results: dict[str, str],
    api_key: str,
) -> dict[str, str]:
    """Distill raw search snippets into direct answers via one batched call
    to sarvam-30b (reasoning_effort=None — same fix as Stage 1 Call B and
    Stage 3: a straightforward extraction task doesn't need reasoning, and
    turning it off guarantees the full token budget goes to writing every
    item's answer instead of risking content=None on a multi-item response).

    Batched into a single call rather than one call per query — a run can
    have several identity queries (one per cache-miss entity) plus up to 3
    context queries, and a call per query would add several sequential
    round-trips to the pipeline for no real benefit.

    Falls back to the raw (truncated) snippet per-query if the call fails or
    a query's answer is missing from the parsed response, mirroring the
    fallback pattern used everywhere else in this pipeline (Stage 1 Call A/B,
    Stage 3 JSON-then-labeled-text) — a nice-to-have step degrading gracefully
    rather than losing material outright.
    """
    identity_items = [(q, t) for q, t in identity_results.items() if t]
    context_items = [(q, t) for q, t in context_results.items() if t]
    if not identity_items and not context_items:
        return {}

    identity_block = "\n\n".join(
        f"ENTITY QUERY: {q}\nMATERIAL: {t[:800]}" for q, t in identity_items
    ) or "(none)"
    context_block = "\n\n".join(
        f"QUESTION: {q}\nMATERIAL: {t[:800]}" for q, t in context_items
    ) or "(none)"

    prompt = _SYNTHESIS_PROMPT.format(identity_block=identity_block, context_block=context_block)
    raw = _call_sarvam(
        prompt,
        api_key,
        _MODEL_FAST,
        system="/no_think Output JSON only.",
        reasoning_effort=None,
    )
    parsed = _parse_json_response(raw) or {}

    # Matched positionally, not by the model's echoed "query" text — confirmed
    # live that the model normalizes the query when echoing it back (e.g.
    # drops the surrounding quotes: '"Rocket Lab" company overview' ->
    # 'Rocket Lab company overview'), which silently broke an exact-string
    # dict lookup and would have made every identity answer fall back to its
    # raw unsynthesized snippet. The "query" field is still requested for
    # trace readability but isn't relied on for correctness.
    synthesized: dict[str, str] = {}
    for items, answers in (
        (identity_items, parsed.get("identity")),
        (context_items, parsed.get("context")),
    ):
        if not isinstance(answers, list):
            continue
        for (q, _), item in zip(items, answers):
            if isinstance(item, dict) and item.get("answer"):
                synthesized[q] = item["answer"]

    for q, t in identity_items + context_items:
        synthesized.setdefault(q, t)

    return synthesized


# ---------------------------------------------------------------------------
# Input language normalization — translate non-English source text to
# English before it ever reaches Stage 1-3. Previously the only place
# Google Translate ran was the FALLBACK path (translate_item, used when
# Sarvam synthesis fails entirely, output language is Hindi) — a Chinese-
# or other-non-English-sourced article that Sarvam *successfully*
# synthesized from went straight into Stage 1-3 prompts in its original
# language, untested and undocumented as a real scenario.
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> str | None:
    """Best-effort language detection on a sample of text.

    Returns None (not "en") on detection failure — e.g. text too short or
    too ambiguous (langdetect raises on these) — so the caller treats an
    undetectable sample the same as English: proceed unchanged rather than
    force a translation call on a guess.
    """
    sample = (text or "").strip()[:500]
    if not sample:
        return None
    try:
        return detect(sample)
    except LangDetectException:
        return None


def _translate_to_english(text: str) -> str:
    """Best-effort translate non-English text to English.

    Falls back to the original text on any failure — same graceful-
    degradation pattern as `translator/translate.py`'s `_safe_translate`:
    a failed translation should degrade quality, not crash the run.
    """
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target="en").translate(text)
    except Exception as e:
        print(f"[language] translate-to-English failed: {e}")
        return text


# ---------------------------------------------------------------------------
# Sarvam orchestrator  +  per-run trace
# ---------------------------------------------------------------------------

# Sentinel: article was actively rejected (not a processing failure).
# Pipeline must NOT fall back to machine translation for these.
SKIP = object()

_run_traces: list[dict] = []


def get_run_traces() -> list[dict]:
    return _run_traces


def _synthesize_sarvam(cluster: list[dict], api_key: str) -> dict | None:
    primary = cluster[0]
    title = primary.get("title", "")
    summary = primary.get("summary", "")
    source_url = primary.get("url", "")

    SEP = "=" * 64
    print(f"\n{SEP}")
    print(f"[ARTICLE] {title[:70]}")
    print(f"          {source_url[:70]}")
    print(SEP)

    trace: dict = {"url": source_url, "title": title, "outcome": "pending"}

    # ------------------------------------------------------------------
    # Stage 0: scrape source article
    # ------------------------------------------------------------------
    page = fetch_page(source_url)
    if isinstance(page, dict):
        source_text = ""
        scrape_error = page.get("error")
    else:
        source_text = page
        scrape_error = None
    trace["stage0"] = {
        "scraped_chars": len(source_text),
        "source_preview": source_text[:400],
        "scrape_error": scrape_error,
    }
    print(f"\n[STAGE 0 - scrape]")
    print(f"  result : {len(source_text)} chars scraped" + (f" (error: {scrape_error})" if scrape_error else ""))

    if len(source_text) + len(summary) < 250:
        msg = f"too little source material ({len(source_text)} scrape + {len(summary)} summary chars)"
        print(f"  SKIP   : {msg}")
        trace["outcome"] = "skipped_no_content"
        _run_traces.append(trace)
        return SKIP

    # Scraping can fail for reasons unrelated to the story's own substance
    # (dead link, JS-rendered page, timeout, WAF block) — fetch_page reports
    # that as an error dict rather than silently returning "". When that
    # happens but the RSS/collector summary is itself substantial (the
    # length check above already guarantees that if source_text is empty),
    # use the summary as source_text instead of starving Stage 1-3 of every
    # fact that isn't already baked into the much shorter title/summary.
    # Confirmed live: without this fallback, a real article's failed scrape
    # caused Stage 2 to drop the story's most important fact entirely and
    # mistranslate the source's own company name from thin context.
    used_summary_fallback = False
    if not source_text and summary:
        source_text = summary
        used_summary_fallback = True
        print(f"  fallback: scrape returned nothing usable — using RSS summary as source text instead")
    trace["stage0"]["used_summary_fallback"] = used_summary_fallback

    # ------------------------------------------------------------------
    # Language normalization: detect non-English source content and
    # translate title/summary/source_text to English before Stage 1 ever
    # sees them. Detection runs on source_text (falling back to summary if
    # source_text is empty) since it's the largest, most reliable sample.
    # ------------------------------------------------------------------
    detected_lang = _detect_language(source_text or summary)
    translated_input = False
    if detected_lang and detected_lang != "en":
        print(f"\n[LANGUAGE] detected '{detected_lang}' — translating to English before Stage 1")
        title = _translate_to_english(title)
        summary = _translate_to_english(summary)
        source_text = _translate_to_english(source_text)
        translated_input = True
    trace["stage0"]["detected_language"] = detected_lang
    trace["stage0"]["translated_to_english"] = translated_input

    # ------------------------------------------------------------------
    # Stage 1: entity extraction + relevance check (sarvam-30b)
    # ------------------------------------------------------------------
    print(f"\n[STAGE 1 - {_MODEL_FAST} entity extraction + relevance]")
    stage1 = _stage1_extract_queries(title, summary, source_text, api_key) or {}
    trace["stage1"] = stage1

    if stage1.get("skip"):
        print(f"  SKIP   : {stage1.get('skip_reason') or 'model flagged as not tech news (no reason captured)'}")
        trace["outcome"] = "skipped_not_tech_news"
        _run_traces.append(trace)
        return SKIP

    search_queries = stage1.get("search_queries", [])
    entities = stage1.get("entities", [])
    entity_labels = ", ".join(f"{e.get('name')} ({e.get('type')})" for e in entities) or "none"
    print(f"  reason   : {stage1.get('skip_reason') or 'none captured'}")
    print(f"  entities : {entity_labels}")
    print(f"  queries  : {search_queries}")

    # ------------------------------------------------------------------
    # Cache check
    # ------------------------------------------------------------------
    print(f"\n[CACHE CHECK]")
    cache = load_cache()
    entity_context_parts: list[str] = []
    entities_needing_search: list[dict] = []
    cache_hits: list[dict] = []
    cache_misses: list[dict] = []

    for entity in entities:
        name = entity.get("name", "")
        is_ambiguous = entity.get("ambiguous", False)
        resolved_sense = entity.get("resolved_sense") if is_ambiguous else None
        record = get_entity(cache, name, resolved_sense=resolved_sense)
        if record:
            sense_label = record.get("sense_label", "")
            label = f"{name} ({sense_label})" if sense_label else name
            preview = record["summary"][:80]
            entity_context_parts.append(f"{label}: {record['summary']}")
            cache_hits.append({"name": name, "preview": preview})
            print(f"  HIT  : {name} -> \"{preview}...\"")
        else:
            entities_needing_search.append(entity)
            cache_misses.append({"name": name, "type": entity.get("type")})
            print(f"  MISS : {name} ({entity.get('type')})")

    trace["cache"] = {"hits": cache_hits, "misses": cache_misses}

    # ------------------------------------------------------------------
    # Web search — split by how much judgment the query actually needs.
    # Plain (unambiguous) entity lookups are cheap "what is X" fact checks —
    # always routed to the free DDG tier, never the research agent, so the
    # high-throughput/paid-feeling API isn't spent on low-level work it
    # doesn't need. Ambiguous entities (need real disambiguation) and
    # GAP/context queries (comparison/why-now, need real reasoning) go to the
    # external research agent (/api/concise) when configured — see
    # writer/concise_search.py. Both fall back to DDG/Google-News-RSS tiers +
    # a dedicated synthesis call (writer/search.py) when
    # CONCISE_API_URL/CONCISE_API_KEY aren't set, e.g. local dev.
    # ------------------------------------------------------------------
    plain_entities = [e for e in entities_needing_search if not e.get("ambiguous")]
    ambiguous_entities = [e for e in entities_needing_search if e.get("ambiguous")]
    # _prepare_context_queries attaches GAP1 as background context to any later query that
    # dangles on a pointing word ("this"/"it") with no named entity of its own — see that
    # function's docstring. This can drop a query outright (only if still dangling even with
    # context attached), so model_queries is reassigned to whatever survives.
    context_query_pairs = _prepare_context_queries(search_queries[:3], entities)
    model_queries = [orig for orig, _ in context_query_pairs]
    dispatch_text_by_query = dict(context_query_pairs)
    use_concise = concise_configured()

    print(
        f"\n[SEARCH - {len(plain_entities)} entity via DDG, "
        f"{len(ambiguous_entities)} ambiguous entity + {len(model_queries)} context via "
        f"{'/api/concise' if use_concise else 'DDG/Google News'}]"
    )

    identity_answers: dict[str, str] = {}
    identity_not_found: dict[str, bool] = {}
    context_answers: dict[str, str] = {}

    # Plain entities always go through DDG. Ambiguous entities and context
    # queries only fall through to DDG when the research agent isn't configured.
    identity_ddg_queries: dict[str, str] = {
        e["name"]: build_identity_query(e["name"], e.get("type", "unknown")) for e in plain_entities
    }
    if not use_concise:
        for e in ambiguous_entities:
            identity_ddg_queries[e["name"]] = build_identity_query(
                e["name"], e.get("type", "unknown"), e.get("resolved_sense")
            )

    identity_ddg_results = (
        search_web(list(identity_ddg_queries.values()), IDENTITY_TIERS) if identity_ddg_queries else {}
    )
    # Dispatch whatever text _prepare_context_queries decided to send (the bare query, or the
    # query with GAP1 attached as background) — then remap the results back to the original
    # query text so downstream trace/entity-context assembly doesn't need to know about this.
    context_ddg_results_raw = (
        search_web([dispatch_text_by_query[q] for q in model_queries], CONTEXT_TIERS)
        if (model_queries and not use_concise)
        else {}
    )
    context_ddg_results = {q: context_ddg_results_raw.get(dispatch_text_by_query[q], "") for q in model_queries}
    for q, text in {**identity_ddg_results, **context_ddg_results}.items():
        tag = "[identity/ddg]" if q in identity_ddg_results else "[context/ddg] "
        print(f"  {tag} {q[:50]:<50} -> {len(text)} chars")

    synthesized_ddg: dict[str, str] = {}
    if identity_ddg_results or context_ddg_results:
        print(f"\n[SYNTHESIS - {_MODEL_FAST}]")
        synthesized_ddg = _synthesize_search_results(identity_ddg_results, context_ddg_results, api_key)
        for q, answer in synthesized_ddg.items():
            print(f"  {q[:55]:<55} -> \"{answer[:80]}\"")

    for name, q in identity_ddg_queries.items():
        identity_answers[name] = synthesized_ddg.get(q, "")
        identity_not_found[name] = False
    if not use_concise:
        for q in model_queries:
            context_answers[q] = synthesized_ddg.get(q, "")

    if use_concise:
        for entity in ambiguous_entities:
            name = entity.get("name", "")
            entity_type = entity.get("type", "unknown")
            resolved_sense = entity.get("resolved_sense")
            question = build_identity_question(name, entity_type, resolved_sense)
            answer = ask_concise(question)
            identity_answers[name] = answer
            identity_not_found[name] = bool(answer) and looks_like_not_found(answer)
            flag = " (not found)" if identity_not_found[name] else ""
            print(f"  [identity/api] {name[:40]:<40} -> {len(answer)} chars{flag}")

        for q in model_queries:
            answer = ask_concise(dispatch_text_by_query[q])
            context_answers[q] = answer
            print(f"  [context/api ] {q[:40]:<40} -> {len(answer)} chars")

    trace["search"] = {
        "backend": "hybrid" if use_concise else "ddg",
        "identity_queries": list(identity_ddg_queries.values())
        + [e["name"] for e in ambiguous_entities if use_concise],
        "context_queries": model_queries,
        "context_queries_with_gap1_attached": [
            q for q in model_queries if dispatch_text_by_query[q] != q
        ],
        "results": {
            **{n: a[:400] for n, a in identity_answers.items()},
            **{q: a[:400] for q, a in context_answers.items()},
        },
    }
    if synthesized_ddg:
        trace["synthesis"] = {q: a[:400] for q, a in synthesized_ddg.items()}

    for q in model_queries:
        answer = context_answers.get(q, "")
        if answer:
            entity_context_parts.append(f"[{q}]:\n{answer}")

    # ------------------------------------------------------------------
    # Cache update — store the identity answer, unless the research agent
    # honestly reported it couldn't find anything (see concise_search.py's
    # _NOT_FOUND_MARKERS). Caching a confident "not found" for the full
    # 45-day TTL would be worse than a plain cache miss, since a miss just
    # retries the search on the next article that mentions the same name.
    # ------------------------------------------------------------------
    cache_updates: list[dict] = []
    for entity in entities_needing_search:
        name = entity.get("name", "")
        entity_type = entity.get("type", "unknown")
        is_ambiguous = entity.get("ambiguous", False)
        resolved_sense = entity.get("resolved_sense") if is_ambiguous else None
        answer = identity_answers.get(name, "")
        if not answer:
            continue
        entity_context_parts.append(f"{name}: {answer}")
        if identity_not_found.get(name):
            print(f"  [cache] skipping store for {name} — agent reported no matching material found")
            continue
        set_entity(cache, name, entity_type, answer[:300].strip(), resolved_sense=resolved_sense)
        cache_updates.append({"name": name, "type": entity_type, "chars_stored": min(300, len(answer))})
    save_cache(cache)

    if cache_updates:
        print(f"\n[CACHE UPDATE - {len(cache_updates)} new entities stored]")
        for u in cache_updates:
            print(f"  {u['name']} ({u['type']}) -> {u['chars_stored']} chars")
    trace["cache_updates"] = cache_updates

    entity_context = "\n\n".join(entity_context_parts) or "No additional context available."
    trace["entity_context_preview"] = entity_context[:600]
    print(f"\n[ENTITY CONTEXT] {len(entity_context)} chars assembled for Stage 2+3")

    # ------------------------------------------------------------------
    # Stage 2: editorial strategy (sarvam-105b)
    # ------------------------------------------------------------------
    print(f"\n[STAGE 2 - {_MODEL_QUALITY} editorial strategy]")
    strategy = _stage2_editorial_strategy(title, summary, source_text, entity_context, api_key)
    if not strategy:
        print("  FAILED")
        trace["outcome"] = "stage2_failed"
        _run_traces.append(trace)
        return None
    trace["stage2"] = strategy
    print(f"  category       : {strategy.get('category')}")
    print(f"  core_narrative : {str(strategy.get('core_narrative', ''))[:80]}...")
    print(f"  planned_length : {strategy.get('planned_length')}")

    # ------------------------------------------------------------------
    # Stage 3: write Hindi article (sarvam-105b)
    # ------------------------------------------------------------------
    print(f"\n[STAGE 3 - {_MODEL_QUALITY} writing Hindi article]")
    article_fields = _stage3_write_article_with_retry(title, source_text, entity_context, strategy, api_key)
    if not article_fields:
        print("  FAILED (retry also failed)")
        trace["outcome"] = "stage3_failed"
        _run_traces.append(trace)
        return None
    trace["stage3"] = article_fields

    total_chars = sum(len(v) for v in article_fields.values() if isinstance(v, str))
    print(f"  chars written  : {total_chars}")
    print(f"  final title    : {article_fields.get('title', '')[:70]}")

    result = {
        **primary,
        "language": "hindi",
        "title": article_fields["title"],
        "summary": article_fields["introduction_lede"],
        "concept_box": article_fields["concept_box"],
        "introduction_lede": article_fields["introduction_lede"],
        "deep_dive_and_context": article_fields["deep_dive_and_context"],
        "strategic_analysis": article_fields["strategic_analysis"],
        "conclusion_and_significance": article_fields["conclusion_and_significance"],
        "category": strategy.get("category", "general"),
        "tags": [e.get("name", "") for e in entities if e.get("name")][:5],
        "sources": [item["url"] for item in cluster if item.get("url")],
    }

    if len(cluster) > 1:
        result["source"] = "synthesized"

    trace["outcome"] = "published"
    _run_traces.append(trace)
    print(f"\n[DONE] article published")
    return result


# ---------------------------------------------------------------------------
# Ollama fallback (used when SARVAM_API_KEY is not configured)
# ---------------------------------------------------------------------------

_OLLAMA_PROMPT = """You are a journalist for TechDrishti, a Hindi-language science and technology publication.
Below are facts from {count} English source articles about the same topic. Write an
ORIGINAL Hindi news article based only on these facts — do not translate or closely
paraphrase any single source's sentences, write fresh sentences in your own words.
Do not add any fact that isn't present in the sources below. Keep it to 3-5 sentences.

Sources:
{sources}

Respond in EXACTLY this format and nothing else:
TITLE: <one line Hindi headline>
BODY: <3-5 sentence Hindi article body>"""

_OLLAMA_RE = re.compile(r"TITLE:\s*(.+?)\s*BODY:\s*(.+)", re.DOTALL)


def _call_ollama(prompt: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("response", "")


def _synthesize_ollama(cluster: list[dict], language: str) -> dict | None:
    sources = "\n".join(
        f"{i + 1}. {item.get('title', '')} — {item.get('summary', '')}"
        for i, item in enumerate(cluster)
    )
    prompt = _OLLAMA_PROMPT.format(count=len(cluster), sources=sources)
    try:
        raw = _call_ollama(prompt)
    except requests.RequestException:
        return None

    match = _OLLAMA_RE.search(raw)
    if not match:
        return None
    title, body = match.group(1).strip(), match.group(2).strip()
    if not title or not body:
        return None

    primary = cluster[0]
    result = {
        **primary,
        "language": language,
        "title": title,
        "summary": body,
        "sources": [item["url"] for item in cluster if item.get("url")],
    }
    if len(cluster) > 1:
        result["source"] = "synthesized"
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def synthesize_article(cluster: list[dict], language: str = "hindi") -> dict | None:
    """Produce one original Hindi article from a cluster of related items.

    Runs the 4-stage research-agent pipeline (scrape → extract → search →
    strategy → write) when SARVAM_API_KEY is set.  Falls back to local Ollama
    when it is not.  Returns None on unrecoverable failure so callers can fall
    back to plain machine translation.
    """
    if language != "hindi":
        return None

    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if api_key:
        result = _synthesize_sarvam(cluster, api_key)
        return result  # may be SKIP, dict, or None

    return _synthesize_ollama(cluster, language)
