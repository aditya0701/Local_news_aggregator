import pipeline as pipeline_mod
from pipeline import _article_id


class TestArticleId:
    def test_deterministic_for_same_url(self):
        url = "https://example.com/article"
        assert _article_id(url) == _article_id(url)

    def test_different_urls_produce_different_ids(self):
        assert _article_id("https://example.com/a") != _article_id("https://example.com/b")

    def test_id_is_twelve_hex_chars(self):
        article_id = _article_id("https://example.com/article")
        assert len(article_id) == 12
        int(article_id, 16)  # raises ValueError if not valid hex


class TestCollectTagsFeedName:
    def test_rss_items_get_tagged_with_their_source_feed_name(self, monkeypatch, tmp_path):
        (tmp_path / "feeds.yaml").write_text(
            "feeds:\n"
            "  - name: Test Feed One\n"
            "    url: https://example.com/feed1\n"
            "  - name: Test Feed Two\n"
            "    url: https://example.com/feed2\n"
        )
        (tmp_path / "github.yaml").write_text("queries: []\n")
        monkeypatch.setattr(pipeline_mod, "CONFIG_DIR", tmp_path)

        def fake_fetch_feed(url):
            return [{"title": f"item from {url}", "url": url, "summary": "s", "source": "rss"}]

        monkeypatch.setattr(pipeline_mod, "fetch_feed", fake_fetch_feed)

        items = pipeline_mod.collect()
        assert len(items) == 2
        names = {item["feed_name"] for item in items}
        assert names == {"Test Feed One", "Test Feed Two"}

    def test_feed_name_distinguishes_items_from_different_feeds(self, monkeypatch, tmp_path):
        (tmp_path / "feeds.yaml").write_text(
            "feeds:\n"
            "  - name: Hacker News\n"
            "    url: https://example.com/hn\n"
            "  - name: TechCrunch\n"
            "    url: https://example.com/tc\n"
        )
        (tmp_path / "github.yaml").write_text("queries: []\n")
        monkeypatch.setattr(pipeline_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(
            pipeline_mod, "fetch_feed",
            lambda url: [{"title": "same-ish title", "url": url, "summary": "s", "source": "rss"}],
        )

        items = pipeline_mod.collect()
        by_feed = {item["url"]: item["feed_name"] for item in items}
        assert by_feed["https://example.com/hn"] == "Hacker News"
        assert by_feed["https://example.com/tc"] == "TechCrunch"
