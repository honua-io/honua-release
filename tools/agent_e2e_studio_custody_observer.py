"""Read original setup retirement and independently observe a separate worker.

This module sends only authenticated GET/Describe/List requests. Private setup
credentials stay in the controller for the exact old-credential refusal probe.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data
from agent_e2e_p10_backend import verify_worker_definition, worker_task_role
from agent_e2e_native_costs import serializable
from agent_e2e_worker_lifecycle import WorkerLifecycle
from datetime import datetime, timezone

ASSERTIONS = {'custody.bootstrap-revoked', 'custody.worker-replaced'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def queue_document(config):
    transport = config['transport']
    require(isinstance(transport, dict) and set(transport) == {'attemptId', 'expiresEpoch', 'commandQueueUrl', 'replyQueueUrl'} and
            transport['attemptId'] == config['attemptId'] and type(transport['expiresEpoch']) is int and
            transport['expiresEpoch'] == config['lifecycle']['expiresEpoch'] and
            transport['commandQueueUrl'] != transport['replyQueueUrl'], 'worker queue transport differs from its exact attempt/lifetime')
    def arn(queue):
        uri = urlsplit(queue)
        require(uri.scheme == 'https' and uri.hostname == 'sqs.'+config['lifecycle']['region']+'.amazonaws.com' and
                not uri.query and not uri.fragment and not uri.username and not uri.password and not uri.port,
                'worker queue origin differs from reviewed transport')
        parts = uri.path.strip('/').split('/')
        require(len(parts) == 2 and parts[0] == config['taskRoleArn'].split(':')[4] and
                re.fullmatch(r'[A-Za-z0-9_-]{1,75}\.fifo', parts[1]) and
                queue == 'https://sqs.'+config['lifecycle']['region']+'.amazonaws.com/'+parts[0]+'/'+parts[1],
                'worker requires its exact account and canonical FIFO queue name')
        return 'arn:aws:sqs:'+config['lifecycle']['region']+':'+parts[0]+':'+parts[1]
    return {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage'], 'Resource': arn(config['transport']['commandQueueUrl'])},
        {'Effect': 'Allow', 'Action': ['sqs:SendMessage'], 'Resource': arn(config['transport']['replyQueueUrl'])}]}


def isolated_definition(definition, config, mcp):
    container = verify_worker_definition(definition, config)
    require(definition.get('taskDefinitionArn') == config['lifecycle']['taskDefinitionArn'] and
            container['secrets'] == [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': mcp['secretArn']+':::'+mcp['versionId']}],
            'worker secret is not the original immutable agent-only MCP configuration')
    require(not container.get('environmentFiles') and not container.get('volumesFrom') and
            not container.get('credentialSpecs') and not container.get('repositoryCredentials') and
            not container.get('logConfiguration', {}).get('secretOptions') and
            sorted(container.get('mountPoints', []), key=lambda row: row.get('sourceVolume', '')) == [
                {'sourceVolume': 'journal', 'containerPath': '/journal', 'readOnly': False},
                {'sourceVolume': 'workspace', 'containerPath': '/workspace', 'readOnly': False}] and
            len(definition.get('volumes', [])) == 2 and
            {row.get('name') for row in definition['volumes']} == {'journal', 'workspace'} and
            all(row in ({'name': row.get('name')}, {'name': row.get('name'), 'host': {}}) for row in definition['volumes']),
            'worker has unreviewed external credential files, volumes or log secret injections')


def isolated_task(native, config, task_arn):
    tasks = native.get('tasks', [])
    require(not native.get('failures') and len(tasks) == 1 and tasks[0].get('taskArn') == task_arn and
            tasks[0].get('taskDefinitionArn') == config['lifecycle']['taskDefinitionArn'] and tasks[0].get('lastStatus') == 'RUNNING' and
            not tasks[0].get('enableExecuteCommand', True) and
            all(row == {'name': 'worker'} for row in tasks[0].get('overrides', {}).get('containerOverrides', [])) and
            not tasks[0].get('overrides', {}).get('taskRoleArn') and not tasks[0].get('overrides', {}).get('executionRoleArn'),
            'actual task worker has unreviewed credential/environment overrides')


def validate(config):
    require(isinstance(config, dict) and set(config) == {'schemaVersion', 'retirement', 'nativeContext', 'workerMcp'} and
            type(config['schemaVersion']) is int and config['schemaVersion'] == 1,
            'custody requires exact original retirement, context and worker MCP references')
    for name in ('retirement', 'nativeContext', 'workerMcp'):
        ref = config[name]
        require(isinstance(ref, dict) and set(ref) == {'key', 'sha256'} and
                isinstance(ref['key'], str) and 0 < len(ref['key']) <= 512 and
                isinstance(ref['sha256'], str) and re.fullmatch('sha256:[a-f0-9]{64}', ref['sha256']),
                'custody reference is absent or not immutable')


def record(state, ref, *, producer, candidate):
    value = state.get('scenarioOperations', {}).get(ref['key'])
    require(isinstance(value, dict) and canonical_digest(value) == ref['sha256'] and
            value.get('producer') == producer and value.get('candidateKey') == candidate,
            'original private custody record is absent or changed')
    return value['value']


def helpers():
    # The private controller executes with this same source directory on its
    # path. Add only the checked sibling directory for standalone backend use.
    directory = str(Path(__file__).resolve().parent/'hosted_pilot')
    if directory not in sys.path:
        sys.path.insert(0, directory)
    from setup_key_retirement import observe_preparation, validate as validate_retirement
    from setup_key_retirement_dispatch import verify_definition
    from worker_mcp_configuration import retained_context, verify_injected_secret
    return observe_preparation, validate_retirement, verify_definition, retained_context, verify_injected_secret


class StudioCustodyObserver:
    def __init__(self, session, *, ecs, s3, states, iam):
        self.session, self.ecs, self.s3, self.states, self.iam = session, ecs, s3, states, iam
        self.config = session.config['studio']['custody']
        validate(self.config)
        self.candidate = session.campaign['componentBindingDigest']
        self.http = session.http('observer')

    def call(self, client, method, **kwargs):
        self.http.authorize()
        answer = getattr(client, method)(**kwargs)
        self.http.authorize()
        return answer

    def originals(self):
        session, config = self.session, self.config
        state = session.durable.state
        completed = record(state, config['retirement'], producer='private-setup-key-retirement', candidate=self.candidate)
        prefix = completed.get('retainedPrefix')
        require(isinstance(prefix, str) and config['retirement']['key'] == prefix+'/completed' and
                completed.get('schema') == 'honua.private-setup-key-retirement/v1' and
                completed.get('fixtureReference') == session.config['studio']['bootstrapFixture'] and
                completed.get('candidateKey') == self.candidate and completed.get('postRevocationProbeStatus') == 401,
                'retirement is not the original saved fixture setup key')
        def stage(name):
            raw = state.get('scenarioOperations', {}).get(prefix+'/'+name)
            require(isinstance(raw, dict) and raw.get('producer') == 'private-setup-key-retirement' and
                    raw.get('candidateKey') == self.candidate, 'original retirement observation is missing')
            return raw['value']
        start = stage('start')
        require(start.get('configurationDigest') == completed.get('configurationDigest') and
                start.get('setupKey') == completed.get('setupKey') and start.get('native') == completed.get('native') and
                start.get('preparation') == completed.get('preparation'), 'retirement start and completed custody differ')
        native = completed['native']
        definition = self.call(self.ecs, 'describe_task_definition', taskDefinition=native['taskDefinitionArn'])['taskDefinition']
        containers = definition.get('containerDefinitions', [])
        require(len(containers) == 1, 'original retirement task definition is ambiguous')
        env = containers[0].get('environment', [])
        require(len(env) == 1 and env[0].get('name') == 'PILOT_SETUP_RETIREMENT_CONFIG',
                'original retirement task lacks its fixed configuration')
        original = json.loads(env[0]['value'])
        preparation, validate_retirement, verify_definition, retained_context, verify_injected_secret = helpers()
        validate_retirement(original, allow_expired=True)
        verify_definition(definition, original)
        require(canonical_digest(original) == completed['configurationDigest'] and
                original['candidateKey'] == self.candidate and original['endpoint'] == session.config['http']['endpoint'] and
                original['setupKey'] == completed['setupKey'] and original['fixtureReference'] == completed['fixtureReference'] and
                original['operation'] == completed['operation'] and definition['taskDefinitionArn'] == native['taskDefinitionArn'],
                'native retirement task belongs to another target, credential or fixture')
        terminal = self.call(self.ecs, 'describe_tasks', cluster=original['clusterArn'], tasks=[native['taskArn']])
        retired = terminal.get('tasks', [])
        require(not terminal.get('failures') and len(retired) == 1 and retired[0].get('taskArn') == native['taskArn'] and
                retired[0].get('taskDefinitionArn') == native['taskDefinitionArn'] and retired[0].get('lastStatus') == 'STOPPED' and
                not retired[0].get('enableExecuteCommand', True) and len(retired[0].get('containers', [])) == 1 and
                retired[0]['containers'][0].get('image') == native['imageUri'] and retired[0]['containers'][0].get('exitCode') == 0,
                'private retirement task still has live setup authority or lacks positive termination proof')
        context = retained_context(session.durable, config['nativeContext'], original['endpoint'])
        agent = context['issuedAgentKey']
        issued = state.get('scenarioOperations', {}).get(context['nativeBootstrapReference']['key'].rsplit('/key-', 1)[0]+'/key-setup', {})
        require(issued.get('producer') == 'private-native-bootstrap' and issued.get('candidateKey') == self.candidate,
                'setup issuance is not the original native bootstrap paired with this agent')
        setup = issued.get('value', {})
        require({key: setup.get(key) for key in ('apiKeyId', 'secretArn', 'versionId')} == original['setupKey'] and
                setup['apiKeyId'] != agent['apiKeyId'] and 'admin:write' in setup.get('permissions', []) and
                'agent:workspace' not in setup['permissions'] and completed['revokedKeyId'] == setup['apiKeyId'] and
                session.initial_fixture()['fixture']['ownerPrincipalId'] == agent['apiKeyId'] and
                session.config['studio']['requesterAgentContext'] == context['agentContext'],
                'bootstrap or task identity differs from the original issued permissions and saved fixture')
        # Recoveries can legitimately carry an ancestor positive control. Every
        # link must point to the immutable original start retained by that task.
        positive_prefix = prefix
        for _ in range(8):
            positive = state.get('scenarioOperations', {}).get(positive_prefix+'/active-setup-read', {})
            if positive:
                break
            ancestor = state.get('scenarioOperations', {}).get(positive_prefix+'/original-retirement', {})
            require(ancestor.get('producer') == 'private-setup-key-retirement' and ancestor.get('candidateKey') == self.candidate,
                    'retirement recovery lacks the original authenticated positive control')
            ancestor = ancestor.get('value', {})
            positive_prefix = ancestor.get('originalPrefix')
            prior = state.get('scenarioOperations', {}).get(str(positive_prefix)+'/start', {})
            require(prior.get('producer') == 'private-setup-key-retirement' and prior.get('candidateKey') == self.candidate and
                    prior.get('value', {}).get('setupKey') == original['setupKey'] and
                    prior['value'].get('fixtureReference') == original['fixtureReference'] and
                    prior['value'].get('native', {}).get('taskArn') == ancestor.get('taskArn'),
                    'retirement recovery changes the original setup task or fixture')
        require(positive.get('producer') == 'private-setup-key-retirement' and positive.get('candidateKey') == self.candidate and
                positive.get('value', {}).get('httpStatus') == 200, 'setup credential has no original authenticated success')
        self.effective(positive['value'].get('body'), setup, status='active', authenticates=True)
        setup_target = state.get('scenarioOperations', {}).get(positive_prefix+'/active-setup-target-read', {})
        require(completed.get('setupTargetReference') == {'key': positive_prefix+'/active-setup-target-read',
                    'sha256': canonical_digest(setup_target)}, 'completed retirement lost its original principal observation binding')
        projected = setup_target.get('value', {})
        target = projected.get('body', {})
        target = target.get('data', target)
        setup_principal = target.get('principal', {})
        require(setup_target.get('producer') == 'private-setup-key-retirement' and
                setup_target.get('candidateKey') == self.candidate and projected.get('httpStatus') == 200 and
                target.get('targetId') == context['target']['targetId'] and
                setup_principal.get('authenticationKind') == 'api-key' and setup_principal.get('isHumanSession') is False and
                setup_principal.get('studioOwnerId') == setup['apiKeyId'] and
                isinstance(setup_principal.get('actorId'), str) and 0 < len(setup_principal['actorId']) <= 512 and
                setup_principal['actorId'] != context['agentPrincipal']['actorId'],
                'original active setup credential lacks its actual native principal projection')
        require(stage('old-setup-denied').get('httpStatus') == 401,
                'original old-credential probe did not refuse authentication')
        require(stage('old-setup-target-denied').get('httpStatus') == 401,
                'original old-credential target probe did not refuse authentication')
        independent = stage('revoked-independent-read')
        require(independent.get('httpStatus') == 200, 'original independent revocation read failed')
        self.effective(independent.get('body'), setup, status='revoked', authenticates=False)
        self.http.authorize()
        verify_injected_secret(setup, session.config['http']['setupCredentialEnv'], original['region'], ecs=self.ecs)
        verify_injected_secret(agent, session.config['studio']['requesterCredentialEnv'], original['region'], ecs=self.ecs)
        with tempfile.TemporaryDirectory(prefix='honua-custody-') as directory:
            stopped = preparation(original, s3=self.s3, ecs=self.ecs, states=self.states, directory=directory)
        self.http.authorize()
        require(all(stopped.get(key) == completed['preparation'].get(key) for key in
                    ('taskArn', 'taskDefinitionArn', 'lastStatus', 'exitCode', 'outcomeObject')) and stopped['lastStatus'] == 'STOPPED',
                'original setup-bearing fixture worker is not independently terminated')
        mcp = record(state, config['workerMcp'], producer='private-worker-mcp', candidate=self.candidate)
        require(config['workerMcp']['key'].endswith('/completed') and
                mcp.get('schema') == 'honua.private-worker-mcp-binding/v1' and mcp.get('nativeContext') == config['nativeContext'],
                'worker MCP credential does not originate from this issued native context')
        intent_key = config['workerMcp']['key'][:-len('/completed')]+'/intent'
        intent = state.get('scenarioOperations', {}).get(intent_key, {})
        binding = intent.get('value', {})
        require(intent.get('producer') == 'private-worker-mcp' and intent.get('candidateKey') == self.candidate and
                canonical_digest(binding) == mcp.get('publicBindingDigest') and
                all(binding.get(key) == mcp.get(key) for key in ('secretArn', 'versionId', 'nativeContext')) and
                binding.get('issuedAgentKey') == agent and binding.get('agentContext') == context['agentContext'] and
                binding.get('endpoint') == original['endpoint']+'/mcp', 'original private MCP binding was changed')
        return {'setup': setup, 'setupPrincipal': setup_principal, 'agent': agent,
                'context': context, 'mcp': mcp, 'preparation': stopped}

    @staticmethod
    def effective(body, setup, *, status, authenticates):
        value = (body or {}).get('data', body or {})
        require(value.get('id') == setup['apiKeyId'] and value.get('status') == status and
                value.get('canAuthenticate') is authenticates and
                sorted(value.get('permissions', [])) == sorted(setup['permissions']),
                'native effective permissions do not prove the exact setup credential state')

    def revoked(self, *, step):
        original = self.originals()
        path = '/api/v1/admin/api-keys/'+original['setup']['apiKeyId']+'/effective-permissions'
        live = self.http.json(step+'-independent-setup', 'GET', path)
        self.effective(data(live), original['setup'], status='revoked', authenticates=False)
        denied = self.session.http('setup').json(step+'-old-setup', 'GET', path)
        require(denied['observation']['httpStatus'] == 401, 'original setup credential still authenticates or only lacks scope')
        denied_target = self.session.http('setup').json(step+'-old-target', 'GET', '/api/v1/agent/target')
        require(denied_target['observation']['httpStatus'] == 401, 'old setup credential can still authenticate its formerly successful target route')
        target = self.session.requester(role='custody-task').json(step+'-task-target', 'GET', '/api/v1/agent/target')
        actual = data(target)
        require(actual.get('principal') == original['context']['agentPrincipal'] and
                actual.get('targetId') == original['context']['target']['targetId'],
                'active task credential differs from original issued agent principal')
        response = self.session.durable.state['scenarioOperations'][target['reference']['key']]
        require(canonical_digest(response) == target['reference']['sha256'], 'original task principal response changed')
        intent = self.session.durable.state['scenarioOperations'][response['intentReference']['key']]
        require(canonical_digest(intent) == response['intentReference']['sha256'], 'original task principal intent changed')
        original['taskPrincipalRead'] = {'intent': copy.deepcopy(intent), 'response': copy.deepcopy(response)}
        result = {'bootstrapPrincipalId': original['setupPrincipal']['actorId'],
            'revokedCredentialIds': [original['setup']['apiKeyId']], 'postRevocationProbeStatus': 401}
        self.session.retain(step+'-revocation', {'observation': result, 'references': [row['reference'] for row in (live, denied, denied_target, target)],
            'originalReferences': copy.deepcopy(self.config)})
        return result, original

    def queue_role(self, config):
        role = worker_task_role(config).rsplit('/', 1)[1]
        attached = self.call(self.iam, 'list_attached_role_policies', RoleName=role)
        inline = self.call(self.iam, 'list_role_policies', RoleName=role)
        require(not attached.get('IsTruncated') and attached.get('AttachedPolicies') == [] and
                not inline.get('IsTruncated') and len(inline.get('PolicyNames', [])) == 1,
                'worker role has additional or unobserved provisioning policies')
        policy = self.call(self.iam, 'get_role_policy', RoleName=role, PolicyName=inline['PolicyNames'][0])
        require(policy.get('RoleName') == role and policy.get('PolicyName') == inline['PolicyNames'][0] and
                policy.get('PolicyDocument') == queue_document(config),
                'actual worker role grants authority beyond its two exact command/reply queues')
        return {'roleArn': config['taskRoleArn'], 'policyDocument': policy['PolicyDocument'],
                'attached': attached, 'inline': inline, 'policy': policy}

    def observe_lifecycle(self):
        session, reads = self.session, []
        outer = self
        class Reader:
            def __init__(self, service, client):
                self.service, self.client = service, client
            def __getattr__(self, method):
                require((self.service, method) in {('stepfunctions', 'describe_execution'),
                    ('stepfunctions', 'get_execution_history'), ('dynamodb', 'get_item'), ('ecs', 'describe_tasks')},
                    'custody lifecycle collector permits only original execution/claim/task reads')
                def read(**arguments):
                    response = outer.call(self.client, method, **arguments)
                    reads.append({'service': self.service, 'operation': method, 'request': copy.deepcopy(arguments),
                        'response': serializable(response), 'observedAt': datetime.now(timezone.utc).isoformat()})
                    require(len(reads) <= 12, 'custody lifecycle original read bound exceeded')
                    return response
                return read
        current = session.lifecycle
        observed = WorkerLifecycle(current.config, states=Reader('stepfunctions', current.states),
            ecs=Reader('ecs', current.ecs), db=Reader('dynamodb', current.db)).observe(session.launch)
        return observed, reads

    def worker(self, *, step, original):
        session = self.session
        session.open_worker()
        session.durable.require_worker_launch(session.launch)
        observed, reads = self.observe_lifecycle()
        require(datetime.now(timezone.utc).timestamp() < session.worker_config['lifecycle']['expiresEpoch'],
                'custody worker lifetime expired; retain original liabilities without reuse')
        require(observed.get('taskStatus') == 'RUNNING' and observed.get('executionStatus') == 'RUNNING' and
                observed.get('taskArn') and observed['taskArn'] != original['preparation']['taskArn'],
                'task worker is missing, reused or no longer running')
        config = session.worker_config
        definition = self.call(self.ecs, 'describe_task_definition', taskDefinition=config['lifecycle']['taskDefinitionArn'])['taskDefinition']
        isolated_definition(definition, config, original['mcp'])
        native = self.call(self.ecs, 'describe_tasks', cluster=config['lifecycle']['clusterArn'], tasks=[observed['taskArn']])
        isolated_task(native, config, observed['taskArn'])
        role = self.queue_role(config)
        result = {'bootstrapWorkerId': original['preparation']['taskArn'], 'taskWorkerId': observed['taskArn'],
            'bootstrapWorkerTerminalState': 'terminated', 'taskPrincipalId': original['context']['agentPrincipal']['actorId'],
            'taskProvisioningCredentialIds': []}
        session.retain(step+'-worker', {'observation': result, 'lifecycle': observed,
            'definition': serializable(definition), 'nativeTasks': serializable(native), 'role': serializable(role),
            'lifecycleReads': reads, 'lifecycleConfiguration': copy.deepcopy(session.lifecycle.config),
            'launch': copy.deepcopy(session.launch), 'workerConfiguration': copy.deepcopy(config), 'mcp': copy.deepcopy(original['mcp']),
            'taskPrincipalRead': copy.deepcopy(original['taskPrincipalRead'])})
        return result
