"""Grok-generated per-profile narrative for the credentialing platform.

Generates a short (~120 word) third-person narrative summary of a
contributor's CV for the truesight.me/credentials/#<slug> page. Per
CREDENTIALING_PLATFORM.md §9a item 3, the result is keyed on a SHA of
the input payload + prompt version, so re-running the builder when the
underlying data has not changed costs zero Grok calls.

API: xAI Grok (chat completions). Reuses the GROK_API_KEY resolution
pattern from truesight_autopilot/app/grok_client.py — env var first,
then ~/Applications/market_research/.env.

Cache location: <data_root>/_cache/grok/<sha256-of-input>.json. Stored
under the data repo so the narrative survives builder reruns without
re-prompting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger('lineage_engine.grok_narrative')

GROK_ENDPOINT = 'https://api.x.ai/v1/chat/completions'
GROK_MODEL = 'grok-4-1-fast-non-reasoning'
PROMPT_VERSION = '2026-05-14-v1'

NARRATIVE_SYSTEM_PROMPT = """\
You are writing a short third-person summary for a public CV on the
TrueSight DAO credentialing platform (truesight.me/credentials).
The DAO is a contribution-based community building Agroverse (cacao
supply chain), Sun Mint (regenerative reforestation), and related
social ventures.

Constraints:
- 100-150 words, single paragraph.
- Third person. Refer to the person by their display name (first use
  full, subsequent uses first name or pronoun).
- Concrete and grounded — name specific projects they worked on, what
  category of contribution dominates (e.g. "primarily community ops
  and partner outreach" / "regular Telegram engagement and content
  curation"), and one notable accomplishment if visible in the data.
- NO marketing language. Avoid "passionate", "dedicated", "visionary",
  "innovative", "thought leader" etc.
- NO "earned X TDG" or numeric flexes — those numbers are displayed
  separately on the page.
- If the person has fewer than 5 contributions OR all in one rubric,
  keep the summary very brief (1-2 sentences). Don't invent depth.
- If the person is a current Governor, mention it once factually.
- If practice events are present (e.g. capoeira sessions), mention
  that they are practicing capoeira via the TrueSight Capoeira
  program. Don't extrapolate skill level.

Return EXACTLY one JSON object with one key:
- "narrative": string — the paragraph.

Do not include any other text or markdown fences.
"""


def load_grok_key() -> str | None:
    k = (os.environ.get('GROK_API_KEY') or '').strip()
    if k:
        return k
    for env_path in [
        Path.home() / 'Applications' / 'market_research' / '.env',
    ]:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('GROK_API_KEY='):
                val = line.split('=', 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _input_signature(cv: dict[str, Any]) -> str:
    """Deterministic SHA-256 of the parts of the CV that should drive
    narrative regeneration. Excludes timestamps and IDs that change every
    rebuild without changing the substance."""
    src = (cv.get('dao_contributions') or {}).get('source') or {}
    summary = (src.get('summary') or {}) if isinstance(src, dict) else {}
    analysis = (src.get('analysis') or {}) if isinstance(src, dict) else {}
    governance = cv.get('governance') or {}
    programs = {
        name: {
            'practice_count': p.get('practice_count'),
            'total_practice_minutes': p.get('total_practice_minutes'),
            'lineage_root': p.get('lineage_root'),
        }
        for name, p in (cv.get('programs') or {}).items()
    }
    payload = {
        'prompt_version': PROMPT_VERSION,
        'model': GROK_MODEL,
        'display_name': cv.get('display_name'),
        'is_governor': bool(governance.get('is_governor')),
        'summary': summary,
        'rubric_categories': analysis.get('rubric_categories'),
        'projects': summary.get('projects'),
        'date_range': summary.get('date_range'),
        'programs': programs,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _build_user_message(cv: dict[str, Any]) -> str:
    src = (cv.get('dao_contributions') or {}).get('source') or {}
    summary = (src.get('summary') or {}) if isinstance(src, dict) else {}
    analysis = (src.get('analysis') or {}) if isinstance(src, dict) else {}
    governance = cv.get('governance') or {}
    programs = cv.get('programs') or {}

    lines = []
    lines.append(f"Display name: {cv.get('display_name') or cv.get('slug')}")
    if governance.get('is_governor'):
        lines.append("Currently a member of the Board of Governors.")
    if summary.get('total_contributions'):
        lines.append(f"Total DAO contributions: {summary['total_contributions']}")
    if summary.get('total_tdg_issued'):
        lines.append(f"Total TDG controlled: {summary['total_tdg_issued']:,.0f}")
    if summary.get('projects'):
        lines.append('Projects: ' + ', '.join(summary['projects']))
    rubric = analysis.get('rubric_categories') or {}
    if rubric:
        top = sorted(rubric.items(), key=lambda x: -x[1])[:8]
        lines.append('Top contribution categories (count): ' + '; '.join(f"{k} ({v})" for k, v in top))
    dr = summary.get('date_range') or {}
    if dr.get('earliest') or dr.get('latest'):
        lines.append(f"Active period: {dr.get('earliest', '?')} - {dr.get('latest', '?')}")
    if programs:
        for pname, p in programs.items():
            lines.append(
                f"Elective program '{p.get('display_name') or pname}': "
                f"{p.get('practice_count', 0)} sessions, "
                f"{p.get('total_practice_minutes', 0)} minutes; "
                f"lineage root '{p.get('lineage_root')}'."
            )
    return '\n'.join(lines)


def generate_narrative(cv: dict[str, Any], cache_dir: Path, timeout: float = 60.0) -> dict[str, Any]:
    """Return {'narrative': str, 'cached': bool, 'source_hash': str}.

    Uses cache_dir/<sha>.json if present; otherwise calls Grok and writes
    the result. Failures return {'narrative': '', 'error': '...'} without
    raising, so the builder degrades gracefully.
    """
    sig = _input_signature(cv)
    cache_path = cache_dir / f'{sig}.json'
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding='utf-8'))
            data['cached'] = True
            data['source_hash'] = sig
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning('Stale cache at %s: %s', cache_path, e)

    api_key = load_grok_key()
    if not api_key:
        return {'narrative': '', 'error': 'GROK_API_KEY not found', 'cached': False, 'source_hash': sig}

    user_msg = _build_user_message(cv)
    payload = {
        'model': GROK_MODEL,
        'temperature': 0.3,
        'messages': [
            {'role': 'system', 'content': NARRATIVE_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_msg},
        ],
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(GROK_ENDPOINT, headers=headers, json=payload)
            resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content'].strip()
        narrative = _extract_narrative(content)
        out = {
            'narrative': narrative,
            'model': GROK_MODEL,
            'prompt_version': PROMPT_VERSION,
            'cached': False,
            'source_hash': sig,
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return out
    except Exception as e:  # noqa: BLE001
        logger.error('Grok narrative failed for %s: %s', cv.get('slug'), e)
        return {'narrative': '', 'error': str(e), 'cached': False, 'source_hash': sig}


def _extract_narrative(content: str) -> str:
    """Pull the 'narrative' string out of the Grok response. Tolerant of
    markdown code fences and leading prose."""
    import re
    text = content.strip()
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if m:
        text = m.group(1)
    else:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            text = m.group(0)
    try:
        return json.loads(text).get('narrative', '').strip()
    except Exception:
        # Fall back to raw content if we can't parse JSON — at least we
        # show something rather than nothing.
        return content.strip()
