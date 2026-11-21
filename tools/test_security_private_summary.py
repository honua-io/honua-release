"""Private security summary is a founder-authenticated lane-host manifest input."""
import argparse
import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

import rc_gate_window as gate
import release_promise as promise

NOW = dt.datetime(2026, 11, 21, 5, 0, 0, tzinfo=dt.timezone.utc)
CANDIDATE = gate.Candidate('a' * 40, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64)
FOUNDER = 'mikemcdougall'
ROOT = Path(__file__).resolve().parents[1]


def valid_summary(**overrides):
    doc = {
        'schema': 'honua.release-promise-receipt/v1',
        'row': 'security-open-high',
        'status': 'PASS',
        'serverSha': CANDIDATE.server_sha,
        'imageDigest': CANDIDATE.server_digest,
        'platformLockDigest': CANDIDATE.platform_lock,
        'observedAt': '2026-11-21T04:00:00Z',
        'assertions': {'privateScoped': True},
        'openHighFindings': 0,
    }
    doc.update(overrides)
    return doc


def security_row():
    return next(row for row in promise.load_contract() if row['id'] == 'security-open-high')


def test_ci_private_tracker_workflow_and_token_script_are_removed():
    assert not (ROOT / '.github/workflows/gate-security-private-summary.yml').exists()
    assert not (ROOT / 'tools/security_private_summary.py').exists()


def test_contract_declares_security_open_high_as_manifest_input():
    row = security_row()
    assert row['source'] == 'manifest-input'
    assert row['required'] == ['rc', 'ga']
    assert row['receipt'] == 'security-open-high.json'
    assert row['assertions'] == {'privateScoped': True}
    assert 'workflow' not in row
    assert promise.producer_workflows(row) == []


@pytest.mark.parametrize('mutation', [
    {'workflow': 'gate-security-private-summary.yml'},
    {'repo': 'honua-io/honua-release'},
    {'artifact': 'release-promise-receipts'},
    {'source': 'actions'},
])
def test_contract_rejects_workflow_fields_on_manifest_input_row(tmp_path, mutation):
    doc = json.loads((ROOT / 'certification/release-promise.v1.json').read_text())
    row = next(r for r in doc['rows'] if r['id'] == 'security-open-high')
    row.update(mutation)
    bad = tmp_path / 'contract.json'
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        promise.load_contract(bad)


def test_manifest_input_file_passes_when_valid(tmp_path):
    summary_file = tmp_path / 'security-open-high.json'
    summary_file.write_text(json.dumps(valid_summary()))
    records = promise.collect(
        [security_row()], 'unused-token', NOW,
        manifest_inputs={'security-open-high': promise.load_manifest_input(
            path=str(summary_file), actor=FOUNDER)},
    )
    result = promise.evaluate([security_row()], records, (CANDIDATE, NOW))[0]
    assert result['status'] == 'PASS'
    assert result['producer'] == 'manifest-input/security-open-high.json'
    assert result['observedAt'] == '2026-11-21T04:00:00Z'
    assert result['actor'] == FOUNDER
    assert len(result['summarySha256']) == 64


def test_manifest_input_inline_json_passes_when_valid():
    records = promise.load_manifest_input(raw_json=json.dumps(valid_summary()), actor=FOUNDER)
    result = promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))
    assert result['status'] == 'PASS'


def test_missing_manifest_input_fails_closed():
    result = promise.evaluate_row(security_row(), promise.load_manifest_input(actor=FOUNDER), (CANDIDATE, NOW))
    assert result['status'] == 'missing'


@pytest.mark.parametrize('actor', ['', 'other-user', 'github-actions[bot]'])
def test_non_founder_actor_cannot_pass_manifest_input(actor):
    records = promise.load_manifest_input(raw_json=json.dumps(valid_summary()), actor=actor)
    result = promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))
    assert result['status'] == 'invalid'


def test_ambiguous_dual_file_and_inline_json_fails_closed(tmp_path):
    summary_file = tmp_path / 'security-open-high.json'
    text = json.dumps(valid_summary())
    summary_file.write_text(text)
    records = promise.load_manifest_input(path=str(summary_file), raw_json=text, actor=FOUNDER)
    assert promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))['status'] == 'invalid'


@pytest.mark.parametrize('change', [
    {'status': 'FAIL'},
    {'openHighFindings': 1},
    {'openHighFindings': -1},
    {'openHighFindings': False},
    {'openHighFindings': '0'},
    {'openHighFindings': None},
    {'serverSha': 'd' * 40},
    {'serverSha': 'A' * 40},
    {'imageDigest': 'sha256:' + 'd' * 64},
    {'imageDigest': 'sha256:' + 'B' * 64},
    {'platformLockDigest': 'sha256:' + 'd' * 64},
    {'observedAt': '2026-11-01T00:00:00Z'},
    {'observedAt': '2026-11-22T00:00:00Z'},
    {'assertions': {}},
    {'assertions': {'privateScoped': False}},
    {'assertions': {'privateScoped': True, 'extra': True}},
    {'issueTitle': 'private finding title'},
])
def test_invalid_or_leaky_manifest_input_fails_closed(change):
    records = promise.load_manifest_input(raw_json=json.dumps(valid_summary(**change)), actor=FOUNDER)
    result = promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))
    assert result['status'] == 'invalid'


@pytest.mark.parametrize('payload', ['not json', '[]', '"' + 'x' * 4097 + '"'])
def test_malformed_or_oversized_manifest_input_fails_closed(tmp_path, payload):
    summary_file = tmp_path / 'security-open-high.json'
    summary_file.write_text(payload)
    for records in (
        promise.load_manifest_input(path=str(summary_file), actor=FOUNDER),
        promise.load_manifest_input(raw_json=payload, actor=FOUNDER),
    ):
        assert promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))['status'] == 'invalid'


def test_nonexistent_manifest_input_file_fails_closed(tmp_path):
    records = promise.load_manifest_input(path=str(tmp_path / 'missing.json'), actor=FOUNDER)
    assert promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))['status'] == 'invalid'


def test_admission_rejects_non_founder_promise_gate_actor():
    from test_release_promise import aggregate, run as make_run, CANDIDATE as T_CANDIDATE, NOW as T_NOW
    record = make_run(promise.load_contract()[0])
    record.update(path='.github/workflows/' + gate.PROMISE_GATE,
                  report=aggregate(), reports=[], created_at=record['run_started_at'],
                  triggering_actor={'login': 'collaborator'})
    result = gate.evaluate({gate.PROMISE_GATE: {'runs': [record]}}, T_NOW, 7, candidate=T_CANDIDATE)
    assert not result['admissible']


def test_main_wires_security_summary_env_and_actor(monkeypatch, tmp_path):
    output = tmp_path / 'report.json'
    summary = valid_summary(observedAt=dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'))
    args = argparse.Namespace(
        server_sha=CANDIDATE.server_sha, server_digest=CANDIDATE.server_digest,
        platform_lock=CANDIDATE.platform_lock, profile='rc',
        security_summary='', security_summary_json='', security_summary_actor='',
        out=str(output),
    )
    seen = {}

    def fake_collect(rows, token, now, manifest_inputs=None):
        seen['manifest_inputs'] = manifest_inputs
        return {promise.SECURITY_SUMMARY_ROW: manifest_inputs[promise.SECURITY_SUMMARY_ROW]}

    monkeypatch.setattr(promise, 'arguments', lambda: (None, args))
    monkeypatch.setattr(promise, 'collect', fake_collect)
    monkeypatch.setenv('GH_TOKEN', 'test-token')
    monkeypatch.setenv('SECURITY_SUMMARY_JSON', json.dumps(summary))
    monkeypatch.setenv('SECURITY_SUMMARY_ACTOR', FOUNDER)
    promise.main()
    report = json.loads(output.read_text())
    sec = next(r for r in report['rows'] if r['id'] == 'security-open-high')
    assert sec['status'] == 'PASS'
    assert sec['actor'] == FOUNDER


def test_gate_release_promise_and_promote_wiring():
    gate_wf = yaml.safe_load((ROOT / '.github/workflows/gate-release-promise.yml').read_text())
    gate_triggers = gate_wf.get('on', gate_wf.get(True))
    assert 'security_summary_json' in gate_triggers['workflow_dispatch']['inputs']
    gate_step = next(s for s in gate_wf['jobs']['promises']['steps']
                     if s.get('name') == 'Evaluate authenticated live producer receipts')
    assert gate_step['env']['SECURITY_SUMMARY_JSON'] == '${{ inputs.security_summary_json }}'
    assert gate_step['env']['SECURITY_SUMMARY_ACTOR'] == '${{ github.triggering_actor }}'

    promote_wf = yaml.safe_load((ROOT / '.github/workflows/promote.yml').read_text())
    promote_step = next(s for s in promote_wf['jobs']['promote']['steps']
                        if s.get('name') == 'Enforce current GA release promises')
    assert "'--reuse-admitted-security-summary'" in promote_step['run']
    assert 'SECURITY_SUMMARY_ACTOR' not in (promote_step.get('env') or {})


def _promise_gate_run(**overrides):
    base = {
        'id': 99, 'status': 'completed', 'conclusion': 'success', 'head_branch': 'trunk',
        'event': 'workflow_dispatch', 'path': f'.github/workflows/{gate.PROMISE_GATE}',
        'head_repository': {'full_name': 'honua-io/honua-release'},
        'actor': {'login': FOUNDER}, 'triggering_actor': {'login': FOUNDER},
        'run_started_at': '2026-11-21T04:00:00Z', 'updated_at': '2026-11-21T04:01:00Z',
    }
    base.update(overrides)
    return base


def _admitted_report(**overrides):
    sec_row = {
        'id': 'security-open-high', 'status': 'PASS', 'observedAt': '2026-11-21T04:00:00Z',
        'actor': FOUNDER, 'summarySha256': 'e' * 64,
    }
    report = {
        'schema': 'honua.release-promise-report/v1', 'status': 'pass',
        'serverSha': CANDIDATE.server_sha, 'imageDigest': CANDIDATE.server_digest,
        'platformLockDigest': CANDIDATE.platform_lock, 'rows': [sec_row],
    }
    report.update(overrides)
    return report


def test_admitted_security_summary_trust_filter(monkeypatch):
    policy = yaml.safe_load((ROOT / 'certification/release-promotion-approval.yaml').read_text())
    promote_actor = 'honua-release-bot[bot]'
    roster = [entry['login'] for entry in policy['approval']['required_reviewers']]
    assert FOUNDER in roster
    assert promote_actor not in roster and promote_actor not in promise.SECURITY_SUMMARY_ACTORS

    def run_reuse(history_runs, report_by_id):
        monkeypatch.setattr(promise, '_history', lambda key, token, now: history_runs)
        monkeypatch.setattr(promise, '_available_artifacts', lambda repo, token, run_id: [{'id': run_id}])
        monkeypatch.setattr(promise, '_producer_reports',
                            lambda row, token, arts: report_by_id.get(arts[0]['id'], []))
        records = promise._admitted_security_summary('token', NOW)
        return promise.evaluate_row(security_row(), records, (CANDIDATE, NOW))

    # 1. Founder trunk run is accepted.
    ok = run_reuse([_promise_gate_run(id=10)], {10: [_admitted_report()]})
    assert ok['status'] == 'PASS'
    assert ok['actor'] == FOUNDER
    assert ok['summarySha256'] == 'e' * 64

    # 2. Non-founder re-run (triggering_actor != actor) is rejected.
    rerun = _promise_gate_run(id=11, actor={'login': FOUNDER}, triggering_actor={'login': promote_actor})
    assert run_reuse([rerun], {11: [_admitted_report()]})['status'] == 'invalid'

    # 3. Non-trunk branch is rejected.
    branch_run = _promise_gate_run(id=12, head_branch='topic')
    assert run_reuse([branch_run], {12: [_admitted_report()]})['status'] == 'invalid'

    # 4. Every contending run returned by _history must validate (no fallback to an older founder run).
    older_ok = _promise_gate_run(id=10, run_started_at='2026-11-21T02:00:00Z', updated_at='2026-11-21T02:01:00Z')
    newer_bad = _promise_gate_run(id=13, triggering_actor={'login': promote_actor},
                                  run_started_at='2026-11-21T04:00:00Z', updated_at='2026-11-21T04:01:00Z')
    assert run_reuse([newer_bad, older_ok], {10: [_admitted_report()], 13: [_admitted_report()]})['status'] == 'invalid'

    # 5. Wrong-candidate admitted report is rejected.
    assert run_reuse([_promise_gate_run(id=14)], {14: [_admitted_report(serverSha='d' * 40)]})['status'] == 'invalid'
