import re

from deep_translator import GoogleTranslator

LANGUAGES = {
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "bengali": "bn",
    "marathi": "mr",
    "gujarati": "gu",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "urdu": "ur",
}

# Words that get capitalized for grammatical reasons (sentence start, title
# case) rather than because they are a proper noun — strip these off the
# edges of a candidate match instead of discarding the whole match.
_EDGE_STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those", "it", "its",
    "i", "we", "they", "he", "she", "you", "in", "on", "at", "for",
    "with", "and", "or", "but", "is", "are", "was", "were", "of", "to",
    "from", "as", "by", "how", "why", "what", "when", "after", "before",
}

_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*(?:[-‑][A-Za-z0-9]+)*(?:\s+[A-Z][a-zA-Z0-9]*)*\b")

# Cache transliterations across items so repeated names (OpenAI, Google,
# GitHub, ...) only cost one translation call each per run.
_name_cache: dict[str, str] = {}


def _safe_translate(translator: GoogleTranslator, text: str) -> str:
    if not text:
        return ""
    try:
        return translator.translate(text)
    except Exception:
        return text


_SENTENCE_BOUNDARY = ".!?"


def _is_strong_candidate(name: str) -> bool:
    """A name is a confident brand/proper-noun signal on its own.

    Covers multi-word names ("Google Gemini"), names with digits
    ("GPT-5.6"), acronyms ("NASA", "IBM"), and CamelCase ("OpenAI",
    "DeepSeek") — these are capitalized regardless of sentence position.
    """
    if " " in name or any(ch.isdigit() for ch in name):
        return True
    if name.isupper() and len(name) >= 2:
        return True
    return any(ch.isupper() for ch in name[1:])


def _has_non_initial_occurrence(text: str, name: str) -> bool:
    """True if `name` appears capitalized somewhere other than a sentence start.

    A single Title-Case word that *only* ever shows up right after a
    period (or at the very start of the text) is most likely capitalized
    for ordinary grammar reasons, not because it's a proper noun.
    """
    for match in re.finditer(re.escape(name), text):
        prefix = text[:match.start()].rstrip()
        if not prefix or prefix[-1] in _SENTENCE_BOUNDARY:
            continue
        return True
    return False


def _extract_proper_nouns(text: str) -> list[str]:
    text = text or ""
    candidates = []
    seen = set()
    for match in _PROPER_NOUN_RE.finditer(text):
        words = match.group().split()
        while words and words[0].lower() in _EDGE_STOPWORDS:
            words.pop(0)
        while words and words[-1].lower() in _EDGE_STOPWORDS:
            words.pop()
        if len(words) > 4:
            continue
        name = " ".join(words)
        key = name.lower()
        if not name or len(name) < 2 or key in seen:
            continue
        if not _is_strong_candidate(name) and not _has_non_initial_occurrence(text, name):
            continue
        seen.add(key)
        candidates.append(name)
    return candidates


def _transliterate_name(name: str, translator: GoogleTranslator) -> str:
    if name not in _name_cache:
        _name_cache[name] = _safe_translate(translator, name)
    return _name_cache[name]


_TOKEN_RE = re.compile(r"\S+")
_EDGE_PUNCT = ".,!?:;।\"'()‘’“”-"


def _find_token_span(translated: str, rendered: str) -> tuple[int, int] | None:
    """Find rendered as a contiguous run of whole tokens in translated text.

    A plain substring search can match inside an unrelated word (e.g. a
    short rendering matching letters buried in a longer word), corrupting
    the sentence on insertion — so we only ever match whole tokens.
    """
    target_tokens = [t.strip(_EDGE_PUNCT) for t in rendered.split() if t.strip(_EDGE_PUNCT)]
    if not target_tokens:
        return None

    spans = [(m.start(), m.end()) for m in _TOKEN_RE.finditer(translated)]
    norm = [translated[start:end].strip(_EDGE_PUNCT) for start, end in spans]

    n = len(target_tokens)
    for i in range(len(spans) - n + 1):
        if norm[i:i + n] == target_tokens:
            return spans[i][0], spans[i + n - 1][1]
    return None


def _annotate_proper_nouns(original: str, translated: str, translator: GoogleTranslator) -> str:
    if not original or not translated:
        return translated
    for name in _extract_proper_nouns(original):
        if f"({name})" in translated:
            continue
        rendered = _transliterate_name(name, translator).strip()
        if not rendered or rendered.lower() == name.lower():
            continue
        span = _find_token_span(translated, rendered)
        if span is None:
            continue
        _, end = span
        translated = f"{translated[:end]} ({name}){translated[end:]}"
    return translated


def translate_item(item: dict, language: str = "hindi") -> dict:
    lang_code = LANGUAGES.get(language, "hi")
    translator = GoogleTranslator(source="auto", target=lang_code)

    title = _safe_translate(translator, item["title"])
    summary = _safe_translate(translator, item["summary"])

    if language == "hindi":
        title = _annotate_proper_nouns(item["title"], title, translator)
        summary = _annotate_proper_nouns(item["summary"], summary, translator)

    return {
        **item,
        "language": language,
        "title": title,
        "summary": summary,
    }
