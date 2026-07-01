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
import urllib.request
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


# Credential profile URL — what the per-slug QR code resolves to.
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

# URL for the DAO members ledger — used to identify sentinels and merge
# them into the index as synthetic entries (no CV, no practice events).
DAO_MEMBERS_URL = (
    'https://raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/dao_members.json'
)


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
    `tribomirim-bahia`) so the registry holds an explicit mapping rather than
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
    the directory page shows them alongside practitioner CVs.
    """
    cv_dir = data_root / '_cache' / 'cv'
    if not cv_dir.is_dir():
        return {}
    preserved: dict[str, dict[str, Any]] = {}
    for f in sorted(cv_dir.glob('*.json')):
        slug = f.stem
        if '__' in slug:
            continue  # skip program-scoped QR / cert artifacts
        data = read_json(f)
        if data is not None and isinstance(data, dict):
            preserved[slug] = data
    return preserved


# ---------------------------------------------------------------------------
# unified CV builder
# ---------------------------------------------------------------------------


def build_unified_cv(
    slug: str,
    practitioner: dict[str, Any] | None,
    preserved: dict[str, Any] | None,
    *,
    pending_by_name: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Merge practitioner data + preserved testimonial into one CV dict.

    The unified CV is the canonical per-person artifact. It contains:
      - display_name, pk_hash, generated_at
      - programs (per-program breakdown with practice_events)
      - dao_contributions (from preserved testimonial, if any)
      - governance (is_governor, voting_rights)
      - narrative (Grok-generated, cached)
      - has_dao_contributions, has_elective_records (boolean flags)
    """
    now = now_utc_iso()
    cv: dict[str, Any] = {
        'slug': slug,
        'display_name': slug,
        'pk_hash': None,
        'generated_at': now,
        'programs': {},
        'dao_contributions': None,
        'governance': {},
        'narrative': None,
        'has_dao_contributions': False,
        'has_elective_records': False,
    }

    if practitioner:
        identity = practitioner.get('identity') or {}
        names = identity.get('names') or []
        cv['display_name'] = names[0] if names else slug
        cv['pk_hash'] = practitioner['pk_hash']
        cv['programs'] = practitioner.get('programs') or {}
        # Elective records: any program with practice events counts
        cv['has_elective_records'] = any(
            p.get('practice_count', 0) > 0 for p in cv['programs'].values()
        )

    if preserved:
        # Merge preserved fields into the CV, preserving practitioner
        # fields where they exist (practitioner data takes precedence).
        for key in ('display_name', 'pk_hash', 'generated_at', 'programs'):
            if key in preserved and not (key == 'programs' and practitioner):
                cv[key] = preserved[key]
        dc = preserved.get('dao_contributions')
        if dc:
            cv['dao_contributions'] = dc
            cv['has_dao_contributions'] = True
        gov = preserved.get('governance')
        if gov:
            cv['governance'] = gov

    return cv


# ---------------------------------------------------------------------------
# Markdown + PDF rendering
# ---------------------------------------------------------------------------


def render_html(cv: dict[str, Any], md: str, qr_path: Path | None = None) -> str:
    """Wrap the Markdown body in a minimal HTML document for WeasyPrint.

    The QR code image (if present) is embedded as a data URI so the PDF
    is self-contained — no external file references.
    """
    qr_data_uri = ''
    if qr_path and qr_path.is_file():
        import base64
        b64 = base64.b64encode(qr_path.read_bytes()).decode('ascii')
        qr_data_uri = f'data:image/png;base64,{b64}'

    # Inline CSS for a clean, job-application-grade PDF.
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: A4;
    margin: 2cm 2.5cm;
  }}
  body {{
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #222;
  }}
  h1 {{ font-size: 20pt; margin-bottom: 0.2em; }}
  h2 {{ font-size: 14pt; margin-top: 1.5em; margin-bottom: 0.5em;
        border-bottom: 1px solid #ccc; padding-bottom: 0.2em; }}
  h3 {{ font-size: 12pt; margin-top: 1em; margin-bottom: 0.3em; }}
  .meta {{ color: #666; font-size: 10pt; margin-bottom: 1.5em; }}
  .qr {{ text-align: center; margin: 2em 0; }}
  .qr img {{ width: 120px; height: 120px; }}
  ul {{ padding-left: 1.5em; }}
  li {{ margin-bottom: 0.3em; }}
  .program-section {{ page-break-inside: avoid; }}
</style>
</head>
<body>
<div class="qr">{"<img src='" + qr_data_uri + "'>" if qr_data_uri else ''}</div>
{md}
</body>
</html>'''


def render_md(cv: dict[str, Any]) -> str:
    """Render a human-readable Markdown CV from the unified CV dict."""
    lines: list[str] = []
    name = cv.get('display_name') or cv['slug']
    lines.append(f'# {name}')
    lines.append('')

    programs = cv.get('programs') or {}
    if programs:
        lines.append('## Practice Activity')
        lines.append('')
        for prog_slug, prog in sorted(programs.items()):
            pname = prog.get('display_name', prog_slug)
            count = prog.get('practice_count', 0)
            minutes = prog.get('total_practice_minutes', 0)
            if count > 0:
                lines.append(f'### {pname}')
                lines.append(f'- **{count}** practice sessions')
                lines.append(f'- **{minutes}** total minutes')
                lines.append('')
                for ev in (prog.get('practice_events') or []):
                    payload = ev.get('payload') or {}
                    ts = ev.get('captured_at', '')
                    desc = payload.get('description', '')
                    dur = payload.get('total_practice_minutes', 0)
                    lines.append(f'- {ts} — {desc or "Practice session"} ({dur} min)')
                lines.append('')

    dc = cv.get('dao_contributions')
    if dc:
        lines.append('## DAO Contributions')
        lines.append('')
        src = (dc.get('source') or {}) if isinstance(dc, dict) else {}
        summary = (src.get('summary') or {}) if isinstance(src, dict) else {}
        tdg = summary.get('total_tdg_issued', 0)
        contribs = summary.get('total_contributions', 0)
        lines.append(f'- **{tdg}** TDG issued')
        lines.append(f'- **{contribs}** contributions')
        lines.append('')

    narrative = cv.get('narrative')
    if narrative and isinstance(narrative, dict):
        text = narrative.get('text', '')
        if text:
            lines.append('## Narrative')
            lines.append('')
            lines.append(text)
            lines.append('')

    lines.append('---')
    lines.append(f'*Generated at {cv.get("generated_at", "")}*')
    return '\n'.join(lines)


def render_pdf(html: str, out_path: Path) -> None:
    """Render HTML to PDF via WeasyPrint. No-op if WeasyPrint unavailable."""
    if not HAS_WEASYPRINT:
        return
    try:
        HTML(string=html).write_pdf(str(out_path))
    except Exception as e:
        print(f'  ⚠ PDF render failed for {out_path.name}: {e}', file=sys.stderr)


def render_qr(slug: str, url: str, out_path: Path, *, logo_path: Path | None = None) -> bool:
    """Generate a QR code PNG for the given URL. Returns True on success."""
    if not HAS_QR:
        return False
    try:
        _generate_qr_with_logo(url, str(out_path), logo_path=str(logo_path) if logo_path else None)
        return True
    except Exception as e:
        print(f'  ⚠ QR render failed for {slug}: {e}', file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------


def build(data_root: Path, *, write_pdfs: bool = True, write_narratives: bool = True) -> dict[str, Any]:
    """Build the full CV cache. Returns a stats dict."""
    practitioners = collect_practitioners(data_root)
    preserved = collect_preserved_cvs(data_root)
    aliases_path = data_root / '_cache' / 'aliases.json'

    # Load existing aliases so we don't re-derive slugs for every pk-hash
    aliases: dict[str, str] = read_json(aliases_path) or {}

    # First pass — ensure every pk-hash has a slug
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
            else:
                cv['narrative'] = n
                narrative_calls += 1
                if n.get('cache_hit'):
                    narrative_hits += 1
        elif existing_narrative and existing_narrative.get('text'):
            # No Grok available this run — preserve the last narrative
            cv['narrative'] = existing_narrative

        # Write per-slug JSON
        write_json(cv_dir / f'{slug}.json', cv)

        # Render Markdown
        md = render_md(cv)
        write_json(cv_dir / f'{slug}.md', md)  # stored as .md but written via write_json for consistency

        # Render QR + PDF for the main credential profile
        profile_url = CREDENTIAL_PROFILE_URL.format(slug=slug)
        qr_path = cv_dir / f'{slug}.qr.png'
        ok = render_qr(slug, profile_url, qr_path)
        if ok and write_pdfs:
            render_pdf(render_html(cv, md, qr_path), cv_dir / f'{slug}.pdf')

        # Per-program credential QR codes + PDFs + certificates
        #
        # Each program the member participates in gets its own QR code
        # that resolves to the program-scoped credential page and a PDF
        # that embeds that QR.
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
            'is_sentinel': False,
            'voting_rights': _coerce_voting_pct(gov.get('total_voting_power_pct')
                                                or gov.get('voting_power_pct')),
            'primary_program': primary_program,
            'programs': program_slugs,
            'has_dao_contributions': cv.get('has_dao_contributions', False),
            'has_elective_records': cv.get('has_elective_records', False),
            'total_tdg_controlled': dc_summary.get('total_tdg_issued') or 0,
            'total_contributions': dc_summary.get('total_contributions') or 0,
            'last_updated': cv['generated_at'],
        })

    # ── Sentinel merge ──────────────────────────────────────────────
    # Fetch dao_members.json to identify sentinels (roles includes 'sentinel')
    # and merge them into the index as synthetic entries with is_sentinel=True.
    try:
        req = urllib.request.Request(DAO_MEMBERS_URL, headers={'User-Agent': 'lineage-engine/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dao_data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'  ⚠ could not fetch dao_members.json for sentinel merge: {e}', file=sys.stderr)
        dao_data = None

    if dao_data:
        # Build sentinel name set from dao_members.json
        sentinel_names: set[str] = set()
        sentinel_raw: dict[str, dict] = {}
        for c in (dao_data.get('contributors') or []):
            roles = c.get('roles') or []
            if 'sentinel' in roles:
                name = c.get('name', '')
                sentinel_names.add(name)
                sentinel_raw[name] = c

        # Mark existing members that are sentinels
        existing_names = {m['display_name'] for m in members}
        for m in members:
            if m['display_name'] in sentinel_names:
                m['is_sentinel'] = True

        # Append synthetic entries for sentinels without a CV
        for name in sorted(sentinel_names):
            if name in existing_names:
                continue
            raw = sentinel_raw.get(name, {})
            members.append({
                'slug': None,  # non-clickable
                'display_name': name,
                'pk_hash': None,
                'is_governor': False,
                'is_sentinel': True,
                'voting_rights': _coerce_voting_pct(raw.get('total_voting_power_pct')),
                'primary_program': None,
                'programs': [],
                'has_dao_contributions': False,
                'has_elective_records': False,
                'total_tdg_controlled': float(raw.get('voting_rights', 0) or 0),
                'total_contributions': 0,
                'last_updated': dao_data.get('generated_at', ''),
            })
    else:
        # Fetch failed — ensure no stale is_sentinel flags linger
        for m in members:
            m.pop('is_sentinel', None)

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
