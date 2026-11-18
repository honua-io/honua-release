"""Regression coverage for adapting real gate verdicts without manufacturing proof."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

import kept_gate_receipt as k

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64


def binding(tmp_path):
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(yaml.safe_dump({'components': {'honua-server': {
        'sha': SHA, 'digest': DIGEST, 'image': 'ghcr.io/honua-io/honua-server:test'}}}))
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({'sourceInputs': {'platformManifest': {
        'sha256': 'sha256:' + hashlib.sha256(manifest.read_bytes()).hexdigest()}},
        'components': {'honua-server': {'artifacts': [{'kind': 'image',
            'coordinate': 'ghcr.io/honua-io/honua-server', 'digest': DIGEST, 'sourceRevision': SHA}]}}}))
    return manifest, lock, 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()


def run(tmp_path, gate='gate-contract', status='pass', **kwargs):
    manifest, lock, digest = binding(tmp_path)
    proof = kwargs.get('proof')
    if isinstance(proof, dict) and proof.get('platformLockDigest') == LOCK:
        kwargs['proof'] = {**proof, 'platformLockDigest': digest}
    return k.emit(k.Verdict(gate, status, manifest, digest, lock=lock,
                            server_sha=SHA, image_digest=DIGEST, **kwargs), tmp_path / 'receipt')


@pytest.mark.parametrize('status', ['blocked', 'skipped', 'fail', '', 'PASS'])
def test_nonpasses_never_emit(tmp_path, status):
    with pytest.raises(ValueError):
        run(tmp_path, status=status)
    assert not (tmp_path / 'receipt').exists()


def test_preserves_exact_candidate_and_raw_success(tmp_path):
    doc, = run(tmp_path)
    assert (doc['serverSha'], doc['imageDigest'], doc['platformLockDigest']) == (SHA, DIGEST, binding(tmp_path)[2])
    assert doc['assertions'] == {'producerPassed': True}
    assert json.loads((tmp_path / 'receipt/contract-compat.json').read_text()) == doc


@pytest.mark.parametrize('proof', [None, {}, {'assertions': {'launchStack': True}}])
def test_compose_or_kind_success_cannot_certify_launch_stack(tmp_path, proof):
    with pytest.raises(ValueError, match='Launch Stack'):
        run(tmp_path, gate='gate-upgrade', proof=proof)


def test_upgrade_workflow_does_not_certify_compose_report_as_launch_stack():
    """A green Compose/kind gate must not fail, or mint operates-8-upgrade, on its own report.

    The receipt step used to feed out/gate-report.json to validate_upgrade and fail the job.
    Launch Stack proof is a separate input.
    """
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/gate-upgrade.yml').read_text())
    for event in ('workflow_dispatch', 'workflow_call'):
        proof_input = workflow[True][event]['inputs']['launch_stack_proof']
        assert proof_input.get('default', '') == ''
    step = next(s for j in workflow['jobs'].values() for s in j.get('steps', [])
                if s.get('uses') == './.github/actions/kept-gate-receipt')
    assert "inputs.launch_stack_proof != ''" in step['if']
    assert step['with']['proof'] == '${{ inputs.launch_stack_proof }}'
    assert 'gate-report.json' not in step['with']['proof']


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
    manifest, lock, digest = binding(tmp_path)
    doc, = k.emit(k.Verdict('gate-sbom', 'pass', manifest, digest, lock=lock,
                            server_sha='d' * 40, image_digest='sha256:' + 'e' * 64), tmp_path / 'receipt')
    assert (doc['serverSha'], doc['imageDigest']) == (SHA, DIGEST)


def test_timestamp_is_accepted_by_consumer(tmp_path):
    import release_promise
    instant = release_promise.instant
    doc, = run(tmp_path)
    assert instant(doc['observedAt']) is not None


def test_unverified_digest_cannot_emit(tmp_path):
    with pytest.raises((ValueError, FileNotFoundError), match='lock|manifest'):
        k.emit(k.Verdict('gate-contract', 'pass', None, LOCK,
                        server_sha=SHA, image_digest=DIGEST), tmp_path / 'receipt')


def test_protocol_adapter_requires_release_tier():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/gate-protocol-certification.yml').read_text())
    step = next(s for j in workflow['jobs'].values() for s in j.get('steps', [])
                if s.get('uses') == './.github/actions/kept-gate-receipt')
    assert "inputs.tier == 'release'" in step['if']


def test_capacity_retains_source_observation_time(tmp_path):
    old = '2020-01-01T00:00:00Z'
    with pytest.raises(ValueError, match='stale'):
        run(tmp_path, gate='capacity-soak', proof={'window': {'endedAt': old}})


def test_lock_must_hash_and_bind_exact_manifest(tmp_path):
    manifest, lock, digest = binding(tmp_path)
    for bad_digest in (LOCK, digest):
        if bad_digest == digest:
            manifest.write_text(manifest.read_text() + '\n# another component set\n')
        with pytest.raises(ValueError, match='lock'):
            k.emit(k.Verdict('gate-contract', 'pass', manifest, bad_digest, lock=lock), tmp_path / 'receipt')
    assert not (tmp_path / 'receipt').exists()


def test_capacity_accepts_fresh_source_but_never_missing_time(tmp_path):
    import datetime as dt
    with pytest.raises(ValueError, match='observation time'):
        run(tmp_path, gate='capacity-soak', proof={})
    observed = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    doc, = run(tmp_path, gate='capacity-soak', proof={'window': {'endedAt': observed}})
    assert doc['row'] == 'operates-capacity'


@pytest.mark.parametrize('field', ['EXPECTED_SDK_JS_SHA', 'EXPECTED_SDK_PYTHON_SHA',
    'EXPECTED_SDK_DOTNET_SHA', 'EXPECTED_GEOSPATIAL_GRPC_SHA', 'EXPECTED_GEOSPATIAL_MCP_SHA',
    'EXPECTED_SDK_JS_VERSION', 'EXPECTED_SDK_PYTHON_VERSION', 'EXPECTED_SDK_DOTNET_VERSION',
    'EXPECTED_CUT_AT', 'EXPECTED_SERVER_CERTIFICATION_SHA', 'EXPECTED_REQUIREMENTS_REVISION'])
def test_protocol_rejects_different_evaluated_component_set(tmp_path, field):
    import protocol_candidate
    manifest, lock, _ = binding(tmp_path)
    frozen = yaml.safe_load((Path(__file__).resolve().parents[1] / 'platform-manifest.yaml').read_text())
    frozen['components']['honua-server'].update(sha=SHA, digest=DIGEST)
    manifest.write_text(yaml.safe_dump(frozen))
    locked = json.loads(lock.read_text())
    locked['sourceInputs']['platformManifest']['sha256'] = 'sha256:' + hashlib.sha256(manifest.read_bytes()).hexdigest()
    lock.write_text(json.dumps(locked))
    digest = 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()
    (tmp_path / 'platform-manifest.yaml').write_bytes(manifest.read_bytes())
    identity = protocol_candidate.prepare(tmp_path, (1, 1, digest), verifier=lambda *args: {
        'qualificationLockDigest': digest, 'producer': {}})
    proof = {key.upper(): value for key, value in identity['inputs'].items() if key.startswith('expected_')}
    proof['EXPECTED_SHA'] = proof.pop('EXPECTED_SOURCE_SHA')
    proof['EXPECTED_DIGEST'] = proof.pop('EXPECTED_IMAGE_DIGEST')
    proof['EXPECTED_REQUIREMENTS_REVISION'] = proof.pop('EXPECTED_REQUIREMENTS_SOURCE_REVISION')
    verdict = k.Verdict('gate-protocol-certification', 'pass', manifest, digest,
                        server_sha=SHA, image_digest=DIGEST, lock=lock, proof=proof)
    assert k.emit(verdict, tmp_path / 'valid')[0]['row'] == 'protocol-compat'
    proof[field] = 'different'
    with pytest.raises(ValueError, match='protocol'):
        k.emit(verdict, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_capacity_preserves_original_time(tmp_path):
    import datetime as dt
    observed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)).isoformat()
    doc, = run(tmp_path, gate='capacity-soak', proof={'window': {'endedAt': observed}})
    assert doc['sourceObservedAt'] == observed


def test_absent_lock_is_regenerated_from_the_consumed_manifest(tmp_path):
    manifest, lock, digest = binding(tmp_path)
    expected = lock.read_bytes()
    lock.unlink()
    (tmp_path / 'compatibility-matrix.yaml').write_text('contracts: {}\n')
    calls = []

    def generate(manifest_path, matrix_path):
        calls.append((manifest_path, matrix_path))
        return expected

    k.ensure_lock(lock, manifest, generate)
    assert calls == [(manifest, tmp_path / 'compatibility-matrix.yaml')]
    doc, = k.emit(k.Verdict('gate-security', 'pass', manifest, digest, lock=lock), tmp_path / 'receipt')
    assert (doc['row'], doc['platformLockDigest']) == ('supply-scan', digest)


def test_regenerated_lock_for_another_candidate_cannot_emit(tmp_path):
    manifest, lock, digest = binding(tmp_path)
    lock.unlink()
    k.ensure_lock(lock, manifest, lambda manifest_path, matrix_path: b'{"other": "candidate"}')
    with pytest.raises(ValueError, match='lock'):
        k.emit(k.Verdict('gate-security', 'pass', manifest, digest, lock=lock), tmp_path / 'receipt')
    assert not (tmp_path / 'receipt').exists()


def test_present_lock_bytes_are_never_regenerated(tmp_path):
    manifest, lock, _ = binding(tmp_path)
    before = lock.read_bytes()

    def refuse(manifest_path, matrix_path):
        raise AssertionError('present lock must be used as given')

    k.ensure_lock(lock, manifest, refuse)
    assert lock.read_bytes() == before


def test_default_regeneration_is_the_qualification_lock_of_the_repo_manifest(tmp_path):
    import generate_platform_lock as generator
    from platform_lock_bundle import canonical_bytes
    root = Path(__file__).resolve().parents[1]
    manifest = tmp_path / 'platform-manifest.yaml'
    manifest.write_bytes((root / 'platform-manifest.yaml').read_bytes())
    lock = tmp_path / 'platform-lock.json'
    k.ensure_lock(lock, manifest)  # no matrix beside the manifest: the repository matrix is used
    draft = generator.generate(root / 'platform-manifest.yaml', root / 'compatibility-matrix.yaml',
                               qualification=True, image_inspector=None)
    assert lock.read_bytes() == canonical_bytes(draft.lock)
