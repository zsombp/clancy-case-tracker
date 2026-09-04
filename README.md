# Clancy Case Tracker

A free, static, automatically updated reference site for the criminal case of *Commonwealth v. Lindsay Clancy* (Plymouth County Superior Court, Massachusetts).

**Live site:** https://zsombp.github.io/clancy-case-tracker/

## How it works

- `index.html` is a single static page. It reads JSON files from `data/` and renders everything in the browser. No server, no tracking, no cookies.
- `scripts/update.py` runs **every hour** via GitHub Actions (`.github/workflows/update.yml`). It pulls public RSS/Atom feeds (Google News, Bing News, YouTube channel feeds for the outlets that cover the case, and Reddit on a best-effort basis), keeps only items that mention the case, de-duplicates them, and merges them into an append-only archive:
  - `data/articles.json` – news coverage
  - `data/videos.json` – video segments and hearing streams
  - `data/discussion.json` – public discussion threads (clearly labelled as non-news)
  - `data/status.json` – last run time and per-source health, shown on the page
- `data/case.json` is the **curated** layer: case status, timeline, people, legal explainer, court information, and primary-source links. Every entry cites its sources. This file is maintained by hand (and by a scheduled review agent) and carries its own "curated as of" date so readers can tell it apart from the automatic feed.

## Editorial principles

- Neutral language. Charges are described as alleged unless and until a jury finds otherwise.
- Every factual claim in the curated layer links to a source.
- Contested claims are presented as what each side asserts.
- Automatic feed items are shown with their outlet name and are not edited.

## Corrections

Open an issue on this repository with the entry, the problem, and a source.

## Running locally

```bash
python3 scripts/update.py
python3 -m http.server 8080
```
Then open http://localhost:8080/.
