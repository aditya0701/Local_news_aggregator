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
