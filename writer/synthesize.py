import re

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b-instruct-q4_K_M"

_RESPONSE_RE = re.compile(r"TITLE:\s*(.+?)\s*BODY:\s*(.+)", re.DOTALL)

_PROMPT_TEMPLATE = """You are a journalist for TechDrishti, a Hindi-language science and technology publication.
Below are facts from {count} English source articles about the same topic. Write an
ORIGINAL Hindi news article based only on these facts — do not translate or closely
paraphrase any single source's sentences, write fresh sentences in your own words.
Do not add any fact that isn't present in the sources below. Keep it to 3-5 sentences.

Sources:
{sources}

Respond in EXACTLY this format and nothing else:
TITLE: <one line Hindi headline>
BODY: <3-5 sentence Hindi article body>
"""


def _build_prompt(cluster: list[dict]) -> str:
    sources = "\n".join(
        f"{i + 1}. {item.get('title', '')} — {item.get('summary', '')}"
        for i, item in enumerate(cluster)
    )
    return _PROMPT_TEMPLATE.format(count=len(cluster), sources=sources)


def _call_ollama(prompt: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": prompt, "stream": False},
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("response", "")


def synthesize_article(cluster: list[dict], language: str = "hindi") -> dict | None:
    """Write one original Hindi article from facts in a cluster of related items.

    Returns None on any failure so callers can fall back to per-item translation
    instead of losing the whole cluster.
    """
    if language != "hindi":
        return None

    prompt = _build_prompt(cluster)
    try:
        raw = _call_ollama(prompt)
    except requests.RequestException:
        return None

    match = _RESPONSE_RE.search(raw)
    if not match:
        return None

    title, body = match.group(1).strip(), match.group(2).strip()
    if not title or not body:
        return None

    primary = cluster[0]
    return {
        **primary,
        "language": language,
        "title": title,
        "summary": body,
        "source": "synthesized",
        "sources": [item["url"] for item in cluster if item.get("url")],
    }
