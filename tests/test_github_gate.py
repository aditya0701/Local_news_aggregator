import writer.github_gate as gate_mod
from writer.github_gate import _BATCH_SIZE, _judge_batch, filter_notable_repos


def _repo(name):
    return {"title": name, "summary": f"description of {name}", "url": f"https://github.com/x/{name}"}


class TestJudgeBatch:
    def test_keeps_only_approved_items(self, monkeypatch):
        batch = [_repo("a"), _repo("b")]
        monkeypatch.setattr(
            gate_mod, "_call_sarvam",
            lambda *a, **k: "1: APPROVE — genuine new model\n2: REJECT — just a guide",
        )
        result = _judge_batch(batch, "fake-key")
        assert result == [batch[0]]

    def test_fails_open_when_call_returns_none(self, monkeypatch):
        batch = [_repo("a"), _repo("b")]
        monkeypatch.setattr(gate_mod, "_call_sarvam", lambda *a, **k: None)
        result = _judge_batch(batch, "fake-key")
        assert result == batch

    def test_missing_verdict_defaults_to_kept(self, monkeypatch):
        batch = [_repo("a"), _repo("b")]
        monkeypatch.setattr(gate_mod, "_call_sarvam", lambda *a, **k: "1: REJECT — not relevant")
        result = _judge_batch(batch, "fake-key")
        # item 2 (index 1) had no verdict line at all -> defaults to kept
        assert result == [batch[1]]

    def test_tolerant_of_period_and_paren_separators(self, monkeypatch):
        batch = [_repo("a"), _repo("b")]
        monkeypatch.setattr(
            gate_mod, "_call_sarvam",
            lambda *a, **k: "1) APPROVE - fine\n2. reject - filler",
        )
        result = _judge_batch(batch, "fake-key")
        assert result == [batch[0]]


class TestFilterNotableRepos:
    def test_batches_large_input(self, monkeypatch):
        items = [_repo(f"r{i}") for i in range(_BATCH_SIZE + 2)]
        calls = []

        def fake_call(prompt, api_key, model, **kwargs):
            calls.append(prompt)
            n = prompt.count("\n") + 1  # rough; overridden below anyway
            return "\n".join(f"{i+1}: APPROVE — ok" for i in range(_BATCH_SIZE))

        monkeypatch.setattr(gate_mod, "_call_sarvam", fake_call)
        result = filter_notable_repos(items, "fake-key")
        assert len(calls) == 2  # two batches for BATCH_SIZE + 2 items
        assert len(result) == len(items)  # everything approved across both batches

    def test_empty_input(self, monkeypatch):
        monkeypatch.setattr(gate_mod, "_call_sarvam", lambda *a, **k: "")
        assert filter_notable_repos([], "fake-key") == []
