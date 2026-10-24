"""Fund the original native E and drive its already installed worker.

No per-turn infrastructure or secret installation occurs here. Unknown starts,
native mutations, provider outcomes or cleanup retain their original liabilities.
Retirement is deliberately not inferred from a stopped task or conversation.
"""
from __future__ import annotations

import copy
import json
import re
import time
from urllib.parse import urlsplit

from agent_e2e_console_dispatcher import McpDispatcher
from agent_e2e_console_loop import ExecutionLoop
from agent_e2e_console_runtime import ExecutionJournal, secret_for, require
from agent_e2e_controller import BedrockModel
from agent_e2e_installed_container import BIND_PROTOCOL
from agent_e2e_execution_protocol import validate_worker_bound
from agent_e2e_installed_worker import InstalledSlot, sdk_original
from agent_e2e_pack import canonical_digest
from agent_e2e_worker import Worker, InstalledSqsBudget, encode
from agent_e2e_execution_witness import original_http


class GuardedModel:
    def __init__(self, model, guard):
        self.model, self.guard, self.config = model, guard, model.config

    def request(self, messages):
        return self.model.request(messages)

    def quote(self, request):
        self.guard()
        return self.model.quote(request)

    def invoke(self, body, **arguments):
        self.guard()
        # The model retains a real response before checking any later fence.
        # Losing authority cannot erase the original paid outcome or its hold.
        return self.model.invoke(body, **arguments)


def queue_role(iam, descriptor, expected_boundary):
    """Fresh complete queue-only role/trust read; no managed-policy fallback."""
    name = descriptor['taskRoleArn'].rsplit('/', 1)[1]
    region, account = descriptor['region'], descriptor['taskRoleArn'].split(':')[4]
    require(isinstance(expected_boundary, str) and re.fullmatch('arn:aws:iam::'+account+r':policy/[A-Za-z0-9+=,.@_/-]+', expected_boundary),
            'worker original reviewed boundary proof is unavailable')
    role = iam.get_role(RoleName=name)
    actual = role.get('Role', {})
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': 'sts:AssumeRole',
        'Principal': {'Service': 'ecs-tasks.amazonaws.com'}, 'Condition': {
            'StringEquals': {'aws:SourceAccount': account}, 'ArnLike': {'aws:SourceArn': 'arn:aws:ecs:'+region+':'+account+':*'}}}]}
    require(actual.get('Arn') == descriptor['taskRoleArn'] and
            actual.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn') == expected_boundary and
            actual.get('AssumeRolePolicyDocument') == trust,
            'worker current role boundary or ECS trust differs from the reviewed installation')
    attached = iam.list_attached_role_policies(RoleName=name)
    inline = iam.list_role_policies(RoleName=name)
    require(not attached.get('IsTruncated') and not attached.get('Marker') and attached.get('AttachedPolicies') == [] and
            not inline.get('IsTruncated') and not inline.get('Marker') and isinstance(inline.get('PolicyNames'), list) and
            len(inline['PolicyNames']) == 1, 'worker current policy inventory is incomplete or widened')
    policy = iam.get_role_policy(RoleName=name, PolicyName=inline['PolicyNames'][0])
    def queue_arn(field):
        uri = urlsplit(descriptor[field])
        return 'arn:aws:sqs:'+region+':'+account+':'+uri.path.rsplit('/', 1)[1]
    document = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage'], 'Resource': queue_arn('commandQueueUrl')},
        {'Effect': 'Allow', 'Action': ['sqs:SendMessage'], 'Resource': queue_arn('replyQueueUrl')}]}
    require(policy.get('RoleName') == name and policy.get('PolicyName') == inline['PolicyNames'][0] and
            policy.get('PolicyDocument') == document, 'worker current authority exceeds its two original queues')
    return {'role': sdk_original(role), 'attached': sdk_original(attached), 'inline': sdk_original(inline), 'policy': sdk_original(policy)}


class InstalledExecutionDriver:
    def __init__(self, *, native, verified, registration, states, ecs, db, sqs, iam,
                 bedrock, guard, clock=time.time, sleep=time.sleep, journal_factory=ExecutionJournal):
        self.native, self.verified, self.registration = native, copy.deepcopy(verified), copy.deepcopy(registration)
        self.states, self.ecs, self.db, self.sqs, self.iam = states, ecs, db, sqs, iam
        self.guard, self.clock, self.sleep = guard, clock, sleep
        self.journal_factory = journal_factory
        require(callable(guard), 'installed driver requires its current authenticated installation guard')
        require(registration['configurationDigest'] == verified['configurationDigest'] and
                registration['runtimeId'] == verified['runtimeId'] and registration['targetId'] == verified['targetId'] and
                registration['candidateKey'] == native.durable.store.candidate and
                registration['registrationDigest'] == native.configuration['runtimeBindingDigest'],
                'installed driver differs from its original native registration')
        self.workers = {row['descriptor']['slotId']: row for row in self.verified['workers']}
        require(1 <= len(self.workers) == len(verified['workers']) <= 4, 'installed physical slots overlap or are unbounded')
        from agent_e2e_runtime_configuration import validate_worker_runtime
        for worker in self.workers.values():
            validate_worker_runtime(worker)
        self.model = BedrockModel(verified['model'], client=bedrock)

    def _authorize(self):
        self.guard()
        self.native.authorize()
        observed = self.native._call('registration-read', 'GET', '/registration')
        require(observed['value'] == self.registration, 'actual native runtime registration or credential changed')

    def _slot(self, selected):
        return InstalledSlot(selected['descriptor'], states=self.states, ecs=self.ecs, db=self.db, clock=self.clock)

    def _queues(self, journal):
        record = journal.record
        return InstalledSqsBudget(self.sqs, kind='controller', candidate_key=record['candidateKey'],
            runtime_id=record['runtimeId'], identity=record['executionId'])

    def _choose(self, journal, original):
        launches = journal.values('worker-intent')
        claim = original['value']['execution'].get('claim')
        selected = launches[0]['input']['slotId'] if launches else claim.get('slotId') if claim else None
        if selected is not None:
            require(selected in self.workers, 'original E cannot move to another installed slot')
            return self.workers[selected]
        for selected in self.workers.values():
            descriptor = selected['descriptor']
            key = descriptor['candidateKey']+'/runtime/'+descriptor['runtimeId']+'/slot/'+descriptor['slotId']
            actual = self.db.get_item(TableName=descriptor['claimTable'], Key={'candidateKey': {'S': key}}, ConsistentRead=True)
            if actual.get('Item') is None:
                return selected
            # Reuse of a released slot requires the complete original retirement
            # transaction/provider/cost proof. No existing row is relabelled free
            # from STOPPED, elapsed time or a missing activeExecutionId field.
        raise ValueError('installed slots retain original unsettled effects; complete retirement proof is required')

    def _fund(self, journal, descriptor):
        self._authorize()
        ledger, record = journal.durable.state['ledger'], journal.record
        owner = secret_for('admission-owner/v1', candidate=record['candidateKey'], runtime=record['runtimeId'], execution=record['executionId'])
        identity = {name: record[field] for name, field in (('execution_id', 'executionId'), ('target_id', 'targetId'),
            ('workspace_id', 'workspaceId'), ('turn_id', 'turnId'))}
        original = [row for row in ledger['interactivePaidAdmissions'] if row['id'] == record['executionId']]
        require(len(original) <= 1, 'original E paid admission is ambiguous')
        expiry = original[0]['expiresEpoch'] if original else min(int(self.clock())+descriptor['maxExecutionSeconds'], descriptor['originalFundingExpiresEpoch'])
        journal.durable.update(journal.durable.path, 'acquire-interactive-attempt', candidate_key=record['candidateKey'],
            **identity, owner_token=owner, expires_epoch=expiry)
        admission = {'executionId': record['executionId'], 'ownerToken': owner}
        prior = journal.values('admission')
        require(not prior or prior == [admission], 'original E admission owner changed')
        if not prior: journal.append('admission', admission)
        ids = {}
        for kind, maximum in (('infrastructure', 'maximumInfrastructureUsd'), ('cleanup', 'maximumCleanupUsd')):
            rid = record['executionId']+'-'+kind
            ids[kind] = rid
            rows = [row for row in journal.durable.state['ledger']['interactiveEntries'] if row['id'] == rid]
            if rows:
                require(len(rows) == 1 and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and
                        rows[0]['amountUsd'] == descriptor['liability'][maximum] and
                        rows[0]['basisDigest'] == descriptor['liability']['basisDigest'], 'original worker liability changed')
            else:
                journal.durable.update(journal.durable.path, 'reserve-interactive', candidate_key=record['candidateKey'],
                    **identity, owner_token=owner, reservation_id=rid, kind=kind, amount=descriptor['liability'][maximum],
                    basis_digest=descriptor['liability']['basisDigest'])
        return expiry, ids

    def _read(self, journal):
        record = journal.record
        observed = self.native.read(record['executionId'], record['workspaceId'], record['turnId'])
        require(observed['value']['requestDigest'] == record['requestDigest'], 'native replaced the original execution authority binding')
        return observed

    def _claim(self, journal, selected, expiry):
        original = self._read(journal)
        capability = secret_for('execution-capability/v1', candidate=journal.record['candidateKey'],
            runtime=journal.record['runtimeId'], execution=journal.record['executionId'], generation=1)
        if original['value']['execution']['state'] == 'queued':
            try:
                self.native.mutate('claim', original, {'claimGeneration': 1, 'slotId': selected['descriptor']['slotId'],
                    'expiresEpoch': expiry}, capability=capability)
            except Exception:
                pass  # Only the following original GET establishes a claim.
            original = self._read(journal)
        claim = original['value']['execution'].get('claim', {})
        require(claim.get('generation') == 1 and claim.get('slotId') == selected['descriptor']['slotId'] and
                claim.get('expiresEpoch') == expiry and isinstance(claim.get('nativeBindingDigest'), str),
                'native claim outcome differs or is unknown; do not replace the original E')
        return original, capability

    def _physical(self, journal, slot, task_arn):
        self._authorize()
        self.native.durable.update(self.native.durable.path, 'check-interactive-attempt',
            candidate_key=journal.record['candidateKey'], execution_id=journal.record['executionId'],
            owner_token=journal.values('admission')[0]['ownerToken'])
        expected = {'runtimeId': slot.config['runtimeId'], 'slotId': slot.config['slotId'],
            'executionArn': slot.execution_arn, 'taskDefinitionArn': slot.config['taskDefinitionArn'],
            'inputDigest': canonical_digest(slot.input)}
        for key, field in ((slot.claim_key, 'executionId'), (slot.slot_key, 'activeExecutionId')):
            row = slot._claim(key)
            require(isinstance(row, dict) and all(row.get(k) == {'S': v} for k, v in
                {**expected, field: journal.record['executionId']}.items()), 'current installed E/slot claim changed')
        response = self.ecs.describe_tasks(cluster=slot.config['clusterArn'], tasks=[task_arn], include=['TAGS'])
        tasks = response.get('tasks', [])
        require(not response.get('failures') and len(tasks) == 1, 'original physical worker is unavailable')
        task = tasks[0]
        wanted = {'candidate': journal.record['candidateKey'], 'runtime': journal.record['runtimeId'],
            'execution': journal.record['executionId'], 'attempt': slot.config['slotId']}
        require(task.get('taskArn') == task_arn and task.get('clusterArn') == slot.config['clusterArn'] and
                task.get('taskDefinitionArn') == slot.config['taskDefinitionArn'] and task.get('lastStatus') == 'RUNNING' and
                task.get('launchType') == 'FARGATE' and all([row.get('value') for row in task.get('tags', [])
                    if row.get('key') == 'honua-agent-'+key] == [value] for key, value in wanted.items()),
                'current worker identity/status differs from its registered original')
        selected = self.workers[slot.config['slotId']]
        policies = queue_role(self.iam, slot.config, selected['proof']['native'].get('permissionsBoundaryArn'))
        journal.append('worker-authority-observation', {'physical': sdk_original(response), 'iam': policies})
        observed = self._read(journal)
        require(observed['value']['execution']['state'] in ('claimed', 'running'),
                'original native execution is no longer eligible for worker effects')
        return observed

    def _binding(self, journal, slot, original, physical, capability):
        queues = self._queues(journal)
        task = physical['taskArn']
        claim = original['value']['execution']['claim']
        if claim.get('workerId') is None:
            try:
                self.native.mutate('worker', original, {'claimGeneration': claim['generation'], 'slotId': slot.config['slotId'],
                    'workerId': task, 'nativeBindingDigest': claim['nativeBindingDigest']}, capability=capability)
            except Exception:
                pass
            original = self._read(journal)
            claim = original['value']['execution']['claim']
        require(claim.get('workerId') == task, 'original native worker binding is unknown or differs')
        value = {'protocol': BIND_PROTOCOL, **{k: slot.input[k] for k in ('candidateKey', 'runtimeId', 'slotId', 'executionId',
            'workspaceId', 'turnId', 'claimGeneration', 'expiresEpoch', 'nativeBindingDigest')}, 'workerId': task}
        prior = journal.values('worker-binding-intent')
        require(not prior or prior == [value], 'original physical worker bind intent changed')
        acknowledged = journal.values('worker-binding-observation')
        if acknowledged:
            require(len(acknowledged) == 1 and journal.values('worker-binding-reply'),
                    'original worker acknowledgement is ambiguous or lacks its worker reply')
            reference = acknowledged[0]['reference']
            record = journal.record
            retained = original_http(journal.durable.state.get('nativeRuntimeHttp', {}), reference,
                path='/api/v1/agent/runtimes/'+record['runtimeId']+'/executions/'+record['executionId']+
                    '?workspaceId='+record['workspaceId']+'&turnId='+record['turnId'],
                endpoint=record['endpoint'], candidate=record['candidateKey'], runtime=record['runtimeId'], execution=record['executionId'])
            observed = self._physical(journal, slot, task)
            require(observed['value'].get('workerBinding') == retained.get('workerBinding'),
                    'current native acknowledgement replaced the original task/context')
            return observed
        if not prior:
            journal.append('worker-binding-intent', value)
            self._physical(journal, slot, task)
            queues.send_message(QueueUrl=slot.config['commandQueueUrl'], MessageBody=encode({**value, 'capability': capability}),
                MessageGroupId=slot.config['slotId'], MessageDeduplicationId=canonical_digest(value)[7:])
        # A lost send ACK is recovered only through native task-authenticated GET
        # and the original reply queue, never by sending the first bind again.
        for _ in range(60):
            self._physical(journal, slot, task)
            response = queues.receive_message(QueueUrl=slot.config['replyQueueUrl'], MaxNumberOfMessages=1,
                WaitTimeSeconds=2, VisibilityTimeout=30)
            for message in response.get('Messages', []):
                require(len(message['Body'].encode()) <= 32768, 'oversized native worker binding reply')
                reply = json.loads(message['Body'])
                validate_worker_bound(reply, value)
                journal.append('worker-binding-reply', {'reply': reply})
                observed = self._read(journal)
                actual = observed['value'].get('workerBinding', {})
                require(actual.get('workerId') == task and actual.get('executionId') == journal.record['executionId'] and
                        actual.get('acknowledgement') == {key: self.registration['taskPrincipal'][key]
                            for key in ('actorId', 'credentialVersion')} and
                        actual.get('contextDigest') == canonical_digest(observed['value']['binding']['turn']['context']),
                        'native did not independently acknowledge the actual registered task/context')
                if not journal.values('worker-binding-observation'):
                    journal.append('worker-binding-observation', {'reference': observed['reference']})
                queues.delete_message(QueueUrl=slot.config['replyQueueUrl'], ReceiptHandle=message['ReceiptHandle'])
                return observed
        raise ValueError('original worker bind outcome remains unknown; no model admission or relaunch')

    def execute(self, original):
        self._authorize()
        journal = self.journal_factory(self.native.durable, original)
        original = self._read(journal)
        require(original['value']['execution']['state'] in ('queued', 'claimed', 'running'),
                'original native execution is no longer eligible for admission')
        selected = self._choose(journal, original)
        slot = self._slot(selected)
        expiry, reservations = self._fund(journal, slot.config)
        claimed, capability = self._claim(journal, selected, expiry)
        claim = claimed['value']['execution']['claim']
        launches = journal.values('worker-intent')
        if launches:
            slot.restore(journal, launches[0])
            physical = slot.observe(journal)
        else:
            launch = slot.bind(claimed, generation=claim['generation'], expiry=expiry,
                native_binding_digest=claim['nativeBindingDigest'], reservation_ids=reservations)
            current = self._read(journal)
            require(current['value']['execution']['state'] == 'claimed' and
                    current['value']['execution']['claim'] == claim,
                    'original native execution changed before its one worker start')
            physical = slot.start(journal, launch)
        while physical['taskStatus'] != 'RUNNING':
            require(physical['taskStatus'] != 'STOPPED' and self.clock() < expiry,
                    'original physical worker stopped/expired; retirement and all liabilities remain unresolved')
            self.sleep(2)
            physical = slot.observe(journal)
        acknowledged = self._binding(journal, slot, self._read(journal), physical, capability)
        def guard():
            observed = self._physical(journal, slot, physical['taskArn'])
            require(observed['value']['execution']['state'] == 'running' and
                    observed['value'].get('workerBinding') == acknowledged['value'].get('workerBinding'),
                    'original native worker claim/context is no longer current')
        worker = Worker(self._queues(journal), {**{k: slot.config[k] for k in ('commandQueueUrl', 'replyQueueUrl')},
            'attemptId': journal.record['executionId'], 'expiresEpoch': expiry}, clock=self.clock)
        dispatcher = McpDispatcher(journal=journal, worker=worker, capability=capability, guard=guard)
        result = ExecutionLoop(native=self.native, journal=journal, original=original,
            model=GuardedModel(self.model, guard), dispatcher=dispatcher, configuration=self.verified['operationPolicy'], clock=self.clock).run()
        # A final message or question is not proof of native completion or full
        # provider retirement. Those independent protocols must settle before
        # any paid slot can be reused.
        return {'executionId': journal.record['executionId'], 'conversation': result, 'retirement': 'unresolved'}
