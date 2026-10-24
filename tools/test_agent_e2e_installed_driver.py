"""Installed entry/authority controls with SDK doubles; never cloud qualification."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_e2e_console_runtime import ExecutionJournal
from agent_e2e_installed_driver import InstalledExecutionDriver, GuardedModel, queue_role
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_execution_protocol import validate_worker_bound
from hosted_pilot import installed_controller as entry
from test_agent_e2e_console_runtime import runtime, original, slot, E
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_setup_http import server
from test_agent_e2e_budget import KEY, BASIS
from test_agent_e2e_runtime_index import runtime_proof_fixture


def config():
    return {'schemaVersion': 1, 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'state': {'region': 'us-east-1', 'bucket': 'original-state-bucket', 'table': 'original-state'},
        'generation': {'key': 'agent-e2e/'+KEY[7:]+'/original-generation.json', 'version': 'actual-version', 'digest': BASIS},
        'declaration': {'path': 'certification/agent-e2e/installed-runtimes/runtime-original.json', 'sha256': BASIS},
        'registration': {'key': 'original-completed-registration', 'sha256': BASIS},
        'identity': {'bucket': 'original-state-bucket', 'key': 'actual-stage-original', 'versionId': 'actual-version', 'sha256': BASIS},
        'overhead': {'operationId': 'runtime', 'basisDigest': BASIS,
            'reservationIds': {'infrastructure': 'runtime-infra', 'cleanup': 'runtime-cleanup'}},
        'imageSource': {'sourceSha': 'a'*40, 'treeId': 'b'*40, 'archiveSha256': 'c'*64},
        'originalFundingExpiresEpoch': 1800000000,
        'poll': {'intervalSeconds': 10, 'maxPending': 16, 'maxLifetimeSeconds': 6000}}


@pytest.mark.parametrize('change', ['credential', 'float-version', 'float-poll', 'extra-environment', 'runtime-path', 'overlap', 'unversioned'])
def test_entry_configuration_refuses_caller_authority_and_unbounded_inputs(change):
    value = config()
    if change == 'credential': value['controlKey'] = 'must-never-be-configured'
    if change == 'float-version': value['schemaVersion'] = True
    if change == 'float-poll': value['poll']['intervalSeconds'] = 10.0
    if change == 'extra-environment': value['model'] = {'modelId': 'unreviewed'}
    if change == 'runtime-path': value['declaration']['path'] = 'certification/agent-e2e/installed-runtimes/foreign.json'
    if change == 'overlap': value['overhead']['reservationIds']['cleanup'] = 'runtime-infra'
    if change == 'unversioned': value['identity']['versionId'] = 'null'
    with pytest.raises(PackError): entry.configuration(json.dumps(value))


def test_entry_missing_checked_custody_dependency_never_contacts_aws_or_native(monkeypatch, capsys):
    monkeypatch.setattr(entry.os, 'getuid', lambda: 10001)
    monkeypatch.setenv(entry.CONFIG_ENV, json.dumps(config()))
    monkeypatch.setenv(entry.CONTROL_ENV, 'private-control-material')
    monkeypatch.setattr(entry.importlib, 'import_module', Mock(side_effect=ImportError('not published')))
    aws = Mock(side_effect=AssertionError('no AWS before actual verifier source'))
    monkeypatch.setattr(entry, 'sdk', aws)
    assert entry.main() == 1 and not aws.called
    assert 'private-control-material' not in capsys.readouterr().err


def test_actual_supervisor_start_input_is_required_before_resolving_final_sidecar():
    value = {'schemaVersion': 1, 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'registrationDigest': BASIS, 'configurationDigest': BASIS,
        'installation': {'bucket': 'actual-bucket', 'key': 'original-sidecar', 'versionId': 'actual-version', 'sha256': BASIS}}
    arn = 'arn:aws:states:us-east-1:123456789012:execution:actual-supervisor:original-start'
    task = {'tags': [{'key': key, 'value': item} for key, item in
        {'honua-agent-candidate': KEY, 'honua-agent-runtime': 'runtime-original', 'honua-agent-controller-execution': arn}.items()]}
    states = Mock()
    states.describe_execution.return_value = {'executionArn': arn, 'status': 'RUNNING', 'redriveCount': 0,
        'inputDetails': {'included': True}, 'input': json.dumps(value)}
    states.get_execution_history.return_value = {'events': [{'id': 1, 'type': 'ExecutionStarted',
        'executionStartedEventDetails': {'input': json.dumps(value), 'inputDetails': {'truncated': False}}}]}
    observed, _ = entry.final_installation_reference(task, states=states, candidate=KEY, runtime='runtime-original')
    assert observed == value
    for changed in ('truncated', 'missing', 'replaced'):
        altered = copy.deepcopy(states.get_execution_history.return_value)
        if changed == 'truncated': altered['events'][0]['executionStartedEventDetails']['inputDetails']['truncated'] = True
        if changed == 'missing': altered['events'] = []
        if changed == 'replaced': altered['events'][0]['executionStartedEventDetails']['input'] = json.dumps({**value, 'candidateKey': 'sha256:'+'f'*64})
        states.get_execution_history.return_value = altered
        with pytest.raises(PackError): entry.final_installation_reference(task, states=states, candidate=KEY, runtime='runtime-original')
        states.get_execution_history.return_value = {'events': [{'id': 1, 'type': 'ExecutionStarted',
            'executionStartedEventDetails': {'input': json.dumps(value), 'inputDetails': {'truncated': False}}}]}


def test_current_network_join_uses_actual_task_eni_and_original_group_ids():
    task = {'taskArn': 'arn:aws:ecs:us-east-1:123456789012:task/controller/'+'a'*32,
        'attachments': [{'type': 'ElasticNetworkInterface', 'status': 'ATTACHED', 'details': [
            {'name': 'networkInterfaceId', 'value': 'eni-0123456789abcdef0'}, {'name': 'subnetId', 'value': 'subnet-original'}]}]}
    identity = {'vpcId': 'vpc-original', 'subnetIds': ['subnet-original'], 'securityGroupIds': ['sg-original']}
    response = {'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-0123456789abcdef0', 'Status': 'in-use',
        'OwnerId': '123456789012', 'VpcId': 'vpc-original', 'SubnetId': 'subnet-original', 'Groups': [{'GroupId': 'sg-original'}]}]}
    ec2 = Mock()
    ec2.describe_network_interfaces.return_value = response
    assert entry.verify_network(task, identity, ec2) == response
    ec2.describe_network_interfaces.assert_called_once_with(NetworkInterfaceIds=['eni-0123456789abcdef0'])
    for field, value in [('VpcId', 'foreign'), ('SubnetId', 'foreign'), ('OwnerId', '999999999999'),
                         ('Groups', [{'GroupId': 'sg-original'}, {'GroupId': 'sg-broad'}])]:
        ec2.describe_network_interfaces.return_value = copy.deepcopy(response)
        ec2.describe_network_interfaces.return_value['NetworkInterfaces'][0][field] = value
        with pytest.raises(PackError): entry.verify_network(task, identity, ec2)


def role_fixture():
    descriptor = {'taskRoleArn': 'arn:aws:iam::123456789012:role/actual-worker', 'region': 'us-east-1',
        'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo',
        'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo'}
    boundary = 'arn:aws:iam::123456789012:policy/actual-boundary'
    iam = Mock()
    iam.get_role.return_value = {'Role': {'Arn': descriptor['taskRoleArn'], 'PermissionsBoundary': {'PermissionsBoundaryArn': boundary},
        'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': 'sts:AssumeRole',
            'Principal': {'Service': 'ecs-tasks.amazonaws.com'}, 'Condition': {'StringEquals': {'aws:SourceAccount': '123456789012'},
                'ArnLike': {'aws:SourceArn': 'arn:aws:ecs:us-east-1:123456789012:*'}}}]}}}
    iam.list_attached_role_policies.return_value = {'AttachedPolicies': []}
    iam.list_role_policies.return_value = {'PolicyNames': ['actual-queue-policy']}
    iam.get_role_policy.return_value = {'RoleName': 'actual-worker', 'PolicyName': 'actual-queue-policy', 'PolicyDocument': {
        'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage'], 'Resource': 'arn:aws:sqs:us-east-1:123456789012:commands.fifo'},
            {'Effect': 'Allow', 'Action': ['sqs:SendMessage'], 'Resource': 'arn:aws:sqs:us-east-1:123456789012:replies.fifo'}]}}
    return iam, descriptor, boundary


def test_current_worker_role_requires_reviewed_boundary_trust_and_complete_queue_only_policy():
    iam, descriptor, boundary = role_fixture()
    assert queue_role(iam, descriptor, boundary)['role'] == iam.get_role.return_value
    for change in ('missing-boundary-proof', 'changed-boundary', 'changed-trust', 'managed', 'truncated', 'wider-queue'):
        iam, descriptor, boundary = role_fixture()
        if change == 'missing-boundary-proof': boundary = None
        if change == 'changed-boundary': iam.get_role.return_value['Role']['PermissionsBoundary']['PermissionsBoundaryArn'] += '-different'
        if change == 'changed-trust': iam.get_role.return_value['Role']['AssumeRolePolicyDocument']['Statement'][0]['Principal'] = {'AWS': '*'}
        if change == 'managed': iam.list_attached_role_policies.return_value['AttachedPolicies'] = [{'PolicyArn': 'unreviewed'}]
        if change == 'truncated': iam.list_role_policies.return_value['IsTruncated'] = True
        if change == 'wider-queue': iam.get_role_policy.return_value['PolicyDocument']['Statement'][0]['Resource'] = '*'
        with pytest.raises(PackError): queue_role(iam, descriptor, boundary)


def test_no_provider_request_can_follow_a_lost_current_authority():
    model = Mock()
    model.config = {'fixture': True}
    guarded = GuardedModel(model, Mock(side_effect=PackError('lost current E')))
    with pytest.raises(PackError): guarded.quote({'messages': []})
    with pytest.raises(PackError): guarded.invoke(b'original')
    assert not model.quote.called and not model.invoke.called


def funding_driver(runtime, server, *, bedrock=None):
    model = {'region': 'us-east-1', 'modelId': 'fixture-profile', 'returnedModelId': 'fixture-returned',
        'tokenCountingModelArn': 'arn:aws:bedrock:us-east-1::foundation-model/fixture-returned',
        'pricingDigest': BASIS, 'inputUsdPerMillion': '3', 'outputUsdPerMillion': '15', 'maxOutputTokens': 128}
    descriptor = {'slotId': 'slot-original', 'maxExecutionSeconds': 600, 'image': 'fixture-image@sha256:'+'a'*64,
        'originalFundingExpiresEpoch': runtime.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch'],
        'liability': {'basisDigest': BASIS, 'maximumInfrastructureUsd': '2', 'maximumCleanupUsd': '0.5'}}
    registration = {'schemaVersion': 1, 'runtimeId': 'runtime-original', 'targetId': 'target-original', 'candidateKey': KEY,
        'configurationDigest': BASIS, 'registrationDigest': BASIS}
    server.respond = lambda method, path, raw: (200, {}, json.dumps(registration if path.endswith('/registration') else runtime.value).encode())
    verified = {'configurationDigest': BASIS, 'runtimeId': 'runtime-original', 'targetId': 'target-original',
        'workers': [{'descriptor': descriptor, 'proof': {'runtime': runtime_proof_fixture(descriptor['image'])}}], 'model': model}
    driver = InstalledExecutionDriver(native=runtime.control, verified=verified, registration=registration,
        states=Mock(), ecs=Mock(), db=Mock(), sqs=Mock(), iam=Mock(), bedrock=bedrock or Mock(), guard=lambda: None, clock=lambda: runtime.now)
    return driver, descriptor


@pytest.mark.parametrize('change', ['missing-proof', 'older-runner'])
def test_unverified_runtime_stops_driver_before_admission_count_or_invoke(runtime, server, monkeypatch, change):
    import sys
    make_proof = runtime_proof_fixture
    def altered(image):
        if change == 'missing-proof': return None
        proof = make_proof(image)
        proof['index']['files']['agent_e2e_mcp_runner.py'] = 'sha256:'+'f'*64
        proof['runtimeBundleDigest'] = canonical_digest(proof['index'])
        return proof
    monkeypatch.setattr(sys.modules[__name__], 'runtime_proof_fixture', altered)
    ledger = copy.deepcopy(runtime.durable.state['ledger'])
    bedrock = Mock()
    with pytest.raises(PackError): funding_driver(runtime, server, bedrock=bedrock)
    assert runtime.durable.state['ledger'] == ledger
    assert not bedrock.count_tokens.called and not bedrock.invoke_model.called


def test_original_e_financial_admission_recovers_lost_ack_without_second_attempt_or_reservation(runtime, server):
    driver, descriptor = funding_driver(runtime, server)
    journal = ExecutionJournal(runtime.durable, original(runtime))
    update = runtime.durable.update
    def lose_after_commit(path, operation, **arguments):
        result = update(path, operation, **arguments)
        if operation == 'acquire-interactive-attempt': raise TimeoutError('original acquisition acknowledgement lost')
        return result
    runtime.durable.update = lose_after_commit
    with pytest.raises(TimeoutError): driver._fund(journal, descriptor)
    assert len(runtime.durable.state['ledger']['interactivePaidAdmissions']) == 1
    assert not journal.values('admission')
    runtime.durable.state = json.loads(json.dumps(runtime.durable.state))
    runtime.durable.update = update
    restarted = ExecutionJournal(runtime.durable, original(runtime))
    first = driver._fund(restarted, descriptor)
    assert driver._fund(restarted, descriptor) == first
    ledger = runtime.durable.state['ledger']
    assert len(ledger['interactivePaidAdmissions']) == 1 and len(ledger['interactiveEntries']) == 2
    assert len(restarted.values('admission')) == 1
    assert {row['executionId'] for row in ledger['interactiveEntries']} == {E}
    assert all(row['state'] == 'reserved' and row['actualUsd'] is None for row in ledger['interactiveEntries'])
    assert not driver.model.client.invoke_model.called


def test_actual_native_interruption_blocks_new_admission_and_worker_start(runtime, server):
    driver, _ = funding_driver(runtime, server)
    captured = original(runtime)
    ledger = copy.deepcopy(runtime.durable.state['ledger'])
    runtime.value['execution'].update(state='interrupted', stateRevision=1)
    with pytest.raises(PackError, match='eligible for admission'):
        driver.execute(captured)
    assert runtime.durable.state['ledger'] == ledger
    assert not driver.states.start_execution.called and not driver.model.client.invoke_model.called


def test_new_native_read_cannot_replace_the_original_execution_binding(runtime, server):
    driver, _ = funding_driver(runtime, server)
    captured = original(runtime)
    runtime.value['requestDigest'] = 'sha256:'+'f'*64
    with pytest.raises(PackError, match='authority binding'):
        driver.execute(captured)
    assert not driver.states.start_execution.called and not driver.model.client.invoke_model.called


def test_native_control_unicode_question_retains_actual_utf8_wire_under_existing_limit(runtime, server):
    text = '漢'*16000
    runtime.control.mutate('question', original(runtime), {'text': text}, capability='a'*64)
    posts = [row for row in server.seen if row[0] == 'POST']
    assert len(posts) == 1 and len(posts[0][3]) < 65536
    assert json.loads(posts[0][3])['text'] == text


def test_worker_binding_local_reference_is_closed_metadata_before_any_retention():
    original = {'protocol': 'honua.installed-worker-bind/v1', 'executionId': E, 'workerId': 'actual-task', 'claimGeneration': 1}
    reply = {**original, 'protocol': 'honua.installed-worker-bound/v1', 'nativeRead': {'key': 'bind-read-1', 'sha256': BASIS}}
    assert validate_worker_bound(reply, original) == reply
    for value in ({'key': 'private-key-material', 'sha256': BASIS}, {'key': 'bind-read-1', 'sha256': BASIS, 'credential': 'private'},
                  {'key': 'bind-read-1', 'sha256': 'not-a-hash'}, {'key': 'bind-read-١', 'sha256': BASIS}):
        with pytest.raises(PackError): validate_worker_bound({**reply, 'nativeRead': value}, original)
    with pytest.raises(PackError): validate_worker_bound({**reply, 'executionId': 'another-execution'}, original)


def test_actual_task_cannot_override_checked_command_environment_roles_or_resource_quote():
    container = {'name': 'installed-controller', 'image': 'account.dkr.ecr.region.amazonaws.com/controller@sha256:'+'a'*64}
    task = {'enableExecuteCommand': False, 'containers': [container.copy()],
            'overrides': {'containerOverrides': [{'name': container['name']}]}}
    entry.verify_task_overrides(task, container)
    for change in ({'taskRoleArn': 'foreign'}, {'cpu': '16384'}, {'memory': '128000'},
        {'containerOverrides': [{'name': container['name'], 'command': ['different']}]},
        {'containerOverrides': [{'name': container['name'], 'environment': [{'name': 'ROOT_KEY', 'value': 'private'}]}]}):
        with pytest.raises(PackError): entry.verify_task_overrides({**task, 'overrides': change}, container)
    with pytest.raises(PackError): entry.verify_task_overrides({**task, 'enableExecuteCommand': True}, container)


@pytest.mark.parametrize('lost_ack,wrong_principal', [(False, False), (True, False), (False, True)])
def test_real_first_bind_joins_worker_sqlite_native_http_and_original_slot_claims(
        runtime, slot, server, tmp_path, monkeypatch, lost_ack, wrong_principal):
    from agent_e2e_installed_container import InstalledBinding, native_binding_ack, native_binding_read
    iam, _, boundary = role_fixture()
    role_name = slot.config['taskRoleArn'].rsplit('/', 1)[1]
    iam.get_role.return_value['Role']['Arn'] = slot.config['taskRoleArn']
    iam.get_role_policy.return_value['RoleName'] = role_name
    # Use the original retained E/admission/claim from the maintained slot
    # fixture. This control never resets a ledger or manufactures retirement.
    body = runtime.value
    body['execution'].update(state='claimed', claim={'generation': 1,
        'expiresEpoch': slot.worker.input['expiresEpoch'], 'slotId': slot.config['slotId'],
        'nativeBindingDigest': BASIS})
    registered = {'actorId': 'actual-task-fixture', 'credentialVersion': 'original-version'}
    registration = {'schemaVersion': 1, 'candidateKey': KEY, 'runtimeId': 'runtime-original', 'targetId': 'target-original',
        'configurationDigest': BASIS, 'registrationDigest': BASIS, 'taskPrincipal': registered}
    model = {'region': 'us-east-1', 'modelId': 'fixture-profile', 'returnedModelId': 'fixture-returned',
        'tokenCountingModelArn': 'arn:aws:bedrock:us-east-1::foundation-model/fixture-returned',
        'pricingDigest': BASIS, 'inputUsdPerMillion': '3', 'outputUsdPerMillion': '15', 'maxOutputTokens': 128}
    driver = InstalledExecutionDriver(native=runtime.control, verified={'configurationDigest': BASIS,
        'runtimeId': 'runtime-original', 'targetId': 'target-original', 'model': model,
        'workers': [{'descriptor': slot.config, 'proof': {'native': {'permissionsBoundaryArn': boundary},
            'runtime': runtime_proof_fixture(slot.config['image'])}}]},
        registration=registration, states=slot.states, ecs=slot.ecs, db=slot.claims, sqs=None, iam=iam,
        bedrock=Mock(), guard=lambda: None, clock=lambda: runtime.now)
    active = {}
    def respond(method, path, raw):
        if path.endswith('/registration'): return 200, {}, json.dumps(registration).encode()
        if path.endswith('/worker-binding'):
            if method == 'POST':
                acknowledgement = {**registered, **({'actorId': 'different-task'} if wrong_principal else {})}
                body['execution']['state'] = 'running'
                body['execution']['stateRevision'] += 1
                body['workerBinding'] = {'schemaVersion': 1, 'state': 'running',
                    **{key: active[key] for key in ('candidateKey', 'runtimeId', 'slotId', 'executionId',
                        'workspaceId', 'turnId', 'claimGeneration', 'expiresEpoch', 'workerId', 'nativeBindingDigest')},
                    'context': copy.deepcopy(body['binding']['turn']['context']),
                    'contextDigest': canonical_digest(body['binding']['turn']['context']),
                    'acknowledgement': acknowledgement}
            return 200, {}, json.dumps(body['workerBinding']).encode()
        if method == 'POST':
            assert path.endswith('/worker')
            body['execution']['claim']['workerId'] = json.loads(raw)['workerId']
            body['execution']['stateRevision'] += 1
        return 200, {}, json.dumps(body).encode()
    server.respond = respond
    transport = {'schemaVersion': 2, 'mode': 'installed-slot', **{key: slot.config[key] for key in
        ('candidateKey', 'runtimeId', 'slotId', 'commandQueueUrl', 'replyQueueUrl', 'originalFundingExpiresEpoch', 'maxExecutionSeconds')}}
    ack_calls = []
    def acknowledge(*arguments):
        ack_calls.append(arguments[2]['executionId'])
        observed = native_binding_ack(*arguments)
        if lost_ack: raise TimeoutError('original native acknowledgement reply lost')
        return observed
    monkeypatch.delenv('HONUA_AGENT_MCP_CONFIG', raising=False)
    binding = InstalledBinding(transport, {'endpoint': server.url+'/mcp', 'credentialKind': 'api-key',
        'credential': 'private-task-fixture', 'workflowView': 'workspace', 'bindingMode': 'native-execution/v1',
        'runtimeId': 'runtime-original', 'slotId': slot.config['slotId']}, worker_id=slot.ecs.task['taskArn'],
        journal=tmp_path/'actual-worker.sqlite', native_read=native_binding_read, native_ack=acknowledge,
        clock=lambda: runtime.now)
    replies, sends, deletes = [], [], []
    def send(**request):
        value = json.loads(request['MessageBody'])
        sends.append(value)
        active.update(value)
        replies.append(binding.accept(value))
        return {'MessageId': 'actual-queue-fixture'}
    queue = SimpleNamespace(send_message=send,
        receive_message=lambda **_: {'Messages': [{'Body': json.dumps(replies[0]), 'ReceiptHandle': 'original-reply'}]},
        delete_message=lambda **request: deletes.append(request['ReceiptHandle']))
    driver.sqs = queue
    physical = slot.worker.start(slot.journal, slot.launch)
    try:
        if wrong_principal:
            with pytest.raises(PackError, match='registered task/context'):
                driver._binding(slot.journal, slot.worker, original(runtime), physical, 'a'*64)
            assert not slot.journal.values('worker-binding-observation') and not deletes
        else:
            observed = driver._binding(slot.journal, slot.worker, original(runtime), physical, 'a'*64)
            assert observed['value']['workerBinding']['acknowledgement'] == registered
            assert deletes == ['original-reply']
            runtime.durable.state = json.loads(json.dumps(runtime.durable.state))
            recovered = ExecutionJournal(runtime.durable, original(runtime))
            assert driver._binding(recovered, slot.worker, original(runtime), physical, 'a'*64)['value']['workerBinding'] == observed['value']['workerBinding']
        assert ack_calls == [E] and len(sends) == 1
        assert len([row for row in server.seen if row[0] == 'POST' and row[1].endswith('/worker-binding')]) == 1
        assert not driver.model.client.invoke_model.called
    finally:
        binding.close()
