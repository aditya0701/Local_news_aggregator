import re

from writer.synthesize import _MODEL_QUALITY, _call_sarvam

# Runs once per pipeline run on whatever GitHub items survive the cheap
# deterministic filters in github_collector.py (denylist, job-listing terms,
# listicle pattern, star floor). Those catch the obvious junk for free; this
# catches subtler cases — off-topic repos, low-substance projects, guides
# that slipped past the title-pattern check — with an editorial judgment call.
_JUDGE_PROMPT = """You are the technical editor for टेकदृष्टि (TechDrishti), a premium Hindi tech publication covering AI and machine learning development. Readers trust us for breaking, substantive developments — not filler.

Only APPROVE a repo if it is a genuine NEW technical development in AI/ML — a new model, a new architecture or technique, a new framework/library/tool that helps AI/ML developers build things, or a new database/infrastructure component built for AI workloads.

REJECT a repo if it is:
- A guide, tutorial, comparison, "awesome-list", or roundup — even about AI topics (titles like "Ultimate Guide", "Top N Tools", "X vs Y Comparison", "Proven System" are guides/marketing, not developments)
- Not related to AI/ML development at all (general programming tools, unrelated domains)
- A generic personal/learning project or toy demo with no real technical contribution
- SEO/clickbait-style content built to rank in search/trending rather than represent real engineering

For each repo below, respond with exactly one line in this format, nothing else:
<number>: APPROVE or REJECT — <one sentence reason>

Repos:
{repo_list}"""

_VERDICT_RE = re.compile(r"^(\d+)[.:)]\s*(APPROVE|REJECT)", re.IGNORECASE | re.MULTILINE)

# Keeps each judgment call's expected output (one line per repo) comfortably
# inside the 4096-token cap even with reasoning overhead — large batches risk
# the response getting cut off before covering every repo.
_BATCH_SIZE = 15


def _judge_batch(batch: list[dict], api_key: str) -> list[dict]:
    repo_list = "\n".join(
        f"{i + 1}. {item.get('title', '')}: {item.get('summary') or '(no description)'}"
        for i, item in enumerate(batch)
    )
    raw = _call_sarvam(
        _JUDGE_PROMPT.format(repo_list=repo_list),
        api_key,
        _MODEL_QUALITY,
        system="/no_think Be direct.",
    )
    if not raw:
        return batch  # fail open — a failed judge call shouldn't silently drop everything

    verdicts = {
        int(m.group(1)) - 1: m.group(2).upper() == "APPROVE"
        for m in _VERDICT_RE.finditer(raw)
    }
    return [item for i, item in enumerate(batch) if verdicts.get(i, True)]


def filter_notable_repos(items: list[dict], api_key: str) -> list[dict]:
    """Editorial judgment gate for GitHub items, batched to limit API calls."""
    kept = []
    for start in range(0, len(items), _BATCH_SIZE):
        kept.extend(_judge_batch(items[start:start + _BATCH_SIZE], api_key))
    return kept
