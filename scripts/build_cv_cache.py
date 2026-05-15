"""
build_cv_cache.py — assemble per-person CV artifacts for the credentialing platform.

Reads from a lineage-credentials checkout:
  programs/<program>/<pk-hash>/practice/*.json   — raw signed practice events
  programs/<program>/<pk-hash>/identity.json     — optional: names, emails
  _cache/cv/<slug>.json                          — pre-existing migrated CVs (Fatima, Emelin)

Writes back:
  _cache/cv/<slug>.json                          — unified structured CV
  _cache/cv/<slug>.md                            — human-readable Markdown
  _cache/cv/<slug>.pdf                           — job-application-grade PDF (WeasyPrint)
  _cache/index.json                              — directory index
  _cache/aliases.json                            — pk-hash → slug map

For v1 / MVP this builder is intentionally simple:
  - Aggregate practice events per pk-hash (counts, totals, per-program breakdown).
  - Preserve any pre-existing _cache/cv/<slug>.json (migrated DAO contribution
    testimonials) — those land in the index too so DAO-only members get a CV
    page with no practice section.
  - Render Markdown + PDF from a small Jinja-ish template (inline string,
    no extra dep).
  - PDF via WeasyPrint when available; degrades gracefully without it.

Subsequent PRs add:
  - Grok narrative summarisation (cache keyed on input-hash).
  - DAO contribution join via fetch_contributions.py when identity.json names
    a known contributor.
  - Incremental diff-driven builds (per CREDENTIALING_PLATFORM.md §9a).
  - is_governor / voting_rights annotation from the Main Ledger.

Repository: https://github.com/TrueSightDAO/lineage-engine
Companion data repo: https://github.com/TrueSightDAO/lineage-credentials
Design doc: https://github.com/TrueSightDAO/agentic_ai_context/blob/main/CREDENTIALING_PLATFORM.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from weasyprint import HTML  # type: ignore
    HAS_WEASYPRINT = True
except Exception:
    HAS_WEASYPRINT = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Lowercase + hyphenated friendly slug. Used when identity.json declares a name."""
    if not name:
        return ''
    normalised = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r"[^a-zA-Z0-9]+", '-', normalised).strip('-').lower()
    return s


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'  ⚠ skip unreadable JSON {path}: {e}', file=sys.stderr)
        return None


def write_json(path: Path, obj: Any) -> None:
    """Strict JSON write — fails loudly on NaN / Infinity so the browser
    never gets handed a `JSON.parse`-rejected token. Browsers reject literal
    `NaN`, but Python's json.dumps emits it by default. allow_nan=False makes
    bad numeric data surface as a build-time error instead of a runtime
    'Network error: Unexpected token N' on the CV page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
        encoding='utf-8',
    )


# ---------------------------------------------------------------------------
# practice-events aggregation
# ---------------------------------------------------------------------------

def collect_practitioners(data_root: Path) -> dict[str, dict[str, Any]]:
    """Walk programs/*/pk-*/ and return one record per pk-hash."""
    practitioners: dict[str, dict[str, Any]] = {}
    programs_root = data_root / 'programs'
    if not programs_root.is_dir():
        return practitioners

    for program_dir in sorted(p for p in programs_root.iterdir() if p.is_dir()):
        program = program_dir.name
        manifest = read_json(program_dir / 'manifest.json') or {}
        for pk_dir in sorted(d for d in program_dir.iterdir() if d.is_dir() and d.name.startswith('pk-')):
            pk_hash = pk_dir.name
            rec = practitioners.setdefault(pk_hash, {
                'pk_hash': pk_hash,
                'identity': {},
                'programs': {},
            })
            identity = read_json(pk_dir / 'identity.json') or {}
            if identity:
                rec['identity'].update(identity)

            practice_dir = pk_dir / 'practice'
            events = []
            if practice_dir.is_dir():
                for f in sorted(practice_dir.glob('*.json')):
                    e = read_json(f)
                    if e is not None:
                        events.append({**e, '_path': str(f.relative_to(data_root))})

            rec['programs'][program] = {
                'display_name': manifest.get('display_name', program),
                'lineage_root': manifest.get('lineage_root'),
                'practice_events': events,
                'practice_count': len(events),
                'total_practice_minutes': sum(
                    (e.get('payload') or {}).get('total_practice_minutes', 0) for e in events
                ),
            }
    return practitioners


def derive_slug(pk_hash: str, identity: dict[str, Any], aliases: dict[str, str]) -> str:
    """Friendly slug from identity.names[0] if available + unique; else pk-hash."""
    names = identity.get('names') or []
    if names:
        candidate = slugify(names[0])
        if candidate:
            # If another pk-hash already claims this slug, suffix with -2, -3, ...
            taken_slugs = {v for k, v in aliases.items() if k != pk_hash}
            if candidate not in taken_slugs:
                return candidate
            for i in range(2, 50):
                trial = f'{candidate}-{i}'
                if trial not in taken_slugs:
                    return trial
    return pk_hash


# ---------------------------------------------------------------------------
# preserved migrated CVs (Fatima, Emelin) → seed index
# ---------------------------------------------------------------------------

def _coerce_voting_pct(raw: Any) -> float:
    """Convert a voting-weight cell ("0.20%" or 0.002) to a float 0-100."""
    if raw in (None, ''):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw) * 100 if raw <= 1 else float(raw)
    s = str(raw).strip().rstrip('%').replace(',', '')
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def collect_preserved_cvs(data_root: Path) -> dict[str, dict[str, Any]]:
    """Pre-existing _cache/cv/<slug>.json entries that aren't tied to a pk-hash yet.

    These are the migrated Fatima / Emelin testimonials. They get indexed so
    the directory page shows them, even though they have no practice events
    yet. The FULL source JSON is kept under the `source` key so the unified
    output preserves every field (raw_contributions, analysis, etc.) and the
    builder can re-run on its own output without losing data.

    Detection: a preserved CV is any `_cache/cv/<slug>.json` that:
      - doesn't have a `pk-` slug prefix (those are practitioner-derived);
      - doesn't already look like a builder-produced unified CV (we mark
        those with `_generator: "build_cv_cache"`).
    """
    cv_dir = data_root / '_cache' / 'cv'
    out: dict[str, dict[str, Any]] = {}
    if not cv_dir.is_dir():
        return out
    for jf in sorted(cv_dir.glob('*.json')):
        slug = jf.stem
        if slug.startswith('pk-'):
            continue
        data = read_json(jf)
        if not data:
            continue
        # If this file was produced by a previous builder run, extract the
        # original `source` payload that the first run preserved. Otherwise
        # treat the whole file as the source.
        if isinstance(data, dict) and data.get('_generator') == 'build_cv_cache':
            source = (data.get('dao_contributions') or {}).get('source') or {}
        else:
            source = data
        summary = (source.get('summary') or {}) if isinstance(source, dict) else {}
        governance = (source.get('governance') or {}) if isinstance(source, dict) else {}
        out[slug] = {
            'slug': slug,
            'display_name': (source.get('contributor_name') if isinstance(source, dict) else None) or slug,
            'source': source,
            'governance': governance,
            'total_contributions': summary.get('total_contributions', 0),
            'total_tdg_provisioned': summary.get('total_tdg_provisioned', 0),
            'date_range': summary.get('date_range', {}),
        }
    return out


# ---------------------------------------------------------------------------
# CV rendering
# ---------------------------------------------------------------------------

def build_unified_cv(slug: str, pk_record: dict[str, Any] | None, preserved: dict[str, Any] | None) -> dict[str, Any]:
    """Merge practitioner record (if any) with preserved testimonial (if any)."""
    identity = (pk_record or {}).get('identity') or {}
    names = identity.get('names') or []
    display_name = (
        names[0] if names else (preserved or {}).get('display_name') or slug
    )
    programs = (pk_record or {}).get('programs') or {}

    cv: dict[str, Any] = {
        'slug': slug,
        'display_name': display_name,
        'pk_hash': (pk_record or {}).get('pk_hash'),
        'identity': identity,
        'generated_at': now_utc_iso(),
        'has_elective_records': bool(programs and any(p['practice_count'] for p in programs.values())),
        'has_dao_contributions': bool(preserved),
        'programs': {
            name: {
                'display_name': p['display_name'],
                'lineage_root': p['lineage_root'],
                'practice_count': p['practice_count'],
                'total_practice_minutes': p['total_practice_minutes'],
                'recent_events': sorted(
                    p['practice_events'],
                    key=lambda e: (e.get('captured_at') or ''),
                    reverse=True,
                )[:20],
            }
            for name, p in programs.items()
        },
    }
    if preserved:
        # Preserve the full source payload so the unified cv.json is a strict
        # superset of the input — re-running the builder on its own output
        # never loses data (raw_contributions, analysis, etc. all stay intact).
        cv['dao_contributions'] = {
            'total_contributions': preserved.get('total_contributions'),
            'total_tdg_provisioned': preserved.get('total_tdg_provisioned'),
            'date_range': preserved.get('date_range'),
            'source': preserved.get('source'),
        }
        cv['governance'] = preserved.get('governance') or {}
    cv['_generator'] = 'build_cv_cache'
    return cv


def render_markdown(cv: dict[str, Any]) -> str:
    lines = []
    lines.append(f"# {cv['display_name']}")
    lines.append('')
    lines.append(f"*TrueSight DAO Credential Profile · generated {cv['generated_at']}*")
    lines.append('')

    if cv.get('has_dao_contributions'):
        dc = cv['dao_contributions']
        lines.append('## DAO Contributions')
        lines.append('')
        lines.append(f"- Total contributions: **{dc.get('total_contributions', 0)}**")
        if dc.get('total_tdg_provisioned'):
            lines.append(f"- Total TDG provisioned: **{dc['total_tdg_provisioned']:,.2f}**")
        dr = dc.get('date_range') or {}
        if dr.get('earliest') and dr.get('latest'):
            lines.append(f"- Active period: {dr['earliest']} – {dr['latest']}")
        lines.append(f"- Source: [_cache/cv/{cv['slug']}.json](https://github.com/TrueSightDAO/lineage-credentials/blob/main/_cache/cv/{cv['slug']}.json)")
        lines.append('')

    for program_name, p in cv.get('programs', {}).items():
        lines.append(f"## {p['display_name']}")
        lines.append('')
        lines.append(f"- Practice sessions logged: **{p['practice_count']}**")
        lines.append(f"- Total practice time: **{p['total_practice_minutes']} minutes**")
        if p.get('lineage_root'):
            lines.append(f"- Lineage root: {p['lineage_root']}")
        if p['recent_events']:
            lines.append('')
            lines.append('### Recent sessions')
            for e in p['recent_events'][:10]:
                cap = e.get('captured_at', '?')
                theme = ((e.get('payload') or {}).get('theme')) or e.get('practice_type', '')
                mins = (e.get('payload') or {}).get('total_practice_minutes', 0)
                src = e.get('_path', '')
                src_link = f"[{src}](https://github.com/TrueSightDAO/lineage-credentials/blob/main/{src})" if src else ''
                lines.append(f"- **{cap}** — {theme} ({mins} min) — {src_link}")
        lines.append('')

    if not cv.get('has_dao_contributions') and not cv.get('programs'):
        lines.append('_No records yet._')

    lines.append('---')
    lines.append('')
    lines.append('TrueSight DAO Credential Profile. Every claim above cites the line in the underlying ledger or repo — open the source link to audit.')
    return '\n'.join(lines) + '\n'


def render_html(cv: dict[str, Any], md_body: str) -> str:
    """Minimal HTML wrap of the Markdown body for WeasyPrint.

    Inline a small stylesheet that targets 'respectable job-application CV'
    typography per the CREDENTIALING_PLATFORM.md decision.
    """
    # Markdown → very-light HTML conversion (paragraphs + headings + lists).
    # Keeping this dep-free for now; can swap to python-markdown later.
    html_body = []
    in_list = False
    for line in md_body.split('\n'):
        stripped = line.rstrip()
        if stripped.startswith('### '):
            if in_list:
                html_body.append('</ul>')
                in_list = False
            html_body.append(f'<h3>{stripped[4:]}</h3>')
        elif stripped.startswith('## '):
            if in_list:
                html_body.append('</ul>')
                in_list = False
            html_body.append(f'<h2>{stripped[3:]}</h2>')
        elif stripped.startswith('# '):
            if in_list:
                html_body.append('</ul>')
                in_list = False
            html_body.append(f'<h1>{stripped[2:]}</h1>')
        elif stripped.startswith('- '):
            if not in_list:
                html_body.append('<ul>')
                in_list = True
            item = stripped[2:]
            # tiny inline markdown: **bold**, *italic*, [text](url)
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            item = re.sub(r'\*(.+?)\*', r'<em>\1</em>', item)
            item = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', item)
            html_body.append(f'<li>{item}</li>')
        elif stripped == '---':
            if in_list:
                html_body.append('</ul>')
                in_list = False
            html_body.append('<hr/>')
        elif stripped == '':
            if in_list:
                html_body.append('</ul>')
                in_list = False
        else:
            if in_list:
                html_body.append('</ul>')
                in_list = False
            line_html = stripped
            line_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line_html)
            line_html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', line_html)
            line_html = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', line_html)
            html_body.append(f'<p>{line_html}</p>')
    if in_list:
        html_body.append('</ul>')

    css = """
      @page { size: A4; margin: 22mm 18mm 22mm 18mm; }
      body { font-family: 'Source Serif Pro', Georgia, serif; color: #1a1a1a; font-size: 11pt; line-height: 1.5; }
      h1 { font-family: 'Source Sans Pro', 'Helvetica Neue', Arial, sans-serif; font-size: 26pt; margin: 0 0 4pt 0; color: #111; }
      h2 { font-family: 'Source Sans Pro', 'Helvetica Neue', Arial, sans-serif; font-size: 13pt; text-transform: uppercase; letter-spacing: 0.08em; color: #333; border-bottom: 1px solid #c8c8c8; padding-bottom: 3pt; margin-top: 18pt; }
      h3 { font-family: 'Source Sans Pro', 'Helvetica Neue', Arial, sans-serif; font-size: 11pt; color: #555; margin-top: 12pt; }
      ul { padding-left: 18pt; }
      li { margin: 2pt 0; }
      em { color: #555; }
      a { color: #245785; text-decoration: none; }
      hr { border: none; border-top: 1px solid #e0e0e0; margin: 18pt 0; }
      p { margin: 4pt 0; }
    """
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><style>{css}</style></head>
<body>{''.join(html_body)}</body></html>"""


def render_pdf(html: str, out_path: Path) -> bool:
    if not HAS_WEASYPRINT:
        print(f'  ⚠ skipping PDF (weasyprint not installed): {out_path}', file=sys.stderr)
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        HTML(string=html).write_pdf(str(out_path))
        return True
    except Exception as e:
        print(f'  ⚠ PDF render failed for {out_path}: {e}', file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------

def build(data_root: Path, write_pdfs: bool = True) -> dict[str, Any]:
    practitioners = collect_practitioners(data_root)
    preserved = collect_preserved_cvs(data_root)
    aliases_path = data_root / '_cache' / 'aliases.json'
    aliases: dict[str, str] = read_json(aliases_path) or {} if aliases_path.is_file() else {}

    # First pass — assign canonical slugs for each pk-hash record.
    for pk_hash, rec in practitioners.items():
        aliases[pk_hash] = derive_slug(pk_hash, rec.get('identity') or {}, aliases)

    # Second pass — build per-slug unified CVs. Indexed by canonical slug.
    cvs_by_slug: dict[str, dict[str, Any]] = {}
    # Practitioners
    for pk_hash, rec in practitioners.items():
        slug = aliases[pk_hash]
        cvs_by_slug[slug] = build_unified_cv(slug, rec, preserved.get(slug))
    # DAO-only preserved testimonials (no matching pk-hash)
    for slug, p in preserved.items():
        if slug in cvs_by_slug:
            continue
        cvs_by_slug[slug] = build_unified_cv(slug, None, p)

    # Write outputs
    cv_dir = data_root / '_cache' / 'cv'
    cv_dir.mkdir(parents=True, exist_ok=True)
    members = []
    for slug, cv in sorted(cvs_by_slug.items()):
        write_json(cv_dir / f'{slug}.json', cv)
        md = render_markdown(cv)
        (cv_dir / f'{slug}.md').write_text(md, encoding='utf-8')
        if write_pdfs:
            render_pdf(render_html(cv, md), cv_dir / f'{slug}.pdf')
        # Pull the headline DAO numbers up to the index so the directory
        # page can show "X TDG · Y contributions" per card without having
        # to fetch every per-slug JSON. TDG Issued (col G) is what the
        # contributor actually controls; col E "Provisioned" reads as
        # approve-but-not-yet-awarded and is misleading on the public
        # directory.
        dc = cv.get('dao_contributions') or {}
        dc_src = (dc.get('source') or {}) if isinstance(dc, dict) else {}
        dc_summary = (dc_src.get('summary') or {}) if isinstance(dc_src, dict) else {}
        gov = cv.get('governance') or {}
        members.append({
            'slug': slug,
            'display_name': cv['display_name'],
            'pk_hash': cv.get('pk_hash'),
            'is_governor': bool(gov.get('is_governor')),
            'voting_rights': _coerce_voting_pct(gov.get('total_voting_power_pct') or gov.get('voting_power_pct')),
            'primary_program': next(iter(cv.get('programs') or {}), None),
            'has_dao_contributions': cv.get('has_dao_contributions', False),
            'has_elective_records': cv.get('has_elective_records', False),
            'total_tdg_controlled': dc_summary.get('total_tdg_issued') or 0,
            'total_contributions': dc_summary.get('total_contributions') or 0,
            'last_updated': cv['generated_at'],
        })

    write_json(data_root / '_cache' / 'index.json', {
        'generated_at': now_utc_iso(),
        'count': len(members),
        'members': members,
    })
    write_json(aliases_path, aliases)

    return {
        'practitioners': len(practitioners),
        'preserved_cvs': len(preserved),
        'total_cvs': len(cvs_by_slug),
        'wrote_pdfs': HAS_WEASYPRINT and write_pdfs,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description='Build CV cache for lineage-credentials.')
    ap.add_argument('--data', default='.', help='Path to a lineage-credentials checkout (default: current dir)')
    ap.add_argument('--no-pdf', action='store_true', help='Skip PDF rendering (faster local iteration)')
    args = ap.parse_args()

    data_root = Path(args.data).resolve()
    if not (data_root / 'programs').is_dir() and not (data_root / '_cache').is_dir():
        print(f'❌ {data_root} does not look like a lineage-credentials checkout (no programs/ or _cache/)', file=sys.stderr)
        sys.exit(1)

    print(f'🚀 Building CV cache against {data_root}')
    stats = build(data_root, write_pdfs=not args.no_pdf)
    print(f'✅ Done: {stats}')


if __name__ == '__main__':
    main()
