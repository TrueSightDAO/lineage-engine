# lineage-engine

Code that produces credential records and rendered CV artifacts for the [TrueSight DAO credentialing platform](https://github.com/TrueSightDAO/agentic_ai_context/blob/main/CREDENTIALING_PLATFORM.md).

This repo is the **engine**. The matching **data** repo is [`lineage-credentials`](https://github.com/TrueSightDAO/lineage-credentials), which holds every practitioner's signed events plus the rendered `_cache/` outputs.

## What this repo holds

- `scripts/` — Python that aggregates raw events + DAO contribution history into per-person CVs.
- (later) Grok prompts, PDF templates (HTML/CSS for WeasyPrint), schema definitions.

## How the two repos work together

```
┌─────────────────────────┐         ┌─────────────────────────┐
│  lineage-credentials    │  ←──    │  lineage-engine         │
│  (DATA)                 │ commits │  (CODE)                 │
│                         │  ─────→ │                         │
│  programs/p/pk-x/...    │         │  scripts/build_cv_cache │
│  _cache/cv/<slug>.*     │         │  templates/             │
│  .github/workflows/     │         │  prompts/               │
│                         │         │                         │
│   GitHub Action runs    │ ──checkout─→  this engine repo    │
│   here, on push         │         │                         │
│                         │         │                         │
│   commits _cache back   │         │   no writes from engine │
│   into itself           │         │   → credentials         │
└─────────────────────────┘         └─────────────────────────┘
```

See the full design in [`CREDENTIALING_PLATFORM.md`](https://github.com/TrueSightDAO/agentic_ai_context/blob/main/CREDENTIALING_PLATFORM.md).

## Setup

```bash
pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

The service account needs **read** access to the [TrueSight DAO Contribution Ledger](https://docs.google.com/spreadsheets/d/1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU/edit).

## Scripts

### `scripts/fetch_contributions.py`

Pulls a single contributor's history out of the Ledger history sheet, analyses it, and writes a JSON file. Used as the foundation for testimonial generation.

```bash
python scripts/fetch_contributions.py 'Fatima Toledo'
# writes ./testimonials/fatima_toledo_contributions.json
```

This script was migrated from `tokenomics/python_scripts/reference_and_testimonials/` on 2026-05-14 as part of the lineage-credentials platform consolidation. The MVP cache builder (forthcoming `scripts/build_cv_cache.py`) wraps this primitive.

## Status

- **2026-05-14** — repo seeded with the migrated testimonial generator. `build_cv_cache.py`, Grok prompts, and PDF templates land in subsequent PRs.
