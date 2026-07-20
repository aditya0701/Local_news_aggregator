"""Phase 6 (finalize_project.md Task F) gap-fill.

`tests/test_entity_cache.py` already covers TTL expiry, sense disambiguation,
and name-normalization collisions directly against `writer/entity_cache.py`'s
pure functions. The one case that file cannot cover on its own is behavioral:
does a fresh cache hit actually stop `_synthesize_sarvam()` from dispatching a
search for that entity? That requires driving the real function end-to-end
with the search layer mocked, which is why this lives here as its own file
rather than as one more case in `tests/test_entity_cache.py`.

Mocking pattern (fetch_page/_detect_language/_stage1_extract_queries/
load_cache/set_entity/save_cache/_stage2_editorial_strategy short-circuit,
search_web/ask_concise spies) is copied from
`tests/test_synthesize.py::TestSearchRoutingByAmbiguity`, which already proved
out driving `_synthesize_sarvam()` this way for the concise-vs-DDG routing
question — same technique, different question.
"""

import writer.synthesize as synthesize_mod

CLUSTER = [{"title": "some headline", "summary": "s" * 300, "url": "http://example.com/a"}]

STAGE1_RESULT = {
    "skip": False,
    "search_queries": [],
    "entities": [{"name": "CachedCo", "type": "company"}],
}

FRESH_HIT = {
    "canonical_name": "CachedCo",
    "entity_type": "company",
    "summary": "a company already known to the cache",
    "last_updated": "2026-07-18T00:00:00+00:00",
}


def _install_common_mocks(monkeypatch, get_entity_fn, search_web_calls, ask_concise_calls):
    monkeypatch.setattr(synthesize_mod, "fetch_page", lambda url: "x" * 300)
    monkeypatch.setattr(synthesize_mod, "_detect_language", lambda text: "en")
    monkeypatch.setattr(synthesize_mod, "_stage1_extract_queries", lambda *a, **k: dict(STAGE1_RESULT))
    monkeypatch.setattr(synthesize_mod, "load_cache", lambda: {})
    monkeypatch.setattr(synthesize_mod, "get_entity", get_entity_fn)
    monkeypatch.setattr(synthesize_mod, "set_entity", lambda *a, **k: None)
    monkeypatch.setattr(synthesize_mod, "save_cache", lambda cache: None)
    monkeypatch.setattr(synthesize_mod, "concise_configured", lambda: False)
    # Stage 2/3 aren't under test here; bail out cleanly right after search.
    monkeypatch.setattr(synthesize_mod, "_stage2_editorial_strategy", lambda *a, **k: None)

    def fake_search_web(queries, tiers):
        search_web_calls.append(list(queries))
        return {q: f"raw:{q}" for q in queries}

    def fake_ask_concise(q):
        ask_concise_calls.append(q)
        return f"concise answer: {q}"

    def fake_synthesize(identity_results, context_results, api_key):
        return {**identity_results, **context_results}

    monkeypatch.setattr(synthesize_mod, "search_web", fake_search_web)
    monkeypatch.setattr(synthesize_mod, "ask_concise", fake_ask_concise)
    monkeypatch.setattr(synthesize_mod, "_synthesize_search_results", fake_synthesize)


class TestCacheHitAvoidsSearch:
    def test_fresh_cache_hit_skips_search_entirely(self, monkeypatch):
        search_web_calls, ask_concise_calls = [], []
        get_entity_fn = lambda cache, name, resolved_sense=None: dict(FRESH_HIT)
        _install_common_mocks(monkeypatch, get_entity_fn, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(CLUSTER, "fake-api-key")

        assert search_web_calls == []
        assert ask_concise_calls == []

    def test_cache_miss_does_dispatch_search(self, monkeypatch):
        """Control case: same setup, but get_entity reports a miss (None) —
        confirms the previous test's silence is because of the cache hit, not
        because search is unconditionally skipped for some other reason."""
        search_web_calls, ask_concise_calls = [], []
        get_entity_fn = lambda cache, name, resolved_sense=None: None
        _install_common_mocks(monkeypatch, get_entity_fn, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(CLUSTER, "fake-api-key")

        ddg_queries = [q for call in search_web_calls for q in call]
        assert any("CachedCo" in q for q in ddg_queries)
