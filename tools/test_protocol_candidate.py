"""Standalone protocol admission binds frozen producer inputs and refuses partial passes."""
from pathlib import Path

import pytest
import yaml

import protocol_candidate as p
from agent_e2e_pack import PackError


@pytest.fixture
def frozen(tmp_path):
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1]/'platform-manifest.yaml').read_text())
    (tmp_path/'platform-manifest.yaml').write_text(yaml.safe_dump(manifest))
    verified = {'qualificationLockDigest': 'sha256:'+'c'*64, 'producer': {'runId': 42, 'runAttempt': 2}}
    calls = []
    def verify(root, run, attempt):
        calls.append((root, run, attempt))
        return verified
    return tmp_path, manifest, verified, verify, calls


def test_inputs_equal_train_frozen_fields(frozen):
    root, manifest, verified, verify, calls = frozen
    result = p.prepare(root, (42, 2, verified['qualificationLockDigest']), verifier=verify)
    assert calls == [(root, 42, 2)]
    assert result['serverSha'] == manifest['components']['honua-server']['sha']
    assert result['imageDigest'] == manifest['components']['honua-server']['digest']
    assert result['inputs']['expected_sdk_python_version'] == manifest['clientArtifacts']['honua-sdk-python-wheel']['version']
    assert result['inputs']['expected_requirements_source_revision'] == manifest['protocolCertification']['ledger']['requirementsSourceRevision']
    assert result['inputs']['expected_geospatial_grpc_sha'] == manifest['components']['geospatial-grpc']['sha']
    assert result['platformLockDigest'] == verified['qualificationLockDigest']


def test_wrong_lock_digest_refused(frozen):
    root, _, _, verify, _ = frozen
    with pytest.raises(PackError, match='requested digest'):
        p.prepare(root, (42, 2, 'sha256:'+'d'*64), verifier=verify)


def test_untrusted_candidate_never_reads_manifest(tmp_path):
    def refuse(*args):
        raise PackError('untrusted producer')
    with pytest.raises(PackError, match='untrusted producer'):
        p.prepare(tmp_path, (42, 2, 'sha256:'+'c'*64), verifier=refuse)


@pytest.mark.parametrize('verdict', [('success', 'pass', 'pass'),
    ('success', '', 'fail'), ('success', 'blocked', 'fail'), ('failure', 'pass', 'fail'),
    ('cancelled', 'pass', 'fail'), ('skipped', 'pass', 'fail')])
def test_receipt_requires_both_evaluator_and_job_pass(frozen, verdict):
    conclusion, status, expected = verdict
    root, _, verified, verify, _ = frozen
    identity = p.prepare(root, (42, 2, verified['qualificationLockDigest']), verifier=verify)
    result = p.receipt(identity, conclusion, status)
    assert result['status'] == expected
    assert result['serverSha'] == identity['serverSha']
    assert result['imageDigest'] == identity['imageDigest']
    assert result['platformLockDigest'] == identity['platformLockDigest']
    assert 'inputs' not in result


def test_frozen_input_cannot_inject_workflow_outputs(frozen):
    root, manifest, verified, verify, _ = frozen
    manifest['protocolCertification']['ledger']['path'] = 'ledger.json\ninjected=value'
    (root/'platform-manifest.yaml').write_text(yaml.safe_dump(manifest))
    with pytest.raises(PackError, match='single-line'):
        p.prepare(root, (42, 2, verified['qualificationLockDigest']), verifier=verify)


def test_wrapper_uses_train_evaluator_and_all_frozen_inputs(frozen):
    root, _, verified, verify, _ = frozen
    repository = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repository/'.github/workflows/protocol-certification-candidate.yml').read_text())
    train = yaml.safe_load((repository/'.github/workflows/release-train.yml').read_text())
    certify = workflow['jobs']['certify']
    assert certify['uses'] == train['jobs']['gate_protocol_certification']['uses']
    assert certify['with']['tier'] == 'release'
    assert certify['with']['enforcement'] == 'strict'
    inputs = p.prepare(root, (42, 2, verified['qualificationLockDigest']), verifier=verify)['inputs']
    assert set(certify['with']) == set(inputs) | {'tier', 'enforcement'}
    assert set(workflow['jobs']['prepare']['outputs']) == set(inputs)
    for key in inputs:
        assert certify['with'][key] == '${{ needs.prepare.outputs.'+key+' }}'
    assert workflow['jobs']['prepare']['steps'][0]['with']['fetch-depth'] == 0
