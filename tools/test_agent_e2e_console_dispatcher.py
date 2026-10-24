"""Actual worker journals/HTTP and queue doubles; no hosted qualification."""
import copy
import json

import pytest

from agent_e2e_console_dispatcher import McpDispatcher
from agent_e2e_console_runtime import ExecutionJournal
from agent_e2e_container import Executor
from agent_e2e_pack import PackError
from agent_e2e_worker import Worker
from test_agent_e2e_console_runtime import runtime, original, E
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_setup_http import server
from test_agent_e2e_mcp import server as mcp_server
from test_agent_e2e_mcp_worker import Exchange
from agent_e2e_mcp_runner import run


@pytest.fixture
def dispatch(runtime, mcp_server, tmp_path):
    journal = ExecutionJournal(runtime.durable, original(runtime))
    intent = {'actionId': 'original-provider-action', 'providerRequestId': 'original-request',
              'providerToolCallId': 'original-call', 'action': {'type': 'tool_use', 'id': 'original-call',
                'name': 'mcp_call_tool', 'input': {'name': 'observed-tool', 'arguments': {'value': 1}}}}
    journal.append('action-intent', intent)
    config = {'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo',
              'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo',
              'attemptId': E, 'expiresEpoch': runtime.now+600}
    agent = {'endpoint': mcp_server.url, 'credentialKind': 'api-key', 'credential': 'private-task-key', 'workflowView': None}
    executor = Executor(config, tmp_path/'worker.sqlite', tmp_path, clock=lambda: runtime.now,
        execute_mcp=lambda action, seconds, _: {'status': 'completed', 'payload': run(action, agent, seconds)})
    queue = Exchange(executor)
    def build(*, selected=journal, guard=lambda: None):
        return McpDispatcher(journal=selected, worker=Worker(queue, config, clock=lambda: runtime.now),
            capability='a'*64, guard=guard)
    return runtime, journal, intent, queue, build


def test_restart_consumes_retained_chunks_without_repeating_native_write(dispatch, mcp_server):
    runtime, journal, intent, queue, build = dispatch
    result = build().execute(intent)
    assert result['error'] is None
    # Reconstruct the entire controller-side journal and Worker, including an
    # independent serialized state payload, rather than preserving sent=set().
    runtime.durable.state = json.loads(json.dumps(runtime.durable.state))
    restored = ExecutionJournal(runtime.durable, original(runtime))
    assert build(selected=restored).recover(copy.deepcopy(intent)) == result
    assert len(queue.requests) == 1
    assert len([row for row in mcp_server.seen if row[3] and row[3].get('method') == 'tools/call']) == 1
    assert restored.values('worker-command-dispatch') == journal.values('worker-command-dispatch')


def test_lost_send_ack_recovers_only_original_reply(dispatch):
    runtime, journal, intent, queue, build = dispatch
    original_send = queue.send_message
    def lost(**request):
        original_send(**request)
        raise TimeoutError('accepted send acknowledgement lost')
    queue.send_message = lost
    with pytest.raises(TimeoutError):
        build().execute(intent)
    runtime.durable.state = json.loads(json.dumps(runtime.durable.state))
    restored = ExecutionJournal(runtime.durable, original(runtime))
    assert build(selected=restored).recover(intent)['error'] is None
    assert len(queue.requests) == 1


def test_lost_guard_after_retention_prevents_delete_and_restart_reuses_original(dispatch):
    runtime, journal, intent, queue, build = dispatch
    def guard():
        if journal.values('worker-command-chunk'):
            raise PackError('native claim lost after chunk retention')
    with pytest.raises(PackError, match='claim lost'):
        build(guard=guard).execute(intent)
    assert journal.values('worker-command-chunk') and not queue.deleted
    assert build().recover(intent)['error'] is None
    assert len(queue.requests) == 1


def test_unretained_foreign_or_terminal_action_cannot_dispatch(dispatch):
    _, journal, intent, queue, build = dispatch
    for changed in ({**intent, 'actionId': 'foreign'}, {**intent, 'action': {'type': 'tool_use',
            'id': 'terminal', 'name': 'terminal', 'input': {'command': 'echo forbidden'}}}):
        with pytest.raises(PackError, match='original provider'):
            build().execute(changed)
    assert not queue.requests


def test_recovery_rejects_changed_dispatch_binding_and_partial_chunks(dispatch):
    _, journal, intent, queue, build = dispatch
    def lost(**request):
        raise TimeoutError('unknown send')
    queue.send_message = lost
    with pytest.raises(TimeoutError):
        build().execute(intent)
    # Tampering cannot redirect recovery to another E, even with a valid
    # provider action and no process-local sent marker.
    journal.record['events'][-1]['value']['binding']['attemptId'] = 'another-execution'
    with pytest.raises(PackError, match='content changed'):
        build().recover(intent)


def test_crash_before_dispatch_resumes_once_from_retained_provider_intent(dispatch):
    _, _, intent, queue, build = dispatch
    assert build().recover(intent)['error'] is None
    assert len(queue.requests) == 1


@pytest.mark.parametrize('corrupt_redelivery', [False, True])
def test_visible_undeleted_old_chunk_is_routed_after_full_restart_before_next_result(dispatch, corrupt_redelivery):
    runtime, journal, intent, queue, build = dispatch
    # Unlike Exchange, this queue retains received messages until DELETE. A
    # receipt changes on redelivery, as it does after SQS visibility expires.
    messages = {}
    original_send = queue.send_message
    def send(**request):
        original_send(**request)
        for chunk in queue.pending:
            messages[len(messages)] = {'chunk': copy.deepcopy(chunk), 'visible': True, 'delivery': 0}
        queue.pending.clear()
    def receive(**request):
        for key, message in messages.items():
            if message['visible']:
                message['visible'] = False
                message['delivery'] += 1
                return {'Messages': [{'Body': json.dumps(message['chunk']),
                    'ReceiptHandle': f"message-{key}-delivery-{message['delivery']}"}]}
        raise AssertionError('No visible original reply remains')
    def delete(**request):
        receipt = request['ReceiptHandle']
        for key, message in list(messages.items()):
            if receipt == f"message-{key}-delivery-{message['delivery']}":
                del messages[key]
                queue.deleted.append(receipt)
                return
        raise AssertionError('A stale receipt cannot acknowledge a message')
    queue.send_message, queue.receive_message, queue.delete_message = send, receive, delete
    def lost_authority():
        if journal.values('worker-command-chunk'):
            raise PackError('authority lost before deleting original reply')
    with pytest.raises(PackError, match='authority lost'):
        build(guard=lost_authority).execute(intent)
    assert len(messages) == 1 and not queue.deleted
    runtime.durable.state = json.loads(json.dumps(runtime.durable.state))
    restored = ExecutionJournal(runtime.durable, original(runtime))
    assert build(selected=restored).recover(intent)['error'] is None
    assert len(messages) == 1 and not queue.deleted
    for message in messages.values():
        message['visible'] = True
        if corrupt_redelivery:
            message['chunk']['payloadDigest'] = 'sha256:'+'f'*64
    following = copy.deepcopy(intent)
    following.update(actionId='second-provider-action', providerToolCallId='second-call')
    following['action']['id'] = 'second-call'
    restored.append('action-intent', following)
    if corrupt_redelivery:
        with pytest.raises(PackError, match='binding changed'):
            build(selected=restored).execute(following)
        assert not queue.deleted and len(messages) == 2
    else:
        assert build(selected=restored).execute(following)['error'] is None
        assert not messages and len(queue.deleted) == 2
        assert queue.deleted[0].endswith('delivery-2')
    assert len(queue.requests) == 2  # Neither native operation is dispatched again.
