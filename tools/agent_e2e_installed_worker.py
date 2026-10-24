"""Controller for a fixed installed worker slot; no per-turn infrastructure.

The immutable native execution, not a case or queue delivery, owns the launch.
Lost launch acknowledgements are reconciled through the original Standard
execution and permanent claim. STOPPED never releases the paid admission/slot.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
import re
import time

from agent_e2e_console_runtime import require, identifier, execution_id, digest
from agent_e2e_pack import canonical_digest
from agent_e2e_worker_lifecycle import WorkerLifecycle, terraform_definition_digest
from agent_e2e_budget import validate as validate_budget, money
from agent_e2e_interactive_budget import validate_retained_funding


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sdk_original(value):
    """Preserve parsed SDK timestamps without an arbitrary string fallback."""
    def timestamp(item):
        require(isinstance(item, datetime) and item.tzinfo is not None, 'unsupported SDK observation value')
        return item.isoformat()
    return json.loads(json.dumps(value, default=timestamp, ensure_ascii=False, allow_nan=False))


class InstalledSlot:
    def __init__(self, configuration, *, states, ecs, db, clock=time.time):
        fields = {'schema', 'candidateKey', 'runtimeId', 'slotId', 'region', 'stateMachineArn',
            'stateMachineRoleArn', 'definitionDigest', 'clusterArn', 'taskDefinitionArn', 'taskRoleArn',
            'claimTable', 'originalFundingExpiresEpoch', 'maxExecutionSeconds', 'commandQueueUrl', 'replyQueueUrl',
            'mcpSecret', 'image', 'liability'}
        require(isinstance(configuration, dict) and set(configuration) == fields and
                configuration['schema'] == 'honua.installed-worker-slot/v1', 'invalid installed worker descriptor')
        c = copy.deepcopy(configuration)
        digest(c['candidateKey']); identifier(c['runtimeId'], 'runtime ID'); digest(c['definitionDigest'])
        require(isinstance(c['slotId'], str) and re.fullmatch(r'[a-z0-9-]{3,40}', c['slotId']), 'invalid physical installed slot ID')
        require(type(c['maxExecutionSeconds']) is int and 1 <= c['maxExecutionSeconds'] <= 6000 and
                type(c['originalFundingExpiresEpoch']) is int and c['originalFundingExpiresEpoch'] > 0,
                'installed worker lifetime is unbounded')
        machine = re.fullmatch(r'arn:aws:states:([a-z]{2}-[a-z]+-[0-9]):([0-9]{12}):stateMachine:([A-Za-z0-9_-]{1,80})', str(c['stateMachineArn']))
        require(machine is not None and machine[1] == c['region'], 'installed lifecycle region differs')
        region, account, family = machine.groups()
        prefix = 'arn:aws:ecs:'+region+':'+account+':'
        require(re.fullmatch(re.escape(prefix)+r'cluster/[A-Za-z0-9_-]+', str(c['clusterArn'])) and
                re.fullmatch(re.escape(prefix)+r'task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*', str(c['taskDefinitionArn'])) and
                all(re.fullmatch('arn:aws:iam::'+account+r':role/[A-Za-z0-9+=,.@_/-]+', str(c[field]))
                    for field in ('taskRoleArn', 'stateMachineRoleArn')),
                'installed resources differ from reviewed account/region')
        require(isinstance(c['claimTable'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{3,255}', c['claimTable']),
                'invalid installed claim table')
        for field in ('commandQueueUrl', 'replyQueueUrl'):
            require(isinstance(c[field], str) and re.fullmatch('https://sqs.'+re.escape(region)+r'.amazonaws.com/'+account+r'/[A-Za-z0-9_-]+\.fifo', c[field]),
                    'installed transport requires exact account/region FIFO queue')
        require(c['commandQueueUrl'] != c['replyQueueUrl'], 'installed transport queues overlap')
        secret = c['mcpSecret']
        require(isinstance(secret, dict) and set(secret) == {'secretArn', 'versionId'} and
                re.fullmatch('arn:aws:secretsmanager:'+region+':'+account+r':secret:[A-Za-z0-9/_+=.@-]+', str(secret['secretArn'])) and
                re.fullmatch(r'[A-Za-z0-9-]{32,64}', str(secret['versionId'])), 'installed MCP secret is not immutable')
        require(isinstance(c['image'], str) and re.fullmatch(account+r'\.dkr\.ecr\.'+region+r'\.amazonaws\.com/[a-z0-9/_-]+@sha256:[0-9a-f]{64}', c['image']),
                'installed worker image must be an exact reviewed digest')
        liability = c['liability']
        require(isinstance(liability, dict) and set(liability) == {'basisDigest', 'maximumInfrastructureUsd', 'maximumCleanupUsd'} and
                all(money(liability[k]) > 0 for k in ('maximumInfrastructureUsd', 'maximumCleanupUsd')),
                'installed worker requires original positive infrastructure/cleanup quote')
        digest(liability['basisDigest'])
        self.config, self.states, self.ecs, self.db, self.clock = c, states, ecs, db, clock
        self.family, self.region, self.account = family, region, account
        self.task_prefix = prefix+'task/'+c['clusterArn'].rsplit('/', 1)[1]+'/'

    def bind(self, native, *, generation, expiry, native_binding_digest, reservation_ids):
        return self._binding(native, generation=generation, expiry=expiry, native_binding_digest=native_binding_digest,
            reservation_ids=reservation_ids, live=True)

    def _binding(self, native, *, generation, expiry, native_binding_digest, reservation_ids, live):
        """The launch input contains authority identity, never a secret/capability."""
        record, binding = native['value']['execution'], native['value']['binding']
        original = execution_id(record['executionId'])
        require(native['value']['candidateKey'] == self.config['candidateKey'] and
                binding['runtimeId'] == self.config['runtimeId'] and type(generation) is int and generation > 0,
                'installed launch differs from original native authority')
        require(type(expiry) is int and 0 < expiry <= self.config['originalFundingExpiresEpoch'] and
                (not live or self.clock() < expiry <= self.clock()+self.config['maxExecutionSeconds']),
            'installed launch exceeds original funding/lifetime')
        value = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'runtimeId': self.config['runtimeId'],
            'slotId': self.config['slotId'], 'executionId': original, 'targetId': binding['targetId'],
            'workspaceId': binding['workspaceId'], 'turnId': binding['turn']['turnId'], 'claimGeneration': generation,
            'expiresEpoch': expiry, 'nativeBindingDigest': digest(native_binding_digest)}
        for key in ('targetId', 'workspaceId', 'turnId'): identifier(value[key], key)
        self.input = value
        self.name = 'worker-'+canonical_digest({'candidateKey': self.config['candidateKey'], 'executionId': original})[7:]
        self.execution_arn = 'arn:aws:states:'+self.region+':'+self.account+':execution:'+self.family+':'+self.name
        self.claim_key = self.config['candidateKey']+'/runtime/'+self.config['runtimeId']+'/execution/'+original
        self.slot_key = self.config['candidateKey']+'/runtime/'+self.config['runtimeId']+'/slot/'+self.config['slotId']
        require(isinstance(reservation_ids, dict) and set(reservation_ids) == {'infrastructure', 'cleanup'} and
                all(isinstance(v, str) and 0 < len(v) <= 256 for v in reservation_ids.values()) and
                len(set(reservation_ids.values())) == 2, 'installed worker requires exact nonoverlapping E reservations')
        return {'configurationDigest': canonical_digest(self.config), 'input': copy.deepcopy(value),
                'inputDigest': canonical_digest(value), 'executionArn': self.execution_arn,
                'reservationIds': copy.deepcopy(reservation_ids)}

    def restore(self, journal, launch):
        """Restore only an original intended launch, including after expiry."""
        require(isinstance(launch, dict) and set(launch) == {'configurationDigest', 'input', 'inputDigest',
                'executionArn', 'reservationIds'} and launch['configurationDigest'] == canonical_digest(self.config) and
                canonical_digest(launch['input']) == launch['inputDigest'] and journal.values('worker-intent') == [launch],
                'installed recovery lacks the unchanged original launch intent')
        record = journal.record
        reference = record['nativeOriginal']
        original = journal.durable.state.get('nativeRuntimeHttp', {}).get(reference['key'])
        require(isinstance(original, dict) and canonical_digest(original) == reference['sha256'],
                'installed recovery lacks original authenticated native execution')
        value = original['response']['body']
        supplied = launch['input']
        restored = self._binding({'value': value}, generation=supplied['claimGeneration'], expiry=supplied['expiresEpoch'],
            native_binding_digest=supplied['nativeBindingDigest'], reservation_ids=launch['reservationIds'], live=False)
        require(restored == launch, 'restored worker differs from original native input/slot')
        self._authorize(journal, launch, reconcile=True)
        return self

    def _authorize(self, journal, launch, *, reconcile=False):
        durable = journal.durable
        durable._require_live_lease()
        validate_retained_funding(durable.state)
        ledger = durable.state['ledger']
        validate_budget(ledger, allow_overrun=reconcile)
        require(ledger['candidateKey'] == self.config['candidateKey'] and
                ledger.get('interactiveAccounting', {}).get('originalExpiresEpoch') == self.config['originalFundingExpiresEpoch'],
                'installed worker differs from original candidate funding')
        admissions = journal.values('admission')
        require(len(admissions) == 1 and admissions[0]['executionId'] == self.input['executionId'],
                'original E paid admission journal missing')
        admission = admissions[0]
        rows = [row for row in ledger.get('interactivePaidAdmissions', []) if row['id'] == self.input['executionId']]
        require(len(rows) == 1 and rows[0]['ownerToken'] == admission['ownerToken'] and
                rows[0]['expiresEpoch'] == self.input['expiresEpoch'] and all(rows[0][k] == self.input[k]
                    for k in ('executionId', 'targetId', 'workspaceId', 'turnId')) and
                0 < rows[0]['expiresEpoch']-rows[0]['createdEpoch'] <= self.config['maxExecutionSeconds'],
                'installed worker admission replaced original E identity/expiry')
        if not reconcile:
            durable.update(durable.path, 'check-interactive-attempt', candidate_key=self.config['candidateKey'],
                execution_id=self.input['executionId'], owner_token=admission['ownerToken'])
        for kind, rid in launch['reservationIds'].items():
            charges = [row for row in ledger['interactiveEntries'] if row['id'] == rid]
            require(len(charges) == 1 and charges[0]['kind'] == kind and
                    charges[0]['basisDigest'] == self.config['liability']['basisDigest'] and
                    all(charges[0][k] == self.input[k] for k in ('executionId', 'targetId', 'workspaceId', 'turnId')) and
                    money(charges[0]['amountUsd']) >= money(self.config['liability'][
                        'maximumInfrastructureUsd' if kind == 'infrastructure' else 'maximumCleanupUsd']) and
                    (reconcile or charges[0]['state'] == 'reserved' and charges[0]['actualUsd'] is None),
                    'installed worker lacks its original E infrastructure/cleanup liability')

    def inspect(self):
        c = self.config
        definition = self.states.describe_state_machine(stateMachineArn=c['stateMachineArn'])
        response = self.ecs.describe_task_definition(taskDefinition=c['taskDefinitionArn'])
        return self._validate_installation(definition, response)

    def _validate_installation(self, definition, response):
        c = self.config
        require(isinstance(definition, dict) and isinstance(response, dict), 'original worker installation is malformed')
        require(definition.get('stateMachineArn') == c['stateMachineArn'] and definition.get('roleArn') == c['stateMachineRoleArn'] and
                definition.get('status') == 'ACTIVE' and definition.get('type') == 'STANDARD' and
                terraform_definition_digest(definition.get('definition')) == c['definitionDigest'],
                'installed lifecycle differs from reviewed source')
        task = response.get('taskDefinition', {})
        containers = task.get('containerDefinitions', [])
        require(task.get('taskDefinitionArn') == c['taskDefinitionArn'] and task.get('taskRoleArn') == c['taskRoleArn'] and
                task.get('status') == 'ACTIVE' and len(containers) == 1,
                'installed task definition/role is not the reviewed worker')
        container = containers[0]
        require(container.get('image') == c['image'], 'installed worker image differs')
        env_rows = container.get('environment', [])
        require(len({row['name'] for row in env_rows}) == len(env_rows), 'duplicate installed worker environment')
        env = {row['name']: row['value'] for row in env_rows}
        require(set(env) == {'HONUA_AGENT_TRANSPORT', 'AWS_DEFAULT_REGION'} and env['AWS_DEFAULT_REGION'] == c['region'],
                'installed worker has unreviewed task environment injections or region')
        transport = {'schemaVersion': 2, 'mode': 'installed-slot',
            **{key: c[key] for key in ('candidateKey', 'runtimeId', 'slotId', 'commandQueueUrl', 'replyQueueUrl',
                'originalFundingExpiresEpoch', 'maxExecutionSeconds')}}
        require(json.loads(env.get('HONUA_AGENT_TRANSPORT', '{}')) == transport,
                'actual installed worker transport differs')
        secrets = container.get('secrets', [])
        require(secrets == [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': c['mcpSecret']['secretArn']+':::'+c['mcpSecret']['versionId']}],
                'worker carries unexpected or mutable credential injections')
        require(not any(key in env for key in ('HONUA_AGENT_MCP_CONFIG', 'HONUA_NATIVE_SETUP_KEY',
                'HONUA_EXTERNAL_EXECUTION_CONTROL_KEY', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')),
                'installed worker environment discloses a privileged credential')
        return {'lifecycle': sdk_original(definition), 'taskDefinition': sdk_original(response)}

    def start(self, journal, launch):
        require(launch['input'] == self.input and launch['configurationDigest'] == canonical_digest(self.config),
                'installed launch input changed')
        journal.durable._require_live_lease()
        self._authorize(journal, launch)
        require(not journal.values('worker-intent'), 'worker already intended; reconcile original execution without relaunch')
        observed = self.inspect()  # Always recheck the current pinned resources.
        prior = journal.values('worker-installation-observation')
        if prior:
            require(len(prior) == 1 and isinstance(prior[0], dict) and set(prior[0]) == {'lifecycle', 'taskDefinition'},
                'original worker installation observation is ambiguous')
            self._validate_installation(prior[0]['lifecycle'], prior[0]['taskDefinition'])
            # Keep the first actual original. Fresh SDK RequestIds/timestamps do
            # not replace it or create an ambiguous second installation receipt.
        else:
            journal.append('worker-installation-observation', observed)
        require(self.clock() < self.input['expiresEpoch'], 'original native worker admission expired')
        journal.append('worker-intent', launch)
        self._authorize(journal, launch)
        # The original E/input is fixed before the API call. A lost response is
        # not permission to retry; observe the deterministic ARN instead.
        response = self.states.start_execution(stateMachineArn=self.config['stateMachineArn'],
            name=self.name, input=canonical(self.input))
        journal.append('worker-start-response', sdk_original(response))
        require(response.get('executionArn') == self.execution_arn, 'installed launch response identity differs')
        return self.observe(journal)

    def _claim(self, key):
        return self.db.get_item(TableName=self.config['claimTable'], Key={'candidateKey': {'S': key}},
            ConsistentRead=True).get('Item')

    def observe(self, journal):
        intents = journal.values('worker-intent')
        require(len(intents) == 1 and intents[0]['input'] == self.input and
                intents[0]['configurationDigest'] == canonical_digest(self.config), 'original installed launch intent missing')
        self._authorize(journal, intents[0], reconcile=True)
        execution = self.states.describe_execution(executionArn=self.execution_arn)
        require(execution.get('executionArn') == self.execution_arn and execution.get('name') == self.name and
                execution.get('stateMachineArn') == self.config['stateMachineArn'] and
                execution.get('inputDetails', {}).get('included') is True and execution.get('redriveCount', 0) == 0 and
                json.loads(execution.get('input', 'null')) == self.input and
                execution.get('status') in ('RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED'),
                'installed execution input/history differs or is unavailable')
        # Share the established bounded original Step Functions launch reader.
        pages = []
        def retain_page(request, response):
            value = {'request': copy.deepcopy(request), 'response': sdk_original(response)}
            journal.append('worker-history', value)
            pages.append(value)
        task_arn, event = WorkerLifecycle._launched_task(self, retain_page=retain_page)
        result = {'executionArn': self.execution_arn, 'executionStatus': execution['status'],
            'taskArn': task_arn, 'launchEventId': event, 'taskStatus': 'unobserved', 'retired': False}
        if task_arn is not None:
            claims = []
            expected = {'executionId': self.input['executionId'], 'runtimeId': self.config['runtimeId'],
                'slotId': self.config['slotId'], 'executionArn': self.execution_arn,
                'taskDefinitionArn': self.config['taskDefinitionArn'], 'inputDigest': canonical_digest(self.input)}
            for key in (self.claim_key, self.slot_key):
                claim = self._claim(key)
                claims.append({'key': key, 'row': copy.deepcopy(claim)})
                fields = copy.deepcopy(expected)
                if key == self.slot_key:
                    fields['activeExecutionId'] = fields.pop('executionId')
                require(claim is not None and all(claim.get(k) == {'S': v} for k, v in fields.items()),
                        'actual installed permanent/slot claim differs')
            result['claims'] = claims
            response = self.ecs.describe_tasks(cluster=self.config['clusterArn'], tasks=[task_arn], include=['TAGS'])
            tasks = response.get('tasks', [])
            require(not response.get('failures') and len(tasks) == 1, 'actual installed physical task unavailable')
            task = tasks[0]
            tags = task.get('tags', [])
            wanted = {'honua-agent-candidate': self.config['candidateKey'], 'honua-agent-attempt': self.config['slotId'],
                'honua-agent-runtime': self.config['runtimeId'], 'honua-agent-execution': self.input['executionId']}
            require(task.get('taskArn') == task_arn and task.get('clusterArn') == self.config['clusterArn'] and
                    task.get('taskDefinitionArn') == self.config['taskDefinitionArn'] and task.get('launchType') == 'FARGATE' and
                    task.get('lastStatus') in ('PROVISIONING', 'PENDING', 'ACTIVATING', 'RUNNING', 'DEACTIVATING', 'STOPPING', 'DEPROVISIONING', 'STOPPED') and
                    all([row.get('value') for row in tags if row.get('key') == key] == [value] for key, value in wanted.items()),
                    'installed worker physical task/authority differs')
            result['taskStatus'] = task['lastStatus']
            result['physicalTask'] = sdk_original(task)
            result['taskResponse'] = sdk_original(response)
        journal.append('worker-observation', {'observation': result, 'execution': sdk_original(execution),
            'launchHistory': copy.deepcopy(pages)})
        return result
