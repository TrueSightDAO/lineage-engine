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

### `scripts/sync_sunmint_program_activity.py`

Syncs SunMint program activity (tree plantings, growth-monitoring visits, plot
registrations) from the public per-event attestation ledger
[`verify_public_signatures`](https://github.com/TrueSightDAO/verify_public_signatures)
into this repo's **data** sibling, `lineage-credentials`, as::

    programs/<slug>/pk-<hash>/sunmint/<msg_id>.json

Attribution is **Option B**: an event belongs to a program when the host in its
signed payload's `Submission Source` line matches a domain registered in
`scripts/sunmint_program_registry.json` (e.g. `cfr.truesight.me` -> `crf-anapu`).
The `sunmint/` subfolder is deliberately **not** `practice/`, so capoeira's
`practice_count` / `total_practice_minutes` semantics are never polluted by tree
data.

The pk-hash uses the canonical primitive shared by the browser, the GAS
`program_admin_endpoint.js`, and `build_cv_cache.py`::

    pk-<hash> = 'pk-' + first 12 chars of base64url(SHA-256(base64-decoded pubkey))

Runs **dry-run by default** (standing convention for new write scripts)::

    python3 scripts/sync_sunmint_program_activity.py --dry-run
    GITHUB_TOKEN=... python3 scripts/sync_sunmint_program_activity.py --push

Idempotent + content-addressed: each file's exact bytes are hashed to a git-blob
sha and compared against the remote file's sha, so re-runs skip unchanged events
(same skip posture as `sync_sunmint_signatures.py`).

### `scripts/build_cv_cache.py` — SunMint activity kind

`build_cv_cache.py` folds two activity kinds into each per-program CV record:

- **Practice** (`programs/<slug>/pk-<hash>/practice/*.json`) — the capoeira-style
  events that drive `practice_count` / `total_practice_minutes`.
- **SunMint** (`programs/<slug>/pk-<hash>/sunmint/*.json`, written by
  `sync_sunmint_program_activity.py`) — tree plantings, growth-monitoring visits
  and plot/boundary registrations. Aggregated into `trees_planted_count`,
  `monitoring_events_count`, `plots_registered_count`, `sunmint_event_count`
  and `last_sunmint_activity_at` (plus the itemized `sunmint_events` for the
  click-through renderer). The two kinds stay **separate** so a tree-only
  contributor never renders a nonsensical "Total practice time: 0 minutes".

`_program_modes(manifest)` tolerates **both** manifest shapes: the new
non-exclusive `program_modes` array (a program can be both a credentialing
cohort *and* a SunMint cohort) and the legacy single `program_mode` string.
`program_activity_score()` counts SunMint activity too, so a student with only
tree plantings still resolves a `primary_program` and is never silently dropped
from `_cache/index.json`.

## Status

- **2026-05-14** — repo seeded with the migrated testimonial generator. `build_cv_cache.py`, Grok prompts, and PDF templates land in subsequent PRs.
