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

try:
    from grok_narrative import generate_narrative as _grok_generate_narrative  # type: ignore
    HAS_GROK = True
except Exception:
    HAS_GROK = False

try:
    from qr_code import generate_qr_with_logo as _generate_qr_with_logo  # type: ignore
    HAS_QR = True
except Exception:
    HAS_QR = False

try:
    from cert_overlay import render_certificate_pdf_overlay as _render_cert_overlay  # type: ignore
    HAS_CERT_OVERLAY = True
except Exception:
    HAS_CERT_OVERLAY = False

try:
    from fetch_pending_chatlogs import (  # type: ignore
        fetch_pending_chatlogs_by_contributor as _fetch_pending_chatlogs,
        SCORED_CHATLOGS_FLOOR_DATE as _PENDING_FLOOR_DATE,
    )
    HAS_PENDING_FETCHER = True
except Exception:
    HAS_PENDING_FETCHER = False
    _PENDING_FLOOR_DATE = '2024-12-13'


# Credential profile URL pattern — what the per-slug QR code resolves to.
CREDENTIAL_PROFILE_URL = 'https://truesight.me/credentials/#{slug}'

# Per-program credential URL — surface where partner-co-branded QR codes resolve.
# Spec: agentic_ai_context/CREDENTIALING_PROGRAM_PAGES.md §15.
PROGRAM_CREDENTIAL_URL = 'https://truesight.me/programs/{program}/credentials/#{slug}'

# Directory where per-program logo PNGs are vendored. One subdirectory per
# program slug; each holds a square-ish `logo.png` that gets composited into
# the centre of the program-scoped QR. A program without a vendored logo is
# skipped silently (warn-and-continue) so partner onboarding isn't blocked
# on the logo being committed first.
SCRIPT_DIR = Path(__file__).resolve().parent
PROGRAM_ASSETS_DIR = SCRIPT_DIR / 'program_assets'
PROGRAM_ASSETS_REGISTRY = PROGRAM_ASSETS_DIR / 'registry.json'


def _load_cert_config(url_program_slug: str) -> dict[str, Any] | None:
    """Load `program_assets/<slug>/cert_config.json` if present.

    Phase 3b.1: cert config (strategy + overlay coordinates) lives in
    lineage-engine alongside the template + font. Phase 3b.2 will lift
    the canonical source up to truesight_me/programs/<slug>/manifest.json
    per spec §17.13.5. For now this is the engine-local home.
    """
    path = PROGRAM_ASSETS_DIR / url_program_slug / 'cert_config.json'
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'  ⚠ cert_config parse error for {url_program_slug}: {e}', file=sys.stderr)
        return None


def _resolve_issued_at(cv: dict[str, Any], data_program_slug: str) -> datetime:
    """Pick the timestamp the certificate prints as the issuance date.

    Heuristic for Phase 3b.1 (before Phase 3b.6 `locked_at` lands):
      - Use the LATEST `recent_events[*].captured_at` for this program if any
      - Fall back to today's date (UTC) if no events recorded
    Once `locked_at` ships, that overrides everything else — but we don't
    consume it yet.
    """
    program_rec = (cv.get('programs') or {}).get(data_program_slug) or {}
    locked_at = program_rec.get('locked_at')
    if locked_at:
        try:
            return datetime.fromisoformat(str(locked_at).replace('Z', '+00:00'))
        except Exception:
            pass
    events = program_rec.get('recent_events') or []
    latest = None
    for ev in events:
        ts = ev.get('captured_at')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    return latest or datetime.now(timezone.utc)


def _render_program_certificate(
    *,
    url_program_slug: str,
    data_program_slug: str,
    cv: dict[str, Any],
    slug: str,
    qr_path: Path,
    out_path: Path,
) -> bool:
    """Phase 3b certificate render. Dispatches on cert_config.strategy.
    Returns True on success, False on warn-and-skip."""
    cfg = _load_cert_config(url_program_slug)
    if not cfg:
        return False  # no cert config — partner not yet on Phase 3b
    strategy = cfg.get('strategy', 'html_template')
    if strategy == 'pdf_overlay':
        if not HAS_CERT_OVERLAY:
            print(f'  ⚠ cert_overlay module not importable; skipping {out_path.name}', file=sys.stderr)
            return False
        template_rel = cfg.get('pdf_template')
        if not template_rel:
            print(f'  ⚠ cert_config missing pdf_template for {url_program_slug}', file=sys.stderr)
            return False
        template_pdf = PROGRAM_ASSETS_DIR / url_program_slug / template_rel
        if not template_pdf.is_file():
            print(f'  ⚠ template missing: {template_pdf}', file=sys.stderr)
            return False
        font_paths = [
            PROGRAM_ASSETS_DIR / url_program_slug / fp
            for fp in cfg.get('font_files') or []
        ]
        try:
            _render_cert_overlay(
                template_pdf=template_pdf,
                out_path=out_path,
                fields=cfg.get('overlay_fields') or {},
                recipient_name=cv.get('display_name') or slug,
                issued_at=_resolve_issued_at(cv, data_program_slug),
                qr_path=qr_path,
                font_files=font_paths,
            )
            return True
        except Exception as e:
            print(f'  ⚠ pdf_overlay render failed for {out_path}: {e}', file=sys.stderr)
            return False
    # 'html_template' strategy: stub branch (spec §17.13 — deferred to a future
    # partner without their own PDF). Silent no-op until implemented.
    return False


def _load_program_url_map() -> dict[str, str]:
    """Map data-side `cv.programs[]` key → URL-side `<program-slug>`.

    Source of truth: `program_assets/registry.json`. The two slug spaces
    can diverge (legacy data-side `capoeira-tribo-mirim` vs URL-side
    `tribomirim`) so the registry holds an explicit mapping rather than
    relying on equality. Missing entries fall back to identity so newly-
    onboarded partners that use the same slug on both sides work without
    extra registry plumbing.
    """
    if not PROGRAM_ASSETS_REGISTRY.is_file():
        return {}
    try:
        reg = json.loads(PROGRAM_ASSETS_REGISTRY.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'  ⚠ program_assets/registry.json parse error: {e}', file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for url_slug, cfg in (reg.get('programs') or {}).items():
        for data_slug in (cfg or {}).get('data_program_slugs') or []:
            out[data_slug] = url_slug
    return out


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
                # source_pages[0] (manifest field) is the canonical "practice
                # platform URL" — the deployed app where sessions happen.
                # We surface it on the CV section so visitors can navigate
                # back to the practice surface.
                'source_pages': manifest.get('source_pages') or [],
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

def build_unified_cv(
    slug: str,
    pk_record: dict[str, Any] | None,
    preserved: dict[str, Any] | None,
    *,
    pending_by_name: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Merge practitioner record (if any) with preserved testimonial (if any).

    `pending_by_name` is a Phase 4.1 hook (§18) — when supplied, look up
    pending Scored Chatlogs entries by this CV's display_name and attach
    them under cv['pending_contributions']. The HTML credential page
    renders this as a separate "Recent activity (pending review)"
    section with caveat banner; the markdown→PDF path ignores it.
    """
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
                'source_pages': p.get('source_pages') or [],
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
    cv['qr_code'] = {
        'path': f'_cache/cv/{slug}.qr.png',
        'target_url': CREDENTIAL_PROFILE_URL.format(slug=slug),
    }

    # Phase 4.1 — attach pending Scored Chatlogs entries indexed by
    # display_name. Empty list (vs missing) means "looked up, none
    # found" — page renderer can use that signal to suppress the
    # section entirely. Dedup is owned by the operator's promotion
    # GAS via Scored Chatlogs col L (already filtered in fetcher).
    if pending_by_name is not None:
        cv['pending_contributions'] = {
            'source_tab': 'Scored Chatlogs',
            'source_url': 'https://docs.google.com/spreadsheets/d/1Tbj7H5ur_egQLRugdXUaSIhEYIKp0vvVv2IZ7WTLCUo/edit',
            'floor_date': _PENDING_FLOOR_DATE,
            'entries': pending_by_name.get(display_name, []),
        }

    cv['_generator'] = 'build_cv_cache'
    return cv


def render_markdown(cv: dict[str, Any]) -> str:
    lines = []
    lines.append(f"# {cv['display_name']}")
    lines.append('')

    gov = cv.get('governance') or {}
    pills = []
    if gov.get('is_governor'):
        pills.append('Governor')
    if cv.get('has_dao_contributions'):
        pills.append('DAO Contributor')
    if cv.get('has_elective_records'):
        pills.append('Practitioner')
    if pills:
        lines.append(f"*{' · '.join(pills)} · TrueSight DAO Credential Profile · generated {cv['generated_at']}*")
    else:
        lines.append(f"*TrueSight DAO Credential Profile · generated {cv['generated_at']}*")
    lines.append('')

    narrative = (cv.get('narrative') or {}).get('text') if isinstance(cv.get('narrative'), dict) else ''
    if narrative:
        lines.append(narrative)
        lines.append('')
        model = (cv.get('narrative') or {}).get('model') or 'grok'
        lines.append(f"*AI-generated summary from ledger data and practice events. Model: {model}.*")
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


def render_html(cv: dict[str, Any], md_body: str, qr_path: Path | None = None) -> str:
    """Minimal HTML wrap of the Markdown body for WeasyPrint.

    Inline a small stylesheet that targets 'respectable job-application CV'
    typography per the CREDENTIALING_PLATFORM.md decision. When qr_path is
    provided and exists, the PDF gets a business-card-style QR in the top-right
    of page 1 that points back to the credential profile URL.
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
      .cv-qr { float: right; width: 28mm; margin: 0 0 4mm 6mm; }
      .cv-qr img { display: block; width: 28mm; height: 28mm; }
      .cv-qr-caption { font-family: 'Source Sans Pro', 'Helvetica Neue', Arial, sans-serif; font-size: 6.5pt; color: #888; text-align: center; margin-top: 1mm; letter-spacing: 0.04em; }
    """

    qr_block = ''
    if qr_path and Path(qr_path).is_file():
        qr_uri = Path(qr_path).resolve().as_uri()
        qr_block = (
            f"<div class='cv-qr'>"
            f"<img src='{qr_uri}' alt='Scan to view profile'/>"
            f"<div class='cv-qr-caption'>scan to view profile</div>"
            f"</div>"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><style>{css}</style></head>
<body>{qr_block}{''.join(html_body)}</body></html>"""


def render_qr(slug: str, target_url: str, out_path: Path, logo_path: Path | None = None) -> bool:
    """Write a QR PNG that resolves to ``target_url`` with a centred logo.

    When ``logo_path`` is omitted, the default TrueSight icon is used (canonical
    credentials surface). For per-program QRs, pass the vendored partner logo
    at ``program_assets/<program-slug>/logo.png``.
    """
    if not HAS_QR:
        print(f'  ⚠ skipping QR (qrcode/Pillow not installed): {out_path}', file=sys.stderr)
        return False
    try:
        if logo_path is not None:
            _generate_qr_with_logo(target_url, out_path, logo_path=logo_path)
        else:
            _generate_qr_with_logo(target_url, out_path)
        return True
    except Exception as e:
        print(f'  ⚠ QR render failed for {out_path}: {e}', file=sys.stderr)
        return False


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

def build(data_root: Path, write_pdfs: bool = True, write_narratives: bool = True) -> dict[str, Any]:
    practitioners = collect_practitioners(data_root)
    preserved = collect_preserved_cvs(data_root)
    aliases_path = data_root / '_cache' / 'aliases.json'
    aliases: dict[str, str] = read_json(aliases_path) or {} if aliases_path.is_file() else {}

    # Phase 4.1 — fetch pending (submitted-but-unreviewed) Scored Chatlogs
    # entries once for the whole build, indexed by Contributor Name. Each
    # build_unified_cv call attaches the slice belonging to that CV's
    # display_name. Quietly degrades to an empty map if sheets API isn't
    # reachable (no credentials in the CI environment, transient failure,
    # etc.) — pending list is best-effort; canonical Ledger history record
    # is unaffected.
    # Spec: agentic_ai_context/CREDENTIALING_PROGRAM_PAGES.md §18.
    pending_by_name: dict[str, list[dict[str, Any]]] = {}
    if HAS_PENDING_FETCHER:
        try:
            from fetch_contributions import setup_google_sheets as _setup_sheets  # type: ignore
            sheets_service = _setup_sheets()
            if sheets_service is not None:
                pending_by_name = _fetch_pending_chatlogs(sheets_service)
        except Exception as e:
            print(f'  ⚠ pending chatlogs fetch failed (continuing): {e}', file=sys.stderr)
    else:
        print('  ⚠ pending chatlogs fetcher not importable — pending sections will be empty', file=sys.stderr)

    # First pass — assign canonical slugs for each pk-hash record.
    for pk_hash, rec in practitioners.items():
        aliases[pk_hash] = derive_slug(pk_hash, rec.get('identity') or {}, aliases)

    # Second pass — build per-slug unified CVs. Indexed by canonical slug.
    cvs_by_slug: dict[str, dict[str, Any]] = {}
    # Practitioners
    for pk_hash, rec in practitioners.items():
        slug = aliases[pk_hash]
        cvs_by_slug[slug] = build_unified_cv(slug, rec, preserved.get(slug), pending_by_name=pending_by_name)
    # DAO-only preserved testimonials (no matching pk-hash)
    for slug, p in preserved.items():
        if slug in cvs_by_slug:
            continue
        cvs_by_slug[slug] = build_unified_cv(slug, None, p, pending_by_name=pending_by_name)

    # Write outputs
    cv_dir = data_root / '_cache' / 'cv'
    cv_dir.mkdir(parents=True, exist_ok=True)
    grok_cache_dir = data_root / '_cache' / 'grok'
    # Phase 3a — preload the data-slug → URL-slug map once per build.
    program_url_map = _load_program_url_map()
    members = []
    narrative_hits = 0
    narrative_calls = 0
    narrative_errors = 0
    for slug, cv in sorted(cvs_by_slug.items()):
        # Generate or recover cached narrative BEFORE writing the JSON so
        # the narrative is part of the unified CV.
        #
        # Idempotency rule: if the new generation FAILS (no API key,
        # transient network error), preserve whatever narrative the
        # previous build wrote. This is essential for CI runs that don't
        # have GROK_API_KEY — without this, every CI rebuild would blank
        # the narratives that a key-equipped local run had populated.
        existing = read_json(cv_dir / f'{slug}.json') or {}
        existing_narrative = existing.get('narrative') if isinstance(existing, dict) else None
        if HAS_GROK and write_narratives:
            n = _grok_generate_narrative(cv, grok_cache_dir)
            if n.get('error'):
                narrative_errors += 1
                if existing_narrative and existing_narrative.get('text'):
                    cv['narrative'] = existing_narrative
            elif n.get('cached'):
                narrative_hits += 1
                cv['narrative'] = {
                    'text': n.get('narrative') or '',
                    'model': n.get('model') or '',
                    'prompt_version': n.get('prompt_version') or '',
                    'source_hash': n.get('source_hash') or '',
                    'cached': True,
                }
            else:
                narrative_calls += 1
                cv['narrative'] = {
                    'text': n.get('narrative') or '',
                    'model': n.get('model') or '',
                    'prompt_version': n.get('prompt_version') or '',
                    'source_hash': n.get('source_hash') or '',
                    'cached': False,
                }
        elif existing_narrative and existing_narrative.get('text'):
            # Grok module not available at all (e.g. import failed). Still
            # carry forward whatever previous run produced.
            cv['narrative'] = existing_narrative
        qr_target = (cv.get('qr_code') or {}).get('target_url') or CREDENTIAL_PROFILE_URL.format(slug=slug)
        qr_path = cv_dir / f'{slug}.qr.png'
        render_qr(slug, qr_target, qr_path)
        write_json(cv_dir / f'{slug}.json', cv)
        md = render_markdown(cv)
        (cv_dir / f'{slug}.md').write_text(md, encoding='utf-8')
        if write_pdfs:
            render_pdf(render_html(cv, md, qr_path), cv_dir / f'{slug}.pdf')

        # Phase 3a — per-program QR + PDF artifacts. For each program this
        # CV participates in, resolve the URL-side slug via the registry
        # at `program_assets/registry.json`, then emit a sibling
        # `<slug>__<url-slug>.qr.png` whose centre carries the partner
        # logo (vendored at `program_assets/<url-slug>/logo.png`) and
        # whose payload is the production URL of the program-scoped
        # credential surface. The matching `<slug>__<url-slug>.pdf`
        # embeds that QR.
        #
        # A program without a vendored logo is silently skipped — partner
        # onboarding shouldn't block on the logo being committed first.
        # Spec: agentic_ai_context/CREDENTIALING_PROGRAM_PAGES.md §15.
        for data_program_slug in sorted((cv.get('programs') or {}).keys()):
            url_program_slug = program_url_map.get(data_program_slug, data_program_slug)
            logo_path = PROGRAM_ASSETS_DIR / url_program_slug / 'logo.png'
            if not logo_path.is_file():
                continue
            program_url = PROGRAM_CREDENTIAL_URL.format(program=url_program_slug, slug=slug)
            program_qr_path = cv_dir / f'{slug}__{url_program_slug}.qr.png'
            ok = render_qr(slug, program_url, program_qr_path, logo_path=logo_path)
            if ok and write_pdfs:
                render_pdf(render_html(cv, md, program_qr_path), cv_dir / f'{slug}__{url_program_slug}.pdf')
            # Phase 3b — partner-branded certificate PDF, dispatched on
            # cert_config.strategy. Emits `<slug>__<url-program-slug>__cert.pdf`
            # only when the partner has Phase 3b configured (cert_config.json
            # present in program_assets). Independent of write_pdfs since
            # the cert PDF doesn't depend on WeasyPrint — it merges a fresh
            # reportlab canvas atop the vendored template via pypdf.
            if ok:
                _render_program_certificate(
                    url_program_slug=url_program_slug,
                    data_program_slug=data_program_slug,
                    cv=cv,
                    slug=slug,
                    qr_path=program_qr_path,
                    out_path=cv_dir / f'{slug}__{url_program_slug}__cert.pdf',
                )
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
        # Multi-program members fix (2026-05-18): exposing the full list
        # of programs the member participates in, not just an arbitrary
        # "primary_program" alphabetical winner. The cohort filter on
        # truesight_me's program-shell.js now matches on `programs`
        # membership instead of `primary_program` equality, so a person
        # in N programs shows up in N cohort listings as expected. The
        # `primary_program` field is preserved for backwards-compat with
        # older readers, but now resolves to "the program with the most
        # logged practice activity" (instead of alphabetical-first) so
        # the historically meaningful primary doesn't flip every time a
        # member joins a new program with an alphabetically-earlier slug.
        programs_dict = cv.get('programs') or {}
        program_slugs = list(programs_dict.keys())
        def _program_activity_score(name: str) -> tuple[int, int]:
            rec = programs_dict.get(name) or {}
            return (
                int(rec.get('practice_count') or 0),
                int(rec.get('total_practice_minutes') or 0),
            )
        primary_program = None
        if program_slugs:
            primary_program = sorted(program_slugs, key=lambda n: _program_activity_score(n), reverse=True)[0]
        members.append({
            'slug': slug,
            'display_name': cv['display_name'],
            'pk_hash': cv.get('pk_hash'),
            'is_governor': bool(gov.get('is_governor')),
            'voting_rights': _coerce_voting_pct(gov.get('total_voting_power_pct') or gov.get('voting_power_pct')),
            'primary_program': primary_program,
            'programs': program_slugs,
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
