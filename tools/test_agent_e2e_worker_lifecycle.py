"""Runtime lifecycle fault tests, not deployed or live scenario qualification."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, preserve_history
from agent_e2e_worker_lifecycle import WorkerLifecycle, terraform_definition_digest
from test_agent_e2e_state import MemoryStore


CANDIDATE = MemoryStore.candidate
ACCOUNT = '123456789012'
DEFINITION = {'StartAt': 'Fixture', 'States': {'Fixture': {'Type': 'Succeed'}}}
CONFIG = {
    'region': 'us-east-1', 'candidateKey': CANDIDATE, 'attemptId': 'attempt-1',
    'context': {'campaignId': 'development-1', 'caseId': 'P10:A:aws-ecs-redis-on', 'attempt': 1, 'campaignMode': 'development'},
    'stateMachineArn': f'arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:worker-1',
    'stateMachineRoleArn': f'arn:aws:iam::{ACCOUNT}:role/worker-lifecycle',
    'definitionDigest': canonical_digest(DEFINITION),
    'clusterArn': f'arn:aws:ecs:us-east-1:{ACCOUNT}:cluster/agent-workers',
    'taskDefinitionArn': f'arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/worker-1:3',
    'claimTable': 'worker-claims', 'expiresEpoch': 2000,
    'transportDigest': 'sha256:'+'e'*64,
    'liability': {'basisDigest': 'sha256:'+'b'*64, 'maximumInfrastructureUsd': '2', 'maximumCleanupUsd': '1'},
}
TASK = f'arn:aws:ecs:us-east-1:{ACCOUNT}:task/agent-workers/'+'c'*32
RESERVATIONS = {'infrastructure': 'infra-1', 'cleanup': 'cleanup-1'}


def emitted_definition():
    return (Path(__file__).parent/'fixtures/worker-definition-digest/terraform-installed-slot.json').read_text()


def test_original_terraform_emission_uses_resource_digest_without_changing_campaign_hashes():
    raw = emitted_definition()
    assert terraform_definition_digest(raw) == 'sha256:2043622fd0bd268cd5b0309bb82fc54d1c25f385719617cba1dc88b25dbd0b60'
    assert canonical_digest(json.loads(raw)) == 'sha256:7fe6054ab345b46fd1cbc3f80a6335e44607a9d1ea1528a936ed53d17deebf83'
    assert terraform_definition_digest(json.dumps(json.loads(raw), indent=2)) == terraform_definition_digest(raw)


@pytest.mark.parametrize('raw', ['{"a":1,"a":2}', '{"é":1,"e\\u0301":2}', '{"a":NaN}',
    '{"a":1e1025}', '{"a":1e-1025}', '{"a":0.'+'1'*129+'}', '[]', '{"a":"\\ud800"}'])
def test_definition_digest_refuses_ambiguous_or_unsupported_input(raw):
    with pytest.raises(PackError):
        terraform_definition_digest(raw)


def test_static_lifecycle_inspector_accepts_only_the_original_terraform_digest(rig):
    raw = emitted_definition()
    rig.worker.config['definitionDigest'] = terraform_definition_digest(raw)
    rig.states.definition['definition'] = raw
    rig.worker.inspect_definition()
    rig.worker.config['definitionDigest'] = canonical_digest(json.loads(raw))
    with pytest.raises(PackError, match='reviewed definition'):
        rig.worker.inspect_definition()


class States:
    def __init__(self):
        self.calls = []
        self.definition = {'stateMachineArn': CONFIG['stateMachineArn'], 'type': 'STANDARD', 'status': 'ACTIVE',
                           'roleArn': CONFIG['stateMachineRoleArn'], 'definition': json.dumps(DEFINITION)}
        self.execution = {}
        self.pages = [{'events': [{'id': 8, 'type': 'TaskStateExited', 'stateExitedEventDetails': {
            'name': 'RunWorker', 'outputDetails': {'truncated': False},
            'output': json.dumps({'launched': {'Tasks': [{'TaskArn': TASK}], 'Failures': []}})}}]}]
        self.lost_reply = False
        self.after_start = lambda: None
    def describe_state_machine(self, **kwargs):
        self.calls.append(('definition', kwargs))
        return copy.deepcopy(self.definition)
    def start_execution(self, **kwargs):
        self.calls.append(('start', kwargs))
        self.after_start()
        if self.lost_reply:
            raise TimeoutError('reply lost after launch')
        return {'executionArn': self.execution['executionArn']}
    def describe_execution(self, **kwargs):
        self.calls.append(('describe', kwargs))
        return copy.deepcopy(self.execution)
    def get_execution_history(self, **kwargs):
        self.calls.append(('history', kwargs))
        return copy.deepcopy(self.pages[int(kwargs.get('nextToken', '0'))])


class Ecs:
    def __init__(self):
        self.calls = []
        self.task = {'taskArn': TASK, 'clusterArn': CONFIG['clusterArn'], 'taskDefinitionArn': CONFIG['taskDefinitionArn'],
                     'launchType': 'FARGATE', 'lastStatus': 'RUNNING', 'tags': [
                         {'key': 'honua-agent-attempt', 'value': CONFIG['attemptId']},
                         {'key': 'honua-agent-candidate', 'value': CANDIDATE}]}
        self.failures = []
        self.lost_stop = False
    def describe_tasks(self, **kwargs):
        self.calls.append(('describe', kwargs))
        return {'tasks': [copy.deepcopy(self.task)], 'failures': self.failures}
    def stop_task(self, **kwargs):
        self.calls.append(('stop', kwargs))
        if self.lost_stop:
            raise TimeoutError('stop reply lost')
        return {'task': copy.deepcopy(self.task)}


class Claims:
    def __init__(self):
        self.rows, self.calls = {}, []
    def get_item(self, **kwargs):
        assert kwargs['ConsistentRead'] is True
        assert kwargs['TableName'] == CONFIG['claimTable']
        self.calls.append(kwargs)
        row = self.rows.get(kwargs['Key']['candidateKey']['S'])
        return {'Item': copy.deepcopy(row)} if row is not None else {}


@pytest.fixture
def rig(tmp_path):
    store = MemoryStore()
    path = tmp_path/'ledger.json'
    durable = DurableExecution(store, path, initialize=True)
    for kind, reservation in RESERVATIONS.items():
        durable.update(path, 'reserve', candidate_key=CANDIDATE, reservation_id=reservation,
                       campaign_id=CONFIG['context']['campaignId'], case_id=CONFIG['context']['caseId'], attempt=1,
                       campaign_mode='development', kind=kind, amount='3', basis_digest=CONFIG['liability']['basisDigest'])
    states, ecs, db = States(), Ecs(), Claims()
    clock = [1000]
    worker = WorkerLifecycle(CONFIG, states=states, ecs=ecs, db=db, clock=lambda: clock[0])
    states.execution = {'executionArn': worker.execution_arn, 'stateMachineArn': CONFIG['stateMachineArn'],
                        'name': worker.name, 'input': '{}', 'inputDetails': {'included': True}, 'status': 'RUNNING', 'redriveCount': 0}
    db.rows[worker.claim_key] = {k: {'S': v} for k, v in {
        'candidateKey': worker.claim_key, 'attemptId': CONFIG['attemptId'], 'executionArn': worker.execution_arn,
        'taskDefinitionArn': CONFIG['taskDefinitionArn']}.items()}
    value = SimpleNamespace(store=store, durable=durable, path=path, states=states, ecs=ecs, db=db, worker=worker, clock=clock)
    yield value
    if not durable.stop.is_set():
        durable.close()


def launch(rig):
    return rig.worker.start(rig.durable, reservation_ids=RESERVATIONS)


def test_start_is_retained_and_prepaid_before_runtime_api(rig):
    def retained():
        record = rig.store.state['workerLaunches']['attempt-1']
        assert record['executionArn'] == rig.worker.execution_arn
        assert record['reservationIds'] == RESERVATIONS
        assert len(rig.store.state['ledger']['entries']) == 2
    rig.states.after_start = retained
    record = launch(rig)
    assert record == rig.durable.state['workerLaunches']['attempt-1']
    starts = [args for operation, args in rig.states.calls if operation == 'start']
    assert starts == [{'stateMachineArn': CONFIG['stateMachineArn'], 'name': rig.worker.name, 'input': '{}'}]
    assert not rig.ecs.calls


def test_lost_start_reply_blocks_same_and_fresh_process_resends(rig, tmp_path):
    rig.states.lost_reply = True
    with pytest.raises(TimeoutError):
        launch(rig)
    with pytest.raises(PackError, match='already attempted'):
        launch(rig)
    rig.durable.close()
    recovered = DurableExecution(rig.store, tmp_path/'recovered.json')
    worker = WorkerLifecycle(CONFIG, states=rig.states, ecs=rig.ecs, db=rig.db, clock=lambda: 1000)
    try:
        with pytest.raises(PackError, match='already retained'):
            worker.start(recovered, reservation_ids=RESERVATIONS)
        record = recovered.state['workerLaunches']['attempt-1']
        observed = worker.observe(record)
        assert observed['taskArn'] == TASK
        assert observed['qualification'] is False
        assert [op for op, _ in rig.states.calls].count('start') == 1
    finally:
        recovered.close()


def test_lost_intent_ack_never_calls_start(rig):
    commit = rig.store.commit
    def lose_ack(payload):
        commit(payload)
        raise TimeoutError('commit reply lost')
    rig.store.commit = lose_ack
    with pytest.raises(TimeoutError):
        launch(rig)
    assert 'attempt-1' in rig.store.state['workerLaunches']
    assert not any(op == 'start' for op, _ in rig.states.calls)


def test_new_worker_identity_cannot_reuse_previous_launch_reservations(rig):
    launch(rig)
    config = copy.deepcopy(CONFIG)
    config['attemptId'] = 'another-worker'
    worker = WorkerLifecycle(config, states=rig.states, ecs=rig.ecs, db=rig.db, clock=lambda: 1000)
    with pytest.raises(PackError, match='already funds'):
        worker.start(rig.durable, reservation_ids=RESERVATIONS)
    assert [op for op, _ in rig.states.calls].count('start') == 1


@pytest.mark.parametrize('field,value', [('state', 'settled'), ('kind', 'model'), ('amountUsd', '0.1'),
    ('campaignId', 'other'), ('caseId', 'P11:A:aws-ecs-redis-on'), ('attempt', 2), ('campaignMode', 'qualification'),
    ('basisDigest', 'sha256:'+'d'*64)])
def test_wrong_or_insufficient_reservation_blocks_start(rig, field, value):
    row = rig.durable.state['ledger']['entries'][0]
    row[field] = value
    if field == 'state':
        row['actualUsd'] = '1'
    with pytest.raises((PackError, ValueError)):
        launch(rig)
    assert not any(op == 'start' for op, _ in rig.states.calls)


def test_missing_cleanup_reservation_blocks_start(rig):
    rig.durable.state['ledger']['entries'].pop()
    with pytest.raises(PackError, match='prepaid'):
        launch(rig)
    assert not any(op == 'start' for op, _ in rig.states.calls)


@pytest.mark.parametrize('phase', ['before', 'after-intent'])
def test_lost_lease_blocks_start(rig, phase):
    calls = []
    def renew():
        calls.append(1)
        if phase == 'before' or len(calls) == 2:
            raise PackError('lease lost')
    rig.store.renew = renew
    with pytest.raises(PackError, match='lease lost'):
        launch(rig)
    assert not any(op == 'start' for op, _ in rig.states.calls)
    rig.store.renew = lambda: None


def test_expiry_after_retaining_intent_blocks_start(rig):
    commit = rig.store.commit
    def expire(payload):
        commit(payload)
        rig.clock[0] = CONFIG['expiresEpoch']
    rig.store.commit = expire
    with pytest.raises(PackError, match='expired'):
        launch(rig)
    assert 'attempt-1' in rig.store.state['workerLaunches']
    assert not any(op == 'start' for op, _ in rig.states.calls)


@pytest.mark.parametrize('field,value', [('type', 'EXPRESS'), ('status', 'DELETING'), ('roleArn', 'other'),
    ('stateMachineArn', 'other'), ('definition', '{}'), ('definition', '{"a":1,"a":2}')])
def test_deployed_definition_drift_blocks_start(rig, field, value):
    rig.states.definition[field] = value
    with pytest.raises(PackError):
        launch(rig)
    assert 'workerLaunches' not in rig.store.state


@pytest.mark.parametrize('field,value', [('region', 'eu-west-1'), ('taskDefinitionArn', CONFIG['taskDefinitionArn'].rsplit(':', 1)[0]),
    ('clusterArn', CONFIG['clusterArn'].replace(ACCOUNT, '000000000000')), ('attemptId', '../other'),
    ('expiresEpoch', True), ('definitionDigest', 'anything')])
def test_invalid_reviewed_configuration_rejected(rig, field, value):
    config = copy.deepcopy(CONFIG)
    config[field] = value
    with pytest.raises(PackError):
        WorkerLifecycle(config, states=rig.states, ecs=rig.ecs, db=rig.db)


@pytest.mark.parametrize('field,value', [('executionArn', 'other'), ('stateMachineArn', 'other'), ('name', 'other'),
    ('input', '{"override":1}'), ('inputDetails', {'included': False}), ('redriveCount', 1), ('status', 'PENDING_REDRIVE')])
def test_foreign_or_redriven_execution_is_not_observed(rig, field, value):
    record = launch(rig)
    rig.states.execution[field] = value
    with pytest.raises(PackError):
        rig.worker.observe(record)
    assert not rig.ecs.calls


@pytest.mark.parametrize('field,value', [('taskArn', TASK[:-1]+'d'), ('clusterArn', 'other'), ('taskDefinitionArn', 'other'),
    ('launchType', 'EC2'), ('tags', []), ('lastStatus', 'UNKNOWN')])
def test_foreign_task_is_never_stopped(rig, field, value):
    record = launch(rig)
    rig.ecs.task[field] = value
    with pytest.raises(PackError):
        rig.worker.stop(rig.durable, record)
    assert not any(op == 'stop' for op, _ in rig.ecs.calls)


def test_changed_claim_prevents_observation(rig):
    record = launch(rig)
    rig.db.rows[rig.worker.claim_key]['executionArn'] = {'S': 'other'}
    with pytest.raises(PackError, match='claim'):
        rig.worker.observe(record)


def test_claim_missing_after_launch_blocks_stop(rig):
    record = launch(rig)
    rig.db.rows.clear()
    with pytest.raises(PackError, match='permanent attempt claim'):
        rig.worker.stop(rig.durable, record)
    assert not rig.ecs.calls


def test_launch_not_yet_observed_does_not_mean_cleanup_or_retry(rig):
    record = launch(rig)
    rig.states.pages = [{'events': []}]
    observed = rig.worker.observe(record)
    assert observed['taskStatus'] == 'unobserved' and observed['stopRecorded'] is False
    with pytest.raises(PackError, match='unresolved'):
        rig.worker.stop(rig.durable, record)
    assert [op for op, _ in rig.states.calls].count('start') == 1
    assert not rig.ecs.calls


def test_history_paginates_forward_and_binds_launch_event(rig):
    record = launch(rig)
    rig.states.pages.insert(0, {'events': [{'id': 1, 'type': 'ExecutionStarted'}], 'nextToken': '1'})
    assert rig.worker.observe(record)['launchEventId'] == 8
    history = [args for op, args in rig.states.calls if op == 'history']
    assert len(history) == 2 and history[1] == {**history[0], 'nextToken': '1'}


@pytest.mark.parametrize('corruption', ['truncated', 'missing-details', 'two-tasks', 'failures', 'foreign-task', 'duplicate-json', 'loop'])
def test_bad_launch_history_blocks_stop(rig, corruption):
    record = launch(rig)
    details = rig.states.pages[0]['events'][0]['stateExitedEventDetails']
    if corruption == 'truncated':
        details['outputDetails']['truncated'] = True
    elif corruption == 'missing-details':
        del details['outputDetails']
    elif corruption == 'duplicate-json':
        details['output'] = '{"launched":{},"launched":{}}'
    elif corruption == 'loop':
        rig.states.pages = [{'events': [], 'nextToken': '0'}]
    else:
        launched = {'Tasks': [{'TaskArn': TASK}], 'Failures': []}
        if corruption == 'two-tasks':
            launched['Tasks'] *= 2
        elif corruption == 'failures':
            launched['Failures'] = [{'reason': 'capacity'}]
        else:
            launched['Tasks'][0]['TaskArn'] = 'foreign'
        details['output'] = json.dumps({'launched': launched})
    with pytest.raises(PackError):
        rig.worker.stop(rig.durable, record)
    assert not any(op == 'stop' for op, _ in rig.ecs.calls)


def test_stop_request_does_not_abort_lifecycle_or_claim_success(rig):
    record = launch(rig)
    result = rig.worker.stop(rig.durable, record)
    assert result['stopRequested'] is True and result['taskStatus'] == 'RUNNING'
    assert result['stopRecorded'] is False and result['qualification'] is False
    assert [args for op, args in rig.ecs.calls if op == 'stop'] == [
        {'cluster': CONFIG['clusterArn'], 'task': TASK, 'reason': 'Honua agent controller cleanup'}]
    assert rig.store.state['ledger']['entries'][1]['state'] == 'reserved'


def test_lost_stop_reply_does_not_release_liability(rig):
    record = launch(rig)
    rig.ecs.lost_stop = True
    before = copy.deepcopy(rig.store.state)
    with pytest.raises(TimeoutError):
        rig.worker.stop(rig.durable, record)
    assert rig.store.state == before


def test_cleanup_still_available_after_cost_overrun(rig):
    record = launch(rig)
    with pytest.raises(ValueError, match='exceeded'):
        rig.durable.update(rig.path, 'settle', candidate_key=CANDIDATE, reservation_id='infra-1', amount='600')
    assert rig.worker.stop(rig.durable, record)['stopRequested'] is True
    assert rig.store.state['ledger']['entries'][0]['actualUsd'] == '600'


def stopped_claim(rig):
    row = copy.deepcopy(rig.db.rows[rig.worker.claim_key])
    row.update(candidateKey={'S': rig.worker.claim_key+'/stopped'}, taskArn={'S': TASK}, state={'S': 'STOPPED'})
    rig.db.rows[rig.worker.claim_key+'/stopped'] = row
    return row


def test_stopped_process_and_immutable_record_are_separate_from_qualification(rig):
    record = launch(rig)
    rig.ecs.task['lastStatus'] = 'STOPPED'
    rig.states.execution['status'] = 'SUCCEEDED'
    assert rig.worker.observe(record)['stopRecorded'] is False
    stopped_claim(rig)
    result = rig.worker.stop(rig.durable, record)
    assert result['stopRecorded'] is True and result['qualification'] is False
    assert not any(op == 'stop' for op, _ in rig.ecs.calls)


def test_expired_worker_is_observable_without_new_start(rig):
    record = launch(rig)
    rig.clock[0] = CONFIG['expiresEpoch']+900
    assert rig.worker.observe(record)['taskArn'] == TASK


@pytest.mark.parametrize('state', ['RUNNING', 'foreign-task'])
def test_stop_claim_cannot_override_conflicting_task_evidence(rig, state):
    record = launch(rig)
    row = stopped_claim(rig)
    if state == 'foreign-task':
        rig.ecs.task['lastStatus'] = 'STOPPED'
        row['taskArn'] = {'S': 'foreign'}
    with pytest.raises(PackError, match='disagrees'):
        rig.worker.observe(record)


@pytest.mark.parametrize('mutation', ['erase', 'rewrite'])
def test_launch_history_cannot_be_reset(rig, mutation):
    launch(rig)
    before = copy.deepcopy(rig.store.state)
    after = copy.deepcopy(before)
    if mutation == 'erase':
        after.pop('workerLaunches')
    else:
        after['workerLaunches']['attempt-1']['executionArn'] = 'different'
    with pytest.raises(PackError, match='launch intent'):
        preserve_history(before, after)


def test_closed_controller_cannot_start_or_stop(rig):
    record = launch(rig)
    rig.durable.close()
    with pytest.raises(PackError, match='lease'):
        rig.worker.stop(rig.durable, record)


def test_lifecycle_not_delivered_to_worker_image(tmp_path):
    from bundle_agent_e2e_container import bundle
    files = bundle(tmp_path/'context')['files']
    assert 'agent_e2e_worker_lifecycle.py' not in files
    assert 'agent_e2e_state.py' not in files


def test_foss4g_worker_start_requires_matching_active_admission_not_just_old_liabilities(tmp_path):
    from agent_e2e_budget import update
    from agent_e2e_worker_lifecycle import validate_reservations
    path=tmp_path/'paid-ledger.json';update(path,'init',candidate_key=CANDIDATE)
    update(path,'amend-foss4g',candidate_key=CANDIDATE)
    lifecycle=WorkerLifecycle(CONFIG,states=None,ecs=None,db=None,clock=lambda:1000)
    record=lifecycle._record(RESERVATIONS)
    for kind,rid in RESERVATIONS.items():
        update(path,'reserve',candidate_key=CANDIDATE,reservation_id=rid,kind=kind,
               amount=CONFIG['liability']['maximumInfrastructureUsd' if kind=='infrastructure' else 'maximumCleanupUsd'],
               basis_digest=CONFIG['liability']['basisDigest'],campaign_id=CONFIG['context']['campaignId'],
               case_id=CONFIG['context']['caseId'],attempt=1,campaign_mode='development')
    ledger=json.loads(path.read_text())
    with pytest.raises(PackError,match='admission'):validate_reservations(record,ledger,now=1000)
    update(path,'acquire-paid-attempt',candidate_key=CANDIDATE,admission_id=CONFIG['attemptId'],
           owner_token='controller-owner-token-0000',campaign_id=CONFIG['context']['campaignId'],
           case_id=CONFIG['context']['caseId'],attempt=1,campaign_mode='development',expires_epoch=1100,now_epoch=1000)
    ledger=json.loads(path.read_text());validate_reservations(record,ledger,now=1000)
    with pytest.raises(PackError,match='admission'):validate_reservations(record,ledger,now=1100)
    for key,value in [('attemptId','other-attempt')]:
        with pytest.raises(PackError,match='admission'):
            validate_reservations({**record,key:value},ledger,now=1000)
    for key,value in [('caseId','other-case'),('campaignId','other-campaign'),('attempt',2),('campaignMode','qualification')]:
        changed=copy.deepcopy(record);changed['context'][key]=value
        with pytest.raises(PackError,match='admission'):validate_reservations(changed,ledger,now=1000)
    for rid in RESERVATIONS.values():update(path,'settle',candidate_key=CANDIDATE,reservation_id=rid,amount=1)
    update(path,'release-paid-attempt',candidate_key=CANDIDATE,admission_id=CONFIG['attemptId'],
           owner_token='controller-owner-token-0000',cleanup_status='pass',reservation_ids=RESERVATIONS,now_epoch=1200)
    released=json.loads(path.read_text())
    with pytest.raises(PackError,match='admission'):
        validate_reservations(record,released,now=1000)
    # Even if an old caller supplies reserved-looking costs, the released
    # admission itself cannot fund another start.
    for row in released['entries']:row.update(state='reserved',actualUsd=None)
    with pytest.raises((PackError,ValueError)):
        validate_reservations(record,released,now=1000)
