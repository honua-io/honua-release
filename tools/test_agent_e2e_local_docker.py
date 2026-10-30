"""Local-docker adapters and the P10 A/B development case. Not a live receipt."""
import copy
import json
import os
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent_e2e_campaign as producer
from agent_e2e_local_docker_lifecycle import COMMAND, LocalDockerLifecycle, refuse_unreviewed
from agent_e2e_native_bindings import validate_case
from agent_e2e_pack import PackError, load_json
from agent_e2e_p10_local_docker import Session
from agent_e2e_scenario_approval import approve_once
from agent_e2e_scenario_batch import observe as observe_batch
from agent_e2e_scenario_fault import arm
from agent_e2e_scenario_install import bind
from agent_e2e_scenario_render import observe as observe_render
from agent_e2e_state import DurableExecution
from test_agent_e2e_backend import Store, CANDIDATE, reserve

ROOT = Path(__file__).resolve().parents[1]
EXPECTATIONS = ROOT/'certification/agent-e2e/expectations/p10-local-docker.json'
CASE = 'P10:A:local-docker'
IMAGE = 'sha256:'+'ab'*32
PRODUCT = 'sha256:'+'cd'*32
CONTAINER = 'ef'*32
BASIS = 'sha256:'+'b'*64


def png(red, green, blue):
    def chunk(kind, data):
        return struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    raw = b'\x00'+bytes((red, green, blue))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))+
            chunk(b'IDAT', zlib.compress(raw))+chunk(b'IEND', b''))


def campaign():
    now = datetime.now(timezone.utc)
    return {'campaignId': 'offline-campaign', 'campaignMode': 'development', 'componentBindingDigest': CANDIDATE,
            'startedAt': (now-timedelta(minutes=1)).isoformat(), 'expiresAt': (now+timedelta(days=1)).isoformat()}


def deployment():
    now = datetime.now(timezone.utc)
    return {'schemaVersion': 1, 'target': 'local-docker', 'componentBindingDigest': CANDIDATE,
            'endpoint': 'http://127.0.0.1:9', 'observedAt': now.isoformat(), 'nativeResponse': [{
                'Id': '12'*32, 'Image': PRODUCT, 'State': {'Running': True},
                'HostConfig': {'Privileged': False, 'Binds': []}}]}


def worker(attempt_id='attempt-local'):
    return {'candidateKey': CANDIDATE, 'attemptId': attempt_id, 'image': IMAGE, 'network': 'honua_candidate',
            'context': {'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1, 'campaignMode': 'development'},
            'reservationIds': {'infrastructure': 'infrastructure-1', 'cleanup': 'cleanup-1'},
            'liability': {'basisDigest': 'sha256:'+'c'*64, 'maximumInfrastructureUsd': '0.20', 'maximumCleanupUsd': '0.20'},
            'transport': {'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/585192672263/'+attempt_id+'-commands.fifo',
                          'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/585192672263/'+attempt_id+'-replies.fifo',
                          'attemptId': attempt_id, 'expiresEpoch': int(time.time())+1000}}


def observed_container(config, *, running):
    return {'Id': CONTAINER, 'Image': IMAGE, 'Name': '/honua-agent-'+config['attemptId'],
            'Config': {'Cmd': list(COMMAND), 'Env': ['PYTHONDONTWRITEBYTECODE=1', *LocalDockerLifecycle(config).environment]},
            'HostConfig': {'Privileged': False, 'NetworkMode': config['network'], 'Binds': []},
            'State': {'Running': running, 'Status': 'running' if running else 'exited'}}


@pytest.fixture
def durable(tmp_path):
    execution = DurableExecution(Store(), tmp_path/'ledger.json', initialize=True)
    reserve(execution)
    try:
        yield execution
    finally:
        execution.close()


def corpus_oracle():
    import importlib.util
    path = ROOT/'certification/agent-e2e/corpus/oracle.py'
    spec = importlib.util.spec_from_file_location('agent_e2e_corpus_oracle', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_p10_expectations_match_corpus_and_pack():
    document = load_json(EXPECTATIONS)
    assert set(document) == {'cases'}
    import agent_e2e_gate as gate
    inventory = gate.inventory(load_json(producer.PACK))
    oracle = corpus_oracle()
    for case_id, value in document['cases'].items():
        validate_case(value)
        assert set(value['assertions']) == set(inventory[case_id]['assertions'])
        variant = case_id.split(':')[1]
        assert value['assertions']['filter.exact'] == oracle.fixture_expectation(variant, 'filter.exact')
        assert value['cleanup'] == {'resourceIds': []}
        resources = value['assertions']['source-content.unchanged']['resources']
        assert resources[0]['resourceId'] == 'shelters' and resources[0]['content']['features']


def test_second_launch_is_refused_and_host_flags_never_run(durable):
    config = worker()
    running = True
    calls = []
    def runner(argv):
        nonlocal running
        calls.append(list(argv))
        if argv[1] == 'run':
            return (CONTAINER+'\n').encode()
        if argv[1:] == ['stop', '--time', '10', 'honua-agent-attempt-local']:
            running = False
            return b''
        return json.dumps([observed_container(config, running=running)]).encode()
    lifecycle = LocalDockerLifecycle(config, runner=runner)
    lifecycle.claim(durable)
    assert calls == []
    started = lifecycle.start(durable)
    assert started['containerId'] == CONTAINER and calls[0][0] == 'docker'
    assert COMMAND[-1] in calls[0] and 'agent_e2e_public_entry.py' not in calls[0]
    assert calls[0].count('-e') == 2 and calls[1][1] == 'inspect'
    with pytest.raises(PackError, match='already started'):
        lifecycle.start(durable)
    assert [call[1] for call in calls].count('run') == 1
    stopped = lifecycle.stop(durable)
    assert stopped['running'] is False
    with pytest.raises(PackError, match='host'):
        refuse_unreviewed(['docker', 'run', '--volume', '/var/run/docker.sock:/var/run/docker.sock', IMAGE])


def test_lost_launch_reply_adopts_the_inspected_container_without_running_again(durable):
    config = worker()
    calls = []
    def runner(argv):
        calls.append(list(argv))
        require_inspect = argv[:2] == ['docker', 'inspect']
        assert require_inspect
        return json.dumps([observed_container(config, running=True)]).encode()
    lifecycle = LocalDockerLifecycle(config, runner=runner)
    lifecycle.claim(durable)
    durable.state['dockerLaunches'][config['attemptId']]['started'] = True
    durable._persist()
    started = lifecycle.start(durable)
    assert started['containerId'] == CONTAINER and started['started'] is True
    assert [call[1] for call in calls] == ['inspect']


def test_verify_fails_closed_without_a_separate_source_snapshot(tmp_path, durable):
    (tmp_path/'expectations.json').write_bytes(EXPECTATIONS.read_bytes())
    expected = load_json(EXPECTATIONS)['cases'][CASE]
    snapshot = expected['assertions']['source-content.unchanged']['resources'][0]
    session = Session(durable=durable, campaign=campaign(), bundle=tmp_path,
                      configuration={'cases': {CASE: {'deployment': deployment(), 'worker': worker()}}},
                      context={'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1, 'campaignMode': 'development'})
    session.retain('town', {'startingState': 'published-town'})
    session.retain('source-before', {'snapshot': snapshot})
    session.retain('query-ordinal-1', {'ordinal': 1, 'result': expected['assertions']['filter.exact']})
    blocked = session.verify()
    assert blocked['qualification'] is False and blocked['status'] == 'fail'
    assert blocked['failedAssertions'] == ['source-content.unchanged']


def test_verify_accepts_an_independent_post_operation_snapshot(tmp_path, durable):
    (tmp_path/'expectations.json').write_bytes(EXPECTATIONS.read_bytes())
    expected = load_json(EXPECTATIONS)['cases'][CASE]
    snapshot = expected['assertions']['source-content.unchanged']['resources'][0]
    session = Session(durable=durable, campaign=campaign(), bundle=tmp_path,
                      configuration={'cases': {CASE: {'deployment': deployment(), 'worker': worker()}}},
                      context={'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1, 'campaignMode': 'development'})
    session.retain('town', {'startingState': 'published-town'})
    session.retain('source-before', {'snapshot': snapshot})
    session.retain('source-after', {'snapshot': snapshot})
    session.retain('query-ordinal-1', {'ordinal': 1, 'result': expected['assertions']['filter.exact']})
    passed = session.verify()
    assert passed['status'] == 'pass' and passed['failedAssertions'] == [] and passed['qualification'] is False


def test_seed_failure_does_not_start_the_worker(tmp_path, durable):
    calls = []
    session = Session(durable=durable, campaign=campaign(), bundle=tmp_path,
                      configuration={'cases': {CASE: {'deployment': deployment(), 'worker': worker()}}},
                      context={'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1, 'campaignMode': 'development'},
                      runner=lambda argv: calls.append(argv))
    def seed():
        raise PackError('published town was not observed')
    with pytest.raises(PackError, match='published town'):
        session.prepare(seed=seed)
    assert calls == []
    assert durable.state['dockerLaunches']['attempt-local']['started'] is False
    with pytest.raises(PackError, match='already retained'):
        session.prepare(seed=lambda: {'startingState': 'published-town'})


def test_p10_refuses_held_out_and_other_targets(tmp_path, durable):
    configuration = {'cases': {CASE: {'deployment': deployment(), 'worker': worker()}}}
    context = {'campaignId': 'offline-campaign', 'caseId': 'P10:C:local-docker', 'attempt': 1, 'campaignMode': 'development'}
    with pytest.raises(PackError, match='A/B'):
        Session(durable=durable, campaign=campaign(), bundle=tmp_path, configuration=configuration, context=context)
    context['caseId'] = 'P10:A:aws-ecs-redis-on'
    with pytest.raises(PackError, match='A/B'):
        Session(durable=durable, campaign=campaign(), bundle=tmp_path, configuration=configuration, context=context)


def test_install_binds_the_running_image_and_refuses_a_privileged_container():
    provenance = {'componentBindingDigest': CANDIDATE, 'serverSourceRevision': 'a'*40,
                  'serverOciDigest': PRODUCT, 'clientArtifactDigests': {'studio': 'sha256:'+'11'*32}}
    health = {'status': 'ok', 'authenticatedReadStatus': 200}
    bound = bind(deployment=deployment(), campaign=campaign(), provenance=provenance, health=health)
    assert bound['qualification'] is False
    assert bound['deployment.ready']['deploymentId'] == 'docker:'+'12'*32
    assert bound['candidate.identity']['serverOciDigest'] == PRODUCT
    privileged = deployment()
    privileged['nativeResponse'][0]['HostConfig']['Privileged'] = True
    with pytest.raises(PackError, match='privileged'):
        bind(deployment=privileged, campaign=campaign(), provenance=provenance, health=health)


def test_approval_uses_a_different_secret_and_is_not_publication(monkeypatch):
    monkeypatch.setenv('REQUESTER_TOKEN', 'requester-secret')
    monkeypatch.setenv('APPROVER_TOKEN', 'approver-secret')
    calls = []
    class Http:
        def json(self, step, method, path, body=None):
            calls.append((step, method, path))
            return {'reference': {'key': 'approval', 'sha256': BASIS}, 'observation': {'httpStatus': 202, 'body': {}}}
    pending = {'status': 'awaiting-approval', 'proposalId': 'proposal-1'}
    result = approve_once(requester_credential_env='REQUESTER_TOKEN', approver_credential_env='APPROVER_TOKEN',
                          approver_http=Http(), pending=pending, requested_by='worker-principal')
    assert result['qualification'] is False and result['httpStatus'] == 202
    assert calls == [('approve-proposal-1', 'POST', '/api/v1/admin/proposals/proposal-1/approve')]
    monkeypatch.setenv('APPROVER_TOKEN', 'requester-secret')
    with pytest.raises(PackError, match='identical'):
        approve_once(requester_credential_env='REQUESTER_TOKEN', approver_credential_env='APPROVER_TOKEN',
                     approver_http=Http(), pending=pending, requested_by='worker-principal')


def test_fault_arms_once_after_commit_and_does_not_replay(durable):
    calls = []
    def injector(record):
        calls.append(copy.deepcopy(record))
        return {'injectionId': 'inject-1', 'status': 503, 'count': 1}
    context = {'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1}
    from agent_e2e_scenario_fault import EXPECTED
    commit = {'resourceId': 'map-1', 'committed': True}
    armed = arm(durable=durable, context=context, fault=EXPECTED, commit=commit, injector=injector)
    assert armed['qualification'] is False and armed['injectionId'] == 'inject-1' and len(calls) == 1
    assert calls[0]['injectionId'] is None
    with pytest.raises(PackError, match='already armed'):
        arm(durable=durable, context=context, fault=EXPECTED, commit=commit, injector=injector)
    assert len(calls) == 1


def test_render_decodes_png_and_refuses_a_screenshot_path():
    observed = observe_render(png=png(0, 0, 0), http_status=200, browser={
        'loadedLayerIds': ['shelters'], 'browserErrors': [], 'regions': {}, 'visibleLabelSourceIds': []})
    assert observed['qualification'] is False and observed['decodedWidth'] == 1 and observed['nonBackgroundPixels'] == 1
    with pytest.raises(PackError, match='screenshot'):
        observe_render(png=png(255, 255, 255), http_status=200, browser={'screenshotPath': 'shot.png'})


def test_batch_refuses_local_docker_without_a_substitute_job():
    with pytest.raises(PackError, match='no native AWS Batch'):
        observe_batch('B05:A:local-docker', 'local-docker')
    with pytest.raises(PackError, match='not implemented'):
        observe_batch('B05:A:aws-batch', 'aws-batch')


def test_development_scope_capabilities_are_present_for_the_local_case():
    pack = load_json(producer.PACK)
    selected, required, _ = producer.execution_scope(pack, {'campaignMode': 'development'}, None, 0, 6, [CASE])
    assert selected == [CASE]
    from agent_e2e_controller import Backend
    assert required <= Backend.REQUIRED | {'scenario-setup', 'independent-oracles', 'durable-attempt-evidence',
                                           'independent-cost-observations', 'independent-expiry-cleanup'}
    assert 'native-batch' not in required
