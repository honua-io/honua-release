"""Resolve only the actual AWS worker task identity from retained native reads."""
from __future__ import annotations
import copy
import re

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_p10_backend import verify_worker_definition, worker_task_role
from agent_e2e_studio_observer import instant
from agent_e2e_worker_lifecycle import WorkerLifecycle
from agent_e2e_studio_custody_observer import isolated_definition, isolated_task, queue_document
from urllib.parse import urlsplit

PROFILE = 'honua.worker-custody-bindings/v1'
ALLOWED = {('custody.worker-replaced', 'taskWorkerId'): 'taskWorkerId'}
IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')


def require(value, reason):
    if not value:
        raise PackError(reason)


def contract(value):
    require(isinstance(value, dict) and set(value) == {'profile', 'fields', 'relationships', 'observers'} and
            value['profile'] == PROFILE and value['fields'] == ['taskWorkerId'], 'invalid finite AWS custody binding contract')
    relationships = value['relationships']
    require(isinstance(relationships, dict) and set(relationships) == {'bootstrapWorkerId', 'taskPrincipalId'} and
            re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[a-f0-9]{32}', relationships['bootstrapWorkerId']) and
            isinstance(relationships['taskPrincipalId'], str) and 0 < len(relationships['taskPrincipalId']) <= 512,
            'custody must freeze original bootstrap worker and actual issued task principal')
    observers = value['observers']
    require(isinstance(observers, dict) and set(observers) in ({'1'}, {'1', '2'}), 'custody observers require exact attempt numbers')
    attempts = []
    for observer in observers.values():
        require(isinstance(observer, dict) and set(observer) == {'worker', 'mcp', 'http'}, 'incomplete custody observer identity')
        worker, mcp, http = observer['worker'], observer['mcp'], observer['http']
        require(isinstance(worker, dict) and set(worker) == {'attemptId', 'imageUri', 'publicBundleDigest', 'taskRoleArn',
            'lifecycle', 'transport', 'evidence'} and
            set(worker['lifecycle']) == {'region', 'stateMachineArn', 'definitionDigest', 'stateMachineRoleArn',
                'clusterArn', 'taskDefinitionArn', 'claimTable', 'expiresEpoch'}, 'custody must freeze the exact reviewed worker configuration')
        worker_task_role(worker)
        queue_document(worker)
        require(isinstance(mcp, dict) and set(mcp) == {'secretArn', 'versionId', 'nativeContext'} and
                re.fullmatch(r'arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:honua-agent-'+re.escape(worker['attemptId'])+r'-mcp-[A-Za-z0-9]{6}', mcp['secretArn']) and
                re.fullmatch('[a-f0-9]{64}', mcp['versionId']) and
                isinstance(mcp['nativeContext'], dict) and set(mcp['nativeContext']) == {'key', 'sha256'} and
                re.fullmatch('sha256:[a-f0-9]{64}', mcp['nativeContext']['sha256']), 'custody lacks exact immutable issued MCP/context binding')
        require(isinstance(http, dict) and set(http) == {'endpoint', 'attemptId', 'credentialKind'} and
                http['credentialKind'] == 'api-key' and re.fullmatch('[a-z0-9-]{1,80}', http['attemptId']),
                'custody requires its exact independent task-principal HTTP observer')
        uri = urlsplit(http['endpoint']); uri.port
        require(uri.hostname and not uri.username and not uri.password and not uri.query and not uri.fragment and not uri.path and
                (uri.scheme == 'https' or uri.scheme == 'http' and uri.hostname in ('localhost', '127.0.0.1', '::1')),
                'custody task observer origin is invalid')
        attempts.append(worker['attemptId'])
    require(len(attempts) == len(set(attempts)), 'custody retries cannot reuse a worker attempt')


def replay(report, declaration, context, *, complete=True):
    contract(declaration)
    require(isinstance(report, dict) and set(report) == {'schemaVersion', 'profile', 'name', 'context', 'contractDigest',
        'stage', 'startedAt', 'completedAt', 'proof', 'proofReference', 'originalRecord', 'values'} and type(report['schemaVersion']) is int and
        report['schemaVersion'] == 1 and report['profile'] == PROFILE and report['stage'] == 'running' and
        report['contractDigest'] == canonical_digest(declaration) and report['context'] == {key: context[key] for key in IDENTITY},
        'custody report differs from the frozen original candidate/attempt')
    require(str(context['attempt']) in declaration['observers'], 'custody report uses an undeclared attempt')
    reference, original_record = report['proofReference'], report['originalRecord']
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'} and
            canonical_digest(original_record) == reference['sha256'] and original_record.get('producer') == 'studio-publication-native' and
            original_record.get('candidateKey') == context['componentBindingDigest'] and
            original_record.get('identity') == {key: context[key] for key in ('campaignId', 'caseId', 'attempt')} and
            original_record.get('phase') == 'custody-started-worker' and original_record.get('value') == report['proof'],
            'custody report lacks its original authenticated producer record')
    observer = declaration['observers'][str(context['attempt'])]
    worker = observer['worker']
    proof = report['proof']
    require(proof.get('workerConfiguration') == worker, 'custody worker differs from its frozen attempt configuration')
    config = proof.get('lifecycleConfiguration', {})
    require(all(config.get(key) == value for key, value in worker['lifecycle'].items()) and
        config.get('candidateKey') == context['componentBindingDigest'] and config.get('attemptId') == worker['attemptId'] and
        config.get('transportDigest') == canonical_digest(worker['transport']) and
        config.get('context') == {key: context[key] for key in IDENTITY if key != 'componentBindingDigest'},
        'original launch context/lifecycle differs from the frozen worker')
    begin, end = instant(report['startedAt']), instant(report['completedAt'])
    require(begin <= end and instant(context['startedAt']) <= begin <= end <= instant(context['completedAt']),
            'custody reads are stale or outside the original producer attempt')
    reads = proof.get('lifecycleReads')
    require(isinstance(reads, list) and 4 <= len(reads) <= 12, 'original lifecycle reads are missing or unbounded')
    remaining = copy.deepcopy(reads)
    class Reader:
        def __init__(self, service): self.service = service
        def __getattr__(self, method):
            def read(**arguments):
                require(remaining, 'custody replay lacks its original native read')
                selected = remaining.pop(0)
                require(set(selected) == {'service', 'operation', 'request', 'response', 'observedAt'} and
                        selected['service'] == self.service and selected['operation'] == method and
                        canonical_digest(selected['request']) == canonical_digest(arguments) and
                        begin <= instant(selected['observedAt']) <= end,
                        'custody original read belongs to another attempt, operation or time')
                return copy.deepcopy(selected['response'])
            return read
    lifecycle = WorkerLifecycle(config, states=Reader('stepfunctions'), ecs=Reader('ecs'), db=Reader('dynamodb'))
    observed = lifecycle.observe(proof.get('launch'))
    require(not remaining and observed == proof.get('lifecycle') and observed['taskStatus'] == observed['executionStatus'] == 'RUNNING',
            'custody lifecycle original did not prove this exact running worker')
    # A durable before-send launch intent plus authenticated native execution
    # start/readback proves an actual start; an unknown StartExecution reply is
    # never resent or replaced by a caller-provided task ARN.
    executions = [row['response'] for row in reads if row['operation'] == 'describe_execution']
    require(len(executions) == 1 and begin <= instant(executions[0].get('startDate')) <= end and
            config['expiresEpoch']-6000 <= instant(executions[0]['startDate']).timestamp() and end.timestamp() < config['expiresEpoch'],
            'original native execution start is missing or stale')
    actual = proof.get('observation', {})
    relationships = declaration['relationships']
    require(actual.get('bootstrapWorkerId') == relationships['bootstrapWorkerId'] and
            actual.get('taskPrincipalId') == relationships['taskPrincipalId'] and
            actual.get('taskWorkerId') == observed['taskArn'] != relationships['bootstrapWorkerId'] and
            actual.get('bootstrapWorkerTerminalState') == 'terminated' and actual.get('taskProvisioningCredentialIds') == [],
            'custody native task identity differs from its original bootstrap/principal relationship')
    from agent_e2e_native_bindings import original
    principal = original(proof.get('taskPrincipalRead'), observer=observer['http'], candidate=context['componentBindingDigest'], path='/api/v1/agent/target')
    require(principal.get('principal', {}).get('actorId') == relationships['taskPrincipalId'] and
            principal['principal'].get('isHumanSession') is False and principal['principal'].get('authenticationKind') == 'api-key',
            'custody task principal lacks its independent authenticated native projection')
    mcp = proof.get('mcp', {})
    require(all(mcp.get(key) == value for key, value in observer['mcp'].items()), 'custody MCP differs from frozen immutable version/context')
    isolated_definition(proof.get('definition', {}), worker, mcp)
    isolated_task(proof.get('nativeTasks', {}), worker, observed['taskArn'])
    role = proof.get('role', {})
    require(role.get('roleArn') == worker_task_role(worker) and role.get('attached', {}).get('AttachedPolicies') == [] and
            not role['attached'].get('IsTruncated') and not role.get('inline', {}).get('IsTruncated') and
            len(role.get('inline', {}).get('PolicyNames', [])) == 1 and role.get('policy', {}).get('PolicyDocument') == role.get('policyDocument') and
            role['policy'].get('RoleName') == worker['taskRoleArn'].rsplit('/', 1)[1] and
            role['policy'].get('PolicyName') == role['inline']['PolicyNames'][0],
            'custody lacks its original independently read worker role authority')
    require(role['policyDocument'] == queue_document(worker),
            'custody worker role contains provisioning authority')
    require(report['values'] == {'taskWorkerId': observed['taskArn']}, 'custody resolved ID differs from original AWS task')
    return copy.deepcopy(report['values'])


def retain(durable, context, *, name, declaration, proof_reference, started_at, completed_at):
    durable._require_live_lease()
    require(isinstance(name, str) and re.fullmatch('[a-z][a-z0-9-]{0,40}', name), 'invalid custody binding name')
    original = durable.state.get('scenarioOperations', {}).get(proof_reference.get('key'))
    require(isinstance(original, dict) and canonical_digest(original) == proof_reference.get('sha256'),
            'custody proof must be an original retained producer record')
    proof = original['value']
    worker = declaration['observers'][str(context['attempt'])]['worker']
    launch = durable.state.get('workerLaunches', {}).get(worker['attemptId'])
    require(launch is not None and canonical_digest(launch) == canonical_digest(proof.get('launch')),
            'custody has no original retained before-send worker launch intent')
    key = durable._fixture_identity(context)+'/'+name+'/running'
    require(key not in durable.state.setdefault('nativeBindings', {}), 'custody worker binding is already retained; no rebinding')
    report = {'schemaVersion': 1, 'profile': PROFILE, 'name': name,
        'context': {field: context[field] for field in IDENTITY}, 'contractDigest': canonical_digest(declaration),
        'stage': 'running', 'startedAt': started_at, 'completedAt': completed_at, 'proof': copy.deepcopy(proof),
        'proofReference': copy.deepcopy(proof_reference), 'originalRecord': copy.deepcopy(original),
        'values': {'taskWorkerId': proof['observation']['taskWorkerId']}}
    replay(report, declaration, {**context, 'startedAt': started_at, 'completedAt': completed_at})
    durable.state['nativeBindings'][key] = copy.deepcopy(report)
    durable._persist()
    return report
