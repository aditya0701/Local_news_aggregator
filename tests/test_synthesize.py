import pytest

import writer.synthesize as synthesize_mod
from writer.synthesize import (
    _SKIP_RE,
    _cap_gap_lines,
    _clean_field_text,
    _detect_language,
    _drop_hallucinated_comparisons,
    _is_meta_line,
    _is_sentence_end,
    _match_label,
    _parse_json_response,
    _parse_labeled_text,
    _parse_stage3_output,
    _query_names_unlisted_competitor,
    _translate_to_english,
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


class TestCapGapLines:
    def test_within_limit_is_unchanged(self):
        analysis = "TYPE: general\nENTITIES: none\nGAP1: a\nGAP2: none\nGAP3: none"
        assert _cap_gap_lines(analysis) == analysis

    def test_truncates_runaway_repetition_at_gap4(self):
        analysis = (
            "TYPE: model_release\nENTITIES: Foo (company)\n"
            "GAP1: real gap one\nGAP2: real gap two\nGAP3: none\n"
            "GAP4: repeated garbage\nGAP5: more garbage\nGAP6: even more garbage"
        )
        result = _cap_gap_lines(analysis)
        assert "GAP4" not in result
        assert "GAP3: none" in result

    def test_extreme_runaway_case(self):
        # Mirrors the real worst case observed live: 300+ near-identical lines.
        analysis = "TYPE: general\nGAP1: a\nGAP2: b\nGAP3: c\n"
        analysis += "\n".join(f"GAP{i}: repeated garbage" for i in range(4, 300))
        result = _cap_gap_lines(analysis)
        assert result == "TYPE: general\nGAP1: a\nGAP2: b\nGAP3: c"

    def test_no_gap_lines_at_all_is_unchanged(self):
        analysis = "TYPE: general\nENTITIES: none"
        assert _cap_gap_lines(analysis) == analysis

    def test_custom_max_gaps(self):
        analysis = "GAP1: a\nGAP2: b"
        assert _cap_gap_lines(analysis, max_gaps=1) == "GAP1: a"


class TestQueryNamesUnlistedCompetitor:
    """Regression tests built from real hallucinated queries observed live —
    Stage 1 repeatedly named "GPT-4 Turbo"/"Claude 3 Opus" as comparison
    targets for a 2026 model neither was ever mentioned alongside, even
    after two rounds of prompt-only fixes failed to stop it (see CLAUDE.md).
    This code-level check is the backstop that actually worked."""

    SOURCE_TEXT = (
        "Mistral AI released Leanstral 1.5, a code agent model designed for Lean 4 proof "
        "assistance. The model contains 119B parameters with 6.5B activated per token. "
        "On FLTEval pass@8 it scored 43.2, exceeding Opus 4.6's 39.6."
    )
    ENTITIES = [
        "Mistral AI", "Leanstral 1.5", "Lean 4", "PutnamBench", "miniF2F",
        "FATE-H", "FATE-X", "FLTEval", "OpenAI",
    ]

    @pytest.mark.parametrize(
        "query",
        [
            "Leanstral 1.5 vs. GPT-4 Turbo performance on PutnamBench",
            "Leanstral 1.5 vs. Claude 3 Opus on FLTEval pass@8",
            "Leanstral 1.5 vs. GPT-4 on O(log n) time complexity for AVL tree implementations",
            "Leanstral 1.5's 119B parameter count compared to other open-source code agents like DeepSeek-Coder-7B",
        ],
    )
    def test_flags_query_naming_unlisted_competitor(self, query):
        assert _query_names_unlisted_competitor(query, self.ENTITIES, self.SOURCE_TEXT) is True

    @pytest.mark.parametrize(
        "query",
        [
            "GLM-5.2 pricing",
            "GLM-5.2 technical architecture details",
            "impact of GLM-5.2 on open-source AI model landscape",
            "Nagarro's three-month volume-weighted average share price on June 25, 2026",
            "Leanstral 1.5 PutnamBench score",
            # Opus 4.6 genuinely IS in the source text -- must not be flagged.
            "Leanstral 1.5 vs Opus 4.6 on FLTEval",
            # Regression: sentence-initial generic word ("Comparison") is not
            # a proper noun -- previously a false positive.
            "Comparison of Leanstral's performance on PutnamBench versus other state-of-the-art models",
            "comparison to other code agent models on PutnamBench",
        ],
    )
    def test_does_not_flag_legitimate_query(self, query):
        assert _query_names_unlisted_competitor(query, self.ENTITIES, self.SOURCE_TEXT) is False

    def test_no_comparison_marker_never_flagged_regardless_of_content(self):
        # No "vs"/"versus"/"compared to"/"comparison" marker at all -- the
        # check should short-circuit without even inspecting entity names.
        query = "Some Totally Unlisted Product Name pricing"
        assert _query_names_unlisted_competitor(query, [], "") is False


class TestDropHallucinatedComparisons:
    SOURCE_TEXT = "Leanstral 1.5 scored 587 on PutnamBench, exceeding Opus 4.6's prior result."
    ENTITIES = [{"name": "Leanstral 1.5"}, {"name": "PutnamBench"}, {"name": "Mistral AI"}]

    def test_drops_only_the_hallucinating_query(self):
        queries = [
            "Leanstral 1.5 PutnamBench score",
            "Leanstral 1.5 vs. GPT-4 Turbo performance on PutnamBench",
            "Leanstral 1.5 vs Opus 4.6 on PutnamBench",
        ]
        result = _drop_hallucinated_comparisons(queries, self.ENTITIES, self.SOURCE_TEXT)
        assert result == [
            "Leanstral 1.5 PutnamBench score",
            "Leanstral 1.5 vs Opus 4.6 on PutnamBench",
        ]

    def test_empty_queries_returns_empty(self):
        assert _drop_hallucinated_comparisons([], self.ENTITIES, self.SOURCE_TEXT) == []

    def test_entities_missing_name_key_does_not_crash(self):
        queries = ["Leanstral 1.5 PutnamBench score"]
        result = _drop_hallucinated_comparisons(queries, [{}], self.SOURCE_TEXT)
        assert result == queries


class TestDetectLanguage:
    """langdetect runs fully locally (no network call), so these exercise
    the real library rather than mocking it."""

    def test_english_text_detected_as_en(self):
        assert _detect_language("This is a normal English sentence about technology.") == "en"

    def test_chinese_text_detected_as_zh(self):
        result = _detect_language("这是一段关于人工智能的中文文本，讲述了新的模型发布。")
        assert result == "zh-cn"

    def test_empty_text_returns_none(self):
        assert _detect_language("") is None
        assert _detect_language(None) is None

    def test_undetectable_symbols_only_returns_none_not_raise(self):
        # langdetect raises LangDetectException internally on this kind of
        # input ("No features in text.") -- confirmed the caller degrades
        # gracefully (None) instead of propagating the exception.
        assert _detect_language("!!! 123456 !!!") is None


class TestTranslateToEnglish:
    def test_empty_text_returned_unchanged(self):
        assert _translate_to_english("") == ""

    def test_successful_translation(self, monkeypatch):
        class FakeTranslator:
            def __init__(self, source, target):
                pass

            def translate(self, text):
                return f"[translated] {text}"

        monkeypatch.setattr(synthesize_mod, "GoogleTranslator", FakeTranslator)
        assert _translate_to_english("some text") == "[translated] some text"

    def test_falls_back_to_original_on_failure(self, monkeypatch):
        class FailingTranslator:
            def __init__(self, source, target):
                pass

            def translate(self, text):
                raise RuntimeError("network error")

        monkeypatch.setattr(synthesize_mod, "GoogleTranslator", FailingTranslator)
        assert _translate_to_english("original text") == "original text"


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
