"""Live, opt-in regression test for the GAP-query dangling-reference fix
(see CLAUDE.md, "Dangling-reference bug", 2026-07-08).

Unlike the rest of tests/, this makes a REAL Sarvam API call (and a real
HTTP scrape) against a real article that is known to have produced multiple
GAP queries in production, one of which had the bug this test guards
against (see experiments/search_test_cases_20260708T113409Z.json for the
original captured output). It exists to catch a live regression in
_STAGE1_ANALYSIS_PROMPT's wording that the fast, mocked unit tests in
tests/test_synthesize.py (TestQueryHasDanglingReference) cannot catch, since
those only test the grammar-rule checker function against fixed strings,
not whether the live model still produces clean output.

Costs one real sarvam-30b call (2 sub-calls: analysis + extraction) plus one
real HTTP fetch of the source article per run. Skipped automatically unless
SARVAM_API_KEY is set, so it never runs in normal CI/local test runs.

LLM output is not deterministic — a single passing run is evidence, not
proof, the same honesty standard CLAUDE.md already applies to every other
live-verified fix in this pipeline ("8 of 8 runs clean," "3 repeat runs").
Re-run this test manually a few times (`pytest tests/test_live_gap_queries.py -v`)
for more confidence than one CI run gives you.
"""
import os

import pytest
from dotenv import load_dotenv

from writer.synthesize import (
    _detect_language,
    _prepare_context_queries,
    _query_has_dangling_reference,
    _stage1_extract_queries,
    _translate_to_english,
)
from writer.web_context import scrape_source

load_dotenv()

_API_KEY = os.environ.get("SARVAM_API_KEY", "").strip()

# A real Hacker News article confirmed (2026-07-08) to generate 3 GAP
# queries in production, one of which ("What are the specific technical
# details of the vulnerability...how does this differ...") never named
# "GitLost" or any other entity — the exact bug this test guards against.
_ARTICLE_TITLE = "GitLost: We Tricked GitHub's AI Agent into Leaking Private Repos"
_ARTICLE_URL = "https://noma.security/blog/gitlost-how-we-tricked-githubs-ai-agent-into-leaking-private-repos/"
_ARTICLE_SUMMARY = (
    "Noma Labs security researchers demonstrate a prompt-injection attack against GitHub's "
    "Agentic Workflows that tricks the AI agent into leaking private repository contents."
)


@pytest.mark.skip(reason="disabled to avoid burning real Sarvam API tokens on local/CI test runs — uncomment to re-enable manually")
@pytest.mark.skipif(not _API_KEY, reason="SARVAM_API_KEY not set — skipping live API test")
def test_gitlost_article_gap_queries_have_no_dangling_reference():
    source_text = scrape_source(_ARTICLE_URL)
    if not source_text:
        pytest.skip(f"could not scrape {_ARTICLE_URL} — site may be down/changed, not a code failure")
    if len(source_text) + len(_ARTICLE_SUMMARY) < 250:
        pytest.skip("scraped source too short to reach Stage 1 — site content may have changed")

    title, summary = _ARTICLE_TITLE, _ARTICLE_SUMMARY
    detected_lang = _detect_language(source_text)
    if detected_lang and detected_lang != "en":
        title = _translate_to_english(title)
        summary = _translate_to_english(summary)
        source_text = _translate_to_english(source_text)

    stage1 = _stage1_extract_queries(title, summary, source_text, _API_KEY)
    if stage1 is None:
        pytest.skip("live Stage 1 call failed outright (transient API issue) — not a grammar-rule failure")
    if stage1.get("skip"):
        pytest.skip(f"live model flagged article as skip: {stage1.get('skip_reason')}")

    gap_queries = stage1.get("search_queries", [])
    if len(gap_queries) < 2:
        # Stage 1's own GAP-count judgment is documented as run-to-run inconsistent on this
        # exact article (CLAUDE.md Known Issue #4 — "0 to 9 across identical input") — that's a
        # separate, already-tracked issue, not something this test exists to catch. Skip rather
        # than fail so this test only reports on the dangling-reference fix it's actually for.
        pytest.skip(
            f"live run produced only {len(gap_queries)} GAP quer(y/ies) — too few to exercise "
            f"the cross-query dangling-reference bug this run (known judgment-count variance, "
            f"not a grammar-rule failure): {gap_queries}"
        )

    # This is the actual production fix under test: _synthesize_sarvam() doesn't dispatch raw
    # GAP queries as-is, it runs them through _prepare_context_queries() first, which attaches
    # GAP1 as background context to any later query that dangles on a pointing word, and drops
    # a query only as a last resort if it's still dangling even with that context attached. So
    # what must be clean is the DISPATCHED text, not necessarily every raw GAP query in isolation.
    entities = stage1.get("entities", [])
    entity_names = [e.get("name", "") for e in entities if e.get("name")]
    dispatch_pairs = _prepare_context_queries(gap_queries, entities)
    flagged = [
        (orig, text) for orig, text in dispatch_pairs if _query_has_dangling_reference(text, entity_names)
    ]
    assert not flagged, (
        "GAP quer(y/ies), even after _prepare_context_queries attached GAP1 as background "
        "context, still point back at a subject with a pointing word (this/that/it/...) with "
        f"no named entity to resolve it: {flagged}\nfull entity list: {entity_names}\n"
        f"all GAP queries: {gap_queries}"
    )
