"""Founder-approved promise additions and measurement admission boundaries."""
import datetime as dt

import pytest
import release_promise as p

WORKFLOWS = ('install', 'publish-layer', 'style', 'share', 'build-app',
             'sign-in-roles', 'recover-incident', 'migrate-service')
NEW_ROWS = {'cloud-image-aot', 'cloud-no-redis-source',
            'security-agent-default-deny', 'operates-8-upgrade'}


def test_new_promises_block_both_profiles():
    rows = {row['id']: row for row in p.load_contract()}
    assert NEW_ROWS <= rows.keys()
    assert all(rows[key]['required'] == ['rc', 'ga'] for key in NEW_ROWS)


def test_sentence_and_click_contract():
    rows = {row['id']: row for row in p.load_outcomes()}
    for workflow in WORKFLOWS:
        for unit in ('sentences', 'clicks'):
            row = rows[f'{unit}-{workflow}']
            assert row['unit'] == unit
            assert row['required'] == ['rc', 'ga']
            assert row['gaMaximum'] is None


@pytest.mark.parametrize('unit', ['sentences', 'clicks'])
def test_count_receipt_requires_correct_archived_unit(unit):
    doc = {'row': f'{unit}-install', 'value': 3, 'unit': unit,
           'releaseNotes': f'<!-- honua-outcome:{unit}-install --> 3 {unit}'}
    assert p.outcomes.valid(doc)
    doc['unit'] = 'minutes'
    assert not p.outcomes.valid(doc)


def test_ga_clicks_need_measurements_but_no_target(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    contract = p.load_outcomes()
    targets = [{**row, 'gaMaximum': None if row['unit'] == 'clicks' else 10}
               for row in contract]
    monkeypatch.setattr(p.outcomes, 'load', lambda path: targets)
    results = [{'id': row['id'], 'status': 'PASS', 'value': 3, 'unit': row['unit'],
                'releaseNotesReported': True, 'observedAt': now.strftime('%Y-%m-%dT%H:%M:%SZ')}
               for row in contract]
    assert p.outcomes.report_valid(results, 'ga', now)
    next(row for row in targets if row['id'] == 'sentences-install')['gaMaximum'] = None
    assert p.outcomes.report_valid(results, 'rc', now)
    assert not p.outcomes.report_valid(results, 'ga', now)
    results = [row for row in results if row['id'] != 'clicks-install']
    assert not p.outcomes.report_valid(results, 'rc', now)


@pytest.mark.parametrize('row_id', sorted(NEW_ROWS))
def test_new_promise_missing_evidence_blocks_admission(row_id):
    from test_release_promise import aggregate, NOW
    report = aggregate()
    next(row for row in report['rows'] if row['id'] == row_id)['status'] = 'missing'
    assert not p.report_valid(report, 'rc', NOW)
    assert not p.report_valid(report, 'ga', NOW)


@pytest.mark.parametrize('row_id', sorted(NEW_ROWS))
def test_new_promise_requires_every_assertion(row_id):
    from test_release_promise import run, CANDIDATE, NOW
    row = next(row for row in p.load_contract() if row['id'] == row_id)
    assert p.evaluate_row(row, [run(row)], (CANDIDATE, NOW))['status'] == 'PASS'
    for key in row['assertions']:
        record = run(row)
        record['reports'][0]['assertions'] = {k: v for k, v in row['assertions'].items() if k != key}
        assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'
