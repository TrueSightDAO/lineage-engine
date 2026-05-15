"""
seed_dao_cvs.py — one-time sweep that materialises a `_cache/cv/<slug>.json`
for every contributor on the TrueSight DAO Contribution Ledger.

This is the "warm the cache" pass we run after the credentialing platform
ships, so members.html / credentials/#<slug> work for everyone on day one
rather than only for people who happen to log a practice event or have a
hand-migrated testimonial (Fatima, Emelin).

Pipeline:
  1. Fetch the full Ledger history ONCE (avoids ~N round-trips to Sheets).
  2. Group by contributor name; analyse per the existing fetch_contributions
     functions so the per-slug JSON matches the shape build_cv_cache.py
     already understands as a "preserved CV".
  3. Write `<data_root>/_cache/cv/<slug>.json` per contributor — the slug is
     `slugify(name)` with -2 / -3 suffix collisions handled the same way
     build_cv_cache.py does.
  4. (Optional) immediately re-run build_cv_cache.build() on the data root
     so the .md / .pdf / index.json are regenerated in one shot.

The output JSON intentionally matches save_contribution_data() in
fetch_contributions.py, so build_cv_cache.py's `collect_preserved_cvs()`
path picks them up without any builder changes.

Re-runnable: writing over an existing `_cache/cv/<slug>.json` that
build_cv_cache.py previously wrapped is safe — the builder preserves the
raw source under `dao_contributions.source` on subsequent runs.

Repository: https://github.com/TrueSightDAO/lineage-engine
Companion data repo: https://github.com/TrueSightDAO/lineage-credentials
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Reuse the existing ledger fetcher
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_contributions as fc  # noqa: E402
import build_cv_cache as bcc  # noqa: E402


def collect_unique_names(all_data: dict) -> list[str]:
    """Return sorted unique contributor names from the full ledger payload."""
    seen: set[str] = set()
    for c in all_data.get('contributions') or []:
        name = (c.get('Contributor Name') or '').strip()
        if name:
            seen.add(name)
    return sorted(seen)


def existing_slug_index(data_root: Path) -> dict[str, str]:
    """Map: lowercased display_name → existing slug (so the sweep does not
    clobber a hand-curated migration like fatima-toledo / emelin-frances-lisboa).
    """
    out: dict[str, str] = {}
    cv_dir = data_root / '_cache' / 'cv'
    if not cv_dir.is_dir():
        return out
    for jf in sorted(cv_dir.glob('*.json')):
        slug = jf.stem
        if slug.startswith('pk-'):
            continue
        data = bcc.read_json(jf) or {}
        if data.get('_generator') == 'build_cv_cache':
            src = (data.get('dao_contributions') or {}).get('source') or {}
        else:
            src = data
        name = (src.get('contributor_name') or '').strip().lower() if isinstance(src, dict) else ''
        if name and name not in out:
            out[name] = slug
    return out


def claim_slug(name: str, taken: set[str]) -> str:
    base = bcc.slugify(name) or 'member'
    if base not in taken:
        taken.add(base)
        return base
    for i in range(2, 200):
        cand = f'{base}-{i}'
        if cand not in taken:
            taken.add(cand)
            return cand
    raise RuntimeError(f'could not allocate slug for {name!r}')


def write_dao_cv_json(data_root: Path, slug: str, contributor_name: str, contributions: list, analysis: dict) -> Path:
    """Write the per-contributor payload that build_cv_cache treats as a
    preserved testimonial. Matches save_contribution_data() in
    fetch_contributions.py exactly so the builder picks it up unchanged."""
    payload = {
        'contributor_name': contributor_name,
        'generated_date': datetime.now().isoformat(),
        'summary': {
            'total_contributions': analysis['total_contributions'],
            'total_tdg_provisioned': analysis['total_tdg_provisioned'],
            'total_tdg_issued': analysis['total_tdg_issued'],
            'projects': analysis['projects'],
            'date_range': analysis['date_range'],
        },
        'analysis': analysis,
        'raw_contributions': contributions,
    }
    out_path = data_root / '_cache' / 'cv' / f'{slug}.json'
    bcc.write_json(out_path, payload)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description='Seed _cache/cv/*.json for every DAO contributor.')
    ap.add_argument('--data', required=True, help='Path to a lineage-credentials checkout')
    ap.add_argument('--rebuild', action='store_true', help='After seeding, run build_cv_cache.build() to render md/pdf/index.')
    ap.add_argument('--no-pdf', action='store_true', help='When --rebuild is passed, skip PDF rendering.')
    ap.add_argument('--limit', type=int, default=0, help='Optional cap on number of contributors (testing).')
    ap.add_argument('--min-contributions', type=int, default=1, help='Skip contributors with fewer than N contributions (default 1).')
    args = ap.parse_args()

    data_root = Path(args.data).resolve()
    if not (data_root / '_cache').is_dir() and not (data_root / 'programs').is_dir():
        print(f'❌ {data_root} does not look like a lineage-credentials checkout', file=sys.stderr)
        sys.exit(1)

    service = fc.setup_google_sheets()
    if not service:
        sys.exit(1)

    print('📥 Fetching full Ledger history…')
    all_data = fc.fetch_all_contributions(service)
    if not all_data:
        sys.exit(1)

    names = collect_unique_names(all_data)
    print(f'🧑‍🤝‍🧑 {len(names)} unique contributor names on the ledger.')

    existing = existing_slug_index(data_root)
    taken: set[str] = set(existing.values())

    if args.limit:
        names = names[: args.limit]
        print(f'  ▶ limited to first {len(names)} for this run.')

    written = 0
    skipped_empty = 0
    skipped_too_few = 0
    for name in names:
        contributions = fc.get_contributor_contributions(all_data, name)
        if not contributions:
            skipped_empty += 1
            continue
        if len(contributions) < args.min_contributions:
            skipped_too_few += 1
            continue
        analysis = fc.analyze_contributions(contributions)
        slug = existing.get(name.lower()) or claim_slug(name, taken)
        out = write_dao_cv_json(data_root, slug, name, contributions, analysis)
        rel = out.relative_to(data_root)
        print(f'  ✓ {name:40s} → {rel}')
        written += 1

    print('')
    print(f'✅ Wrote {written} CV stubs ({skipped_empty} empty, {skipped_too_few} below --min-contributions).')

    if args.rebuild:
        print('')
        print('🔁 Rebuilding _cache (md + pdf + index.json)…')
        stats = bcc.build(data_root, write_pdfs=not args.no_pdf)
        print(f'✅ Builder stats: {stats}')


if __name__ == '__main__':
    main()
