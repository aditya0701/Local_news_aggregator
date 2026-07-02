import json
import os
import re

import requests

from writer.entity_cache import get_entity, load_cache, save_cache, set_entity
from writer.search import search_web
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

_STAGE1_PROMPT = """Research assistant for TechDrishti (Hindi tech publication).
Extract entities and search queries from the article below.

Title: {title}
Summary: {summary}
Source: {source_text}

Output ONLY this JSON (no explanation):
{{"search_queries":["<comparison or context query>","<why-now query>"],"entities":[{{"name":"<EntityName>","type":"<type>"}}]}}

Types: company | startup | ai_model | product | person | researcher | technology | protocol | regulation | event | organization | material
For ambiguous names add: "ambiguous":true, "resolved_sense":"which meaning applies here and why"

Query rules (max 3, English only):
- ONLY comparisons, market context, "why now" — NOT "what is X" identity lookups
- Good: "Rocket Lab vs SpaceX revenue 2025" | Bad: "what is Rocket Lab" """

_STAGE2_PROMPT = """You are the editorial director of टेकदृष्टि (TechDrishti), a Hindi science and technology publication.

Article Title: {title}
Summary: {summary}

Source Text:
{source_text}

Research Context (entity knowledge + web search results):
{entity_context}

Build an editorial strategy for the Hindi article. Output ONLY valid JSON with exactly these keys:
{{
  "core_narrative": "the real story and underlying tension, one paragraph",
  "key_facts_and_quotes": "the facts/figures/statements that must appear in the article",
  "disambiguation_targets": "which terms need inline explanation and how to handle them",
  "category": "acquisition|model_release|ban_regulation|repo_analysis|general",
  "planned_length": "e.g. 4 paragraphs, moderate complexity"
}}

Keep every field short — this is a planning step, not a writing step."""

_STAGE3_PROMPT = """You are writing for टेकदृष्टि (TechDrishti), a calm, clear, trustworthy Hindi science and technology publication.

Readers: educated, curious Hindi-speaking tech enthusiasts.
Voice: शांत, स्पष्ट, भरोसेमंद — no hype, no clickbait.
Goal: make the READER feel smarter. Clean, modern, direct Hindi — no heavy tatsam vocabulary.

Editorial Strategy:
{strategy}

Source Material — Title: {title}
{source_text_block}

Research Context:
{entity_context}

STRATEGIC_ANALYSIS instruction: {category_framing}

Write the article now using this EXACT labeled format (each label on its own line, content follows the colon on the same line):

TITLE: <one-line insightful Hindi headline>
CONCEPT_BOX: <1-3 sentences explaining the single hardest concept or name for a newcomer>
LEDE: <opening paragraph — break the news, set tone, introduce key entities>
DEEP_DIVE_AND_CONTEXT: <core of the article — mechanics, numbers, who said what, woven into narrative prose>
STRATEGIC_ANALYSIS: <connect to the broader ecosystem per the instruction above>
CONCLUSION_AND_SIGNIFICANCE: <what this means for the reader or developer; end with a strong closing line>

Rules:
- लेख पूरी तरह हिंदी में लिखें। (Write the entire article in Hindi — every word of every section.)
- Transliterate technical terms: हिंदी (English) — e.g. ओपन सोर्स (Open Source)
- Never translate or closely paraphrase source sentences — write fresh Hindi sentences from the facts
- Do not add any fact not present in the provided sources
- Hedge all predictions: हो सकता है, संभावना है — never state predictions as fact
- 3-6 well-developed paragraphs total"""

_LABELED_SECTIONS = [
    "TITLE",
    "CONCEPT_BOX",
    "LEDE",
    "DEEP_DIVE_AND_CONTEXT",
    "STRATEGIC_ANALYSIS",
    "CONCLUSION_AND_SIGNIFICANCE",
]

# ---------------------------------------------------------------------------
# Sarvam API helpers
# ---------------------------------------------------------------------------


def _call_sarvam(
    prompt: str, api_key: str, model: str, system: str | None = None
) -> str | None:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        response = requests.post(
            SARVAM_URL,
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "max_tokens": 4096},
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


def _parse_labeled_text(raw: str | None) -> dict | None:
    """Parse Stage 3 labeled-text output into a dict of article fields."""
    if not raw:
        return None
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        matched = False
        for label in _LABELED_SECTIONS:
            if line.startswith(f"{label}:"):
                current = label
                fields[current] = [line[len(label) + 1:].strip()]
                matched = True
                break
        if not matched and current:
            stripped = line.strip()
            if stripped:
                fields[current].append(stripped)

    result = {k: " ".join(v).strip() for k, v in fields.items()}
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


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def _stage1_extract_queries(
    title: str, summary: str, source_text: str, api_key: str
) -> dict | None:
    prompt = _STAGE1_PROMPT.format(
        title=title,
        summary=summary[:500],
        source_text=source_text[:800] if source_text else "(not available)",
    )
    raw = _call_sarvam(
        prompt,
        api_key,
        _MODEL_FAST,
        system="/no_think Be direct. Output JSON only.",
    )
    return _parse_json_response(raw)


def _stage2_editorial_strategy(
    title: str,
    summary: str,
    source_text: str,
    entity_context: str,
    api_key: str,
) -> dict | None:
    prompt = _STAGE2_PROMPT.format(
        title=title,
        summary=summary[:500],
        source_text=source_text[:2000] if source_text else "(not available)",
        entity_context=entity_context[:3000],
    )
    raw = _call_sarvam(prompt, api_key, _MODEL_QUALITY)
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
    source_text_block = (
        f"Source Text:\n{source_text[:2500]}" if source_text else ""
    )
    prompt = _STAGE3_PROMPT.format(
        strategy=json.dumps(strategy, ensure_ascii=False, indent=2),
        title=title,
        source_text_block=source_text_block,
        entity_context=entity_context[:2500],
        category_framing=category_framing,
    )
    raw = _call_sarvam(
        prompt,
        api_key,
        _MODEL_QUALITY,
        system="/no_think Write the article directly using the labeled format. No preamble.",
    )
    return _parse_labeled_text(raw)


# ---------------------------------------------------------------------------
# Sarvam orchestrator
# ---------------------------------------------------------------------------


def _synthesize_sarvam(cluster: list[dict], api_key: str) -> dict | None:
    primary = cluster[0]
    title = primary.get("title", "")
    summary = primary.get("summary", "")
    source_url = primary.get("url", "")

    # Stage 0: scrape source article
    source_text = scrape_source(source_url)
    print(f"[synthesize] stage0 scraped {len(source_text)} chars from {source_url[:60]}")

    # Stage 1: extract search queries + entities (30B, fast + cheap)
    stage1 = _stage1_extract_queries(title, summary, source_text, api_key) or {}
    search_queries = stage1.get("search_queries", [])
    entities = stage1.get("entities", [])
    print(f"[synthesize] stage1 -> {len(search_queries)} queries, {len(entities)} entities")

    # Entity cache check — collect misses for identity queries
    cache = load_cache()
    entity_context_parts: list[str] = []
    entities_needing_search: list[dict] = []

    for entity in entities:
        name = entity.get("name", "")
        is_ambiguous = entity.get("ambiguous", False)
        resolved_sense = entity.get("resolved_sense") if is_ambiguous else None

        record = get_entity(cache, name, resolved_sense=resolved_sense)
        if record:
            sense_label = record.get("sense_label", "")
            label = f"{name} ({sense_label})" if sense_label else name
            entity_context_parts.append(f"{label}: {record['summary']}")
        else:
            entities_needing_search.append(entity)

    # Build entity identity queries from code (NOT from model output)
    identity_queries = [
        f'"{e["name"]}" {e.get("type", "unknown")} overview'
        for e in entities_needing_search
    ]

    # Model queries are context/comparison/"why now" only (already capped at 3 by prompt)
    model_queries = search_queries[:3]
    all_queries = identity_queries + model_queries

    search_results: dict[str, str] = {}
    if all_queries:
        search_results = search_web(all_queries)
        for q, text in search_results.items():
            if text:
                entity_context_parts.append(f"[{q}]:\n{text[:600]}")
    print(
        f"[synthesize] search returned {len(search_results)} results "
        f"({len(identity_queries)} identity, {len(model_queries)} context)"
    )

    # Store search results to cache keyed by identity query
    for entity in entities_needing_search:
        name = entity.get("name", "")
        entity_type = entity.get("type", "unknown")
        is_ambiguous = entity.get("ambiguous", False)
        resolved_sense = entity.get("resolved_sense") if is_ambiguous else None

        identity_q = f'"{name}" {entity_type} overview'
        text = search_results.get(identity_q, "")
        if text:
            set_entity(cache, name, entity_type, text[:300].strip(), resolved_sense=resolved_sense)
    save_cache(cache)

    entity_context = "\n\n".join(entity_context_parts) or "No additional context available."

    # Stage 2: editorial strategy (105B, quality)
    strategy = _stage2_editorial_strategy(title, summary, source_text, entity_context, api_key)
    if not strategy:
        print("[synthesize] stage2 failed")
        return None
    print(f"[synthesize] stage2 -> category={strategy.get('category')}")

    # Stage 3: write Hindi article (105B, labeled text output)
    article_fields = _stage3_write_article(title, source_text, entity_context, strategy, api_key)
    if not article_fields:
        print("[synthesize] stage3 failed")
        return None
    print("[synthesize] stage3 done")

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
        "sources": [item["url"] for item in cluster if item.get("url")],
    }

    if len(cluster) > 1:
        result["source"] = "synthesized"

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
        return _synthesize_sarvam(cluster, api_key)

    return _synthesize_ollama(cluster, language)
