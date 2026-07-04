import pytest

from collectors.github_collector import MIN_STARS, _is_denied, _looks_like_listicle


class TestLooksLikeListicle:
    def test_year_and_marketing_word_flags_as_listicle(self):
        assert _looks_like_listicle("Ultimate Claude Guide 2026") is True

    def test_year_alone_is_not_a_listicle(self):
        assert _looks_like_listicle("Release Notes 2026") is False

    def test_marketing_word_alone_is_not_a_listicle(self):
        assert _looks_like_listicle("The Ultimate Testing Library") is False

    def test_case_insensitive_marketing_word(self):
        assert _looks_like_listicle("TOP 10 Agent Frameworks 2027") is True

    def test_unrelated_text_is_not_a_listicle(self):
        assert _looks_like_listicle("A new inference engine for LLMs") is False


class TestIsDenied:
    def _repo(self, **overrides):
        repo = {
            "full_name": "someone/some-repo",
            "description": "A genuinely new inference engine for LLMs",
            "stargazers_count": MIN_STARS + 50,
        }
        repo.update(overrides)
        return repo

    def test_denylist_term_in_description(self):
        repo = self._repo(description="A jailbreak toolkit for models")
        assert _is_denied(repo) is True

    def test_job_listing_term(self):
        repo = self._repo(full_name="someone/internship-tracker-2026")
        assert _is_denied(repo) is True

    def test_listicle_pattern(self):
        repo = self._repo(full_name="someone/ultimate-ai-guide-2026")
        assert _is_denied(repo) is True

    def test_below_min_stars_is_denied(self):
        repo = self._repo(stargazers_count=MIN_STARS - 1)
        assert _is_denied(repo) is True

    def test_legitimate_repo_is_not_denied(self):
        repo = self._repo()
        assert _is_denied(repo) is False

    def test_missing_description_does_not_crash(self):
        repo = self._repo(description=None)
        assert _is_denied(repo) is False
