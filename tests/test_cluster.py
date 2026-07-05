from writer.cluster import group_by_topic


def _item(title):
    return {"title": title, "url": f"https://example.com/{hash(title)}"}


class TestGroupByTopic:
    def test_genuinely_same_story_gets_merged(self):
        # Real pair from a live multi-feed batch: a digest post ("The
        # Download") that name-checks the same story as a dedicated article
        # on it. Verified with all-mpnet-base-v2: this pair scores 0.622
        # similarity, comfortably above the 0.44 threshold.
        items = [
            _item("Why California's carbon manure math doesn't add up"),
            _item("The Download: Anthropic launches Claude Science, and California's carbon manure math"),
            _item("A new GPU architecture ships this year"),
            _item("Local bakery wins regional award"),
            _item("Google redesigns its search box after 25 years"),
            _item("DeepMind and A24 announce research partnership"),
        ]
        clusters = group_by_topic(items)
        merged = [c for c in clusters if len(c) > 1]
        assert len(merged) == 1
        assert len(merged[0]) == 2

    def test_same_story_in_genuinely_different_wording_gets_merged(self):
        # The specific gap TF-IDF (bag-of-words) could never close: same
        # underlying story, almost no shared vocabulary. TF-IDF scored this
        # pair only 0.236 (indistinguishable from confirmed false
        # positives); all-mpnet-base-v2 scores it 0.460, above threshold.
        # This is the exact case the switch to embeddings was made for —
        # duplicate articles reaching readers is treated as worse than an
        # occasional wrongly-merged pair of distinct stories (see
        # CLAUDE.md "Deduplication / Clustering").
        items = [
            _item("The Download: a startup has a solution for AI's groupthink problem"),
            _item("LLMs are stuck in a groupthink groove, and one startup thinks it has the fix"),
        ]
        clusters = group_by_topic(items)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_unrelated_stories_sharing_one_generic_word_stay_separate(self):
        # Regression: a previous TF-IDF/word-overlap version merged these
        # purely because both titles contain "disaster" — a single generic
        # word, no real connection. Verified with all-mpnet-base-v2: scores
        # 0.267, below the 0.44 threshold.
        items = [_item("Xbox is a disaster"), _item("Review: Supergirl is not the disaster its low box office suggests")]
        clusters = group_by_topic(items)
        assert len(clusters) == 2

    def test_same_company_related_products_stay_separate_but_one_pair_does_not(self):
        # Embeddings compare meaning, not just shared vocabulary, so unlike
        # the old TF-IDF version this can't guarantee every same-company
        # pair stays separate — verified directly: "Claude Science" and
        # "Cowork" correctly stay separate (0.417, below threshold), but
        # "Claude Science" and the funding-round item score 0.645, above
        # threshold, and DO merge. This is a confirmed, accepted false
        # positive (see CLAUDE.md) — the trade-off was chosen deliberately
        # because it's the mirror-image failure of the case this design
        # switch was made to fix, and judged less harmful than publishing
        # a duplicate.
        items = [
            _item("Claude Science is Anthropic's newest flagship product"),
            _item("Anthropic launches Cowork, a Claude Desktop agent that works in your files"),
            _item("Claude Code costs up to $200 a month, competitors respond"),
            _item("Anthropic raises new funding round for Claude infrastructure"),
        ]
        clusters = group_by_topic(items)
        merged = [c for c in clusters if len(c) > 1]
        assert len(merged) == 1
        assert {i["title"] for i in merged[0]} == {
            "Claude Science is Anthropic's newest flagship product",
            "Anthropic raises new funding round for Claude infrastructure",
        }
        singles = [c for c in clusters if len(c) == 1]
        assert len(singles) == 2

    def test_filler_word_overlap_does_not_merge_unrelated_titles(self):
        # Regression: "here"/"first" are generic filler words that inflated
        # overlap between two unrelated stories under the old word-overlap
        # approach. Verified with all-mpnet-base-v2: scores 0.299, below
        # the 0.44 threshold.
        items = [
            _item("Google just redesigned the search box for the first time in 25 years — here's why it matters"),
            _item("Google DeepMind and A24 announce first-of-its-kind research partnership"),
        ]
        clusters = group_by_topic(items)
        assert len(clusters) == 2

    def test_completely_unrelated_titles_never_merge(self):
        items = [_item("A new GPU architecture ships this year"), _item("Local bakery wins regional award")]
        clusters = group_by_topic(items)
        assert len(clusters) == 2

    def test_single_item_returns_single_cluster(self):
        clusters = group_by_topic([_item("Some solo headline")])
        assert len(clusters) == 1
        assert len(clusters[0]) == 1

    def test_empty_input(self):
        assert group_by_topic([]) == []
