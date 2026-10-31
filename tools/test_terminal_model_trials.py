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
    import fnmatch
    summary_name = next(step['with']['name'] for step in aggregate['steps'] if step.get('name') == 'Upload trial summary')
    assert not fnmatch.fnmatch(summary_name.replace('${{ github.run_attempt }}', '1'), 'terminal-model-canary-trial-*-attempt-1')
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


def test_cloud_costs_fail_when_missing_duplicate_or_over_run_ceiling(tmp_path):
    from decimal import Decimal
    def cost(name, amount, *, attempt='1'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'gate-report-cloud.json').write_text(json.dumps({'finalCost': {'scope': 'run', 'runId': '42',
            'runAttempt': attempt, 'currency': 'USD', 'status': 'pass', 'amountUsd': amount}}))
    cost('cloud-gate-report-trial-1', '12')
    with pytest.raises(ValueError, match='missing'):
        trials.aggregate_costs(tmp_path, 2, '42')
    cost('cloud-gate-report-trial-2', '8')
    assert trials.aggregate_costs(tmp_path, 2, '42')['estimateUsd'] == '20'
    with pytest.raises(ValueError, match='exceeds'):
        trials.aggregate_costs(tmp_path, 2, '42', Decimal('19'))
    cost('cloud-gate-report-trial-02', '0')
    with pytest.raises(ValueError, match='duplicate'):
        trials.aggregate_costs(tmp_path, 2, '42')


def test_lambda_lock_binding_requires_manifest_bytes_and_observed_mirror(tmp_path):
    import hashlib
    import terminal_model_canary as canary
    index, source, mirror = ('sha256:' + c * 64 for c in 'abc')
    server = {'image': 'registry.example/honua:tag', 'digest': index,
              'awsLambdaImage': 'registry.example/lambda:tag', 'awsLambdaDigest': source,
              'awsLambdaEcrDigest': mirror}
    manifest = {'components': {'honua-server': server}}
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    manifest_hash = 'sha256:' + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lock = {'sourceInputs': {'platformManifest': {'sha256': manifest_hash}}, 'components': {'honua-server': {
        'artifacts': [{'kind': 'image', 'coordinate': 'registry.example/honua', 'digest': index}]}}}
    lock_path = tmp_path / 'lock.json'
    lock_path.write_text(json.dumps(lock))
    digest = 'sha256:' + hashlib.sha256(lock_path.read_bytes()).hexdigest()
    proof = {'reference': server['awsLambdaImage'] + '@' + source, 'verified': True,
             'source': 'lambda-function-readback', 'observedDigests': [mirror]}
    assert canary.verify_lock_binding(lock_path, digest, manifest, proof, manifest_path)['serverDigest'] == source
    with pytest.raises(canary.CanaryError):
        canary.verify_lock_binding(lock_path, digest, manifest, {**proof, 'observedDigests': [index]}, manifest_path)
    manifest_path.write_text('{}')
    with pytest.raises(canary.CanaryError):
        canary.verify_lock_binding(lock_path, digest, manifest, proof, manifest_path)


def test_lambda_readback_rejects_an_unexpected_ecr_digest():
    import types
    import terminal_model_ecs_image as cloud
    source, mirror = ('sha256:' + c * 64 for c in 'ab')
    manifest = {'components': {'honua-server': {'awsLambdaImage': 'registry.example/lambda:tag',
        'awsLambdaDigest': source, 'awsLambdaEcrDigest': mirror}}}
    class Target:
        _workdir = 'fixture'
        def _tf(self, root, *args):
            return types.SimpleNamespace(stdout='function')
    def response(digest):
        def run(cmd, **kwargs):
            body = {'Code': {'RepositoryType': 'ECR', 'ResolvedImageUri': 'ecr.example/server@' + digest},
                    'Configuration': {'PackageType': 'Image'}}
            return types.SimpleNamespace(stdout=json.dumps(body))
        return run
    assert cloud.observe_lambda(Target(), manifest, run=response(mirror))['observedDigests'] == [mirror]
    with pytest.raises(ValueError, match='verification failed'):
        cloud.observe_lambda(Target(), manifest, run=response(source))


def test_cloud_lock_transfer_uses_isolated_read_only_job_and_budget_splits():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cell = yaml.safe_load((root / '.github/workflows/e2e-cloud-aws-cell.yml').read_text())
    job = cell['jobs']['candidate-lock']
    assert job['permissions'] == {'actions': 'read'}
    assert all('RELEASE_GH_TOKEN' not in json.dumps(step) for step in job['steps'])
    assert 'candidate-lock' in cell['jobs']['journey']['needs']
    assert all('RELEASE_GH_TOKEN' not in json.dumps(step) for step in cell['jobs']['journey']['steps'])
    wf = yaml.safe_load((root / '.github/workflows/terminal-model-canary.yml').read_text())
    assert 'per_trial_ceiling' in wf['jobs']['aws-ecs']['with']['cost_ceiling_usd']
    aggregate = wf['jobs']['aggregate']
    assert any('aggregate' in str(step.get('run', '')) or '--cost-directory' in str(step.get('run', '')) for step in aggregate['steps'])
