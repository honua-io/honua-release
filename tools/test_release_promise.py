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
            'head_repository': {'full_name': row['repo']},
            'path': '.github/workflows/' + row['workflow'],
            'run_started_at': '2026-11-14T00:00:00Z', 'updated_at': '2026-11-14T00:01:00Z',
            'reports': [evidence(row)]}


def test_contract_covers_every_required_promise():
    rows = p.load_contract(CONTRACT)
    assert {r['id'] for r in rows} == {
        *(f'operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)), 'operates-12-notify',
        *(f'ai-{s}' for s in ('operate', 'publish', 'build', 'migrate', 'injection')),
        'security-hardened', 'security-open-high', 'cloud-launch-stack', 'security-whitehat', 'chaos'}
    assert len(rows) == 20


def test_missing_rows_fail_closed():
    results = p.evaluate(p.load_contract(CONTRACT), {}, (CANDIDATE, NOW))
    assert len(results) == 20
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
    for result in results:
        if result['id'] in p.adversarial.IDS:
            result.update(status='PASS', observedAt='2026-11-14T00:00:00Z')
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
    record.update(path='.github/workflows/' + w.PROMISE_GATE,
                  report=doc, created_at=record['run_started_at'])
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


def test_whitehat_and_chaos_are_required_and_need_detailed_evidence():
    rows = {r['id']: r for r in p.load_contract(CONTRACT)}
    assert rows['security-whitehat']['required'] == ['rc', 'ga']
    assert rows['chaos']['required'] == ['rc', 'ga']
    for key in ('security-whitehat', 'chaos'):
        assert p.evaluate_row(rows[key], [run(rows[key])], (CANDIDATE, NOW))['status'] != 'PASS'


def whitehat_doc():
    row = next(r for r in p.load_contract(CONTRACT) if r['id'] == 'security-whitehat')
    doc = evidence(row)
    doc.update(stackName='honua-gate-whitehat-test', stackId='test-stack-id',
               openNewHighFindings=0, openNewCriticalFindings=0,
               reviews=[{'model': model, 'reviewer': model, 'source': True, 'live': True,
                         'status': 'PASS', 'report': f'review-{i}.md'}
                        for i, model in enumerate(sorted(p.adversarial.MODELS))])
    doc['_archivedReports'] = [r['report'] for r in doc['reviews']]
    return doc


def test_whitehat_requires_both_independent_source_and_live_reports():
    doc = whitehat_doc()
    assert p.adversarial.valid(doc)
    for field in ('source', 'live', 'status', 'model', 'reviewer', 'report'):
        changed = copy.deepcopy(doc)
        changed['reviews'][1][field] = changed['reviews'][0][field] if field in (
            'model', 'reviewer', 'report') else None
        assert not p.adversarial.valid(changed)
    for change in ({'stackName': 'unrelated'}, {'stackId': ''}, {'openNewHighFindings': 1},
                   {'openNewCriticalFindings': True}, {'_archivedReports': []}):
        assert not p.adversarial.valid({**doc, **change})


def test_whitehat_reports_must_be_archived_with_matching_hash():
    import hashlib
    import io
    import zipfile
    doc = whitehat_doc()
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w') as archive:
        for review in doc['reviews']:
            archive.writestr(review['report'], 'Sanitized review evidence')
            review['reportSha256'] = hashlib.sha256(b'Sanitized review evidence').hexdigest()
    doc['_archivedReports'] = p.adversarial.archived_reports(doc, blob.getvalue())
    assert p.adversarial.valid(doc)
    doc['reviews'][0]['reportSha256'] = '0' * 64
    doc['_archivedReports'] = p.adversarial.archived_reports(doc, blob.getvalue())
    assert not p.adversarial.valid(doc)


def test_chaos_needs_every_fault_and_all_three_evidence_grades():
    doc = {'row': 'chaos', 'faults': [dict(id=key, **{
        grade: {'status': 'PASS', 'evidence': 'sanitized incident reference'}
        for grade in ('telemetry', 'logging', 'remediation')}) for key in p.adversarial.FAULTS]}
    assert p.adversarial.valid(doc)
    for grade in ('telemetry', 'logging', 'remediation'):
        changed = copy.deepcopy(doc)
        changed['faults'][0][grade]['status'] = 'FAIL'
        assert not p.adversarial.valid(changed)
        changed['faults'][0][grade] = {'status': 'PASS', 'evidence': ''}
        assert not p.adversarial.valid(changed)
    doc['faults'].pop()
    assert not p.adversarial.valid(doc)


@pytest.mark.parametrize('change', [
    {'event': 'pull_request'}, {'head_branch': 'topic'},
    {'path': '.github/workflows/forged.yml'}, {'status': 'in_progress'},
    {'head_repository': {'full_name': 'untrusted/fork'}},
])
def test_aggregate_admission_rejects_untrusted_execution(change):
    record = run({'workflow': w.PROMISE_GATE, **p.load_contract(CONTRACT)[0]})
    record.update(path='.github/workflows/' + w.PROMISE_GATE,
                  report=aggregate(), reports=[], created_at=record['run_started_at'])
    record.update(change)
    result = w.evaluate({w.PROMISE_GATE: {'runs': [record]}}, NOW, 7, candidate=CANDIDATE)
    assert not result['admissible']


def test_admitted_outcomes_reach_published_rc_notes():
    import rc_release_notes
    record = run(p.load_contract(CONTRACT)[0])
    record.update(path='.github/workflows/' + w.PROMISE_GATE,
                  report=aggregate(), reports=[], created_at=record['run_started_at'])
    result = w.evaluate({w.PROMISE_GATE: {'runs': [record]}}, NOW, 7, candidate=CANDIDATE)
    notes = rc_release_notes.render({'components': {}}, w.render_markdown(result),
                                    '2026.1-rc.2', None, None, [])
    for row in record['report']['outcomes']:
        assert p.outcomes.notes_line({'row': row['id'], 'value': row['value']}) in notes


def test_ga_promotion_enforces_current_promise_policy_before_candidate_checkout():
    import yaml
    workflow = yaml.safe_load((CONTRACT.parents[1] / '.github/workflows/promote.yml').read_text())
    steps = workflow['jobs']['promote']['steps']
    gate = next(i for i, s in enumerate(steps) if s.get('name') == 'Enforce current GA release promises')
    checkout = next(i for i, s in enumerate(steps)
                    if s.get('with', {}).get('ref') == '${{ steps.train.outputs.source_sha }}')
    assert gate < checkout
    assert "'--profile', 'ga'" in steps[gate]['run']
    assert 'promotion-evidence/trains/' in steps[gate]['run']
    finalize = next(s for s in steps if s.get('id') == 'finalize')
    assert 'ga-promise-outcomes.md' in finalize['run']


@pytest.mark.parametrize('refused', [False, True])
def test_ga_promotion_checks_selected_lock_and_retains_measurements(tmp_path, monkeypatch, refused):
    import hashlib
    import subprocess
    import yaml
    step = next(s for s in yaml.safe_load(Path('.github/workflows/promote.yml').read_text())
                ['jobs']['promote']['steps'] if s.get('name') == 'Enforce current GA release promises')
    script = step['run'].split("python - <<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
    root = tmp_path / 'promotion-evidence/trains/123'
    root.mkdir(parents=True)
    (root / 'platform-lock.json').write_bytes(b'immutable selected lock bytes')
    (root / 'platform-manifest.yaml').write_text(yaml.safe_dump({'components': {'honua-server': {
        'sha': CANDIDATE.server_sha, 'digest': CANDIDATE.server_digest}}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('RC_RUN_ID', '123')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))

    def collect(command, *, check):
        assert check is True
        assert command[command.index('--profile') + 1] == 'ga'
        assert command[command.index('--server-sha') + 1] == CANDIDATE.server_sha
        assert command[command.index('--server-digest') + 1] == CANDIDATE.server_digest
        assert command[command.index('--platform-lock') + 1] == (
            'sha256:' + hashlib.sha256(b'immutable selected lock bytes').hexdigest())
        if refused:
            raise subprocess.CalledProcessError(1, command)
        Path(command[-1]).write_text(json.dumps(aggregate()))

    monkeypatch.setattr(subprocess, 'run', collect)
    if refused:
        with pytest.raises(subprocess.CalledProcessError):
            exec(script, {})
        assert not (tmp_path / 'ga-promise-outcomes.md').exists()
    else:
        exec(script, {})
        assert (tmp_path / 'ga-promise-outcomes.md').read_text().count('<!-- honua-outcome:') == 18
