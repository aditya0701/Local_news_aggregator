from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# Small, free, local model (~420MB, runs on CPU, no API key) — chosen over
# all-MiniLM-L6-v2 after direct empirical comparison (see CLAUDE.md
# "Deduplication / Clustering" for the full test table): MiniLM raised the
# one real semantic-duplicate case this replaces TF-IDF for, but raised a
# false positive even more, putting it ABOVE the true positive (no
# threshold could separate them). mpnet keeps that same false positive
# below the true positives it needs to catch.
_MODEL_NAME = "all-mpnet-base-v2"

# Tuned empirically against the same real false-positive/true-positive
# titles documented for the old TF-IDF version, plus the case TF-IDF
# couldn't catch (the "groupthink" pair, two headlines about the same
# story in genuinely different wording). At 0.44: both known true
# positives score above it (0.622, 0.460) and most known false positives
# score below it (0.417, 0.299, 0.267) — EXCEPT one confirmed false
# positive (0.645, two distinct Anthropic announcements — a product launch
# and a funding round) that scores higher than a true positive, so it
# cannot be excluded by any threshold. That specific miss is a deliberate,
# user-approved tradeoff, not an oversight: duplicate articles reaching
# readers (the failure TF-IDF still has) reads as the newspaper visibly
# repeating itself, which is worse than occasionally folding one distinct
# story into another's write-up. See CLAUDE.md for the full before/after
# evidence and the explicit reasoning for accepting this failure mode.
_SIMILARITY_THRESHOLD = 0.44

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def group_by_topic(items: list[dict]) -> list[list[dict]]:
    """Greedily group items whose titles describe the same underlying story.

    Uses sentence-embedding cosine similarity over titles only (no
    full-article-text fetch, to stay consistent with the rest of the
    pipeline's snippet-first approach). Unlike the TF-IDF version this
    replaces, embeddings compare meaning rather than shared vocabulary, so
    two headlines describing the same event in completely different
    wording can still be recognized as duplicates — the specific gap
    TF-IDF could not close (see CLAUDE.md).

    Trade-off, accepted deliberately: embeddings can also score two
    headlines as similar because they share a company/product context,
    even when the underlying events are different — the mirror-image
    failure mode of TF-IDF's under-merging. This is intentionally accepted
    for this pipeline: publishing two articles about the same story is a
    reader-visible embarrassment ("this paper is repeating itself"), while
    an occasional wrongly-merged pair of distinct stories is not.
    """
    if len(items) <= 1:
        return [[item] for item in items]

    titles = [item.get("title", "") or "" for item in items]
    try:
        embeddings = _get_model().encode(titles)
    except Exception as e:
        # Model unavailable (e.g. first-run download failure with no
        # network) — fail safe to "nothing matches" rather than crashing
        # the whole pipeline run, same graceful-degradation pattern used
        # everywhere else in this codebase.
        print(f"[cluster] embedding model unavailable, skipping clustering: {e}")
        return [[item] for item in items]

    similarity = cosine_similarity(embeddings)

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
            if similarity[i, j] >= _SIMILARITY_THRESHOLD:
                cluster.append(items[j])
                assigned[j] = True
        clusters.append(cluster)

    return clusters
