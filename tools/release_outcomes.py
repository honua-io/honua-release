"""Numeric, candidate-bound release outcome measurements and GA target checks."""
import datetime as dt
import json
import math
from pathlib import Path

from execution_order import instant

CONTRACT = Path(__file__).resolve().parents[1] / 'certification/release-promise.v1.json'

IDS = frozenset({'install-console', 'unattended-person', 'studio-publish-layer', 'studio-build-app',
                 *(f'recovery-operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)),
                 'recovery-operates-12-notify',
                 *(f'developer-{item}-{mode}-skills' for item in ('map', 'app')
                   for mode in ('with', 'without'))})


def load(path):
    rows = json.loads(path.read_text())['outcomes']
    if len(rows) != len(IDS) or {r['id'] for r in rows} != IDS:
        raise ValueError('release outcome coverage incomplete')
    for row in rows:
        if row['unit'] != 'minutes' or row['required'] != ['rc', 'ga']:
            raise ValueError('outcomes must report minutes for RC and GA')
        if row['id'] == 'unattended-person' and row.get('gaMaximum') != 0:
            raise ValueError('unattended GA target must be zero person-minutes')
    return rows


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def notes_line(doc):
    return f"<!-- honua-outcome:{doc['row']} --> {doc['value']} minutes"


def valid(doc):
    return (number(doc.get('value')) and doc.get('unit') == 'minutes'
            and isinstance(doc.get('releaseNotes'), str)
            and notes_line(doc) in doc['releaseNotes'].splitlines())


def summarize(row, docs):
    values = [doc['value'] for doc in docs]
    maximum = row.get('gaMaximum')
    return {'value': max(values), 'unit': 'minutes', 'releaseNotesReported': True,
            'targetMet': number(maximum) and all(value <= maximum for value in values)}


def report_valid(rows, profile, now):
    if (not isinstance(rows, list) or len(rows) != len(IDS)
            or any(not isinstance(r, dict) for r in rows)
            or {r.get('id') for r in rows} != IDS):
        return False
    targets = {r['id']: r.get('gaMaximum') for r in load(CONTRACT)}
    return all(row.get('status') == 'PASS' and number(row.get('value'))
               and row.get('unit') == 'minutes' and row.get('releaseNotesReported') is True
               and instant(row.get('observedAt')) is not None
               and now - dt.timedelta(days=7) <= instant(row['observedAt']) <= now
               and (profile == 'rc' or (number(targets[row['id']])
                    and row['value'] <= targets[row['id']])) for row in rows)
