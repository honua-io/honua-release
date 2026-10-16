"""Trusted runtime use of Terraform-defined Fargate workers; no provisioning.

Configuration is a reviewed controller input, never model output. A retained
launch intent permits one StartExecution call, including when its reply is lost.
Observation/recovery never starts or redrives an execution, nor clears a claim.
"""
from __future__ import annotations

import copy
import json
import re
import time

from agent_e2e_budget import money, validate as validate_budget
from agent_e2e_pack import PackError, canonical_digest


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value)


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise PackError('duplicate lifecycle JSON field')
            value[key] = item
        return value
    def constant(_):
        raise PackError('nonfinite lifecycle JSON number')
    if not isinstance(raw, str) or len(raw.encode()) > 1024 * 1024:
        raise PackError('missing or oversized lifecycle JSON')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def validate_reservations(record, ledger):
    """Only retained, unsettled, exact-attempt liabilities permit a launch."""
    validate_budget(ledger)
    if ledger['candidateKey'] != record['candidateKey']:
        raise PackError('worker reservation belongs to another candidate')
    for kind, minimum in (('infrastructure', 'maximumInfrastructureUsd'), ('cleanup', 'maximumCleanupUsd')):
        rows = [r for r in ledger['entries'] if r['id'] == record['reservationIds'][kind]]
        if (len(rows) != 1 or rows[0]['kind'] != kind or rows[0]['state'] != 'reserved'
                or any(rows[0].get(k) != v for k, v in record['context'].items())
                or rows[0]['basisDigest'] != record['liability']['basisDigest']
                or money(rows[0]['amountUsd']) < money(record['liability'][minimum])):
            raise PackError('worker launch lacks its prepaid infrastructure/cleanup liability')


def open_lifecycle(config):
    """Single SDK attempt: ambiguous mutation replies require observation."""
    lifecycle = WorkerLifecycle(config, states=None, ecs=None, db=None)
    import boto3
    from botocore.config import Config
    options = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1})
    clients = {name: boto3.client(service, region_name=config['region'], config=options)
               for name, service in (('states', 'stepfunctions'), ('ecs', 'ecs'), ('db', 'dynamodb'))}
    lifecycle.states, lifecycle.ecs, lifecycle.db = clients['states'], clients['ecs'], clients['db']
    return lifecycle


class WorkerLifecycle:
    def __init__(self, config, *, states, ecs, db, clock=time.time):
        fields = {'region', 'candidateKey', 'attemptId', 'context', 'stateMachineArn',
                  'definitionDigest', 'stateMachineRoleArn', 'clusterArn', 'taskDefinitionArn',
                  'claimTable', 'expiresEpoch', 'liability'}
        if not isinstance(config, dict) or set(config) != fields:
            raise PackError('invalid worker lifecycle configuration')
        c = copy.deepcopy(config)
        if (not _digest(c['candidateKey']) or not _digest(c['definitionDigest'])
                or not isinstance(c['attemptId'], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', c['attemptId'])
                or type(c['expiresEpoch']) is not int or c['expiresEpoch'] <= 0
                or not isinstance(c['region'], str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', c['region'])
                or not isinstance(c['claimTable'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,255}', c['claimTable'])):
            raise PackError('invalid worker lifecycle identity')
        machine = re.fullmatch(r'arn:aws:states:([^:]+):([0-9]{12}):stateMachine:([A-Za-z0-9_-]{1,80})', str(c['stateMachineArn']))
        if not machine or machine[1] != c['region']:
            raise PackError('worker requires an exact regional Standard state machine')
        region, account, name = machine.groups()
        prefix = f'arn:aws:ecs:{region}:{account}:'
        if (not re.fullmatch(re.escape(prefix)+r'cluster/[A-Za-z0-9_-]+', str(c['clusterArn']))
                or not re.fullmatch(re.escape(prefix)+r'task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*', str(c['taskDefinitionArn']))
                or not re.fullmatch(f'arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+', str(c['stateMachineRoleArn']))):
            raise PackError('worker resources differ from reviewed account/region')
        context = c['context']
        if (not isinstance(context, dict) or set(context) != {'campaignId', 'caseId', 'attempt', 'campaignMode'}
                or any(not isinstance(context[k], str) or not 0 < len(context[k]) <= 256 for k in ('campaignId', 'caseId'))
                or type(context['attempt']) is not int or context['attempt'] not in (1, 2)
                or context['campaignMode'] not in ('development', 'qualification')):
            raise PackError('invalid worker case binding')
        liability = c['liability']
        if (not isinstance(liability, dict) or set(liability) != {'basisDigest', 'maximumInfrastructureUsd', 'maximumCleanupUsd'}
                or not _digest(liability['basisDigest'])
                or any(money(liability[k]) <= 0 for k in ('maximumInfrastructureUsd', 'maximumCleanupUsd'))):
            raise PackError('missing reviewed worker liability bounds')
        self.config, self.states, self.ecs, self.db, self.clock = c, states, ecs, db, clock
        self.config_digest = canonical_digest(c)
        self.name = 'worker-'+canonical_digest({'candidateKey': c['candidateKey'], 'attemptId': c['attemptId']})[7:]
        self.execution_arn = f'arn:aws:states:{region}:{account}:execution:{name}:{self.name}'
        self.task_prefix = prefix+'task/'+c['clusterArn'].rsplit('/', 1)[1]+'/'
        self.claim_key = c['candidateKey']+'/worker/'+c['attemptId']
        self.attempted = False

    def _record(self, reservation_ids):
        if (not isinstance(reservation_ids, dict) or set(reservation_ids) != {'infrastructure', 'cleanup'}
                or any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in reservation_ids.values())
                or len(set(reservation_ids.values())) != 2):
            raise PackError('invalid worker reservation identities')
        return {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'attemptId': self.config['attemptId'],
                'configurationDigest': self.config_digest, 'executionArn': self.execution_arn,
                'context': copy.deepcopy(self.config['context']), 'liability': copy.deepcopy(self.config['liability']),
                'reservationIds': dict(reservation_ids)}

    def _check_record(self, record):
        if not isinstance(record, dict) or record != self._record(record.get('reservationIds')):
            raise PackError('worker launch differs from reviewed configuration')

    def _before_expiry(self):
        if not self.clock() < self.config['expiresEpoch'] <= self.clock()+6000:
            raise PackError('worker launch is expired or exceeds bounded lifetime')

    def inspect_definition(self):
        c = self.config
        observed = self.states.describe_state_machine(stateMachineArn=c['stateMachineArn'])
        if (observed.get('stateMachineArn') != c['stateMachineArn'] or observed.get('type') != 'STANDARD'
                or observed.get('status') != 'ACTIVE' or observed.get('roleArn') != c['stateMachineRoleArn']
                or canonical_digest(_json(observed.get('definition'))) != c['definitionDigest']):
            raise PackError('deployed worker lifecycle differs from reviewed definition')

    def start(self, durable, *, reservation_ids):
        """Launch once; retain intent even when StartExecution has no reply."""
        self._before_expiry()
        if self.attempted:
            raise PackError('worker launch already attempted; recover by observation')
        self.inspect_definition()
        record = self._record(reservation_ids)
        self.attempted = True
        durable.retain_worker_launch(record)
        durable.require_worker_launch(record, launching=True)
        self._before_expiry()
        # This is not an atomic fence against a controller paused after its lease
        # check. Terraform's permanent attempt/candidate claims serialize workers.
        response = self.states.start_execution(stateMachineArn=self.config['stateMachineArn'], name=self.name, input='{}')
        if response.get('executionArn') != self.execution_arn:
            raise PackError('worker start response has another execution identity')
        return record

    def _claim(self, key):
        row = self.db.get_item(TableName=self.config['claimTable'], Key={'candidateKey': {'S': key}},
                               ConsistentRead=True).get('Item')
        if row is None:
            return None
        expected = {'candidateKey': key, 'attemptId': self.config['attemptId'], 'executionArn': self.execution_arn,
                    'taskDefinitionArn': self.config['taskDefinitionArn']}
        if any(row.get(k) != {'S': v} for k, v in expected.items()):
            raise PackError('worker claim has another execution identity')
        return row

    def _launched_task(self):
        # Forward history finds the single RunWorker result near the beginning,
        # without downloading every periodic DescribeTasks result. The reviewed
        # definition has one launch, no launch Retry and no redrive permission.
        token, seen, last_id = None, set(), 0
        for _ in range(8):
            args = {'executionArn': self.execution_arn, 'includeExecutionData': True, 'reverseOrder': False, 'maxResults': 100}
            if token:
                args['nextToken'] = token
            page = self.states.get_execution_history(**args)
            events = page.get('events')
            if not isinstance(events, list) or len(events) > 100:
                raise PackError('invalid worker execution history')
            for event in events:
                event_id = event.get('id')
                if type(event_id) is not int or event_id <= last_id:
                    raise PackError('worker history order changed')
                last_id = event_id
                if event.get('type') == 'ExecutionRedriven':
                    raise PackError('worker execution was redriven')
                details = event.get('stateExitedEventDetails', {})
                if event.get('type') == 'TaskStateExited' and details.get('name') == 'RunWorker':
                    if details.get('outputDetails', {}).get('truncated') is not False:
                        raise PackError('worker launch history is truncated or unavailable')
                    launched = _json(details.get('output')).get('launched', {})
                    tasks = launched.get('Tasks')
                    if launched.get('Failures') or not isinstance(tasks, list) or len(tasks) != 1:
                        raise PackError('worker task launch remains ambiguous')
                    arn = tasks[0].get('TaskArn')
                    if not isinstance(arn, str) or not re.fullmatch(re.escape(self.task_prefix)+r'[0-9a-f]{32}', arn):
                        raise PackError('worker launch returned a foreign task')
                    return arn, event_id
            token = page.get('nextToken')
            if not token:
                return None, None
            if not isinstance(token, str) or len(token) > 1024 or token in seen:
                raise PackError('worker history pagination changed')
            seen.add(token)
        raise PackError('worker history exceeded bounded discovery window')

    def observe(self, record):
        """Bounded independent read; a stopped process is not goal/cleanup success."""
        self._check_record(record)
        execution = self.states.describe_execution(executionArn=self.execution_arn)
        if (execution.get('executionArn') != self.execution_arn
                or execution.get('stateMachineArn') != self.config['stateMachineArn']
                or execution.get('name') != self.name or execution.get('input') != '{}'
                or execution.get('inputDetails', {}).get('included') is not True
                or execution.get('redriveCount', 0) != 0
                or execution.get('status') not in ('RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED')):
            raise PackError('worker execution identity or history differs')
        claim = self._claim(self.claim_key)
        stopped = self._claim(self.claim_key+'/stopped')
        task_arn, event_id = self._launched_task()
        result = {'executionArn': self.execution_arn, 'executionStatus': execution['status'],
                  'taskArn': task_arn, 'launchEventId': event_id, 'taskStatus': 'unobserved',
                  'stopRecorded': False, 'qualification': False}
        if task_arn is None:
            if stopped is not None:
                raise PackError('stop claim lacks the matching launch history')
            return result
        if claim is None:
            raise PackError('launched worker lacks its permanent attempt claim')
        task = self._task(task_arn)
        result['taskStatus'] = task['lastStatus']
        if stopped is not None:
            if (stopped.get('state') != {'S': 'STOPPED'} or stopped.get('taskArn') != {'S': task_arn}
                    or task['lastStatus'] != 'STOPPED'):
                raise PackError('worker stop claim disagrees with observed task')
            result['stopRecorded'] = True
        return result

    def _task(self, arn):
        response = self.ecs.describe_tasks(cluster=self.config['clusterArn'], tasks=[arn], include=['TAGS'])
        tasks = response.get('tasks')
        if response.get('failures') or not isinstance(tasks, list) or len(tasks) != 1:
            raise PackError('worker task cannot be independently observed')
        task = tasks[0]
        tags = task.get('tags', [])
        expected_tags = {'honua-agent-attempt': self.config['attemptId'], 'honua-agent-candidate': self.config['candidateKey']}
        if (task.get('taskArn') != arn or task.get('clusterArn') != self.config['clusterArn']
                or task.get('taskDefinitionArn') != self.config['taskDefinitionArn'] or task.get('launchType') != 'FARGATE'
                or task.get('lastStatus') not in ('PROVISIONING', 'PENDING', 'ACTIVATING', 'RUNNING', 'DEACTIVATING', 'STOPPING', 'DEPROVISIONING', 'STOPPED')
                or any([t.get('value') for t in tags if t.get('key') == k] != [v] for k, v in expected_tags.items())):
            raise PackError('worker task identity, tags or state differs')
        return task

    def stop(self, durable, record):
        """Request stop of the observed exact task; leave lifecycle/claims intact."""
        durable.require_worker_launch(record)
        observed = self.observe(record)
        if observed['taskArn'] is None:
            raise PackError('worker launch is unresolved; independent expiry cleanup remains required')
        if observed['taskStatus'] == 'STOPPED':
            return observed
        durable.require_worker_launch(record)
        response = self.ecs.stop_task(cluster=self.config['clusterArn'], task=observed['taskArn'],
                                      reason='Honua agent controller cleanup')
        if response.get('task', {}).get('taskArn') != observed['taskArn']:
            raise PackError('worker stop acknowledgement has another task identity')
        return {**observed, 'stopRequested': True}
