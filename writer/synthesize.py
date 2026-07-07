import json
import os
import re

import requests
from deep_translator import GoogleTranslator
from langdetect import LangDetectException, detect

from writer.entity_cache import get_entity, load_cache, save_cache, set_entity
from writer.search import CONTEXT_TIERS, IDENTITY_TIERS, search_web
from writer.web_context import scrape_source

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

   CRITICAL -- a GAP query is a SEARCH QUERY, not an analysis question. A search engine can only
   return something that already exists published somewhere (a number, a spec, a date, a price, a
   quote) -- it cannot return a judgment, an opinion, or a prediction that nobody has written
   down. Before writing a GAP query, ask yourself: "could a search realistically return one
   specific fact that answers this?" If the honest answer is no -- if answering it requires
   forming an opinion or predicting the future -- rewrite it as a request for the underlying
   fact(s) instead, and let the editor draw the conclusion from those facts. This applies whether
   the gap is about a single entity (e.g. one product's price) or about comparing two entities --
   either way, ask for the fact(s), not the verdict.

   WRONG (asks for a judgment nobody has published -- a search will never satisfy this):
   "What is the strategic significance of Z.ai's open-source release in the context of the US ban
   on Anthropic, and what are the likely long-term implications for the global AI model market?"
   CORRECT (asks for a fact that lets the editor reach that judgment):
   "Chinese AI model adoption by US companies after Anthropic export ban"

   WRONG (speculative, no source could ever answer this):
   "What are the potential cultural integration challenges between Persistent Systems and Nagarro,
   and how might this affect the post-merger performance of the combined entity?"
   CORRECT (plain background facts about the two companies -- lets the editor make the point):
   "Nagarro headquarters employee count history"

   CORRECT (a single entity's own fact -- fine on its own, no comparison needed):
   "Leanstral 1.5 API pricing"
   CORRECT (a comparison is fine too, AS LONG AS it is still asking for a fact, e.g. published
   benchmark numbers, not an opinion about what those numbers mean):
   "GLM-5.2 GPT-5.5 benchmark score comparison"

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
        ) and stripped.lower() not in _CONNECTOR_WORDS and stripped.lower() not in _GENERIC_ANALYSIS_WORDS
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
plain text (not JSON yet — that comes later). Cover exactly these five things, in order:

CORE NARRATIVE: the real story and underlying tension, one sentence.
KEY FACTS AND QUOTES: the facts/figures/statements that must appear in the article.
DISAMBIGUATION TARGETS: which terms need inline Hindi explanation for a newcomer.
CATEGORY: exactly one of acquisition|model_release|ban_regulation|repo_analysis|general.
PARAGRAPH PLAN: a numbered list, 4-6 paragraphs — use as many as the story genuinely has
  substance for (a maximum driven by real content, not a target to hit; a thin story with only
  4 paragraphs' worth of real material should stay at 4, don't pad with restated facts to reach
  6). For each paragraph, write 2-4 sentences specifying exactly what to say, which
  facts/entities/numbers belong in it, and the tone — enough detail that a writer needs zero
  additional thinking to produce a full 3-5 sentence paragraph from it. List out the specific
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
  ]
}}

Include exactly as many paragraph_plan entries as the PARAGRAPH PLAN section above lists (4-6) —
no more, no fewer, no invented paragraphs."""

_STAGE3_PROMPT = """You are a Hindi writer for टेकदृष्टि. The editorial team has done all the planning — your ONLY job is to execute the writing plan below exactly as instructed.

DO NOT re-plan, re-think, or restructure. Follow each paragraph instruction below, but NEVER copy the
instruction's own wording into the article — the plan tells you WHAT to cover, not what to literally
write. Turn each instruction into actual flowing Hindi prose that DOES what it says, using the source
facts and entity definitions provided.

Write a COMPREHENSIVE, substantial article, not a short summary — you have a generous token budget for
this, use it. Every body paragraph (introduction_lede, deep_dive_and_context, strategic_analysis,
conclusion_and_significance) should be full, multi-sentence Hindi prose that genuinely explains
mechanism, context, background, and implications from the plan and source facts — not a one-line
gist of the paragraph's topic. deep_dive_and_context in particular is the main body of the article:
it should be the longest section, covering every middle paragraph from the plan in real depth.

CRITICAL — instruction vs. content, do not confuse the two:
WRONG (copies the instruction itself, explains nothing): "उपकरण के पीछे की तकनीक की व्याख्या करें। वर्णन करें कि यह कैसे काम करता है।"
CORRECT (actually executes it): "यह उपकरण परफ्यूजन तकनीक का उपयोग करता है, जो आंख की धमनी के माध्यम से ऑक्सीजन युक्त तरल पहुँचाता है।"
If a sentence you're about to write contains a verb like "करें"/"दें" telling the reader what to do (व्याख्या करें, वर्णन करें, उल्लेख करें, शामिल करें), you are copying the instruction, not writing the article — rewrite it as a direct statement of fact instead.

--- WRITING PLAN (describes what each paragraph must cover — an instruction to you, not text to reproduce) ---
Title idea: {title}
Paragraph plan:
{paragraph_plan}

Category framing for STRATEGIC_ANALYSIS paragraph: {category_framing}

--- SOURCE FACTS (use these, do not invent) ---
{source_text_block}

--- ENTITY DEFINITIONS ---
{entity_context}

Mapping the plan's paragraphs (there may be 4-6) onto the JSON fields below:
- The FIRST paragraph in the plan -> introduction_lede
- EVERY paragraph BETWEEN the first and the last (Para 2 through the second-to-last, however
  many that is) -> deep_dive_and_context, combined into one thorough, multi-paragraph-worth section
- The LAST paragraph in the plan -> strategic_analysis, using the category framing

Output ONLY valid JSON with exactly these keys (no markdown, no preamble, no code fences):
{{
  "title": "<one sharp Hindi headline based on the title idea>",
  "concept_box": "<2-3 sentences — explain the ONE hardest concept for a newcomer, in simple Hindi>",
  "introduction_lede": "<actual prose fulfilling Para 1's instruction, not the instruction itself — 3-5 substantial sentences>",
  "deep_dive_and_context": "<actual prose combining every middle paragraph's instruction in full depth, not the instructions themselves — this is the main body, cover every fact/comparison/quote from those instructions, several sentences per paragraph covered>",
  "strategic_analysis": "<actual prose fulfilling the final paragraph's instruction using the category framing — 3-5 substantial sentences>",
  "conclusion_and_significance": "<one strong closing paragraph — 3-4 sentences on what this means for the reader>"
}}

Language rules (CRITICAL):
- हर वाक्य हिंदी में — क्रिया, संयोजन, विशेषण सब हिंदी में
- CORRECT: "स्वायत्त एजेंट (Autonomous Agent) एक सरल निर्णय-चक्र पर काम करते हैं।"
- WRONG: "Individual agents बहुत simple हैं और एक loop follow करते हैं।"
- Technical terms: देवनागरी पहले, English parentheses में — मेमोरी स्टोर (Memory Store)
- कोई भी fact जो source में नहीं है वो मत लिखो
- Predictions hedge करो: हो सकता है, संभावना है
- The JSON keys themselves must stay exactly as given in English — only the values are Hindi text"""

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
    "CONCEPT_BOX": ["CONCEPT_BOX", "कॉन्सेप्ट बॉक्स", "अवधारणा बॉक्स", "कांसेप्ट बॉक्स"],
    "LEDE": ["LEDE", "लेड", "लीड"],
    "DEEP_DIVE_AND_CONTEXT": [
        "DEEP_DIVE_AND_CONTEXT", "डीप डाइव एंड कॉन्टेक्स्ट", "डीप डाइव और संदर्भ",
        "गहन विश्लेषण और संदर्भ", "गहन विश्लेषण",
    ],
    "STRATEGIC_ANALYSIS": ["STRATEGIC_ANALYSIS", "रणनीतिक विश्लेषण", "रणनीतिक दृष्टिकोण"],
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


_STAGE3_FIELDS = [
    "title", "concept_box", "introduction_lede",
    "deep_dive_and_context", "strategic_analysis", "conclusion_and_significance",
]


def _parse_labeled_text(raw: str | None) -> dict | None:
    """Parse Stage 3 legacy labeled-text output into a dict of article fields."""
    if not raw:
        return None
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        match = _match_label(line)
        if match:
            current, rest = match
            fields[current] = [rest] if rest else []
        elif current:
            stripped = line.strip()
            if stripped and not _is_meta_line(stripped):
                fields[current].append(stripped)

    result = {k: _trim_to_last_sentence(" ".join(v).strip()) for k, v in fields.items()}
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
    """Parse Stage 3 output: JSON is the primary format; legacy labeled-text
    is kept as a fallback in case the model ignores the JSON instruction."""
    if not raw:
        return None
    parsed = _parse_json_response(raw)
    if parsed and all(isinstance(parsed.get(k), str) and parsed.get(k) for k in ("title", "introduction_lede")):
        return {k: _clean_field_text(parsed.get(k, "")) for k in _STAGE3_FIELDS}
    return _parse_labeled_text(raw)


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
    if skip_match.group(1).lower() == "yes":
        return {"skip": True}

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
        result["search_queries"] = _drop_hallucinated_comparisons(
            result.get("search_queries", []), result.get("entities", []), source_text
        )
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
        system="/no_think Output JSON only. No preamble.",
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
    source_text = scrape_source(source_url)
    trace["stage0"] = {
        "scraped_chars": len(source_text),
        "source_preview": source_text[:400],
    }
    print(f"\n[STAGE 0 - scrape]")
    print(f"  result : {len(source_text)} chars scraped")

    if len(source_text) + len(summary) < 250:
        msg = f"too little source material ({len(source_text)} scrape + {len(summary)} summary chars)"
        print(f"  SKIP   : {msg}")
        trace["outcome"] = "skipped_no_content"
        _run_traces.append(trace)
        return SKIP

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
        print(f"  SKIP   : model flagged as not tech news")
        trace["outcome"] = "skipped_not_tech_news"
        _run_traces.append(trace)
        return SKIP

    search_queries = stage1.get("search_queries", [])
    entities = stage1.get("entities", [])
    entity_labels = ", ".join(f"{e.get('name')} ({e.get('type')})" for e in entities) or "none"
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
    # Web search — identity queries ("what is X") and context queries
    # ("why now"/comparison) go through different free tiers, since they need
    # different kinds of sources: Wikipedia has stable definitions but no
    # news, so identity queries try Wikipedia first; context queries need
    # recent material Wikipedia can't give, so those try Google News RSS
    # first. Both fall back to DDG. See writer/search.py for details.
    # ------------------------------------------------------------------
    identity_queries = [
        f'"{e["name"]}" {e.get("type", "unknown")} overview'
        for e in entities_needing_search
    ]
    model_queries = search_queries[:3]

    print(f"\n[SEARCH - {len(identity_queries)} identity + {len(model_queries)} context queries]")
    identity_results = search_web(identity_queries, IDENTITY_TIERS) if identity_queries else {}
    context_results = search_web(model_queries, CONTEXT_TIERS) if model_queries else {}
    search_results = {**identity_results, **context_results}
    for q, text in search_results.items():
        tag = "[identity]" if q in identity_results else "[context] "
        print(f"  {tag} {q[:55]:<55} -> {len(text)} chars")

    trace["search"] = {
        "identity_queries": identity_queries,
        "context_queries": model_queries,
        "results": {q: text[:400] for q, text in search_results.items()},
    }

    # ------------------------------------------------------------------
    # Synthesis — distill each query's raw material into a direct answer
    # (sarvam-30b, reasoning off) instead of dumping raw snippet soup into
    # entity_context, where Stage 2 would have to guess which part matters.
    # ------------------------------------------------------------------
    print(f"\n[SYNTHESIS - {_MODEL_FAST}]")
    synthesized_results = _synthesize_search_results(identity_results, context_results, api_key)
    for q, answer in synthesized_results.items():
        print(f"  {q[:55]:<55} -> \"{answer[:80]}\"")
    trace["synthesis"] = {q: a[:400] for q, a in synthesized_results.items()}

    for q in model_queries:
        answer = synthesized_results.get(q, "")
        if answer:
            entity_context_parts.append(f"[{q}]:\n{answer}")

    # ------------------------------------------------------------------
    # Cache update — store the synthesized (not raw) identity answer
    # ------------------------------------------------------------------
    cache_updates: list[dict] = []
    for entity in entities_needing_search:
        name = entity.get("name", "")
        entity_type = entity.get("type", "unknown")
        is_ambiguous = entity.get("ambiguous", False)
        resolved_sense = entity.get("resolved_sense") if is_ambiguous else None
        identity_q = f'"{name}" {entity_type} overview'
        answer = synthesized_results.get(identity_q, "")
        if answer:
            entity_context_parts.append(f"{name}: {answer}")
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
