"""Numeric, candidate-bound release outcome measurements and GA target checks."""
import datetime as dt
import json
import math
from pathlib import Path

from execution_order import instant

CONTRACT = Path(__file__).resolve().parents[1] / 'certification/release-promise.v1.json'

WORKFLOWS = ('install', 'publish-layer', 'style', 'share', 'build-app',
             'sign-in-roles', 'recover-incident', 'migrate-service')
COUNT_UNITS = {f'{unit}-{workflow}': unit for unit in ('sentences', 'clicks')
               for workflow in WORKFLOWS}

RC_IDS = frozenset({
    'install-console', 'unattended-person', 'recovery-operates-restore',
    *(f'recovery-operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)),
    'recovery-operates-12-notify',
})
GA_ONLY_IDS = frozenset({
    *COUNT_UNITS, 'studio-publish-layer', 'studio-build-app',
    *(f'developer-{item}-{mode}-skills' for item in ('map', 'app')
      for mode in ('with', 'without')),
})
IDS = RC_IDS | GA_ONLY_IDS


def expected_profiles(row_id):
    return ['ga'] if row_id in GA_ONLY_IDS else ['rc', 'ga']


def load(path):
    rows = json.loads(path.read_text())['outcomes']
    if len(rows) != len(IDS) or {r['id'] for r in rows} != IDS:
        raise ValueError('release outcome coverage incomplete')
    for row in rows:
        if row['unit'] != unit_for(row['id']) or row['required'] != expected_profiles(row['id']):
            raise ValueError('outcomes must report their declared unit and profile requirements')
        if row['id'] == 'unattended-person' and row.get('gaMaximum') != 0:
            raise ValueError('unattended GA target must be zero person-minutes')
    return rows


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def unit_for(row_id):
    return COUNT_UNITS.get(row_id, 'minutes')


def notes_line(doc):
    return f"<!-- honua-outcome:{doc['row']} --> {doc['value']} {unit_for(doc['row'])}"


def valid(doc):
    return (number(doc.get('value')) and doc.get('row') in IDS
            and doc.get('unit') == unit_for(doc['row'])
            and isinstance(doc.get('releaseNotes'), str)
            and notes_line(doc) in doc['releaseNotes'].splitlines())


def summarize(row, docs):
    values = [doc['value'] for doc in docs]
    maximum = row.get('gaMaximum')
    return {'value': max(values), 'unit': row['unit'], 'releaseNotesReported': True,
            'targetMet': None if row['unit'] == 'clicks' else (
                number(maximum) and all(value <= maximum for value in values))}


def _row_valid(row, targets, context):
    profile, now = context
    observed = instant(row.get('observedAt'))
    if (row.get('status') != 'PASS' or not number(row.get('value'))
            or row.get('unit') != unit_for(row['id']) or row.get('releaseNotesReported') is not True
            or observed is None or not now - dt.timedelta(days=7) <= observed <= now):
        return False
    if profile == 'rc' or unit_for(row['id']) == 'clicks':
        return True
    target = targets[row['id']]
    return number(target) and row['value'] <= target


def report_valid(rows, profile, now):
    if (not isinstance(rows, list) or len(rows) != len(IDS)
            or any(not isinstance(r, dict) for r in rows)
            or {r.get('id') for r in rows} != IDS):
        return False
    targets = {r['id']: r.get('gaMaximum') for r in load(CONTRACT)}
    required = IDS if profile == 'ga' else RC_IDS
    return all(_row_valid(row, targets, (profile, now)) for row in rows if row['id'] in required)


def render_markdown(rows):
    lines = ["\n## Measured release outcomes\n"]
    lines.extend(
        f"- {row['id']}: " + notes_line({'row': row['id'], 'value': row['value']})
        for row in sorted(rows, key=lambda row: row['id'])
        if row.get('status') == 'PASS' and number(row.get('value'))
    )
    return "\n".join(lines) + "\n"
