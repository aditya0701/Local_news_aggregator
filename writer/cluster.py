import re

_STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those", "it", "its",
    "i", "we", "they", "he", "she", "you", "in", "on", "at", "for",
    "with", "and", "or", "but", "is", "are", "was", "were", "of", "to",
    "from", "as", "by", "how", "why", "what", "when", "after", "before",
    "new", "now", "out", "up", "into", "over", "than", "more",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _significant_words(title: str) -> set[str]:
    words = _WORD_RE.findall((title or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    overlap = a & b
    if len(overlap) >= 2:
        return True
    smaller = min(len(a), len(b))
    return smaller > 0 and len(overlap) / smaller >= 0.5


def group_by_topic(items: list[dict]) -> list[list[dict]]:
    """Greedily group items whose titles share enough significant words.

    Works on title overlap only (no full-text fetch) so it stays consistent
    with the rest of the pipeline's snippet-only approach to source material.
    """
    word_sets = [_significant_words(item.get("title", "")) for item in items]
    assigned = [False] * len(items)
    clusters: list[list[dict]] = []

    for i, item in enumerate(items):
        if assigned[i]:
            continue
        cluster = [item]
        assigned[i] = True
        for j in range(i + 1, len(items)):
            if assigned[j]:
                continue
            if _similar(word_sets[i], word_sets[j]):
                cluster.append(items[j])
                assigned[j] = True
        clusters.append(cluster)

    return clusters
