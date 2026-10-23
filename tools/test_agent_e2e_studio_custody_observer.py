"""Local original-HTTP and SDK custody controls; no hosted qualification."""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

import pytest

import agent_e2e_studio_custody_observer as custody
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server
from test_agent_e2e_studio_backend import publication_session, studio


@pytest.fixture
def observed(adapter, server, tmp_path, monkeypatch):
    real = custody.helpers()
    prepare, validate_retirement, verify_definition, retained_context, verify_injected = real
    from controller_archive import bundle_bytes, canonical, digest
    from controller_entry import file_manifest
    from test_setup_key_retirement import SetupRetirement
    retirement = SetupRetirement(); retirement.setUp()
    config = retirement.config
    candidate = config['candidateKey']
    config['endpoint'] = server.url
    # Only this contract fixture uses a loopback native API. The production
    # validator retains its mandatory HTTPS pilot origin without a bypass.
    def validate_loopback(value, **kwargs):
        assert value['endpoint'] == server.url
        validate_retirement(value | {'endpoint': 'https://agent.pilot.demo.honua.io'}, **kwargs)
    monkeypatch.setattr(custody, 'helpers', lambda: (prepare, validate_loopback, verify_definition, retained_context, verify_injected))
    session = SimpleNamespace(durable=adapter.durable, campaign={'componentBindingDigest': candidate})
    session.attempt_id = 'custody'
    session.context = {**adapter.http.context, 'campaignMode': 'development'}
    session.started_at = (datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
    session.durable._fixture_identity = lambda context: canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    state = session.durable.state
    state['scenarioOperations'] = copy.deepcopy(retirement.durable.state['scenarioOperations'])
    state['studioBootstrapFixtures'] = copy.deepcopy(retirement.durable.state['studioBootstrapFixtures'])
    fixture = state['studioBootstrapFixtures'][config['fixtureReference']['key']]
    agent = next(r['value'] for k, r in state['scenarioOperations'].items() if k.endswith('/key-agent'))
    agent.update(secretArn=config['setupKey']['secretArn'].replace('/setup-', '/agent-'), versionId='f'*64,
                 permissions=['admin:read', 'agent:workspace', *agent['permissions'][1:]])
    agent_identity = custody.public_key_id(agent['apiKeyId'])
    setup = next(r['value'] for k, r in state['scenarioOperations'].items() if k.endswith('/key-setup'))
    setup_identity = custody.public_key_id(setup['apiKeyId'])
    native_prefix = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': candidate,
                                     'operationId': config['nativeBootstrapOperationId']})
    phase = canonical_digest({'kind': 'actual-context-contract'})
    reference = {'key': phase+'/completed'}
    workspace = {'workspaceId': 'observed-workspace', 'viewId': 'setup', 'targetId': 'observed-target', 'revision': 0}
    turn = {'turnId': 'observed-turn', 'status': 'Active', 'context': workspace}
    principal = {'actorId': 'apikey:api-key:'+agent_identity, 'studioOwnerId': agent_identity,
                 'tenantId': 'public', 'isHumanSession': False, 'authenticationKind': 'api-key'}
    human = {'actorId': 'human-subject', 'studioOwnerId': 'opaque-human-owner', 'tenantId': 'public',
             'isHumanSession': True, 'authenticationKind': 'operator-bearer'}
    status = SimpleNamespace(setup='active', refusal=401, wrong_principal=False)
    monkeypatch.setenv('CUSTODY_OLD_KEY', retirement.setup)
    monkeypatch.setenv('CUSTODY_TASK_KEY', 'contract-agent-private-key-'+'x'*48)
    monkeypatch.setenv('CUSTODY_HUMAN', 'contract-human-private-bearer-'+'x'*48)
    def http(role):
        return ScenarioHttp(durable=session.durable, context=adapter.http.context, endpoint=server.url,
            credential_env={'setup': 'CUSTODY_OLD_KEY', 'task': 'CUSTODY_TASK_KEY', 'human': 'CUSTODY_HUMAN'}.get(role, 'SCENARIO_TEST_TOKEN'),
            credential_kind='bearer' if role == 'human' else 'api-key',
            attempt_id='custody-custody-task' if role == 'task' else 'custody-'+role)
    def respond(method, path, body):
        assert method == 'GET'
        headers = {k.lower(): v for k,v in server.seen[-1][2].items()}
        old = headers.get('x-api-key') == retirement.setup
        if path.endswith('/effective-permissions'):
            if old and status.setup == 'revoked': return status.refusal, {}, b'{"status":401}'
            value = {'id': setup_identity, 'status': status.setup, 'permissions': setup['permissions'],
                     'canAuthenticate': status.setup == 'active'}
        elif path == '/api/v1/agent/target':
            if old and status.setup == 'revoked': return status.refusal, {}, b'{"status":401}'
            who = human if headers.get('authorization') else principal
            value = {'targetId': workspace['targetId'], 'principal': copy.deepcopy(who)}
            if status.wrong_principal: value['principal']['actorId'] = 'unrelated-agent'
        elif '/turns/' in path: value = turn
        else: value = workspace
        return 200, {}, json.dumps(value).encode()
    server.respond = respond
    context = {'schema': 'honua.native-agent-context/v1', 'candidateKey': candidate, 'issuedAgentKey': agent,
        'studioOwnerPrincipalId': agent_identity, 'workspaceAgentPrincipalId': principal['actorId'],
        'agentPrincipal': principal, 'humanCreatorPrincipal': human, 'target': {'targetId': workspace['targetId'], 'principal': human},
        'nativeBootstrapReference': {'key': native_prefix+'/key-agent',
            'sha256': canonical_digest(state['scenarioOperations'][native_prefix+'/key-agent'])},
        'agentContext': {'workspaceId': workspace['workspaceId'], 'turnId': turn['turnId']},
        'workspace': workspace, 'turn': turn, 'nativeOriginals': {}}
    for name, path, role in [('target', '/api/v1/agent/target', 'human'), ('agentTarget', '/api/v1/agent/target', 'task'),
            ('workspaceReadback', '/api/v1/agent/workspaces/observed-workspace?viewId=setup', 'task'),
            ('turnReadback', '/api/v1/agent/workspaces/observed-workspace/turns/observed-turn', 'task')]:
        transport = http(role); transport.identity['attemptId'] = 'native-context-'+phase[7:31]
        transport.client.identity['attemptId'] = transport.identity['attemptId']
        context['nativeOriginals'][name] = transport.json('context-'+name.lower(), 'GET', path)
    reference['sha256'] = canonical_digest(context)
    state['studioBootstrapOperations'] = {reference['key']: context}
    prefix = canonical_digest({'kind': 'setup-key-retirement', 'candidateKey': candidate,
        'operationId': config['operation']['operationId'], 'fixture': config['fixtureReference']})
    producer_task = config['clusterArn'].replace(':cluster/', ':task/')+'/'+'1'*32
    definition_arn = config['taskDefinitionFamily'].replace('setup-key-retirement', 'campaign-controller')+':2'
    outcome_ref = config['preparationOutcome']
    evidence_prefix = 'controller/evidence/'+candidate[7:]+'/original/'
    output = tmp_path/'original-output'; output.mkdir()
    (output/'preparation-result.json').write_bytes(canonical({'schema': 'honua.private-controller-preparation-result/v1',
        'operation': 'prepare-studio-fixture', 'producerTaskArn': producer_task, 'candidateKey': candidate,
        'qualification': False, 'result': config['fixtureReference']}))
    archive = bundle_bytes(output)
    def object_ref(name, raw):
        return {'bucket': config['bucket'], 'key': evidence_prefix+name, 'versionId': 'immutable-original-version', 'sha256': digest(raw)}
    archive_ref = object_ref('evidence.tar', archive)
    manifest = canonical({'schema': 'honua.private-controller-artifacts/v1', 'kind': 'private-evidence',
        'invocationId': 'original', 'archive': archive_ref, 'immutableInputs': ['preparation.json'], 'files': file_manifest(output)})
    manifest_ref = object_ref('manifest.json', manifest)
    outcome = canonical({'schema': 'honua.private-controller-outcome/v1', 'status': 'completed', 'exitCode': 0,
        'invocation': {'operation': 'prepare-studio-fixture', 'candidateKey': candidate}, 'taskArn': producer_task,
        'image': {'uri': config['imageUri']}, 'invocationId': 'original', 'artifacts': {'evidence': {'manifest': manifest_ref, 'archive': archive_ref}}})
    outcome_ref['sha256'] = digest(outcome)
    preparation = {'taskArn': producer_task, 'taskDefinitionArn': definition_arn, 'lastStatus': 'STOPPED', 'exitCode': 0,
                   'observation': 'ecs-describe-tasks', 'outcomeObject': outcome_ref}
    receipt = {'schema': 'honua.private-setup-key-retirement/v1', 'candidateKey': candidate,
        'configurationDigest': canonical_digest(config), 'operation': config['operation'], 'setupKey': config['setupKey'],
        'fixtureReference': config['fixtureReference'], 'preparation': preparation, 'native': retirement.native,
        'revokedKeyId': setup_identity, 'postRevocationProbeStatus': 401, 'retainedPrefix': prefix}
    records = state['scenarioOperations']
    def retain_original(name, value):
        records[prefix+'/'+name] = {'producer': 'private-setup-key-retirement', 'candidateKey': candidate, 'value': copy.deepcopy(value)}
    retain_original('completed', receipt)
    retain_original('start', {key: receipt[key] for key in ('configurationDigest', 'setupKey', 'fixtureReference', 'preparation', 'native')})
    for stage, status_name, authenticates, code in [('active-setup-read', 'active', True, 200),
            ('revoked-independent-read', 'revoked', False, 200), ('old-setup-denied', None, False, 401)]:
        retain_original(stage, {'httpStatus': code, 'body': {'data': {'id': setup_identity, 'status': status_name,
            'canAuthenticate': authenticates, 'permissions': setup['permissions']}}})
    retain_original('active-setup-target-read', {'httpStatus': 200, 'body': {'targetId': workspace['targetId'],
        'principal': {'actorId': 'original-observed-setup-actor', 'studioOwnerId': setup_identity,
            'authenticationKind': 'api-key', 'isHumanSession': False}}})
    retain_original('old-setup-target-denied', {'httpStatus': 401, 'body': {'status': 401}})
    receipt['setupTargetReference'] = {'key': prefix+'/active-setup-target-read',
        'sha256': canonical_digest(records[prefix+'/active-setup-target-read'])}
    records[prefix+'/completed']['value'] = copy.deepcopy(receipt)
    mcp = {'schema': 'honua.private-worker-mcp-binding/v1', 'secretArn': 'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-agent-custody-mcp-ABC123',
           'versionId': '9'*64, 'nativeContext': reference}
    public = {key: mcp[key] for key in ('secretArn', 'versionId', 'nativeContext')}
    public.update(issuedAgentKey=agent, agentContext=context['agentContext'], endpoint=server.url+'/mcp')
    mcp['publicBindingDigest'] = canonical_digest(public)
    for stage, value in [('intent', public), ('completed', mcp)]:
        records['native-worker-mcp:contract/'+stage] = {'producer': 'private-worker-mcp', 'candidateKey': candidate, 'value': value}
    session.config = {'http': {'endpoint': server.url, 'setupCredentialEnv': 'CUSTODY_OLD_KEY'},
        'studio': {'bootstrapFixture': config['fixtureReference'], 'requesterCredentialEnv': 'CUSTODY_TASK_KEY',
            'requesterAgentContext': context['agentContext'], 'custody': {'schemaVersion': 1,
                'retirement': {'key': prefix+'/completed', 'sha256': canonical_digest(records[prefix+'/completed'])},
                'nativeContext': reference, 'workerMcp': {'key': 'native-worker-mcp:contract/completed',
                    'sha256': canonical_digest(records['native-worker-mcp:contract/completed'])}}}}
    session.http = http
    session.requester = lambda **kwargs: http('task')
    session.initial_fixture = lambda: fixture
    retained = {}
    session.phase_key = lambda name: canonical_digest({'producer': 'studio-publication-native',
        'context': {key: session.context[key] for key in ('campaignId', 'caseId', 'attempt')}, 'phase': name})
    def retain(name, value):
        assert name not in retained
        retained[name] = copy.deepcopy(value)
        records[session.phase_key(name)] = {'producer': 'studio-publication-native', 'candidateKey': candidate,
            'identity': {key: session.context[key] for key in ('campaignId', 'caseId', 'attempt')}, 'phase': name, 'value': copy.deepcopy(value)}
        session.durable._persist()
        return {'key': session.phase_key(name), 'sha256': canonical_digest(records[session.phase_key(name)])}
    session.retain = retain
    session.value = retained.get
    account, region = '585192672263', 'us-east-1'
    role = 'arn:aws:iam::'+account+':role/honua-agent-custody-worker'
    worker_definition = {'taskDefinitionArn': 'arn:aws:ecs:'+region+':'+account+':task-definition/honua-agent-custody:1',
        'volumes': [{'name': 'workspace', 'host': {}}, {'name': 'journal'}],
        'taskRoleArn': role, 'executionRoleArn': role.replace('-worker', '-execution'), 'containerDefinitions': [{
            'name': 'worker', 'image': 'immutable-worker@sha256:'+'8'*64, 'user': '10001:10001', 'readonlyRootFilesystem': True,
            'privileged': False, 'command': ['python', '-I', '/app/agent_e2e_container_entry.py'],
            'linuxParameters': {'initProcessEnabled': True, 'capabilities': {'drop': ['ALL']}},
            'mountPoints': [{'sourceVolume': 'workspace', 'containerPath': '/workspace', 'readOnly': False},
                {'sourceVolume': 'journal', 'containerPath': '/journal', 'readOnly': False}],
            'environment': [{'name': 'HONUA_AGENT_TRANSPORT', 'value': '{}'}, {'name': 'AWS_DEFAULT_REGION', 'value': region}],
            'secrets': [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': mcp['secretArn']+':::'+mcp['versionId']}]}]}
    session.worker_config = {'attemptId': 'custody', 'taskRoleArn': role, 'imageUri': worker_definition['containerDefinitions'][0]['image'],
        'publicBundleDigest': 'sha256:'+'8'*64, 'evidence': {'region': region, 'bucket': 'fixture-evidence'},
        'transport': {}, 'lifecycle': {'clusterArn': config['clusterArn'], 'region': region,
            'taskDefinitionArn': worker_definition['taskDefinitionArn'], 'stateMachineArn': 'arn:aws:states:'+region+':'+account+':stateMachine:honua-agent-custody',
            'definitionDigest': 'sha256:'+'7'*64, 'stateMachineRoleArn': role.replace('-worker', '-lifecycle'),
            'claimTable': 'fixture-claims', 'expiresEpoch': 4102444800}}
    transport = {'attemptId': 'custody', 'expiresEpoch': int(datetime.now(timezone.utc).timestamp())+3600,
        'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/'+account+'/honua-agent-custody-commands.fifo',
        'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/'+account+'/honua-agent-custody-replies.fifo'}
    session.worker_config['transport'] = transport
    session.worker_config['lifecycle']['expiresEpoch'] = transport['expiresEpoch']
    worker_definition['containerDefinitions'][0]['environment'][0]['value'] = json.dumps(transport)
    worker_task = {'taskArn': producer_task[:-32]+'2'*32, 'taskDefinitionArn': worker_definition['taskDefinitionArn'],
        'clusterArn': config['clusterArn'], 'launchType': 'FARGATE', 'tags': [
            {'key': 'honua-agent-attempt', 'value': 'custody'}, {'key': 'honua-agent-candidate', 'value': candidate}],
        'lastStatus': 'RUNNING', 'enableExecuteCommand': False, 'overrides': {'containerOverrides': [{'name': 'worker'}]}}
    controller = producer_task[:-32]+'3'*32
    monkeypatch.setenv('PILOT_CONTROLLER_TASK_ARN', controller)
    controller_definition = {'containerDefinitions': [{'name': 'campaign-controller', 'secrets': [
        {'name': env, 'valueFrom': key['secretArn']+':::'+key['versionId']}
        for env, key in [('CUSTODY_OLD_KEY', setup), ('CUSTODY_TASK_KEY', agent)]]}]}
    ecs, s3, states, iam = (MagicMock() for _ in range(4))
    tasks = {producer_task: {'taskArn': producer_task, 'taskDefinitionArn': definition_arn, 'lastStatus': 'STOPPED',
        'enableExecuteCommand': False, 'containers': [{'image': config['imageUri'], 'exitCode': 0}]},
        worker_task['taskArn']: worker_task, controller: {'taskArn': controller, 'taskDefinitionArn': 'controller:3'}}
    tasks[retirement.native['taskArn']] = {'taskArn': retirement.native['taskArn'],
        'taskDefinitionArn': retirement.native['taskDefinitionArn'], 'lastStatus': 'STOPPED', 'enableExecuteCommand': False,
        'containers': [{'image': retirement.native['imageUri'], 'exitCode': 0}]}
    ecs.describe_tasks.side_effect = lambda **kwargs: {'tasks': [tasks[arn] for arn in kwargs['tasks']]}
    definitions = {retirement.native['taskDefinitionArn']: retirement.definition(config),
        'controller:3': controller_definition, worker_definition['taskDefinitionArn']: worker_definition}
    ecs.describe_task_definition.side_effect = lambda **kwargs: {'taskDefinition': definitions[kwargs['taskDefinition']]}
    objects = {outcome_ref['key']: outcome, manifest_ref['key']: manifest, archive_ref['key']: archive}
    s3.get_object.side_effect = lambda **kwargs: {'VersionId': kwargs['VersionId'], 'Body': io.BytesIO(objects[kwargs['Key']])}
    iam.list_attached_role_policies.return_value = {'AttachedPolicies': []}
    iam.list_role_policies.return_value = {'PolicyNames': ['queue-only']}
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage'],
         'Resource': 'arn:aws:sqs:'+region+':'+account+':honua-agent-custody-commands.fifo'},
        {'Effect': 'Allow', 'Action': ['sqs:SendMessage'], 'Resource': 'arn:aws:sqs:'+region+':'+account+':honua-agent-custody-replies.fifo'}]}
    iam.get_role_policy.return_value = {'RoleName': 'honua-agent-custody-worker', 'PolicyName': 'queue-only', 'PolicyDocument': policy}
    session.open_worker = lambda: None
    from agent_e2e_worker_lifecycle import WorkerLifecycle
    live = {**session.worker_config['lifecycle'], 'candidateKey': candidate, 'attemptId': 'custody',
        'context': {key: session.context[key] for key in ('campaignId', 'caseId', 'attempt', 'campaignMode')},
        'transportDigest': canonical_digest(transport),
        'liability': {'basisDigest': 'sha256:'+'b'*64, 'maximumInfrastructureUsd': '1', 'maximumCleanupUsd': '1'}}
    claims = MagicMock()
    session.lifecycle = WorkerLifecycle(live, states=states, ecs=ecs, db=claims)
    session.launch = session.lifecycle._record({'infrastructure': 'infrastructure', 'cleanup': 'cleanup'})
    state['workerLaunches'] = {'custody': copy.deepcopy(session.launch)}
    def require_launch(launch):
        if launch != state['workerLaunches']['custody']: raise PackError('different original launch')
    session.durable.require_worker_launch = require_launch
    execution = {'executionArn': session.lifecycle.execution_arn, 'stateMachineArn': live['stateMachineArn'],
        'name': session.lifecycle.name, 'input': '{}', 'inputDetails': {'included': True}, 'redriveCount': 0,
        'status': 'RUNNING', 'startDate': datetime.now(timezone.utc).isoformat()}
    states.describe_execution.return_value = execution
    states.get_execution_history.return_value = {'events': [{'id': 8, 'type': 'TaskStateExited', 'stateExitedEventDetails': {
        'name': 'RunWorker', 'outputDetails': {'truncated': False}, 'output': json.dumps({'launched': {
            'Tasks': [{'TaskArn': worker_task['taskArn']}], 'Failures': []}})}}]}
    claim = {key: {'S': value} for key, value in {'candidateKey': session.lifecycle.claim_key,
        'attemptId': 'custody', 'executionArn': session.lifecycle.execution_arn, 'taskDefinitionArn': live['taskDefinitionArn']}.items()}
    claims.get_item.side_effect = lambda **kwargs: {'Item': claim} if kwargs['Key']['candidateKey']['S'] == session.lifecycle.claim_key else {}
    status.setup = 'revoked'
    observer = custody.StudioCustodyObserver(session, ecs=ecs, s3=s3, states=states, iam=iam)
    return SimpleNamespace(observer=observer, status=status, session=session, records=retained, ecs=ecs, iam=iam,
        tasks=tasks, producer_task=producer_task, worker_task=worker_task, worker_definition=worker_definition,
        setup=setup, context=context, prefix=prefix, policy=policy, server=server)


def test_original_success_revocation_refusal_and_separate_native_worker_are_independently_read(observed):
    result, original = observed.observer.revoked(step='before')
    assert result['bootstrapPrincipalId'] == 'original-observed-setup-actor'
    assert result['revokedCredentialIds'] == [observed.setup['apiKeyId']] and result['postRevocationProbeStatus'] == 401
    replaced = observed.observer.worker(step='worker', original=original)
    assert replaced['bootstrapWorkerId'] == observed.producer_task
    assert replaced['taskWorkerId'] == observed.worker_task['taskArn']
    assert replaced['taskProvisioningCredentialIds'] == []
    assert observed.iam.get_role_policy.call_count == 1
    assert all(method == 'GET' for method, *_ in observed.server.seen)
    for proof in observed.records.values():
        assert 'private-key' not in json.dumps(proof)


def test_actual_sdk_datetime_fields_are_preserved_as_json_safe_originals(observed):
    observed.worker_definition['registeredAt'] = datetime.now(timezone.utc)
    observed.worker_task['startedAt'] = datetime.now(timezone.utc)
    _, original = observed.observer.revoked(step='timestamp-before')
    observed.observer.worker(step='timestamp-worker', original=original)
    proof = observed.records['timestamp-worker-worker']
    assert proof['definition']['registeredAt'] == observed.worker_definition['registeredAt'].isoformat()
    assert proof['nativeTasks']['tasks'][0]['startedAt'] == observed.worker_task['startedAt'].isoformat()
    assert json.loads(json.dumps(proof)) == proof


def test_authentication_key_material_cannot_enter_public_identifier_paths_or_digest_records(observed, monkeypatch):
    original = observed.observer.originals()
    secret = 'private-authentication-material-that-is-not-a-native-uuid'
    original = {**original, 'setup': {**original['setup'], 'apiKeyId': secret}}
    monkeypatch.setattr(observed.observer, 'originals', lambda: original)
    calls = len(observed.server.seen)
    with pytest.raises(PackError, match='UUID metadata'):
        observed.observer.revoked(step='invalid-public-identifier')
    assert len(observed.server.seen) == calls
    assert secret not in json.dumps(observed.session.durable.state)


def test_custody_uses_the_real_workers_required_fifo_transport_contract(observed):
    from agent_e2e_worker import Worker
    from agent_e2e_studio_custody_observer import queue_document
    worker = observed.session.worker_config
    Worker(None, worker['transport'])
    assert queue_document(worker) == observed.policy
    assert all(row['Resource'].endswith('.fifo') for row in observed.policy['Statement'])


@pytest.mark.parametrize('change', ['standard', 'foreign-account', 'foreign-region', 'trailing-slash', 'query',
    'same-queues', 'wrong-attempt', 'missing-expiry', 'different-expiry'])
def test_fifo_suffix_does_not_relax_exact_queue_account_origin_attempt_or_lifetime(observed, change):
    from agent_e2e_studio_custody_observer import queue_document
    worker = observed.session.worker_config
    transport = worker['transport']
    key = 'commandQueueUrl'
    if change == 'standard': transport[key] = transport[key][:-5]
    elif change == 'foreign-account': transport[key] = transport[key].replace('/585192672263/', '/999999999999/')
    elif change == 'foreign-region': transport[key] = transport[key].replace('us-east-1', 'us-west-2')
    elif change == 'trailing-slash': transport[key] += '/'
    elif change == 'query': transport[key] += '?configuration=other'
    elif change == 'same-queues': transport[key] = transport['replyQueueUrl']
    elif change == 'wrong-attempt': transport['attemptId'] = 'other-attempt'
    elif change == 'missing-expiry': del transport['expiresEpoch']
    elif change == 'different-expiry': transport['expiresEpoch'] += 1
    with pytest.raises(PackError): queue_document(worker)


@pytest.mark.parametrize('change', ['old-active', '403', '404', 'wrong-principal', 'no-original-positive', 'missing-original-turn',
    'wrong-completed-hash', 'bootstrap-running', 'setup-version-changed', 'no-setup-principal', 'retirement-still-running'])
def test_refusal_alone_missing_originals_or_nonterminal_bootstrap_never_prove_custody(observed, change):
    state = observed.session.durable.state
    if change == 'old-active': observed.status.setup = 'active'
    elif change in ('403', '404'): observed.status.refusal = int(change)
    elif change == 'wrong-principal': observed.status.wrong_principal = True
    elif change == 'no-original-positive': del state['scenarioOperations'][observed.prefix+'/active-setup-read']
    elif change == 'missing-original-turn':
        ref = observed.context['nativeOriginals']['turnReadback']['reference']
        del state['scenarioOperations'][ref['key']]
    elif change == 'wrong-completed-hash': observed.observer.config['retirement']['sha256'] = 'sha256:'+'0'*64
    elif change == 'bootstrap-running': observed.tasks[observed.producer_task]['lastStatus'] = 'RUNNING'
    elif change == 'setup-version-changed': observed.setup['versionId'] = '0'*64
    elif change == 'no-setup-principal': del state['scenarioOperations'][observed.prefix+'/active-setup-target-read']
    elif change == 'retirement-still-running':
        receipt = state['scenarioOperations'][observed.prefix+'/completed']['value']
        observed.tasks[receipt['native']['taskArn']]['lastStatus'] = 'RUNNING'
    with pytest.raises((PackError, ValueError)): observed.observer.revoked(step='refused')
    assert 'refused-revocation' not in observed.records


@pytest.mark.parametrize('change', ['same-worker', 'extra-secret', 'secret-version', 'environment-override', 'role-override',
    'privileged-role', 'extra-attached-policy', 'policy-page', 'provision-policy', 'missing-policy-permission',
    'external-env-file', 'shared-host-volume', 'log-secret'])
def test_worker_identity_secret_role_and_provisioning_authority_are_not_inferred_from_names(observed, change):
    _, original = observed.observer.revoked(step='before')
    if change == 'same-worker': original['preparation']['taskArn'] = observed.worker_task['taskArn']
    elif change == 'extra-secret': observed.worker_definition['containerDefinitions'][0]['secrets'].append({'name': 'ROOT', 'valueFrom': 'root'})
    elif change == 'secret-version': observed.worker_definition['containerDefinitions'][0]['secrets'][0]['valueFrom'] += 'different'
    elif change == 'environment-override': observed.worker_task['overrides']['containerOverrides'][0]['environment'] = [{'name': 'ROOT', 'value': 'privileged'}]
    elif change == 'role-override': observed.worker_task['overrides']['taskRoleArn'] = 'root-role'
    elif change == 'privileged-role': observed.worker_definition['taskRoleArn'] = 'root-role'
    elif change == 'extra-attached-policy': observed.iam.list_attached_role_policies.return_value = {'AttachedPolicies': [{'PolicyArn': 'admin'}]}
    elif change == 'policy-page': observed.iam.list_role_policies.return_value['IsTruncated'] = True
    elif change == 'provision-policy': observed.policy['Statement'].append({'Effect': 'Allow', 'Action': '*', 'Resource': '*'})
    elif change == 'missing-policy-permission': observed.iam.list_role_policies.side_effect = PermissionError('actual read denied')
    elif change == 'external-env-file': observed.worker_definition['containerDefinitions'][0]['environmentFiles'] = [{'type': 's3', 'value': 'private'}]
    elif change == 'shared-host-volume': observed.worker_definition['volumes'][0]['host'] = {'sourcePath': '/root-credentials'}
    elif change == 'log-secret': observed.worker_definition['containerDefinitions'][0]['logConfiguration'] = {'secretOptions': [{'name': 'root', 'valueFrom': 'private'}]}
    with pytest.raises((PackError, PermissionError)): observed.observer.worker(step='refused', original=original)
    assert 'refused-worker' not in observed.records


def test_p14_deployment_reads_use_active_observer_after_setup_retirement():
    from agent_e2e_studio_backend import Session
    value = Session.__new__(Session)
    value.setup = SimpleNamespace(json=lambda *args: pytest.fail('revoked setup was used for ordinary deployment reads'))
    value.http = lambda role: role
    assert value.deployment_http() == 'observer'


def test_configured_custody_without_original_before_model_proof_remains_failed(publication_session, studio):
    from test_agent_e2e_studio_backend import select
    value = publication_session
    value.config['studio']['custody'] = {'configuration': 'present-without-original-before-proof'}
    value.retain('publication-ordinal-1', select(studio))
    studio.state.approved = True
    result = value.verify()
    assert {'custody.bootstrap-revoked', 'custody.worker-replaced'} <= set(result['failedAssertions'])
    assert not {'custody.bootstrap-revoked', 'custody.worker-replaced'} & set(value.value('verification')['unsupportedAssertions'])
    assert value.value('custody-verification-refused') == {'reasonCode': 'original-native-custody-proof-refused'}
