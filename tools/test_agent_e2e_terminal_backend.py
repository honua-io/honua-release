"""Actual local worker-executor transport contracts, never live qualification."""
import copy
import json

import pytest

from agent_e2e_container import Executor
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_terminal_backend import TerminalActions
from agent_e2e_telemetry import Journal
from agent_e2e_worker import Worker
from agent_e2e_worker_lifecycle import WorkerLifecycle
from test_agent_e2e_mcp_worker import Exchange
from test_agent_e2e_worker import CONFIG as TRANSPORT
from test_agent_e2e_worker_lifecycle import rig, CONFIG, launch


class TerminalExchange(Exchange):
    def receive_message(self, **kwargs):
        value = self.pending.pop(0)
        return {'Messages': [{'Body': json.dumps(value), 'ReceiptHandle': 'terminal-reply'}]}


@pytest.fixture
def terminal_wiring(rig, tmp_path):
    transport = {**TRANSPORT, 'expiresEpoch': CONFIG['expiresEpoch']}
    rig.worker = WorkerLifecycle({**CONFIG, 'transportDigest': canonical_digest(transport)},
        states=rig.states, ecs=rig.ecs, db=rig.db, clock=lambda: 1000)
    launched = launch(rig)
    context = {**CONFIG['context'], 'componentBindingDigest': CONFIG['candidateKey'], 'knowledge': {}}
    journal = Journal(tmp_path/'terminal-journals', context, sink=rig.durable.event)
    journal.emit('start', modelId='offline-model-fixture', returnedModelId='offline-model-fixture')
    journal.emit('context', campaignMode='development', knowledge={}, conceptIds=[], workflowIds=[])
    action = {'type': 'tool_use', 'id': 'chosen-terminal', 'name': 'terminal', 'input': {
        'command': 'printf actual-offline-worker; test -z "$AWS_ACCESS_KEY_ID"', 'timeoutSeconds': 5}}
    digest = canonical_digest({key: action[key] for key in ('name', 'input')})
    journal.emit('action-intent', action=1, kind='terminal', requestDigest=digest)
    dispatch = {'ordinal': 1, 'requestDigest': digest, 'intentDigest': journal.previous,
        'actionId': canonical_digest({**{key: context[key] for key in
            ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}, 'ordinal': 1})[7:]}
    executor = Executor(transport, tmp_path/'terminal.sqlite', tmp_path, clock=lambda: 1000)
    queue = TerminalExchange(executor)
    worker = Worker(queue, transport, clock=lambda: 1000)
    retained = {}
    def retain(name, value):
        if name in retained:
            raise PackError('retained terminal phase; do not replay')
        retained[name] = copy.deepcopy(value)
        return {'key': name, 'sha256': canonical_digest(value)}
    router = TerminalActions(durable=rig.durable, lifecycle=rig.worker, launch=launched,
                              worker=worker, retain=retain)
    try:
        yield router, context, action, dispatch, retained, queue, rig
    finally:
        journal.close()
        executor.db.close()


def test_model_selected_terminal_executes_and_retains_actual_output_before_return(terminal_wiring, monkeypatch):
    router, context, action, dispatch, retained, queue, rig = terminal_wiring
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'controller-canary-not-for-command')
    result = router.execute(context, action, dispatch, timeout=5)
    assert result['agentResult']['content'] == {'exitCode': 0, 'stdout': 'actual-offline-worker', 'stderr': ''}
    assert result['agentResult']['isError'] is False
    reply = retained['terminal-reply-'+dispatch['actionId']]
    assert rig.store.state['workerActions'][dispatch['actionId']]['replyDigest'] == canonical_digest(reply)
    assert len(queue.requests) == 1 and queue.requests[0]['command'] == action['input']['command']
    with pytest.raises(PackError, match='already has retained evidence'):
        router.execute(context, action, dispatch, timeout=5)
    assert len(queue.requests) == 1


def test_changed_terminal_command_cannot_reach_worker(terminal_wiring):
    router, context, action, dispatch, _, queue, _ = terminal_wiring
    action['input']['command'] = 'printf changed'
    with pytest.raises(PackError, match='latest retained controller intent'):
        router.execute(context, action, dispatch)
    assert not queue.requests


def test_terminal_intent_retention_failure_prevents_queue_send(terminal_wiring):
    router, context, action, dispatch, _, queue, _ = terminal_wiring
    router.retain = lambda *_: (_ for _ in ()).throw(TimeoutError('lost durable acknowledgement'))
    with pytest.raises(TimeoutError):
        router.execute(context, action, dispatch)
    assert not queue.requests
