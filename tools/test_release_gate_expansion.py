"""Promise additions and measurement admission boundaries."""
import datetime as dt

import pytest
import release_promise as p

WORKFLOWS = ('install', 'publish-layer', 'style', 'share', 'build-app',
             'sign-in-roles', 'recover-incident', 'migrate-service')
NEW_ROWS = {'cloud-image-aot', 'cloud-no-redis-source',
            'security-agent-default-deny', *p.rc2_rigor.IDS}
KEPT_ROWS = {'supply-sbom', 'supply-scan', 'contract-compat', 'docs-executable',
             'clients-installed', 'operates-telemetry', 'operates-restore'}


def test_existing_gates_are_required_with_both_companion_receipts():
    rows = {row['id']: row for row in p.load_contract()}
    assert KEPT_ROWS <= rows.keys()
    assert all(rows[key]['required'] == ['rc', 'ga'] for key in KEPT_ROWS)
    assert p.producer_workflows(rows['contract-compat']) == ['gate-contract.yml', 'gate-contract-live.yml']
    assert p.producer_workflows(rows['docs-executable']) == ['gate-executable-docs.yml', 'gate-docs.yml']
    assert rows['operates-8-upgrade']['workflow'] == 'gate-upgrade.yml'
    assert rows['operates-8-upgrade']['required'] == ['ga']
    assert rows['operates-8-upgrade']['assertions']['launchStack'] is True
    assert rows['protocol-compat']['required'] == rows['operates-capacity']['required'] == ['ga']


@pytest.mark.parametrize('row_id', ['contract-compat', 'docs-executable'])
def test_companion_producers_cannot_hide_each_others_failure(row_id):
    from test_release_promise import run, CANDIDATE, NOW
    row = next(row for row in p.load_contract() if row['id'] == row_id)
    records = [{**run(row), 'id': i + 1, 'path': '.github/workflows/' + workflow}
               for i, workflow in enumerate(p.producer_workflows(row))]
    assert p.evaluate_row(row, records, (CANDIDATE, NOW))['status'] == 'PASS'
    assert p.evaluate_row(row, records[:1], (CANDIDATE, NOW))['status'] == 'missing'
    records[0]['conclusion'] = 'failure'
    assert p.evaluate_row(row, records, (CANDIDATE, NOW))['status'] == 'invalid'


def test_restore_recovery_is_measured_in_minutes():
    row = next(row for row in p.load_outcomes() if row['id'] == 'recovery-operates-restore')
    assert row['workflow'] == 'gate-dr.yml'
    assert row['unit'] == 'minutes'
    assert row['required'] == ['rc', 'ga']


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
            assert row['required'] == ['ga']
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


@pytest.mark.parametrize('row_id', ['cloud-image-aot', 'cloud-no-redis-source',
                                    'security-agent-default-deny', 'operates-8-upgrade'])
def test_new_promise_requires_every_assertion(row_id):
    from test_release_promise import run, CANDIDATE, NOW
    row = next(row for row in p.load_contract() if row['id'] == row_id)
    assert p.evaluate_row(row, [run(row)], (CANDIDATE, NOW))['status'] == 'PASS'
    for key in row['assertions']:
        record = run(row)
        record['reports'][0]['assertions'] = {k: v for k, v in row['assertions'].items() if k != key}
        assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'
