"""Regression coverage for adapting real gate verdicts without manufacturing proof."""
import json
from pathlib import Path

import pytest
import yaml

import kept_gate_receipt as k

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64


def run(tmp_path, gate='gate-contract', status='pass', **kwargs):
    return k.emit(k.Verdict(gate, status, None, LOCK,
                            server_sha=SHA, image_digest=DIGEST, **kwargs), tmp_path / 'receipt')


@pytest.mark.parametrize('status', ['blocked', 'skipped', 'fail', '', 'PASS'])
def test_nonpasses_never_emit(tmp_path, status):
    with pytest.raises(ValueError):
        run(tmp_path, status=status)
    assert not (tmp_path / 'receipt').exists()


def test_preserves_exact_candidate_and_raw_success(tmp_path):
    doc, = run(tmp_path)
    assert (doc['serverSha'], doc['imageDigest'], doc['platformLockDigest']) == (SHA, DIGEST, LOCK)
    assert doc['assertions'] == {'producerPassed': True}
    assert json.loads((tmp_path / 'receipt/contract-compat.json').read_text()) == doc


@pytest.mark.parametrize('proof', [None, {}, {'assertions': {'launchStack': True}}])
def test_compose_or_kind_success_cannot_certify_launch_stack(tmp_path, proof):
    with pytest.raises(ValueError, match='Launch Stack'):
        run(tmp_path, gate='gate-upgrade', proof=proof)


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, None])
def test_restore_rejects_invalid_measurement(tmp_path, value):
    with pytest.raises(ValueError):
        run(tmp_path, gate='gate-dr', proof={'status': 'pass', 'measurements': {'rtoMs': value}})


def test_restore_converts_actual_rto_to_archived_minutes(tmp_path):
    _, outcome = run(tmp_path, gate='gate-dr', proof={'status': 'pass', 'measurements': {'rtoMs': 90000}})
    assert outcome['value'] == 1.5
    assert (tmp_path / 'receipt/release-notes.md').read_text() == (
        '<!-- honua-outcome:recovery-operates-restore --> 1.5 minutes\n')


def test_all_existing_producers_use_the_adapter_and_explicit_candidate_digest():
    root = Path(__file__).resolve().parents[1]
    for gate in k.ROWS:
        workflow = yaml.safe_load((root / '.github/workflows' / (gate + '.yml')).read_text())
        for event in ('workflow_dispatch', 'workflow_call'):
            if event in workflow[True]:
                assert 'platform_lock_digest' in workflow[True][event]['inputs']
        steps = [s for j in workflow['jobs'].values() for s in j.get('steps', [])
                 if s.get('uses') == './.github/actions/kept-gate-receipt']
        assert len(steps) == 1
        assert 'run' not in steps[0]
        assert steps[0]['with']['gate'] == gate
        assert 'success()' in steps[0]['if']


def test_launch_stack_requires_every_candidate_identity(tmp_path):
    proof = {'serverSha': SHA, 'imageDigest': DIGEST, 'platformLockDigest': LOCK,
             'assertions': dict.fromkeys(('live', 'rollingUpgrade', 'nMinusOneReaderCompatible',
                                         'rollbackPassed', 'launchStack'), True)}
    for field in ('serverSha', 'imageDigest', 'platformLockDigest'):
        with pytest.raises(ValueError, match='Launch Stack'):
            run(tmp_path, gate='gate-upgrade', proof={**proof, field: 'different-candidate'})
    doc, = run(tmp_path, gate='gate-upgrade', proof=proof)
    assert doc['assertions'] == {'producerPassed': True, **proof['assertions']}


def test_manifest_uses_actual_source_and_image(tmp_path):
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(yaml.safe_dump({'components': {'honua-server': {'sha': SHA, 'digest': DIGEST}}}))
    doc, = k.emit(k.Verdict('gate-sbom', 'pass', manifest, LOCK,
                            server_sha='d' * 40, image_digest='sha256:' + 'e' * 64), tmp_path / 'receipt')
    assert (doc['serverSha'], doc['imageDigest']) == (SHA, DIGEST)
