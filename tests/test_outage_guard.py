"""Guards against silently publishing degraded output when Sarvam/translation fail.

Background: Sarvam credit ran out on 2026-10-23 and ~1,000 untranslated English
articles were published over the following month because every failure was
swallowed and the Google-Translate fallback returned the original text.
"""
import pytest
import requests

import pipeline as pipeline_mod
import translator.translate as translate_mod
import writer.synthesize as synthesize_mod
from writer.synthesize import SarvamOutage, _call_sarvam


class _Resp:
    def __init__(self, status_code, body="", payload=None):
        self.status_code = status_code
        self.text = body
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


def _ok():
    return _Resp(200, payload={"choices": [{"message": {"content": "hi"}}]})


@pytest.fixture(autouse=True)
def _reset_counter():
    synthesize_mod._consecutive_failures = 0
    yield
    synthesize_mod._consecutive_failures = 0


class TestSarvamOutage:
    @pytest.mark.parametrize("status", [401, 402, 403])
    def test_auth_or_billing_status_aborts_immediately(self, monkeypatch, status):
        monkeypatch.setattr(synthesize_mod.requests, "post", lambda *a, **k: _Resp(status, "no credit"))
        with pytest.raises(SarvamOutage, match=str(status)):
            _call_sarvam("p", "key", "m")

    def test_single_transient_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(synthesize_mod.requests, "post", lambda *a, **k: _Resp(500))
        assert _call_sarvam("p", "key", "m") is None

    def test_consecutive_failures_trip_the_breaker(self, monkeypatch):
        monkeypatch.setattr(synthesize_mod.requests, "post", lambda *a, **k: _Resp(500))
        for _ in range(synthesize_mod._OUTAGE_THRESHOLD - 1):
            assert _call_sarvam("p", "key", "m") is None
        with pytest.raises(SarvamOutage):
            _call_sarvam("p", "key", "m")

    def test_success_resets_the_counter(self, monkeypatch):
        responses = iter([_Resp(500)] * 4 + [_ok()] + [_Resp(500)] * 4)
        monkeypatch.setattr(synthesize_mod.requests, "post", lambda *a, **k: next(responses))
        results = [_call_sarvam("p", "key", "m") for _ in range(9)]
        assert results == [None] * 4 + ["hi"] + [None] * 4


class _FakeGoogle:
    fail = False

    def __init__(self, source, target):
        pass

    def translate(self, text):
        if _FakeGoogle.fail:
            raise RuntimeError("TooManyRequests")
        return "नमस्ते " + text


class TestTranslateItemStrict:
    ITEM = {"title": "OpenAI ships a model", "summary": "A longer summary.", "url": "u", "source": "rss"}

    def test_returns_item_when_translation_worked(self, monkeypatch):
        monkeypatch.setattr(translate_mod, "GoogleTranslator", _FakeGoogle)
        _FakeGoogle.fail = False
        result = translate_mod.translate_item(self.ITEM, "hindi")
        assert result is not None and result["language"] == "hindi"

    def test_returns_none_when_translator_fails_and_text_stays_english(self, monkeypatch):
        monkeypatch.setattr(translate_mod, "GoogleTranslator", _FakeGoogle)
        _FakeGoogle.fail = True
        try:
            assert translate_mod.translate_item(self.ITEM, "hindi") is None
        finally:
            _FakeGoogle.fail = False


class TestPipelineAborts:
    def test_run_exits_nonzero_and_writes_nothing_on_outage(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pipeline_mod, "OUTPUT_DIR", tmp_path / "output")
        monkeypatch.setattr(
            pipeline_mod, "collect",
            lambda: [{"title": "t", "url": "https://e.com/a", "summary": "s" * 300, "source": "rss"}],
        )
        monkeypatch.setattr(pipeline_mod, "group_by_topic", lambda items: [items])

        def boom(cluster, language):
            raise SarvamOutage("no credit")

        monkeypatch.setattr(pipeline_mod, "synthesize_article", boom)
        with pytest.raises(SystemExit) as exc:
            pipeline_mod.run()
        assert exc.value.code == 1
        assert not (tmp_path / "output" / "articles_hindi.json").exists()

    def test_untranslated_items_are_not_stored(self, monkeypatch, tmp_path):
        out = tmp_path / "output"
        monkeypatch.setattr(pipeline_mod, "OUTPUT_DIR", out)
        monkeypatch.setattr(
            pipeline_mod, "collect",
            lambda: [{"title": "t", "url": "https://e.com/a", "summary": "s", "source": "rss"}],
        )
        monkeypatch.setattr(pipeline_mod, "group_by_topic", lambda items: [items])
        monkeypatch.setattr(pipeline_mod, "synthesize_article", lambda c, l: None)
        monkeypatch.setattr(pipeline_mod, "translate_item", lambda item, lang: None)
        pipeline_mod.run()
        import json
        assert json.loads((out / "articles_hindi.json").read_text(encoding="utf-8")) == []
