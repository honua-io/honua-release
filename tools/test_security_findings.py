import json
from unittest.mock import Mock

import pytest
import yaml

import security_findings as findings


FIXED = {'id': 'SEC-9', 'repo': 'honua-io/honua-server', 'status': 'fixed', 'fixedBy': '5082'}
MERGED = {'merged_at': '2026-09-22T14:59:38Z', 'title': 'Configuration hardening',
          'body': 'SEC-9.', 'base': {'ref': 'trunk', 'repo': {'default_branch': 'trunk'}}}


def contract(tmp_path, rows=None):
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps({'schema_version': 1, 'release_line': '2026.1',
                                'expected_count': 1, 'findings': [FIXED] if rows is None else rows}))
    return path


def test_gate_accepts_merged_default_branch_pr_citing_exact_id(tmp_path):
    fetch = Mock(return_value=MERGED)
    findings.verify_contract(contract(tmp_path), fetch)
    fetch.assert_called_once_with('api', 'repos/honua-io/honua-server/pulls/5082')


@pytest.mark.parametrize('response, message', [
    ({**MERGED, 'merged_at': None}, 'not merged'),
    ({**MERGED, 'base': {'ref': 'preview', 'repo': {'default_branch': 'trunk'}}}, 'not the default branch'),
    ({**MERGED, 'base': {'ref': 'trunk', 'repo': {}}}, 'not the default branch'),
    ({**MERGED, 'body': 'SEC-90'}, 'does not cite SEC-9'),
])
def test_gate_rejects_unproven_dispositions(tmp_path, response, message):
    with pytest.raises(ValueError, match=message):
        findings.verify_contract(contract(tmp_path), Mock(return_value=response))


def test_gate_fails_closed_on_unreadable_pr(tmp_path):
    with pytest.raises(ValueError, match='unreadable'):
        findings.verify_contract(contract(tmp_path), Mock(side_effect=ValueError('unreadable')))


def test_open_finding_fails_without_fetching_a_fix(tmp_path):
    fetch = Mock()
    row = {key: value for key, value in {**FIXED, 'status': 'open'}.items() if key != 'fixedBy'}
    with pytest.raises(ValueError, match='remain open'):
        findings.verify_contract(contract(tmp_path, [row]), fetch)
    fetch.assert_not_called()


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'schema_version': 2}, {'release_line': '2027.1'},
    {'expected_count': True}, {'expected_count': 2}, {'findings': []},
    {'private_detail': 'not allowed'}, {'findings': [None]},
    {'findings': [{**FIXED, 'id': None}]}, {'findings': [{**FIXED, 'repo': None}]},
    {'findings': [{**FIXED, 'fixedBy': True}]},
    {'findings': [{**FIXED, 'status': 'open'}]},
    {'findings': [{**FIXED, 'detail': 'not allowed'}]},
    {'findings': [FIXED, FIXED], 'expected_count': 2},
])
def test_contract_rejects_unknown_shape_or_inconsistent_count(tmp_path, change):
    path = contract(tmp_path)
    path.write_text(json.dumps({**json.loads(path.read_text()), **change}))
    with pytest.raises(ValueError):
        findings.load_contract(path)


def test_committed_contract_preserves_public_dispositions():
    rows = findings.load_contract(findings.CONTRACT)
    assert len(rows) == 10
    assert all(row['status'] == 'fixed' for row in rows)
    assert {row['id'] for row in rows} == {
        'SEC-4', 'SEC-5', 'SEC-9', 'SEC-13', 'SEC-14', 'SEC-16', 'SEC-18', 'SEC-21', 'SEC-23', 'SEC-28'}


def test_workflow_uses_standalone_contract_without_dated_records():
    workflow_path = findings.CONTRACT.parents[1] / '.github/workflows/gate-security-findings.yml'
    text = workflow_path.read_text()
    workflow = yaml.safe_load(text)
    steps = workflow['jobs']['security-findings']['steps']
    assert any('python tools/security_findings.py' in step.get('run', '') for step in steps)
    assert 'release_decision_record' not in text and 'docs/2026.1-release-decision-' not in text
