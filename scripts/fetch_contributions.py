"""
Fetch DAO member contributions from TrueSight DAO Contribution Ledger.

Migrated from tokenomics/python_scripts/reference_and_testimonials/fetch_contributions.py
on 2026-05-14 as part of the lineage-credentials platform consolidation.
Credentials path is now configurable via the GOOGLE_APPLICATION_CREDENTIALS
env var (Google standard) so this script is portable across repos.

Repository: https://github.com/TrueSightDAO/lineage-engine
Companion data repo: https://github.com/TrueSightDAO/lineage-credentials
Design doc: https://github.com/TrueSightDAO/agentic_ai_context/blob/main/CREDENTIALING_PLATFORM.md
"""

import json
import os
import sys
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build

# Google Sheets API setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SPREADSHEET_ID = '1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU'
SHEET_NAME = 'Ledger history'
HEADER_ROW = 4

# Where to look for the service-account credentials JSON. Env var takes
# precedence so CI / scripts in other repos can point at their own copy.
DEFAULT_CREDS_HINTS = [
    os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', ''),
    # Sibling tokenomics checkout (handy during local dev / migration window)
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'tokenomics', 'python_scripts', 'schema_validation', 'gdrive_schema_credentials.json',
    ),
]


def setup_google_sheets():
    """Initialize Google Sheets API service.

    Honours GOOGLE_APPLICATION_CREDENTIALS first; falls back to a sibling
    tokenomics checkout (so the script keeps working during the migration
    period when both repos may live next to each other on a developer
    machine).
    """
    creds_path = next((p for p in DEFAULT_CREDS_HINTS if p and os.path.exists(p)), None)
    if not creds_path:
        print('❌ No credentials file found.', file=sys.stderr)
        print('   Set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON', file=sys.stderr)
        print('   with read access to the Contribution Ledger.', file=sys.stderr)
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        service = build('sheets', 'v4', credentials=creds)
        print(f'✅ Google Sheets API initialized (creds: {creds_path})')
        return service
    except Exception as e:  # noqa: BLE001
        print(f'❌ Failed to initialize Google Sheets API: {e}', file=sys.stderr)
        return None


def fetch_all_contributions(service):
    """Fetch all contributions from the Ledger history sheet."""
    try:
        range_name = f'{SHEET_NAME}!A{HEADER_ROW}:P'
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
        ).execute()
        values = result.get('values', [])

        if not values:
            print('❌ No data found in sheet')
            return None

        headers = values[0]
        data_rows = values[1:]
        print(f'\n📊 Found {len(data_rows)} contribution records')
        print(f'📋 Headers: {headers}\n')

        contributions = []
        for row in data_rows:
            while len(row) < len(headers):
                row.append('')
            contribution = {h: (row[i] if i < len(row) else '') for i, h in enumerate(headers)}
            contributions.append(contribution)

        return {
            'headers': headers,
            'contributions': contributions,
            'total_count': len(contributions),
        }
    except Exception as e:  # noqa: BLE001
        print(f'❌ Error fetching contributions: {e}', file=sys.stderr)
        return None


def get_contributor_contributions(all_data, contributor_name):
    """Filter contributions for a specific contributor (case-insensitive partial match)."""
    if not all_data:
        return None
    needle = contributor_name.lower()
    filtered = [c for c in all_data['contributions'] if needle in c.get('Contributor Name', '').lower()]
    print(f"\n🔍 Found {len(filtered)} contributions for '{contributor_name}'")
    return filtered


def _to_float(value):
    """Best-effort numeric coercion for ledger cells.

    Returns 0.0 for empty / unparseable / NaN values. Strips comma-thousand
    separators (operators sometimes enter "5,000.00" by hand). NaN is treated
    as 0 specifically because at least one ledger row (row 2582 as of
    2026-05-14) has the literal string "NaN" — a stale-data artifact that
    must not silently propagate into JSON via Python's allow_nan default
    (browsers reject NaN tokens as invalid JSON).
    """
    import math
    if value in (None, '', 0, '0', '0.0'):
        return 0.0
    if isinstance(value, str):
        value = value.strip().replace(',', '')
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def analyze_contributions(contributions):
    """Analyze and categorize contributions."""
    if not contributions:
        return None

    analysis = {
        'total_contributions': len(contributions),
        'total_tdg_provisioned': 0,
        'total_tdg_issued': 0,
        'projects': set(),
        'rubric_categories': {},
        'date_range': {'earliest': None, 'latest': None},
        'contribution_types': [],
        'status_breakdown': {},
    }

    for c in contributions:
        tdg_prov = _to_float(c.get('TDGs Provisioned'))
        analysis['total_tdg_provisioned'] += tdg_prov
        analysis['total_tdg_issued'] += _to_float(c.get('TDGs Issued'))

        project = (c.get('Project Name') or '').strip()
        if project:
            analysis['projects'].add(project)
        rubric = (c.get('Rubric classification') or '').strip()
        if rubric:
            analysis['rubric_categories'][rubric] = analysis['rubric_categories'].get(rubric, 0) + 1
        status = (c.get('Status') or '').strip()
        if status:
            analysis['status_breakdown'][status] = analysis['status_breakdown'].get(status, 0) + 1
        status_date = (c.get('Status date') or '').strip()
        if status_date and len(status_date) == 8:
            r = analysis['date_range']
            if not r['earliest'] or status_date < r['earliest']:
                r['earliest'] = status_date
            if not r['latest'] or status_date > r['latest']:
                r['latest'] = status_date
        contribution_made = (c.get('Contribution Made') or '').strip()
        if contribution_made:
            analysis['contribution_types'].append({
                'description': contribution_made,
                'project': project,
                'tdg': tdg_prov,
                'date': status_date,
                'status': status,
            })

    analysis['projects'] = sorted(analysis['projects'])
    return analysis


def format_date(date_str):
    """Convert YYYYMMDD to readable format."""
    if not date_str or len(date_str) != 8:
        return date_str
    try:
        return datetime.strptime(date_str, '%Y%m%d').strftime('%B %Y')
    except ValueError:
        return date_str


def print_contribution_summary(contributor_name, contributions, analysis):
    """Print a formatted summary of contributions."""
    print('\n' + '=' * 80)
    print(f'📝 CONTRIBUTION SUMMARY FOR: {contributor_name.upper()}')
    print('=' * 80)

    if not contributions or not analysis:
        print('❌ No contributions found')
        return

    print('\n📊 OVERVIEW:')
    print(f"   • Total Contributions: {analysis['total_contributions']}")
    print(f"   • Total TDG Provisioned: {analysis['total_tdg_provisioned']:,.2f}")
    print(f"   • Total TDG Issued: {analysis['total_tdg_issued']:,.2f}")
    r = analysis['date_range']
    if r['earliest'] and r['latest']:
        print(f"   • Active Period: {format_date(r['earliest'])} - {format_date(r['latest'])}")

    print(f"\n🎯 PROJECTS INVOLVED ({len(analysis['projects'])}):")
    for p in analysis['projects']:
        print(f'   • {p}')

    print('\n📋 CONTRIBUTION CATEGORIES:')
    for rubric, count in sorted(analysis['rubric_categories'].items(), key=lambda x: x[1], reverse=True):
        print(f'   • {rubric}: {count} contribution(s)')

    print('\n✅ STATUS BREAKDOWN:')
    for status, count in sorted(analysis['status_breakdown'].items(), key=lambda x: x[1], reverse=True):
        print(f'   • {status}: {count}')

    print('\n📝 DETAILED CONTRIBUTIONS:')
    for i, c in enumerate(analysis['contribution_types'], 1):
        print(f"\n   {i}. {c['description']}")
        print(f"      Project: {c['project']}")
        print(f"      TDG Awarded: {c['tdg']:,.2f}")
        print(f"      Date: {format_date(c['date'])}")
        print(f"      Status: {c['status']}")

    print('\n' + '=' * 80 + '\n')


def save_contribution_data(contributor_name, contributions, analysis, output_dir='testimonials'):
    """Save contribution data to JSON file."""
    if not contributions:
        return None

    os.makedirs(output_dir, exist_ok=True)
    safe_name = contributor_name.lower().replace(' ', '_').replace('.', '')
    filepath = os.path.join(output_dir, f'{safe_name}_contributions.json')

    data = {
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

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f'💾 Saved contribution data to: {filepath}')
    return filepath


def main():
    if len(sys.argv) < 2:
        print('Usage: python fetch_contributions.py <contributor_name> [output_dir]')
        print("Example: python fetch_contributions.py 'Fatima Toledo' ./testimonials")
        sys.exit(1)

    contributor_name = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'testimonials'

    print(f'\n🚀 Fetching contributions for: {contributor_name}')
    print('📊 Source: TrueSight DAO Contribution Ledger')
    print(f'🔗 https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=0\n')

    service = setup_google_sheets()
    if not service:
        sys.exit(1)

    all_data = fetch_all_contributions(service)
    if not all_data:
        sys.exit(1)

    contributions = get_contributor_contributions(all_data, contributor_name)
    if not contributions:
        print(f"\n❌ No contributions found for '{contributor_name}'")
        print('\n💡 Available contributors (sample):')
        unique_names = set(c.get('Contributor Name', '') for c in all_data['contributions'][:50])
        for name in sorted(unique_names)[:20]:
            if name:
                print(f'   • {name}')
        sys.exit(1)

    analysis = analyze_contributions(contributions)
    print_contribution_summary(contributor_name, contributions, analysis)
    save_contribution_data(contributor_name, contributions, analysis, output_dir=output_dir)
    print('✅ Done. Use this data to generate testimonials.')


if __name__ == '__main__':
    main()
