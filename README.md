# Hindi Newspaper

A scraper that pulls content from GitHub (trending repos, releases, discussions) and RSS feeds, then translates and curates it into Hindi and other Indian regional languages — aiming to deliver good intellectual/tech content to Indian readers in their own language.

## How it works

1. **Collectors** (`collectors/`) pull raw items:
   - `github_collector.py` — trending repos / topics via the GitHub REST API
   - `rss_collector.py` — articles from configured RSS feeds
2. **Translator** (`translator/translate.py`) converts collected English content into Hindi (and optionally other Indian languages) via a translation API.
3. **Pipeline** (`pipeline.py`) ties collection → translation → output (JSON/Markdown digest) together.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys
```

## Run

```bash
python pipeline.py
```

## Configuration

- `config/feeds.yaml` — list of RSS feed URLs to pull from.
- `config/github.yaml` — GitHub search/trending queries.
- `.env` — secrets (GitHub token, translation API key).

## Status

Early scaffold — collectors and translator are stubbed out and ready to be filled in.
