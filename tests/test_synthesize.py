import pytest

from writer.synthesize import (
    _SKIP_RE,
    _clean_field_text,
    _is_meta_line,
    _is_sentence_end,
    _match_label,
    _parse_json_response,
    _parse_labeled_text,
    _parse_stage3_output,
    _trim_to_last_sentence,
)


class TestParseJsonResponse:
    def test_plain_json(self):
        assert _parse_json_response('{"a": 1}') == {"a": 1}

    def test_json_in_markdown_fence(self):
        raw = "```json\n{\"a\": 1}\n```"
        assert _parse_json_response(raw) == {"a": 1}

    def test_json_with_reasoning_preamble(self):
        raw = "Let me think about this...\n{\"a\": 1, \"b\": \"text\"}"
        assert _parse_json_response(raw) == {"a": 1, "b": "text"}

    def test_first_balanced_object_wins_over_truncated_rewrite(self):
        # Mirrors a real failure mode: reasoning_content sometimes contains a
        # complete JSON object followed by a second, truncated rewrite attempt.
        raw = '{"a": 1} {"a": 2, "b"'
        assert _parse_json_response(raw) == {"a": 1}

    def test_nested_braces(self):
        raw = '{"a": {"nested": true}, "b": 2}'
        assert _parse_json_response(raw) == {"a": {"nested": True}, "b": 2}

    def test_none_input(self):
        assert _parse_json_response(None) is None

    def test_empty_string(self):
        assert _parse_json_response("") is None

    def test_no_json_present(self):
        assert _parse_json_response("just some text, no braces here") is None

    def test_unparseable_garbage(self):
        assert _parse_json_response("{not valid json at all}") is None


class TestIsSentenceEnd:
    def test_decimal_point_between_digits_is_not_sentence_end(self):
        text = "GLM-5.2 launched today"
        i = text.index(".")
        assert _is_sentence_end(text, i) is False

    def test_percentage_decimal_is_not_sentence_end(self):
        text = "up 32.8% this quarter"
        i = text.index(".")
        assert _is_sentence_end(text, i) is False

    def test_period_after_word_is_sentence_end(self):
        text = "This is a sentence. Next one."
        i = text.index(".")
        assert _is_sentence_end(text, i) is True

    def test_non_period_char_is_always_sentence_end(self):
        assert _is_sentence_end("hello!", 5) is True


class TestTrimToLastSentence:
    def test_already_ends_cleanly_with_danda(self):
        text = "यह एक वाक्य है।"
        assert _trim_to_last_sentence(text) == text

    def test_trims_mid_sentence_trailing_fragment(self):
        text = "पहला वाक्य। दूसरा अधूरा वाक्य"
        assert _trim_to_last_sentence(text) == "पहला वाक्य।"

    def test_does_not_truncate_on_trailing_decimal_number(self):
        # Regression: a "." was previously treated as sentence-end even when
        # part of a version number/percentage, truncating "GLM-5.2" to "GLM-5."
        text = "मॉडल का नाम GLM-5.2 है। यह तेज़ है और स्कोर 32.8"
        result = _trim_to_last_sentence(text)
        assert result == "मॉडल का नाम GLM-5.2 है।"

    def test_empty_string(self):
        assert _trim_to_last_sentence("") == ""

    def test_no_punctuation_at_all_returns_original(self):
        text = "no punctuation here at all"
        assert _trim_to_last_sentence(text) == text


class TestIsMetaLine:
    def test_hindi_line_is_not_meta(self):
        assert _is_meta_line("यह एक सामान्य वाक्य है।") is False

    def test_let_me_prefix_is_meta(self):
        assert _is_meta_line("Let me rewrite this section for clarity") is True

    def test_note_prefix_is_meta(self):
        assert _is_meta_line("Note: this section needs review") is True

    def test_short_ascii_fragment_is_not_meta(self):
        # <=6 ascii chars shouldn't trip the meta-commentary heuristic
        assert _is_meta_line("GLM-5.2 जारी हुआ") is False

    def test_blank_line_is_not_meta(self):
        assert _is_meta_line("   ") is False


class TestCleanFieldText:
    def test_strips_meta_lines_and_trims(self):
        raw = "Let me write this.\nयह पहला वाक्य है।\nयह अधूरा"
        result = _clean_field_text(raw)
        assert "Let me" not in result
        assert result == "यह पहला वाक्य है।"

    def test_empty_input(self):
        assert _clean_field_text("") == ""
        assert _clean_field_text(None) == ""


class TestMatchLabel:
    def test_english_label(self):
        assert _match_label("TITLE: कुछ शीर्षक") == ("TITLE", "कुछ शीर्षक")

    def test_hindi_label_synonym(self):
        assert _match_label("शीर्षक: कुछ शीर्षक") == ("TITLE", "कुछ शीर्षक")

    def test_markdown_wrapped_label(self):
        assert _match_label("**शीर्षक:** कुछ शीर्षक") == ("TITLE", "कुछ शीर्षक")

    def test_unrecognized_label_returns_none(self):
        assert _match_label("RANDOM: कुछ पाठ") is None

    def test_line_without_colon_returns_none(self):
        assert _match_label("no colon in this line") is None


class TestParseLabeledText:
    def test_full_labeled_response(self):
        raw = (
            "TITLE: परीक्षण शीर्षक\n"
            "LEDE: यह पहला पैराग्राफ है।\n"
            "STRATEGIC_ANALYSIS: यह रणनीतिक विश्लेषण है।\n"
        )
        result = _parse_labeled_text(raw)
        assert result["title"] == "परीक्षण शीर्षक"
        assert result["introduction_lede"] == "यह पहला पैराग्राफ है।"
        assert result["strategic_analysis"] == "यह रणनीतिक विश्लेषण है।"

    def test_missing_title_or_lede_returns_none(self):
        raw = "STRATEGIC_ANALYSIS: कुछ पाठ\n"
        assert _parse_labeled_text(raw) is None

    def test_none_input(self):
        assert _parse_labeled_text(None) is None


class TestParseStage3Output:
    def test_valid_json_takes_priority(self):
        raw = (
            '{"title": "शीर्षक", "concept_box": "बॉक्स", '
            '"introduction_lede": "लीड।", "deep_dive_and_context": "गहराई।", '
            '"strategic_analysis": "विश्लेषण।", "conclusion_and_significance": "निष्कर्ष।"}'
        )
        result = _parse_stage3_output(raw)
        assert result["title"] == "शीर्षक"
        assert result["introduction_lede"] == "लीड।"

    def test_falls_back_to_labeled_text_when_json_incomplete(self):
        raw = "TITLE: परीक्षण\nLEDE: यह लीड है।\n"
        result = _parse_stage3_output(raw)
        assert result["title"] == "परीक्षण"

    def test_returns_none_when_nothing_parses(self):
        assert _parse_stage3_output("no structure here, no json, no labels") is None

    def test_none_input(self):
        assert _parse_stage3_output(None) is None


class TestSkipRegex:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("REASON: this is why\nSKIP: yes", "yes"),
            ("REASON: this is why\nSKIP: no", "no"),
            ("REASON: this is why\nSKIP: YES", "YES"),
            ("some preamble\nSKIP:   no  \ntrailer text", "no"),
        ],
    )
    def test_matches_yes_no_case_insensitive(self, raw, expected):
        match = _SKIP_RE.search(raw)
        assert match is not None
        assert match.group(1).lower() == expected.lower()

    def test_no_match_when_absent(self):
        assert _SKIP_RE.search("no skip field here") is None
