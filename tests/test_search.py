import sys
import types

import writer.search as search_mod
from writer.search import _ddg_search, _scrape_page, search_web


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class TestScrapePage:
    def test_filters_boilerplate_paragraphs(self, monkeypatch):
        html = (
            "<html><body>"
            "<p>Please accept all cookies to continue</p>"
            "<p>This is the real article content that matters.</p>"
            "</body></html>"
        )
        monkeypatch.setattr(
            search_mod.requests, "get", lambda *a, **k: FakeResponse(html)
        )
        result = _scrape_page("https://example.com/article")
        assert "cookie" not in result.lower()
        assert "real article content" in result

    def test_returns_empty_on_request_failure(self, monkeypatch):
        def raise_err(*a, **k):
            raise search_mod.requests.RequestException("boom")

        monkeypatch.setattr(search_mod.requests, "get", raise_err)
        assert _scrape_page("https://example.com/broken") == ""


class TestDdgSearch:
    def _install_fake_ddgs(self, monkeypatch, hits):
        class FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query, max_results=6):
                return hits

        fake_module = types.ModuleType("ddgs")
        fake_module.DDGS = FakeDDGS
        monkeypatch.setitem(sys.modules, "ddgs", fake_module)

    def test_excludes_wikipedia_domains(self, monkeypatch):
        hits = [
            {"href": "https://en.wikipedia.org/wiki/Rocket_Lab", "body": "wiki text [15]"},
            {"href": "https://techcrunch.com/rocket-lab", "body": "real news text"},
        ]
        self._install_fake_ddgs(monkeypatch, hits)
        monkeypatch.setattr(search_mod, "_scrape_page", lambda url: "")
        result = _ddg_search("Rocket Lab")
        assert "wiki text" not in result
        assert "real news text" in result

    def test_empty_hits_returns_empty_string(self, monkeypatch):
        self._install_fake_ddgs(monkeypatch, [])
        assert _ddg_search("nonexistent query xyz") == ""


class TestSearchWeb:
    def test_first_nonempty_tier_wins(self):
        tier1 = lambda q: ""
        tier2 = lambda q: f"result for {q}"
        results = search_web(["a query"], tiers=[tier1, tier2])
        assert results["a query"] == "result for a query"

    def test_stops_at_first_tier_with_content(self):
        calls = []

        def tier1(q):
            calls.append("tier1")
            return "first tier result"

        def tier2(q):
            calls.append("tier2")
            return "second tier result"

        results = search_web(["q"], tiers=[tier1, tier2])
        assert results["q"] == "first tier result"
        assert calls == ["tier1"]

    def test_all_tiers_empty_returns_empty_string(self):
        tiers = [lambda q: "", lambda q: ""]
        results = search_web(["q"], tiers=tiers)
        assert results["q"] == ""

    def test_multiple_queries_handled_independently(self):
        tier = lambda q: f"answer:{q}"
        results = search_web(["q1", "q2"], tiers=[tier])
        assert results == {"q1": "answer:q1", "q2": "answer:q2"}
