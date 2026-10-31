import json

import pytest

import terminal_model_trials as trials


def receipt(status='pass', attribution=None):
    return {'attempt': 1, 'mode': 'genuine-model', 'cell': 'local-docker', 'guidance': 'none',
            'endpoint': {'model': 'model-a'}, 'lockDigest': None, 'status': status,
            'failureAttribution': attribution}


def write(root, name, value):
    directory = root / name
    directory.mkdir()
    (directory / 'terminal-model-canary-receipt.json').write_text(json.dumps(value))


def result(root, n=2):
    return trials.aggregate(root, n, cell='local-docker', guidance='none', model='model-a', lock_digest=None)


def test_results_and_exact_interval(tmp_path):
    write(tmp_path, 'terminal-model-canary-trial-1-attempt-1', receipt())
    write(tmp_path, 'terminal-model-canary-trial-2-attempt-1', receipt('fail', 'model'))
    report = result(tmp_path)
    assert (report['passed'], report['trials']) == (1, 2)
    assert report['failureAttribution'] == {'model': 1, 'infrastructure': 0, 'undetermined': 0}
    assert report['clopperPearson95'] == pytest.approx([0.012579117, 0.987420883])


def test_rejects_missing_duplicates_reruns_and_wrong_arm(tmp_path):
    write(tmp_path, 'terminal-model-canary-trial-1-attempt-1', receipt())
    with pytest.raises(ValueError, match='missing'):
        result(tmp_path)
    write(tmp_path, 'terminal-model-canary-trial-01-attempt-1', receipt())
    with pytest.raises(ValueError, match='duplicate'):
        result(tmp_path)
    (tmp_path / 'terminal-model-canary-trial-01-attempt-1' / 'terminal-model-canary-receipt.json').unlink()
    (tmp_path / 'terminal-model-canary-trial-01-attempt-1').rmdir()
    write(tmp_path, 'terminal-model-canary-trial-2-attempt-2', receipt())
    with pytest.raises(ValueError, match='unexpected'):
        result(tmp_path)
    (tmp_path / 'terminal-model-canary-trial-2-attempt-2' / 'terminal-model-canary-receipt.json').unlink()
    (tmp_path / 'terminal-model-canary-trial-2-attempt-2').rmdir()
    write(tmp_path, 'terminal-model-canary-trial-2-attempt-1', {**receipt(), 'attempt': 2})
    with pytest.raises(ValueError, match='identity'):
        result(tmp_path)


def test_lock_requires_matching_bytes_manifest_and_pulled_image(tmp_path):
    import hashlib
    import terminal_model_canary as canary

    digest = 'sha256:' + 'a' * 64
    manifest = {'components': {'honua-server': {'image': 'registry.example/honua:tag', 'digest': digest}}}
    lock = {'components': {'honua-server': {'artifacts': [{'kind': 'image', 'coordinate': 'registry.example/honua', 'digest': digest}]}}}
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(lock))
    lock_digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    observed = {'reference': 'registry.example/honua:tag@' + digest, 'verified': True, 'source': 'docker-pull'}
    verified = canary.verify_lock_binding(path, lock_digest, manifest, observed)
    assert verified['lockDigest'] == lock_digest and verified['serverDigest'] == digest
    for bad in ({**observed, 'verified': False}, {**observed, 'reference': 'registry.example/honua:tag@sha256:' + 'b' * 64}, {**observed, 'source': 'manifest'}):
        with pytest.raises(canary.CanaryError):
            canary.verify_lock_binding(path, lock_digest, manifest, bad)
    with pytest.raises(canary.CanaryError):
        canary.verify_lock_binding(path, 'sha256:' + 'c' * 64, manifest, observed)
    different = {'components': {'honua-server': {'image': 'registry.example/honua:tag', 'digest': 'sha256:' + 'b' * 64}}}
    with pytest.raises(canary.CanaryError):
        canary.verify_lock_binding(path, lock_digest, different, observed)


def test_workflow_preserves_first_attempts_and_isolates_trials():
    import yaml
    root = __import__('pathlib').Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/terminal-model-canary.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert triggers['workflow_dispatch']['inputs']['trials']['default'] == '1'
    for name in ('harness', 'local-docker', 'aws-ecs'):
        job = workflow['jobs'][name]
        assert 'github.run_attempt == 1' in job['if']
        assert 'trial' in job['strategy']['matrix']
    assert workflow['jobs']['aws-ecs']['strategy']['max-parallel'] == 1
    aggregate = workflow['jobs']['aggregate']
    assert 'always()' in aggregate['if']
    assert any('terminal-model-canary-trial-*-attempt-1' == step.get('with', {}).get('pattern') for step in aggregate['steps'])
    local = workflow['jobs']['local-docker']
    script = '\n'.join(str(step.get('run', '')) for step in local['steps'])
    assert 'verify_lock_binding' in script and 'docker image inspect' in script
    cell = yaml.safe_load((root / '.github/workflows/e2e-cloud-aws-cell.yml').read_text())
    assert 'inputs.trial_id' in cell['env']['CELL_ARTIFACT']


def test_ecs_readback_records_each_running_task_digest_and_refuses_mismatch():
    import types
    import terminal_model_ecs_image as ecs
    digest, child = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
    manifest = {'components': {'honua-server': {'image': 'registry.example/honua:tag', 'digest': digest,
        'platformDigests': {'amd64': child}, 'awsEcsArchitecture': 'x86_64'}}}
    class Target:
        _workdir = 'fixture'
        def _tf(self, root, *args):
            return types.SimpleNamespace(stdout={'ecs_cluster_name': 'cluster', 'ecs_service_name': 'service'}[args[-1]])
    def run(cmd, **kwargs):
        if cmd[2] == 'list-tasks':
            body = {'taskArns': ['task-1']}
        else:
            body = {'tasks': [{'taskArn': 'task-1', 'lastStatus': 'RUNNING', 'containers': [
                {'name': 'server', 'lastStatus': 'RUNNING', 'image': 'registry.example/honua@' + digest, 'imageDigest': child}]}]}
        return types.SimpleNamespace(stdout=json.dumps(body))
    proof = ecs.observe(Target(), manifest, run=run)
    assert proof['observedDigests'] == [child]
    assert proof['reference'] == 'registry.example/honua:tag@' + digest
    wrong = {'components': {'honua-server': {**manifest['components']['honua-server'], 'platformDigests': {'amd64': 'sha256:' + 'c' * 64}}}}
    with pytest.raises(ValueError, match='verification failed'):
        ecs.observe(Target(), wrong, run=run)


def test_ecs_lock_binding_requires_the_locked_platform_child(tmp_path):
    import hashlib
    import terminal_model_canary as canary
    index, child = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
    server = {'image': 'registry.example/honua:tag', 'digest': index, 'platformDigests': {'amd64': child}}
    manifest = {'components': {'honua-server': server}}
    lock = {'components': {'honua-server': {'artifacts': [{'kind': 'image', 'coordinate': 'registry.example/honua', 'digest': index, 'platformDigests': {'amd64': child}}]}}}
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(lock))
    lock_digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    proof = {'reference': server['image'] + '@' + index, 'verified': True, 'source': 'ecs-task-readback', 'observedDigests': [child]}
    assert canary.verify_lock_binding(path, lock_digest, manifest, proof)['observedDigests'] == [child]
    with pytest.raises(canary.CanaryError, match='outside the locked'):
        canary.verify_lock_binding(path, lock_digest, manifest, {**proof, 'observedDigests': ['sha256:' + 'c' * 64]})
    with pytest.raises(canary.CanaryError, match='platform image digests'):
        canary.verify_lock_binding(path, lock_digest, {'components': {'honua-server': {**server, 'platformDigests': {'amd64': 'sha256:' + 'c' * 64}}}}, proof)


def test_pulled_image_observation_requires_repo_digest():
    import terminal_model_canary as canary
    digest = 'sha256:' + 'a' * 64
    manifest = {'components': {'honua-server': {'image': 'registry.example/honua:tag', 'digest': digest}}}
    assert canary.pulled_image_proof(manifest, [{'RepoDigests': ['registry.example/honua@' + digest]}])['verified']
    for inspection in ([], [{'RepoDigests': []}], [{'RepoDigests': ['registry.example/honua@sha256:' + 'b' * 64]}]):
        with pytest.raises(canary.CanaryError):
            canary.pulled_image_proof(manifest, inspection)
