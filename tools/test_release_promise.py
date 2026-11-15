"""Release promises require authenticated, fresh, candidate-bound producer evidence."""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest
import release_promise as p
import rc_gate_window as w

NOW = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
CANDIDATE = w.Candidate('a' * 40, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64)
CONTRACT = Path(__file__).resolve().parents[1] / 'certification/release-promise.v1.json'


def evidence(row):
    return {'schema': 'honua.release-promise-receipt/v1', 'row': row['id'], 'status': 'PASS',
            'serverSha': CANDIDATE.server_sha, 'imageDigest': CANDIDATE.server_digest,
            'platformLockDigest': CANDIDATE.platform_lock,
            'observedAt': '2026-11-14T00:00:00Z', 'openHighFindings': 0,
            'assertions': row['assertions'], 'stackName': 'honua-gate-test'}


def run(row):
    return {'id': 1, 'status': 'completed', 'conclusion': 'success',
            'head_branch': 'trunk', 'event': 'workflow_dispatch',
            'path': '.github/workflows/' + row['workflow'],
            'run_started_at': '2026-11-14T00:00:00Z', 'updated_at': '2026-11-14T00:01:00Z',
            'reports': [evidence(row)]}


def test_contract_covers_every_required_promise():
    rows = p.load_contract(CONTRACT)
    assert {r['id'] for r in rows} == {
        *(f'operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)), 'operates-12-notify',
        *(f'ai-{s}' for s in ('operate', 'publish', 'build', 'migrate', 'injection')),
        'security-hardened', 'security-open-high', 'cloud-launch-stack'}
    assert len(rows) == 18


def test_missing_rows_fail_closed():
    results = p.evaluate(p.load_contract(CONTRACT), {}, (CANDIDATE, NOW))
    assert len(results) == 18
    assert all(r['status'] == 'missing' for r in results)


def test_valid_receipt_passes():
    row = p.load_contract(CONTRACT)[0]
    assert p.evaluate_row(row, [run(row)], (CANDIDATE, NOW))['status'] == 'PASS'


@pytest.mark.parametrize('change', [
    {'status': 'FAIL'}, {'serverSha': 'd' * 40}, {'imageDigest': 'sha256:' + 'd' * 64},
    {'platformLockDigest': 'sha256:' + 'd' * 64}, {'observedAt': '2026-11-01T00:00:00Z'},
    {'observedAt': '2026-11-16T00:00:00Z'}, {'schema': 'unknown'}, {'row': 'other'},
    {'certifying': False},
])
def test_invalid_receipts_cannot_pass(change):
    row = p.load_contract(CONTRACT)[0]
    record = run(row)
    record['reports'][0].update(change)
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'


@pytest.mark.parametrize('change', [
    {'conclusion': 'failure'}, {'head_branch': 'topic'}, {'event': 'pull_request'},
    {'path': '.github/workflows/forged.yml'}, {'run_started_at': None},
    {'updated_at': '2026-11-16T00:00:00Z'},
])
def test_untrusted_or_invalid_run_cannot_pass(change):
    row = p.load_contract(CONTRACT)[0]
    record = run(row)
    record.update(change)
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'


def test_newer_failure_without_receipt_invalidates_old_pass():
    row = p.load_contract(CONTRACT)[0]
    failed = copy.deepcopy(run(row))
    failed.update(id=2, conclusion='failure', reports=[],
                  run_started_at='2026-11-14T02:00:00Z', updated_at='2026-11-14T02:01:00Z')
    assert p.evaluate_row(row, [run(row), failed], (CANDIDATE, NOW))['status'] != 'PASS'


@pytest.mark.parametrize('count', [1, -1, False, '0', None])
def test_private_findings_must_be_integer_zero(count):
    row = next(r for r in p.load_contract(CONTRACT) if r['id'] == 'security-open-high')
    record = run(row)
    record['reports'][0]['openHighFindings'] = count
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'


def test_duplicate_or_missing_contract_row_rejected(tmp_path):
    doc = json.loads(CONTRACT.read_text())
    doc['rows'][-1] = doc['rows'][0]
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        p.load_contract(path)


def test_promise_gate_is_required_and_candidate_bound():
    assert 'gate-release-promise.yml' in w.RC_GATES
    assert 'gate-release-promise.yml' in w.CANDIDATE_BOUND_GATES


def aggregate():
    results = p.evaluate(p.load_contract(CONTRACT),
                         {r['id']: [run(r)] for r in p.load_contract(CONTRACT)}, (CANDIDATE, NOW))
    return {'schema': 'honua.release-promise-report/v1', 'status': 'pass',
            'serverSha': CANDIDATE.server_sha, 'imageDigest': CANDIDATE.server_digest,
            'platformLockDigest': CANDIDATE.platform_lock, 'rows': results,
            'outcomes': [{'id': key, 'status': 'PASS', 'value': 0, 'unit': 'minutes',
                          'releaseNotesReported': True, 'targetMet': True,
                          'observedAt': '2026-11-14T00:00:00Z'} for key in p.outcomes.IDS]}


def test_rc_allows_preview_ai_but_ga_requires_all_five_rows(monkeypatch):
    targets = [{**row, 'gaMaximum': 0} for row in p.load_outcomes()]
    monkeypatch.setattr(p.outcomes, 'load', lambda path: targets)
    doc = aggregate()
    assert p.report_valid(doc, 'ga', NOW)
    for row in doc['rows']:
        if row['id'].startswith('ai-'):
            row['status'] = 'missing'
    assert p.report_valid(doc, 'rc', NOW)
    assert not p.report_valid(doc, 'ga', NOW)


def test_expired_rows_in_recent_aggregate_block_admission():
    doc = aggregate()
    doc['rows'][0]['observedAt'] = '2026-11-01T00:00:00Z'
    assert not p.report_valid(doc, 'rc', NOW)


@pytest.mark.parametrize('profile,expected', [('rc', True), ('ga', False)])
def test_admission_applies_promise_schema_and_profile(profile, expected):
    doc = aggregate()
    doc['rows'][-1]['status'] = 'PASS'
    next(row for row in doc['rows'] if row['id'] == 'ai-migrate')['status'] = 'missing'
    record = run(p.load_contract(CONTRACT)[0])
    record.update(report=doc, created_at=record['run_started_at'])
    result = w.evaluate({w.PROMISE_GATE: {'runs': [record]}}, NOW, 7,
                        candidate=CANDIDATE, profile=profile)
    assert result['admissible'] is expected


def test_bare_candidate_pass_does_not_certify_promise_gate():
    doc = evidence(p.load_contract(CONTRACT)[0])
    doc['status'] = 'pass'
    record = run(p.load_contract(CONTRACT)[0])
    record.update(report=doc, reports=[], created_at=record['run_started_at'])
    result = w.evaluate({w.PROMISE_GATE: {'runs': [record]}}, NOW, 7, candidate=CANDIDATE)
    assert not result['admissible']


@pytest.mark.parametrize('row_id', ['cloud-launch-stack', 'security-hardened'])
def test_live_stack_requirements_cannot_be_omitted(row_id):
    row = next(r for r in p.load_contract(CONTRACT) if r['id'] == row_id)
    record = run(row)
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] == 'PASS'
    record['reports'][0]['assertions'] = {}
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'


def test_collector_uses_declared_artifact_and_filename(monkeypatch):
    row = p.load_contract(CONTRACT)[0]
    record = run(row)
    monkeypatch.setattr(w, '_completed_history', lambda *args: ([record], True))
    monkeypatch.setattr(p, '_artifacts', lambda *args: [
        {'id': 7, 'name': row['artifact'], 'size_in_bytes': 100, 'expired': False}])
    import io
    import zipfile
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w') as archive:
        archive.writestr(row['receipt'], json.dumps(evidence(row)))
    monkeypatch.setattr(w, '_get_bytes', lambda *args: blob.getvalue())
    records = p.collect([row], 'token', NOW)
    assert p.evaluate([row], records, (CANDIDATE, NOW))[0]['status'] == 'PASS'
    row = {**row, 'receipt': 'wrong.json'}
    assert p.evaluate([row], p.collect([row], 'token', NOW), (CANDIDATE, NOW))[0]['status'] != 'PASS'


def outcome_record():
    row = p.load_outcomes()[0]
    record = run(row)
    record['reports'][0].update(value=12.5, unit='minutes', releaseNotes='candidate measurement')
    return row, record


def test_outcome_requires_numeric_minutes_and_release_notes():
    row, record = outcome_record()
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'
    record['reports'][0]['releaseNotes'] = p.outcomes.notes_line(record['reports'][0])
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] == 'PASS'
    record['reports'][0]['value'] = True
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'


def test_outcomes_reported_without_targets_pass_rc_but_not_ga():
    report = aggregate()
    report.pop('outcomes')
    assert not p.report_valid(report, 'rc', NOW)
    measurements = []
    for row in p.load_outcomes():
        record = run(row)
        record['reports'][0].update(value=12.5, unit='minutes')
        record['reports'][0]['releaseNotes'] = p.outcomes.notes_line(record['reports'][0])
        measurements.append(p.evaluate_row(row, [record], (CANDIDATE, NOW)))
    report['outcomes'] = measurements
    assert p.report_valid(report, 'rc', NOW)
    assert not p.report_valid(report, 'ga', NOW)


@pytest.mark.parametrize('value', [10 ** 400, float('inf'), float('nan'), -1, True])
def test_invalid_measurement_numbers_fail_closed(value):
    assert not p.outcomes.number(value)


def test_malformed_notes_fail_closed():
    assert not p.outcomes.valid({'row': 'install-console', 'value': 1,
                                'unit': 'minutes', 'releaseNotes': None})


def test_receipt_cannot_claim_notes_absent_from_artifact(monkeypatch):
    import io
    import zipfile
    row, record = outcome_record()
    doc = record['reports'][0]
    doc['releaseNotes'] = p.outcomes.notes_line(doc)
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w') as archive:
        archive.writestr(row['receipt'], json.dumps(doc))
    monkeypatch.setattr(w, '_get_bytes', lambda *args: blob.getvalue())
    artifacts = [{'id': 7, 'name': row['artifact'], 'size_in_bytes': 100, 'expired': False}]
    record['reports'] = p._reports(row, 'token', artifacts)
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] != 'PASS'
