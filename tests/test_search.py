import sys
import types

import writer.search as search_mod
from writer.search import _ddg_search, _negative_keywords, _scrape_page, build_identity_query, search_web


class TestScrapePage:
    """_scrape_page is now a thin str-only wrapper around the shared
    writer.web_context.fetch_page — boilerplate-filtering/failure-handling
    behavior itself is covered by tests/test_web_context.py."""

    def test_returns_text_on_success(self, monkeypatch):
        monkeypatch.setattr(search_mod, "fetch_page", lambda url, max_chars: "real article content")
        assert _scrape_page("https://example.com/article") == "real article content"

    def test_returns_empty_string_when_fetch_page_errors(self, monkeypatch):
        monkeypatch.setattr(
            search_mod, "fetch_page", lambda url, max_chars: {"error": "boom", "url": url}
        )
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


class TestNegativeKeywords:
    def test_extracts_terms_after_not(self):
        assert _negative_keywords("AI-generated image name, not the Indian sweet") == ["indian", "sweet"]

    def test_no_not_clause_returns_empty(self):
        assert _negative_keywords("GitHub project for automating diary entries") == []

    def test_drops_stopwords_and_dedupes(self):
        result = _negative_keywords("not the of and Harry Harry Potter")
        assert result == ["harry", "potter"]

    def test_caps_at_three_terms(self):
        result = _negative_keywords("not the Fictional British Wizarding School Character Name")
        assert len(result) <= 3


class TestBuildIdentityQuery:
    def test_no_resolved_sense_matches_old_bare_query(self):
        assert build_identity_query("Rocket Lab", "company") == '"Rocket Lab" company overview'

    def test_appends_resolved_sense_as_context(self):
        query = build_identity_query(
            "Tom Riddle", "product", "GitHub project reference, not the Harry Potter character"
        )
        assert '"Tom Riddle" product overview' in query
        assert "GitHub project reference" in query

    def test_appends_negative_keywords_from_resolved_sense(self):
        query = build_identity_query(
            "Ladoo", "ai_model", "AI-generated image name, not the Indian sweet"
        )
        assert "-indian" in query
        assert "-sweet" in query

    def test_none_resolved_sense_adds_nothing_extra(self):
        query = build_identity_query("xovi", "product", None)
        assert query == '"xovi" product overview'


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
