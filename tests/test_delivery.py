import json

from publish.delivery import SUMMARY_CHARS, build_card, build_delivery, normalize_category


def _article(article_id, **overrides):
    article = {
        "id": article_id,
        "title": "एक शीर्षक",
        "summary": "एक सारांश",
        "category": "general",
        "tags": ["OpenAI"],
        "url": f"https://example.com/{article_id}",
        "sources": [f"https://example.com/{article_id}"],
        "source": "rss",
        "first_seen": "2026-07-15T05:55:01.759728+00:00",
        "introduction_lede": "लीड",
        "deep_dive_and_context": "मुख्य भाग",
    }
    article.update(overrides)
    return article


class TestNormalizeCategory:
    def test_valid_key_passes_through(self):
        assert normalize_category("model_release") == "model_release"

    def test_hindi_label_maps_back_to_its_key(self):
        # A real article in the July archive (id fb2486151cd6) has its category
        # stored as the Hindi label instead of the machine key, which the
        # frontend's category filter can't match.
        assert normalize_category("सामान्य") == "general"
        assert normalize_category("मॉडल रिलीज") == "model_release"

    def test_truncated_key_recovers_via_unambiguous_prefix(self):
        assert normalize_category("repo") == "repo_analysis"

    def test_unknown_and_missing_fall_back_to_general(self):
        assert normalize_category("nonsense") == "general"
        assert normalize_category(None) == "general"
        assert normalize_category("") == "general"


class TestBuildCard:
    def test_short_summary_is_left_alone(self):
        card = build_card(_article("a1", summary="छोटा सारांश"))
        assert card["summary"] == "छोटा सारांश"

    def test_long_summary_is_cut_on_a_word_boundary_with_an_ellipsis(self):
        summary = "शब्द " * 200
        card = build_card(_article("a1", summary=summary))
        assert card["summary"].endswith("…")
        assert len(card["summary"]) <= SUMMARY_CHARS + 1
        # A word boundary cut, not a mid-word chop.
        assert not card["summary"].removesuffix("…").endswith("शब")

    def test_carries_a_sources_count_rather_than_the_url_list(self):
        # index.html only ever reads sources.length, so shipping every URL in
        # every card is page weight the grid never renders.
        card = build_card(_article("a1", sources=["https://a.com", "https://b.com"]))
        assert card["sources_count"] == 2
        assert "sources" not in card

    def test_sources_count_falls_back_to_the_single_url(self):
        card = build_card(_article("a1", sources=None))
        assert card["sources_count"] == 1

    def test_tags_are_capped_at_what_the_grid_renders(self):
        card = build_card(_article("a1", tags=["A", "B", "C", "D", "E"]))
        assert card["tags"] == ["A", "B", "C"]

    def test_empty_fields_are_dropped_rather_than_stored_as_null(self):
        card = build_card(_article("a1", image=None, feed_name=""))
        assert "image" not in card
        assert "feed_name" not in card

    def test_body_fields_stay_out_of_the_card(self):
        card = build_card(_article("a1"))
        assert "deep_dive_and_context" not in card
        assert "introduction_lede" not in card


class TestBuildDelivery:
    def test_writes_one_file_per_article_with_the_full_body(self, tmp_path):
        build_delivery([_article("a1"), _article("a2")], tmp_path)
        written = json.loads((tmp_path / "articles" / "a1.json").read_text(encoding="utf-8"))
        assert written["deep_dive_and_context"] == "मुख्य भाग"
        assert (tmp_path / "articles" / "a2.json").exists()

    def test_shards_the_index_by_month(self, tmp_path):
        build_delivery(
            [
                _article("a1", first_seen="2026-08-02T00:00:00+00:00"),
                _article("a2", first_seen="2026-07-15T00:00:00+00:00"),
            ],
            tmp_path,
        )
        assert [c["id"] for c in json.loads((tmp_path / "index" / "2026-08.json").read_text(encoding="utf-8"))] == ["a1"]
        assert [c["id"] for c in json.loads((tmp_path / "index" / "2026-07.json").read_text(encoding="utf-8"))] == ["a2"]

    def test_manifest_lists_newest_month_first(self, tmp_path):
        # index.html loads shards[0] on page load, so ordering decides whether
        # the homepage opens on the newest month or the oldest.
        build_delivery(
            [
                _article("a1", first_seen="2026-07-15T00:00:00+00:00"),
                _article("a2", first_seen="2026-08-02T00:00:00+00:00"),
            ],
            tmp_path,
        )
        manifest = json.loads((tmp_path / "index" / "manifest.json").read_text(encoding="utf-8"))
        assert [s["month"] for s in manifest["shards"]] == ["2026-08", "2026-07"]

    def test_manifest_counts_the_whole_archive_per_category(self, tmp_path):
        build_delivery(
            [
                _article("a1", category="model_release"),
                _article("a2", category="model_release"),
                _article("a3", category="सामान्य"),
            ],
            tmp_path,
        )
        manifest = json.loads((tmp_path / "index" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["total"] == 3
        assert manifest["categories"] == {"model_release": 2, "general": 1}

    def test_articles_without_an_id_are_skipped(self, tmp_path):
        # Nothing can link to them, so they'd be unreachable files.
        stats = build_delivery([_article("a1"), {"title": "no id"}], tmp_path)
        assert stats["total"] == 1

    def test_undated_articles_get_their_own_shard(self, tmp_path):
        build_delivery([_article("a1", first_seen=None)], tmp_path)
        assert (tmp_path / "index" / "undated.json").exists()

    def test_rerunning_unchanged_rewrites_nothing(self, tmp_path):
        # A daily run rebuilds the whole delivery layer; without this, every
        # article file would be rewritten and git would see the whole archive
        # as changed every morning.
        articles = [_article("a1"), _article("a2")]
        build_delivery(articles, tmp_path)
        stats = build_delivery(articles, tmp_path)
        assert stats["articles_written"] == 0
        assert stats["shards_written"] == 0

    def test_changed_article_is_rewritten(self, tmp_path):
        build_delivery([_article("a1")], tmp_path)
        stats = build_delivery([_article("a1", title="नया शीर्षक")], tmp_path)
        assert stats["articles_written"] == 1

    def test_removed_article_is_pruned(self, tmp_path):
        build_delivery([_article("a1"), _article("a2")], tmp_path)
        stats = build_delivery([_article("a1")], tmp_path)
        assert not (tmp_path / "articles" / "a2.json").exists()
        assert stats["pruned"] == 1

    def test_emptied_month_shard_is_pruned(self, tmp_path):
        build_delivery(
            [_article("a1", first_seen="2026-07-15T00:00:00+00:00"),
             _article("a2", first_seen="2026-08-02T00:00:00+00:00")],
            tmp_path,
        )
        build_delivery([_article("a2", first_seen="2026-08-02T00:00:00+00:00")], tmp_path)
        assert not (tmp_path / "index" / "2026-07.json").exists()
        assert (tmp_path / "index" / "manifest.json").exists()
