import pytest

import writer.synthesize as synthesize_mod
from writer.synthesize import (
    _SKIP_RE,
    _cap_gap_lines,
    _clean_deep_dive_text,
    _clean_field_text,
    _detect_language,
    _drop_hallucinated_comparisons,
    _entity_referenced_in_query,
    _is_meta_line,
    _is_sentence_end,
    _match_label,
    _parse_json_response,
    _parse_labeled_text,
    _parse_stage3_output,
    _prepare_context_queries,
    _query_has_dangling_reference,
    _query_names_unlisted_competitor,
    _stage3_write_article_with_retry,
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


class TestCleanDeepDiveText:
    def test_preserves_paragraph_breaks(self):
        raw = "पहला पैराग्राफ।\n\nदूसरा पैराग्राफ।\n\nतीसरा पैराग्राफ।"
        result = _clean_deep_dive_text(raw)
        assert result == "पहला पैराग्राफ।\n\nदूसरा पैराग्राफ।\n\nतीसरा पैराग्राफ।"

    def test_strips_meta_lines_within_a_paragraph(self):
        raw = "Let me write this.\nपहला पैराग्राफ।\n\nदूसरा पैराग्राफ।"
        result = _clean_deep_dive_text(raw)
        assert "Let me" not in result
        assert result == "पहला पैराग्राफ।\n\nदूसरा पैराग्राफ।"

    def test_trims_only_last_paragraph_to_last_sentence(self):
        raw = "पहला पैराग्राफ।\n\nदूसरा वाक्य। अधूरा"
        result = _clean_deep_dive_text(raw)
        assert result == "पहला पैराग्राफ।\n\nदूसरा वाक्य।"

    def test_drops_empty_paragraphs(self):
        raw = "पहला पैराग्राफ।\n\n\n\nदूसरा पैराग्राफ।"
        result = _clean_deep_dive_text(raw)
        assert result == "पहला पैराग्राफ।\n\nदूसरा पैराग्राफ।"

    def test_single_paragraph_unchanged(self):
        assert _clean_deep_dive_text("एक ही पैराग्राफ।") == "एक ही पैराग्राफ।"

    def test_empty_input(self):
        assert _clean_deep_dive_text("") == ""
        assert _clean_deep_dive_text(None) == ""


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


class TestStage3WriteArticleWithRetry:
    """Built from a real production incident: a real TechCrunch article
    (humanoid-robotics/Agility Robotics) fell back to the sparse
    translate-only view because Stage 3 failed once — re-running the exact
    same inputs succeeded immediately, twice, confirming a one-off transient
    API hiccup rather than a deterministic content/prompt problem. This
    retry is the fix; these tests cover the retry control flow itself
    (the underlying Sarvam call is mocked, not re-tested here)."""

    ARGS = ("title", "source text", "entity context", {"category": "general", "paragraph_plan": []}, "fake-api-key")

    def test_succeeds_first_try_no_retry_needed(self, monkeypatch):
        calls = []

        def fake_stage3(*args):
            calls.append(args)
            return {"title": "ok"}

        monkeypatch.setattr(synthesize_mod, "_stage3_write_article", fake_stage3)
        result = _stage3_write_article_with_retry(*self.ARGS)
        assert result == {"title": "ok"}
        assert len(calls) == 1

    def test_fails_once_then_succeeds_on_retry(self, monkeypatch):
        responses = [None, {"title": "ok on retry"}]

        def fake_stage3(*args):
            return responses.pop(0)

        monkeypatch.setattr(synthesize_mod, "_stage3_write_article", fake_stage3)
        result = _stage3_write_article_with_retry(*self.ARGS)
        assert result == {"title": "ok on retry"}

    def test_fails_both_times_returns_none(self, monkeypatch):
        monkeypatch.setattr(synthesize_mod, "_stage3_write_article", lambda *args: None)
        assert _stage3_write_article_with_retry(*self.ARGS) is None

    def test_retries_at_most_once_not_in_a_loop(self, monkeypatch):
        calls = []

        def fake_stage3(*args):
            calls.append(args)
            return None

        monkeypatch.setattr(synthesize_mod, "_stage3_write_article", fake_stage3)
        _stage3_write_article_with_retry(*self.ARGS)
        assert len(calls) == 2


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


class TestQueryHasDanglingReference:
    """Grammar-rule check for the dangling-reference bug (see CLAUDE.md,
    2026-07-08): a GAP query that points back at its subject with a
    demonstrative ("this vulnerability", "that attack") instead of naming it
    only makes sense within one conversation, but each GAP query is
    dispatched to the search backend as an independent standalone question.
    `test_real_gitlost_gap_queries_from_a_live_run` below is the actual real
    example this was built from — the two other tests here just isolate the
    rule with clean synthetic cases."""

    ENTITY_NAMES = ["GitLost", "GitHub", "Agentic Workflows"]

    def test_flags_demonstrative_with_no_named_entity(self):
        query = (
            "What are the specific technical details of the vulnerability, such as the exact "
            "payload or the sequence of actions the agent takes, and how does this differ from "
            "a standard repository access control bypass?"
        )
        assert _query_has_dangling_reference(query, self.ENTITY_NAMES) is True

    def test_does_not_flag_when_subject_is_named_explicitly(self):
        query = "How does the GitLost vulnerability compare to previously disclosed supply-chain attacks?"
        assert _query_has_dangling_reference(query, self.ENTITY_NAMES) is False

    def test_does_not_flag_query_with_no_demonstrative_at_all(self):
        query = "What is GitLost and why is it considered a critical vulnerability?"
        assert _query_has_dangling_reference(query, self.ENTITY_NAMES) is False

    def test_real_gitlost_gap_queries_from_a_live_run(self):
        """These three exact strings are what a real production Stage 1 run
        generated for a real Hacker News article ("GitLost: We Tricked
        GitHub's AI Agent into Leaking Private Repos", captured in
        experiments/search_test_cases_20260708T113409Z.json) — not
        hand-written examples. GAP1 named GitLost explicitly; GAP2 is the bug
        this whole fix is for (it never repeats "GitLost" or any other named
        entity); GAP3 stays clean because "Agentic Workflows" is itself
        named."""
        gap1 = (
            "How does the GitLost vulnerability's attack vector and impact compare to other "
            "known prompt injection attacks against AI agents, and what does this reveal about "
            "the security of the underlying Agentic Workflows technology?"
        )
        gap2 = (
            "What are the specific technical details of the vulnerability, such as the exact "
            "payload or the sequence of actions the agent takes, and how does this differ from "
            "a standard repository access control bypass?"
        )
        gap3 = (
            "Beyond the immediate data leakage, what are the long-term implications for "
            "organizations using GitHub Agentic Workflows, and what specific mitigation "
            "strategies or security best practices should they adopt to prevent similar attacks?"
        )
        assert _query_has_dangling_reference(gap1, self.ENTITY_NAMES) is False
        assert _query_has_dangling_reference(gap2, self.ENTITY_NAMES) is True
        assert _query_has_dangling_reference(gap3, self.ENTITY_NAMES) is False

    def test_abbreviated_multiword_entity_reference_is_not_a_false_positive(self):
        """Real false positive caught by the live test (tests/test_live_gap_queries.py): a query
        referred to the entity "GitHub Agentic Workflows" as just "the Agentic Workflows
        architecture" — dropping the "GitHub" prefix is normal English, not a dangling
        reference, and should not be flagged."""
        query = (
            "What is the underlying technical mechanism of the Agentic Workflows architecture "
            "that allows a public issue to trigger an action on private repositories, and how "
            "does this design choice create the vulnerability?"
        )
        assert _query_has_dangling_reference(query, ["GitHub Agentic Workflows"]) is False


class TestEntityReferencedInQuery:
    def test_exact_single_word_match(self):
        assert _entity_referenced_in_query("GitLost", "what is gitlost about?") is True

    def test_single_word_no_match(self):
        assert _entity_referenced_in_query("GitLost", "what is the vulnerability about?") is False

    def test_abbreviated_multiword_reference_counts(self):
        assert _entity_referenced_in_query("GitHub Agentic Workflows", "the agentic workflows architecture") is True

    def test_single_shared_generic_word_is_not_enough(self):
        assert _entity_referenced_in_query("GitHub Agentic Workflows", "other automation workflows exist") is False

    def test_unrelated_text_no_match(self):
        assert _entity_referenced_in_query("GitHub Agentic Workflows", "completely unrelated text") is False

    def test_two_word_entity_name_requires_both_words_not_just_one(self):
        """Found via tests/test_gap_context_fixtures.py replaying real captured Stage 1 output:
        with "half the words rounded up", a 2-word name's threshold rounds down to 1, so a
        single shared generic word was enough to count as a match. A real GAP query about
        GitLost contained the plain English word "actions" ("...the sequence of actions the
        agent takes...") which alone satisfied the 2-word entity name "GitHub Actions" -- a
        false match on a coincidental generic word, not an actual reference to that entity."""
        query = "how does this differ, given the sequence of actions the agent takes?"
        assert _entity_referenced_in_query("GitHub Actions", query) is False

    def test_two_word_entity_name_matches_when_both_words_present(self):
        query = "how does this compare to other github actions workflows?"
        assert _entity_referenced_in_query("GitHub Actions", query) is True


class TestPrepareContextQueries:
    """Added 2026-07-08 after a live test (tests/test_live_gap_queries.py) confirmed the
    prompt-only fix in _STAGE1_ANALYSIS_PROMPT does not reliably prevent dangling references —
    same lesson already learned for hallucinated comparisons (TestDropHallucinatedComparisons
    above). Instead of dropping a dangling query outright, this attaches the first GAP query as
    background context to it (Stage 1's checklist ordering reliably puts the article's central
    "what is X" question first), only dropping as a last resort if it's still dangling even with
    that context attached."""

    ENTITIES = [{"name": "GitLost", "type": "product"}, {"name": "GitHub Agentic Workflows", "type": "technology"}]

    def test_first_query_is_always_kept_unchanged(self):
        first = "What is GitLost and why is it considered a critical vulnerability?"
        result = _prepare_context_queries([first], self.ENTITIES)
        assert result == [(first, first)]

    def test_dangling_later_query_gets_gap1_attached_as_context(self):
        first = "What is GitLost and why is it considered a critical vulnerability?"
        dangling = (
            "What are the specific technical details of the vulnerability, that allow it to be "
            "triggered by a public issue?"
        )
        result = _prepare_context_queries([first, dangling], self.ENTITIES)
        assert result[0] == (first, first)
        orig, dispatch_text = result[1]
        assert orig == dangling
        assert dispatch_text != dangling
        assert first in dispatch_text
        assert dangling in dispatch_text

    def test_non_dangling_later_query_is_kept_unchanged(self):
        first = "What is GitLost and why is it considered a critical vulnerability?"
        clean = "How does the Agentic Workflows architecture allow this to be triggered?"
        result = _prepare_context_queries([first, clean], self.ENTITIES)
        assert result[1] == (clean, clean)

    def test_dropped_only_if_still_dangling_with_context_attached(self):
        # Neither query names any entity at all -- attaching the first as context to the
        # second doesn't help, so both are dropped rather than sent dangling. (The first query
        # gets no special exemption from the final filter -- if GAP1 itself dangles with no
        # entity to name, there's nothing earlier to rescue it either.)
        first = "What does this reveal about the security of AI-powered automation in general?"
        dangling = "How does this compare to other known attacks against it?"
        result = _prepare_context_queries([first, dangling], self.ENTITIES)
        assert result == []

    def test_first_query_kept_when_it_does_not_dangle_even_with_no_entity_match(self):
        # A first query with no pointing word at all should never be dropped, regardless of
        # whether it happens to name an entity.
        first = "What are the industry-standard mitigations for prompt injection attacks?"
        result = _prepare_context_queries([first], self.ENTITIES)
        assert result == [(first, first)]

    def test_empty_queries_returns_empty(self):
        assert _prepare_context_queries([], self.ENTITIES) == []

    def test_real_gitlost_gap_queries_from_a_live_run(self):
        """Same real captured queries used in TestQueryHasDanglingReference — GAP2 is the actual
        bug this fix is for."""
        gap1 = (
            "How does the GitLost vulnerability's attack vector and impact compare to other "
            "known prompt injection attacks against AI agents, and what does this reveal about "
            "the security of the underlying Agentic Workflows technology?"
        )
        gap2 = (
            "What are the specific technical details of the vulnerability, such as the exact "
            "payload or the sequence of actions the agent takes, and how does this differ from "
            "a standard repository access control bypass?"
        )
        gap3 = (
            "Beyond the immediate data leakage, what are the long-term implications for "
            "organizations using GitHub Agentic Workflows, and what specific mitigation "
            "strategies or security best practices should they adopt to prevent similar attacks?"
        )
        result = _prepare_context_queries([gap1, gap2, gap3], self.ENTITIES)
        origs = [orig for orig, _ in result]
        assert origs == [gap1, gap2, gap3]
        dispatch_by_orig = dict(result)
        assert dispatch_by_orig[gap1] == gap1
        assert dispatch_by_orig[gap2] != gap2 and gap1 in dispatch_by_orig[gap2]
        assert dispatch_by_orig[gap3] == gap3


class TestSearchRoutingByAmbiguity:
    """Covers the 2026-07-08 change: unambiguous entities are cheap "what is
    X" lookups and should always go to the free DDG tier, never the
    research agent (/api/concise) — that's reserved for ambiguous entities
    (need real disambiguation) and GAP/context queries (need real
    reasoning). Drives the real _synthesize_sarvam() function end-to-end
    with every I/O dependency mocked, so this exercises the actual routing
    logic in writer/synthesize.py rather than a reimplementation of it."""

    CLUSTER = [{"title": "some headline", "summary": "s" * 300, "url": "http://example.com/a"}]

    STAGE1_RESULT = {
        "skip": False,
        "search_queries": ["a context question"],
        "entities": [
            {"name": "PlainCo", "type": "company"},
            {
                "name": "Ambico",
                "type": "product",
                "ambiguous": True,
                "resolved_sense": "the software, not the mascot",
            },
        ],
    }

    def _install_common_mocks(self, monkeypatch, use_concise, search_web_calls, ask_concise_calls):
        monkeypatch.setattr(synthesize_mod, "fetch_page", lambda url: "x" * 300)
        monkeypatch.setattr(synthesize_mod, "_detect_language", lambda text: "en")
        monkeypatch.setattr(synthesize_mod, "_stage1_extract_queries", lambda *a, **k: dict(self.STAGE1_RESULT))
        monkeypatch.setattr(synthesize_mod, "load_cache", lambda: {})
        monkeypatch.setattr(synthesize_mod, "get_entity", lambda cache, name, resolved_sense=None: None)
        monkeypatch.setattr(synthesize_mod, "set_entity", lambda *a, **k: None)
        monkeypatch.setattr(synthesize_mod, "save_cache", lambda cache: None)
        monkeypatch.setattr(synthesize_mod, "concise_configured", lambda: use_concise)
        # Short-circuit right after the search block — Stage 2/3 aren't what's
        # under test here, and returning None just makes _synthesize_sarvam
        # bail out cleanly once the assertions-relevant work is done.
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

    def test_unambiguous_entity_always_routed_to_ddg_even_when_concise_configured(self, monkeypatch):
        search_web_calls, ask_concise_calls = [], []
        self._install_common_mocks(monkeypatch, True, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(self.CLUSTER, "fake-api-key")

        ddg_queries = [q for call in search_web_calls for q in call]
        assert any("PlainCo" in q for q in ddg_queries)
        assert not any("PlainCo" in q for q in ask_concise_calls)

    def test_ambiguous_entity_routed_to_concise_when_configured(self, monkeypatch):
        search_web_calls, ask_concise_calls = [], []
        self._install_common_mocks(monkeypatch, True, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(self.CLUSTER, "fake-api-key")

        ddg_queries = [q for call in search_web_calls for q in call]
        assert any("Ambico" in q for q in ask_concise_calls)
        assert not any("Ambico" in q for q in ddg_queries)

    def test_context_query_routed_to_concise_when_configured(self, monkeypatch):
        search_web_calls, ask_concise_calls = [], []
        self._install_common_mocks(monkeypatch, True, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(self.CLUSTER, "fake-api-key")

        ddg_queries = [q for call in search_web_calls for q in call]
        assert "a context question" in ask_concise_calls
        assert "a context question" not in ddg_queries

    def test_ambiguous_entity_and_context_fall_back_to_ddg_when_concise_not_configured(self, monkeypatch):
        search_web_calls, ask_concise_calls = [], []
        self._install_common_mocks(monkeypatch, False, search_web_calls, ask_concise_calls)

        synthesize_mod._synthesize_sarvam(self.CLUSTER, "fake-api-key")

        ddg_queries = [q for call in search_web_calls for q in call]
        assert any("PlainCo" in q for q in ddg_queries)
        assert any("Ambico" in q for q in ddg_queries)
        assert "a context question" in ddg_queries
        assert ask_concise_calls == []
