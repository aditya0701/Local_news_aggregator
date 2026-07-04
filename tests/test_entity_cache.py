from datetime import datetime, timedelta, timezone

from writer.entity_cache import _is_fresh, _normalize, get_entity, set_entity


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestNormalize:
    def test_lowercases_and_strips_punctuation(self):
        assert _normalize("OpenAI, Inc.") == "openai inc"

    def test_strips_surrounding_whitespace(self):
        assert _normalize("  Bolt Graphics  ") == "bolt graphics"


class TestIsFresh:
    def test_record_within_ttl_is_fresh(self):
        assert _is_fresh({"last_updated": _iso_days_ago(10)}) is True

    def test_record_past_ttl_is_stale(self):
        assert _is_fresh({"last_updated": _iso_days_ago(46)}) is False

    def test_malformed_record_is_not_fresh(self):
        assert _is_fresh({"last_updated": "not-a-date"}) is False

    def test_missing_field_is_not_fresh(self):
        assert _is_fresh({}) is False


class TestGetEntity:
    def test_unknown_entity_returns_none(self):
        assert get_entity({}, "Nonexistent Corp") is None

    def test_unambiguous_fresh_record_returned(self):
        cache = {"bolt graphics": {"summary": "a GPU startup", "last_updated": _iso_days_ago(1)}}
        record = get_entity(cache, "Bolt Graphics")
        assert record is not None
        assert record["summary"] == "a GPU startup"

    def test_unambiguous_stale_record_returns_none(self):
        cache = {"bolt graphics": {"summary": "a GPU startup", "last_updated": _iso_days_ago(50)}}
        assert get_entity(cache, "Bolt Graphics") is None

    def test_ambiguous_without_resolved_sense_returns_none(self):
        cache = {"lean": {"senses": [{"sense_label": "programming language", "summary": "...", "last_updated": _iso_days_ago(1)}]}}
        assert get_entity(cache, "Lean") is None

    def test_ambiguous_with_matching_fresh_sense(self):
        cache = {
            "lean": {
                "senses": [
                    {"sense_label": "programming language", "summary": "a theorem prover", "last_updated": _iso_days_ago(1)},
                ]
            }
        }
        record = get_entity(cache, "Lean", resolved_sense="programming language")
        assert record is not None
        assert record["summary"] == "a theorem prover"

    def test_ambiguous_with_matching_but_stale_sense_returns_none(self):
        cache = {
            "lean": {
                "senses": [
                    {"sense_label": "programming language", "summary": "a theorem prover", "last_updated": _iso_days_ago(60)},
                ]
            }
        }
        assert get_entity(cache, "Lean", resolved_sense="programming language") is None

    def test_ambiguous_with_non_matching_sense_returns_none(self):
        cache = {
            "lean": {
                "senses": [
                    {"sense_label": "programming language", "summary": "...", "last_updated": _iso_days_ago(1)},
                ]
            }
        }
        assert get_entity(cache, "Lean", resolved_sense="manufacturing methodology") is None


class TestSetEntity:
    def test_unambiguous_entity_overwrites_flat_record(self):
        cache = {}
        set_entity(cache, "Bolt Graphics", "startup", "a GPU startup")
        assert cache["bolt graphics"]["summary"] == "a GPU startup"
        assert cache["bolt graphics"]["canonical_name"] == "Bolt Graphics"

        set_entity(cache, "Bolt Graphics", "startup", "updated summary")
        assert cache["bolt graphics"]["summary"] == "updated summary"

    def test_ambiguous_entity_appends_new_sense(self):
        cache = {}
        set_entity(cache, "Lean", "technology", "a theorem prover", resolved_sense="programming language")
        set_entity(cache, "Lean", "methodology", "a manufacturing approach", resolved_sense="manufacturing methodology")
        senses = cache["lean"]["senses"]
        assert len(senses) == 2
        labels = {s["sense_label"] for s in senses}
        assert labels == {"programming language", "manufacturing methodology"}

    def test_ambiguous_entity_never_overwrites_existing_sense(self):
        cache = {}
        set_entity(cache, "Lean", "technology", "original summary", resolved_sense="programming language")
        set_entity(cache, "Lean", "technology", "a different summary", resolved_sense="programming language")
        senses = cache["lean"]["senses"]
        assert len(senses) == 1
        assert senses[0]["summary"] == "original summary"
