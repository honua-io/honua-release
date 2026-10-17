"""The off profile proves actual refusal and fixture serving, never full GP completion."""
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deterministic_refusal_profile as profile

STAMP = '2026-10-17T10:00:00Z'
AFTER = '2026-10-17T10:01:00Z'
NOW = datetime(2026, 10, 17, 10, 2, tzinfo=timezone.utc)
SERVER = {'sourceSha': 'a' * 40, 'image': 'ghcr.io/honua/server@sha256:' + 'b' * 64}
REFUSAL = {'topology': 'redis-off', 'rulingStatus': 'ruled', 'tool': 'honua_execute_plan',
    'code': 'unavailable', 'missingDependency': 'redis', 'manifestCapability': 'jobs.runner',
    'manifestReasonCode': 'dependency-unavailable'}


@pytest.fixture
def sample(tmp_path):
    target = {'id': 'aws-ecs/redis-off', 'kind': 'aws-ecs', 'replicaBaseUrl': 'https://cell.example',
              'execution': {'buffer': {'point': [0, 0], 'srid': 3857, 'distance': 2}}}
    target_path = tmp_path / 'target-1.json'
    target_path.write_text(json.dumps(target))
    seed = tmp_path / 'extended/seed-manifest.json'
    seed.parent.mkdir()
    seed.write_text(json.dumps({'slice1': {'e2e_src_fs': {'service': 'e2e', 'layerId': 7}},
                                'layers': {'e2e_src_fs': 7}}))
    fixture = profile.oracle().fixture_binding(seed)
    raw = {'mode': 'live', 'status': 'blocked', 'generatedAt': STAMP, 'server': SERVER,
           'roster': {'status': 'pass'}, 'target': {'baseUrl': 'https://cell.example',
               'configSha256': profile.sha(target_path.read_bytes())},
           'stages': [{'number': n, 'status': 'pass' if n <= 2 else 'blocked',
                       'evidence': {'uri': 'urn:actual#honua-run=123/2', 'source': 'live-aws-ecs',
                           'freshness': 'verified-current', 'completeness': 'complete', 'observedAt': STAMP}}
                      for n in range(1, 9)]}
    contract = json.loads((profile.ROOT / 'certification/terminal-journey/journey.v1.json').read_bytes())
    for stage, descriptor in zip(raw['stages'], contract['stages'], strict=True):
        stage.update(stage=descriptor['id'], command=descriptor['command'])
    raw_bytes = json.dumps(raw).encode()
    raw_path = tmp_path / 'receipt-1.json'
    raw_path.write_bytes(raw_bytes)
    record = {'cell': 'aws-ecs/redis-off', 'number': 1, 'runId': '123', 'runAttempt': '2',
              'candidateDigest': 'c' * 64, 'receiptSha256': profile.sha(raw_bytes),
              'profileObservation': {'source': 'aws-control-plane', 'before': SERVER, 'after': SERVER,
                                     'observedAt': AFTER, 'seedManifestSha256': fixture['seedManifestSha256']}}
    public_fixture = {k: v for k, v in fixture.items() if k != 'expectedFeatures'}
    proof = {'schemaVersion': 'deterministic-redis-off-serving-refusal.v1', 'profile': profile.PROFILE,
        'mode': 'deterministic', 'status': 'pass', 'executionOutcome': 'refused', 'functionalGpExecuted': False,
        'cleanup': 'pass', 'cell': record['cell'], 'attempt': 1, 'runId': '123', 'runAttempt': '2',
        'candidateDigest': record['candidateDigest'], 'server': SERVER, 'rawReceiptSha256': profile.sha(raw_bytes),
        'targetConfigSha256': profile.sha(target_path.read_bytes()), 'sourceStatus': 'blocked',
        'planSha256': profile.sha(profile.canonical(profile.plan(target, '123', 1))), 'generatedAt': AFTER,
        'servingProof': {'fixtureBinding': public_fixture, 'featureCount': 2, 'featureIds': ['1', '2'],
                         'independentReadMatches': True, 'featuresSha256': 'd' * 64},
        'assertions': dict.fromkeys(profile.ASSERTIONS, 'pass'), 'refusalProof': REFUSAL}
    return proof, record, raw, raw_bytes, target_path


def validate(sample, **kwargs):
    proof, record, raw, raw_bytes, target_path = sample
    return profile.validate(proof, record, raw, raw_bytes, target_path, cell=record['cell'],
        digest='c' * 64, expected_server=SERVER, at=NOW, **kwargs)


def test_blocked_raw_is_preserved_and_never_claims_gp_execution(sample):
    before = copy.deepcopy(sample)
    assert validate(sample) == {'profile': 'redis-off-serving-refusal', 'executionOutcome': 'refused',
                                'functionalGpExecuted': False, 'completedAt': AFTER}
    assert sample == before
    assert sample[2]['status'] == 'blocked'
    assert sample[2]['stages'][4]['status'] == 'blocked'


@pytest.mark.parametrize('section,key,value', [
    (0, 'profile', 'full-workflow'), (0, 'mode', 'genuine-model'), (0, 'status', 'blocked'),
    (0, 'functionalGpExecuted', True), (0, 'executionOutcome', 'completed'), (0, 'cleanup', 'fail'),
    (0, 'candidateDigest', 'd' * 64), (0, 'runAttempt', '1'), (0, 'runId', '124'),
    (0, 'sourceStatus', 'pass'), (0, 'server', {**SERVER, 'sourceSha': 'f' * 40}),
    (0, 'rawReceiptSha256', 'e' * 64), (0, 'planSha256', 'e' * 64),
    (0, 'targetConfigSha256', 'f' * 64), (0, 'generatedAt', '2026-10-17T10:03:00Z'),
    (0, 'generatedAt', '2026-10-16T09:00:00Z'), (2, 'mode', 'build'), (2, 'status', 'fail'),
    (2, 'roster', {'status': 'blocked'}), (1, 'profileObservation', {}),
])
def test_rejects_wrong_or_missing_identity_status_and_freshness(sample, section, key, value):
    sample[section][key] = value
    with pytest.raises((ValueError, KeyError)):
        validate(sample)


@pytest.mark.parametrize('mutation', ['wrong-after', 'wrong-before', 'future-after', 'old-after', 'failed-stage',
    'blocked-auth', 'wrong-job', 'harness-source', 'missing-refusal', 'job-id', 'nested-operation',
    'wrong-service', 'wrong-layer', 'empty-serving', 'wrong-ids', 'denial-missing', 'catalog-missing'])
def test_rejects_actual_oracle_security_and_native_observation_gaps(sample, mutation):
    proof, record, raw, _, _ = sample
    if mutation in ('wrong-after', 'wrong-before'):
        record['profileObservation'][mutation.split('-')[1]] = {**SERVER, 'image': 'wrong'}
    elif mutation.endswith('-after'):
        record['profileObservation']['observedAt'] = '2026-10-17T10:03:00Z' if mutation == 'future-after' else STAMP
    elif mutation == 'failed-stage': raw['stages'][2]['status'] = 'fail'
    elif mutation == 'blocked-auth': raw['stages'][1]['status'] = 'blocked'
    elif mutation == 'wrong-job': raw['stages'][0]['evidence']['uri'] = 'urn:actual#honua-run=123/1'
    elif mutation == 'harness-source': raw['stages'][0]['evidence']['source'] = 'harness-build'
    elif mutation == 'missing-refusal': proof['refusalProof'] = {}
    elif mutation == 'job-id': proof['refusalProof'] = {**REFUSAL, 'jobId': 'real-job'}
    elif mutation == 'nested-operation': proof['refusalProof'] = {**REFUSAL, 'raw': json.dumps({'operationId': 'op'})}
    elif mutation == 'wrong-service': proof['servingProof']['fixtureBinding']['serviceId'] = 'irrelevant'
    elif mutation == 'wrong-layer': proof['servingProof']['fixtureBinding']['layerId'] = 8
    elif mutation == 'empty-serving': proof['servingProof']['featureCount'] = 0
    elif mutation == 'wrong-ids': proof['servingProof']['featureIds'] = ['7', '8']
    elif mutation == 'denial-missing': proof['assertions'].pop('viewerDenial')
    elif mutation == 'catalog-missing': proof['assertions']['catalogExact'] = 'fail'
    with pytest.raises((ValueError, KeyError)):
        validate(sample)


def test_seed_manifest_changes_invalidate_retained_serving_proof(sample):
    seed = sample[4].parent / 'extended/seed-manifest.json'
    seed.write_text(seed.read_text() + '\n')
    with pytest.raises(ValueError, match='authored candidate fixture'):
        validate(sample)


@pytest.mark.parametrize('cell', ['aws-ecs/redis-on', 'aws-serverless/redis-on', 'aws-eks/redis-off'])
def test_refusal_sidecar_cannot_admit_full_workflow_or_preview_cells(sample, cell):
    sample[1]['cell'] = cell
    with pytest.raises(ValueError): validate(sample)


class Adapter:
    def __init__(self, proof): self.proof, self.calls, self.cleanup = proof, [], 'pass'
    def op_setup(self, request): self.calls.append(('setup', request)); return {'status': 'ready', 'workspaceId': 'owned'}
    def op_execute(self, request):
        self.calls.append(('execute', request))
        result = {'accepted': False, 'executionOutcome': 'refused', 'functionalGpExecuted': False,
                  'typedRefusal': self.proof['refusalProof']} if request['action']['tool'] == 'honua_execute_plan' else {}
        return {'status': 'pass', 'result': result}
    def op_verify(self, request):
        return {'status': 'pass', 'executionOutcome': self.proof['executionOutcome'],
                'functionalGpExecuted': self.proof['functionalGpExecuted'], 'assertions': self.proof['assertions'],
                'servingProof': self.proof['servingProof'], 'typedRefusal': self.proof['refusalProof']}
    def op_teardown(self, request): self.calls.append(('teardown', request)); return {'status': self.cleanup}


def produce(sample, adapter):
    _, record, _, _, path = sample
    # Deployment prerequisite testing belongs to its own focused helper suite; these controls
    # exercise the fixed producer actions and fail-closed independently verified results.
    with mock.patch('tools.terminal_model_canary.validate_deterministic_receipt', return_value={'status': 'pass'}):
        return profile.produce(record, path.parent / 'receipt-1.json', path,
            adapter=adapter, pinned={}, digest='c' * 64, cell=record['cell'])


def test_producer_issues_authored_serving_and_valid_durable_request_then_cleans_principals(sample):
    adapter = Adapter(sample[0])
    result = produce(sample, adapter)
    assert result['status'] == 'pass' and result['functionalGpExecuted'] is False
    actions = [request['action'] for operation, request in adapter.calls if operation == 'execute']
    assert [action['tool'] for action in actions] == ['honua_query_features', 'honua_execute_plan', 'honua_list_capabilities']
    assert actions[0]['arguments']['serviceId'] == 'e2e' and actions[0]['arguments']['layerId'] == 7
    assert actions[1]['arguments']['plan']['steps'][0]['inputs'] == {
        'wkb': 'AQEAAAAAAAAAAAAAAAAAAAAAAAAA', 'srid': '3857', 'distance': '2', 'geodesic': 'false'}
    assert adapter.calls[-1] == ('teardown', {'workspaceId': 'owned'})


@pytest.mark.parametrize('fault', ['accepted-gp', 'no-denial', 'wrong-capability', 'cleanup'])
def test_producer_fails_for_real_execution_security_or_cleanup_contradiction(sample, fault):
    adapter = Adapter(sample[0])
    if fault == 'accepted-gp': sample[0]['functionalGpExecuted'] = True
    elif fault == 'no-denial': sample[0]['assertions']['anonymousDenial'] = 'fail'
    elif fault == 'wrong-capability': sample[0]['refusalProof'] = {**REFUSAL, 'manifestCapability': 'other'}
    else: adapter.cleanup = 'fail'
    result = produce(sample, adapter)
    assert result['status'] == 'fail'
    assert adapter.calls[-1][0] == 'teardown'


@pytest.mark.parametrize('change', ['empty', 'short', 'reverse', 'wrong-id', 'wrong-command', 'wrong-number'])
def test_standalone_validator_rejects_incomplete_or_relabelled_canonical_source(sample, change):
    stages = sample[2]['stages']
    if change == 'empty': stages.clear()
    elif change == 'short': stages.pop()
    elif change == 'reverse': stages.reverse()
    elif change == 'wrong-id': stages[0]['stage'] = 'different'
    elif change == 'wrong-command': stages[0]['command'] = 'different'
    else: stages[0]['number'] = 7
    with pytest.raises(ValueError, match='complete ordered canonical journey'):
        validate(sample)


@pytest.mark.parametrize('mutation', ['driver', 'raw-job', 'seed-handoff', 'wrong-buffer'])
def test_refusal_cannot_claim_model_credit_or_forge_fixture_execution(sample, mutation):
    proof, record, raw, _, target = sample
    if mutation == 'driver': record['driver'] = 'genuine-model'
    elif mutation == 'raw-job': raw['stages'][4]['jobId'] = 'actually-executed'
    elif mutation == 'seed-handoff': record['profileObservation']['seedManifestSha256'] = 'e' * 64
    else:
        value = json.loads(target.read_bytes()); value['execution']['buffer']['distance'] = 200
        target.write_text(json.dumps(value))
        proof['targetConfigSha256'] = raw['target']['configSha256'] = profile.sha(target.read_bytes())
        proof['planSha256'] = profile.sha(profile.canonical(profile.plan(value, '123', 1)))
    with pytest.raises(ValueError): validate(sample)


def attach_fixture_profile(directory, raw, record):
    """Explicit test-only modernization of legacy full-journey fixture inputs.

    No production/recorded evidence is rewritten. Each sidecar is a hand-authored proof for
    the maintained fixture; negative tests mutate those bytes or identity fields afterwards.
    """
    number, cell = record['number'], record['cell']
    target = {'id': cell, 'kind': cell.split('/')[0], 'replicaBaseUrl': 'https://cell.invalid',
              'execution': {'buffer': {'point': [0, 0], 'srid': 3857, 'distance': 2}}}
    target_path = directory / f'target-{number}.json'
    target_path.write_text(json.dumps(target))
    seed = directory / 'extended/seed-manifest.json'
    seed.parent.mkdir(exist_ok=True)
    seed.write_text(json.dumps({'slice1': {'e2e_src_fs': {'service': 'e2e', 'layerId': 7}},
                                'layers': {'e2e_src_fs': 7}}))
    fixture = profile.oracle().fixture_binding(seed)
    raw['target'].update(baseUrl='https://cell.invalid', redisTopology='redis-off', configSha256=profile.sha(target_path.read_bytes()))
    if raw['status'] == 'pass':
        gp = raw['stages'][4]
        for key in ('jobId', 'operationId', 'operationInstanceId', 'jobStatus', 'resourceUri', 'jobCreatedAt'):
            gp[key] = None
        gp['topologyRefusal'] = {**REFUSAL, 'ruling': 'redis-governed-control-plane-2026-10-08',
                                 'deploymentEnvironment': 'test'}
    raw_bytes = json.dumps(raw).encode()
    record['receiptSha256'] = profile.sha(raw_bytes)
    proof = {'schemaVersion': 'deterministic-redis-off-serving-refusal.v1', 'profile': profile.PROFILE,
        'mode': 'deterministic', 'cell': cell, 'runId': record['runId'], 'runAttempt': record['runAttempt'],
        'attempt': number, 'candidateDigest': record['candidateDigest'], 'server': raw['server'],
        'status': 'pass' if raw['status'] == 'pass' else 'fail', 'sourceStatus': raw['status'],
        'executionOutcome': 'refused', 'functionalGpExecuted': False, 'cleanup': 'pass',
        'generatedAt': raw['generatedAt'], 'rawReceiptSha256': profile.sha(raw_bytes),
        'targetConfigSha256': profile.sha(target_path.read_bytes()),
        'planSha256': profile.sha(profile.canonical(profile.plan(target, record['runId'], number))),
        'assertions': dict.fromkeys(profile.ASSERTIONS, 'pass'), 'refusalProof': REFUSAL,
        'servingProof': {'fixtureBinding': {k:v for k,v in fixture.items() if k != 'expectedFeatures'},
                        'featureCount': 2, 'featureIds': ['1','2'], 'independentReadMatches': True,
                        'featuresSha256': 'd'*64}}
    path = directory / f'serving-refusal-{number}.json'
    path.write_text(json.dumps(proof))
    record['servingRefusal'] = {'file': path.name, 'sha256': profile.sha(path.read_bytes())}
    record['profileObservation'] = {'source': 'aws-control-plane', 'before': raw['server'], 'after': raw['server'],
        'observedAt': raw['generatedAt'], 'seedManifestSha256': fixture['seedManifestSha256']}
    return raw_bytes


@pytest.mark.parametrize('extra', [{'resourceUri': 'honua://jobs/real-job'}, {'jobCreatedAt': STAMP},
    {'encoded': json.dumps({'nested': {'resourceUri': 'honua://jobs/real-job'}})}])
def test_refusal_rejects_nested_or_encoded_partial_job_evidence(extra):
    with pytest.raises(ValueError): profile.check_refusal({**REFUSAL, **extra})


@pytest.mark.parametrize('artifact', ['receipt-1.json', 'target-1.json', 'serving-refusal-1.json', 'extended/seed-manifest.json'])
def test_adjacent_profile_artifacts_cannot_be_symlinks(sample, artifact, tmp_path):
    proof, record, raw, raw_bytes, target = sample
    sidecar = tmp_path / 'serving-refusal-1.json'
    sidecar.write_text(json.dumps(proof))
    record['servingRefusal'] = {'file': sidecar.name, 'sha256': profile.sha(sidecar.read_bytes())}
    file = tmp_path / artifact
    outside = tmp_path.parent / (tmp_path.name + '-external.json')
    outside.write_bytes(file.read_bytes()); file.unlink(); file.symlink_to(outside)
    pinned = {'components': {'honua-server': {'sha': SERVER['sourceSha'],
        'image': 'ghcr.io/honua/server', 'digest': 'sha256:' + 'b'*64}}}
    try:
        with pytest.raises(ValueError, match='unsafe or missing'):
            profile.validate_record(record, raw, tmp_path / 'receipt-1.json', cell=record['cell'],
                pinned=pinned, digest='c'*64, at=NOW)
    finally: outside.unlink()


def test_real_profile_driver_setup_and_execute_receive_only_private_tenant_context(tmp_path, monkeypatch):
    import os
    import cloud_journey as cloud
    driver, adapter = cloud.drivers()
    target = cloud.cloud_targets().build(json.loads(adapter.DEFAULT_TARGET.read_bytes()),
        cell='aws-ecs/redis-off', endpoint='https://cell.example', tenant_fixture=True)
    path = tmp_path / 'target.json'; path.write_text(json.dumps(target))
    seed = tmp_path / 'extended/seed-manifest.json'; seed.parent.mkdir()
    seed.write_text(json.dumps({'slice1': {'e2e_src_fs': {'service': 'e2e', 'layerId': 7}}, 'layers': {'e2e_src_fs': 7}}))
    manifest = cloud.manifest(); image = cloud.candidate_image(target['id'], manifest)
    deployment = tmp_path / 'deployment.json'
    deployment.write_text(json.dumps({'status': 'pass', 'target': {'id': target['id'],
        'baseUrl': target['replicaBaseUrl'], 'configSha256': profile.sha(path.read_bytes())},
        'server': {'sourceSha': manifest['components']['honua-server']['sha'], 'image': image},
        'stages': [{'number': 1, 'status': 'pass', 'checks': [{'id': '1.1-candidate-image', 'status': 'pass'}],
                    'evidence': {'source': 'live-aws-ecs', 'freshness': 'verified-current'}}]}))
    workspace = mock.Mock(status='pass', install_notes=[])
    workspace.as_receipt.return_value = {'status': 'pass'}
    stages = sys.modules['stages']
    observation = stages.Observation(ready=True, setup_view_present=True)
    key = 'a'*64; seen = []
    def credentials(target, workdir, base_url, **kwargs):
        assert target['tenantFixture']
        assert kwargs.get('tenant_signing_key') == key
        assert not any(name.startswith(('AWS_', 'ACTIONS_ID_TOKEN_REQUEST_', 'HONUA_AWS_'))
                       or name in ('GH_TOKEN', 'GITHUB_TOKEN') for name in os.environ)
        seen.append(kwargs.get('mint', False))
        return {'operator': 'run-owned-operator'}
    engine = mock.Mock()
    engine.view.side_effect = RuntimeError('stop after real execution credential handoff')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'must-strip')
    monkeypatch.setenv('GH_TOKEN', 'must-strip')
    monkeypatch.setenv('HONUA_CLOUD_JOURNEY_ADMIN', 'app-only-key')
    with mock.patch.object(adapter, 'STATE_ROOT', tmp_path / 'sessions'), \
         mock.patch.object(adapter.pins, 'resolve_client_workspace', return_value=workspace), \
         mock.patch.object(adapter.pins, 'install_executables', return_value=(tmp_path / 'bin', '', [])), \
         mock.patch.object(driver, 'observe', return_value=observation), \
         mock.patch.object(adapter.local_fixture, 'credentials', side_effect=credentials), \
         mock.patch.object(adapter, 'Transport'), \
         mock.patch.object(adapter.executor, 'JourneyExecutor', return_value=engine), \
         mock.patch.object(adapter.local_fixture, 'revoke', return_value=([], [])):
        with driver.pins.candidate_sandbox(), cloud.tenant_application_key(key):
            setup = adapter.op_setup({'target': str(path), 'profile': profile.PROFILE,
                'deploymentReceipt': {'path': str(deployment), 'sha256': profile.sha(deployment.read_bytes())},
                'servingFixture': {'path': str(seed), 'sha256': profile.sha(seed.read_bytes())}})
            assert setup['status'] == 'ready'
            with pytest.raises(RuntimeError, match='real execution credential handoff'):
                adapter.op_execute({'workspaceId': setup['workspaceId'], 'profile': profile.PROFILE,
                                    'stage': 'serving-read', 'action': {}})
            state_bytes = adapter._state_path(setup['workspaceId']).read_bytes()
            assert key.encode() not in state_bytes and b'must-strip' not in state_bytes
            assert adapter.op_teardown({'workspaceId': setup['workspaceId']})['status'] == 'pass'
        assert 'HONUA_CANARY_TENANT_SIGNING_KEY' not in os.environ
    assert seen == [True, False]
