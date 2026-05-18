"""
fetch_pending_chatlogs.py — Phase 4.1.

Pulls submitted-but-unreviewed contribution claims out of the
**Scored Chatlogs** tab on the operator's scoring workbook and
returns them grouped by Contributor Name, so `build_cv_cache.py`
can attach each contributor's pending claims to their CV record.

Source spreadsheet: TrueSightDAO Scored Chatlogs workbook
  https://docs.google.com/spreadsheets/d/1Tbj7H5ur_egQLRugdXUaSIhEYIKp0vvVv2IZ7WTLCUo/edit
Tab:               `Scored Chatlogs`
Header row:        3
Floor date:        2024-12-13  (first submission on this sheet)

Dedup convention (owned by the operator's promotion GAS):
  Column L "Main Ledger Row Number" is populated when a scoring is
  approved and the entry is promoted into the Main Ledger's
  "Ledger history" tab. Phase 4.1 treats:
    - L empty   → pending; surface in cv['pending_contributions']
    - L non-empty → already in Ledger history; skip (would double-count)

Spec: agentic_ai_context/CREDENTIALING_PROGRAM_PAGES.md §18.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from fetch_contributions import setup_google_sheets  # share the same auth path


SCORED_CHATLOGS_SPREADSHEET_ID = '1Tbj7H5ur_egQLRugdXUaSIhEYIKp0vvVv2IZ7WTLCUo'
SCORED_CHATLOGS_TAB = 'Scored Chatlogs'
SCORED_CHATLOGS_HEADER_ROW = 3
SCORED_CHATLOGS_FLOOR_DATE = '2024-12-13'  # surfaced in the page caveat banner

# Headers expected on row 3 (defensive name → index mapping in the fetcher).
EXPECTED_HEADERS = [
    'Contributor Name',          # A
    'Project Name',              # B
    'Contribution Made',         # C
    'Rubric classification',     # D
    'TDGs Provisioned',          # E
    'Status',                    # F
    'TDGs Issued',               # G
    'Status date',               # H
    'Existing Contributor',      # I
    'Reporter Name',             # J
    'Scoring Hash Key',          # K
    'Main Ledger Row Number',    # L  ← dedup primary key
    'Reviewer Email',            # M
]


def fetch_pending_chatlogs_by_contributor(service) -> dict[str, list[dict[str, Any]]]:
    """Return a mapping of Contributor Name → list of pending entries.

    A row is "pending" iff col L (Main Ledger Row Number) is empty.
    Rows missing a contributor name are dropped (no slug to attach to).
    """
    if service is None:
        print('  ⚠ fetch_pending_chatlogs: no sheets service; returning empty', file=sys.stderr)
        return {}
    try:
        rng = f"'{SCORED_CHATLOGS_TAB}'!A{SCORED_CHATLOGS_HEADER_ROW}:M"
        result = service.spreadsheets().values().get(
            spreadsheetId=SCORED_CHATLOGS_SPREADSHEET_ID,
            range=rng,
        ).execute()
    except Exception as e:
        print(f'  ⚠ fetch_pending_chatlogs sheets error: {e}', file=sys.stderr)
        return {}

    rows = result.get('values', [])
    if not rows:
        return {}

    headers = rows[0]
    # Build a name → index map defensively, falling back to position if a header
    # changed wording (resilient against minor copy edits on the sheet).
    idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        h_stripped = (h or '').strip()
        if h_stripped:
            idx[h_stripped] = i
    def col(name: str, fallback: int) -> int:
        return idx.get(name, fallback)

    i_name      = col('Contributor Name',       0)
    i_project   = col('Project Name',           1)
    i_contrib   = col('Contribution Made',      2)
    i_rubric    = col('Rubric classification',  3)
    i_provis    = col('TDGs Provisioned',       4)
    i_status    = col('Status',                 5)
    i_issued    = col('TDGs Issued',            6)
    i_statusdt  = col('Status date',            7)
    i_existing  = col('Existing Contributor',   8)
    i_reporter  = col('Reporter Name',          9)
    i_hashkey   = col('Scoring Hash Key',      10)
    i_ledgerrow = col('Main Ledger Row Number',11)
    i_reviewer  = col('Reviewer Email',        12)

    out: dict[str, list[dict[str, Any]]] = {}
    pending_count = 0
    settled_count = 0
    for row in rows[1:]:
        # Pad short rows so index lookups don't blow up.
        if len(row) <= i_ledgerrow:
            row = row + [''] * (i_ledgerrow + 1 - len(row))
        ledger_row = (row[i_ledgerrow] or '').strip()
        if ledger_row:
            settled_count += 1
            continue  # already promoted; skip (Ledger history will surface it)

        name = (row[i_name] or '').strip() if i_name < len(row) else ''
        if not name:
            continue

        entry = {
            'contributor_name': name,
            'project_name':     (row[i_project]  or '').strip() if i_project  < len(row) else '',
            'contribution':     (row[i_contrib]  or '').strip() if i_contrib  < len(row) else '',
            'rubric':           (row[i_rubric]   or '').strip() if i_rubric   < len(row) else '',
            'tdg_provisioned':  (row[i_provis]   or '').strip() if i_provis   < len(row) else '',
            'status':           (row[i_status]   or '').strip() if i_status   < len(row) else '',
            'tdg_issued':       (row[i_issued]   or '').strip() if i_issued   < len(row) else '',
            'status_date':      (row[i_statusdt] or '').strip() if i_statusdt < len(row) else '',
            'existing':         (row[i_existing] or '').strip() if i_existing < len(row) else '',
            'reporter':         (row[i_reporter] or '').strip() if i_reporter < len(row) else '',
            'scoring_hash':     (row[i_hashkey]  or '').strip() if i_hashkey  < len(row) else '',
            'reviewer_email':   (row[i_reviewer] or '').strip() if i_reviewer < len(row) else '',
        }
        out.setdefault(name, []).append(entry)
        pending_count += 1

    # Sort each contributor's entries by status_date desc so the page shows
    # newest-first naturally without per-render sort logic.
    for name in out:
        out[name].sort(key=lambda e: e.get('status_date') or '', reverse=True)

    print(f'  fetch_pending_chatlogs: {pending_count} pending across {len(out)} contributors '
          f'({settled_count} settled rows skipped)', file=sys.stderr)
    return out
