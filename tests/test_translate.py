import pytest

import translator.translate as translate_mod
from translator.translate import (
    _annotate_proper_nouns,
    _extract_proper_nouns,
    _find_token_span,
    _has_non_initial_occurrence,
    _is_strong_candidate,
)


@pytest.fixture(autouse=True)
def clear_name_cache():
    translate_mod._name_cache.clear()
    yield
    translate_mod._name_cache.clear()


class FakeTranslator:
    def __init__(self, mapping):
        self.mapping = mapping

    def translate(self, text):
        return self.mapping.get(text, text)


class TestIsStrongCandidate:
    def test_multi_word_name(self):
        assert _is_strong_candidate("Google Gemini") is True

    def test_name_with_digit(self):
        assert _is_strong_candidate("GPT-5") is True

    def test_all_caps_acronym(self):
        assert _is_strong_candidate("NASA") is True

    def test_camel_case_name(self):
        assert _is_strong_candidate("OpenAI") is True

    def test_ordinary_capitalized_word_is_not_strong(self):
        assert _is_strong_candidate("The") is False


class TestHasNonInitialOccurrence:
    def test_only_occurrence_at_text_start_is_not_non_initial(self):
        text = "Apple has many products."
        assert _has_non_initial_occurrence(text, "Apple") is False

    def test_occurrence_mid_sentence_is_non_initial(self):
        text = "My favorite company is Google for search."
        assert _has_non_initial_occurrence(text, "Google") is True

    def test_occurrence_only_after_period_is_not_non_initial(self):
        text = "Intro line. Apple releases new hardware."
        assert _has_non_initial_occurrence(text, "Apple") is False


class TestExtractProperNouns:
    def test_extracts_camelcase_and_digit_names(self):
        text = "OpenAI released GPT-5 today, and Google confirmed the news."
        names = _extract_proper_nouns(text)
        assert "OpenAI" in names
        assert "GPT-5" in names

    def test_leading_stopword_only_match_is_dropped(self):
        text = "The company launched a product."
        names = _extract_proper_nouns(text)
        assert "The" not in names

    def test_empty_text(self):
        assert _extract_proper_nouns("") == []
        assert _extract_proper_nouns(None) == []

    def test_no_duplicate_names(self):
        text = "OpenAI and OpenAI again."
        names = _extract_proper_nouns(text)
        assert names.count("OpenAI") == 1


class TestFindTokenSpan:
    def test_finds_exact_whole_token(self):
        translated = "कंपनी OpenAI ने घोषणा की"
        span = _find_token_span(translated, "OpenAI")
        assert span is not None
        start, end = span
        assert translated[start:end] == "OpenAI"

    def test_does_not_match_substring_inside_longer_token(self):
        translated = "यह OpenAI की खबर है"
        # "AI" is a substring of the "OpenAI" token but not a token of its own
        assert _find_token_span(translated, "AI") is None

    def test_returns_none_when_absent(self):
        translated = "कोई संबंधित शब्द नहीं है"
        assert _find_token_span(translated, "OpenAI") is None


class TestAnnotateProperNouns:
    def test_annotates_transliterated_name(self):
        original = "OpenAI released a new model."
        translated = "ओपनएआई ने एक नया मॉडल जारी किया।"
        translator = FakeTranslator({"OpenAI": "ओपनएआई"})
        result = _annotate_proper_nouns(original, translated, translator)
        assert "(OpenAI)" in result
        assert result.startswith("ओपनएआई (OpenAI)")

    def test_skips_when_already_annotated(self):
        original = "OpenAI released a new model."
        translated = "ओपनएआई (OpenAI) ने एक नया मॉडल जारी किया।"
        translator = FakeTranslator({"OpenAI": "ओपनएआई"})
        result = _annotate_proper_nouns(original, translated, translator)
        assert result.count("(OpenAI)") == 1

    def test_skips_when_rendered_equals_name(self):
        original = "NASA launched a probe."
        translated = "NASA ने एक जांच लॉन्च की।"
        translator = FakeTranslator({"NASA": "NASA"})
        result = _annotate_proper_nouns(original, translated, translator)
        assert result == translated

    def test_empty_inputs_return_translated_unchanged(self):
        translator = FakeTranslator({})
        assert _annotate_proper_nouns("", "कुछ पाठ", translator) == "कुछ पाठ"
        assert _annotate_proper_nouns("Original", "", translator) == ""
