"""Controller-loss reconciliation contracts; no model or real cloud calls."""
import copy
import json

import pytest

from agent_e2e_reply_recovery import drain_mcp_replies
from agent_e2e_worker import PackError, ReplyChunks, reply_messages, Worker
from agent_e2e_container import Executor
from test_agent_e2e_worker_evidence import S3, evidence
from test_agent_e2e_mcp_worker import request, reply, Exchange
from test_agent_e2e_worker import CONFIG


class Queue:
    """Only receives/deletes: any accidental dispatch fails the test."""
    def __init__(self, chunks):
        self.pending = copy.deepcopy(chunks)
        self.received, self.deleted = [], []
        self.before_delete = lambda: None
    def receive_message(self, **kwargs):
        self.received.append(kwargs)
        if not self.pending:
            return {}
        part = self.pending.pop(0)
        return {'Messages':[{'Body':json.dumps(part), 'ReceiptHandle':str(part.get('index', 0))}]}
    def delete_message(self, **kwargs):
        self.before_delete()
        self.deleted.append(kwargs)


def drain(queue, sink, **kwargs):
    return drain_mcp_replies(queue, reply_queue_url=CONFIG['replyQueueUrl'],
        binding=ReplyChunks(request()).expected, evidence=sink, clock=lambda:10000, **kwargs)


@pytest.mark.parametrize('retained', [0, 1, 3, 4])
def test_recovery_combines_durable_and_pending_chunks_after_attempt_expiry(retained):
    s3 = S3()
    parts = reply_messages(reply(), request())
    assert len(parts) == 4
    old = evidence(s3)
    for part in parts[:retained]:
        old(part)
    pending = list(reversed(parts[retained:]))
    if pending:
        pending.insert(0, pending[0])  # SQS duplicate delivery.
    queue, restored = Queue(pending), evidence(s3)
    def before_delete():
        # Both the header and this chunk have exact retained versions before ack.
        index = pending[len(queue.deleted)]['index']
        assert restored.references[index]['version']
        assert restored.header_reference['version']
    queue.before_delete = before_delete
    result = drain(queue, restored)
    assert result['reply'] == reply() and result['qualification'] is False
    assert len(result['chunks']) == 4
    assert len(s3.objects) == 5 and all(len(rows) == 1 for rows in s3.objects.values())
    if retained == len(parts):
        assert queue.received == []
    else:
        assert len(queue.deleted) == (1 if retained == 3 else len(pending))
        # Once complete, redundant deliveries can remain for governed teardown.


@pytest.mark.parametrize('crash_before_first_reply', [False, True])
def test_actual_worker_crash_recovers_without_second_execution(tmp_path, crash_before_first_reply):
    s3, executions = S3(), []
    def execute(*args):
        executions.append(args)
        return {'status':'completed','payload':{'data':'x'*100000}}
    executor = Executor(CONFIG, tmp_path/'worker.sqlite', tmp_path, clock=lambda:1000, execute_mcp=execute)
    class CrashingExchange(Exchange):
        def receive_message(self, **kwargs):
            if crash_before_first_reply or self.deleted:
                raise ConnectionError('controller process lost')
            return super().receive_message(**kwargs)
    queue = CrashingExchange(executor)
    sink = evidence(s3)
    with pytest.raises(ConnectionError):
        Worker(queue, CONFIG, clock=lambda:1000).execute_mcp(request()['action'], 'action-1', retain_chunk=sink, retain_dispatch=sink.retain_dispatch)
    assert queue.deleted == ([] if crash_before_first_reply else ['0'])
    restarted = evidence(s3)
    dispatch = restarted.load_dispatch()
    assert dispatch['reference'] == sink.dispatch_reference
    restored = drain_mcp_replies(Queue(queue.pending), reply_queue_url=CONFIG['replyQueueUrl'],
        binding=dispatch['binding'], evidence=restarted, clock=lambda:10000)
    assert restored['reply']['payload'] == {'data':'x'*100000}
    assert len(executions) == len(queue.requests) == 1


def test_dispatch_retention_failure_never_sends_command_and_keeps_binding(tmp_path):
    s3 = S3()
    sink = evidence(s3)
    def lose_ack(key):
        if key == sink.dispatch_key:
            raise TimeoutError('dispatch binding acknowledgement lost')
    s3.after_write = lose_ack
    executor = Executor(CONFIG, tmp_path/'worker.sqlite', tmp_path, clock=lambda:1000)
    queue = Exchange(executor)
    worker = Worker(queue, CONFIG, clock=lambda:1000)
    with pytest.raises(TimeoutError):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=sink, retain_dispatch=sink.retain_dispatch)
    assert queue.requests == queue.deleted == []
    assert evidence(s3).load_dispatch()['binding'] == ReplyChunks(request()).expected
    with pytest.raises(PackError, match='already dispatched'):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=sink, retain_dispatch=sink.retain_dispatch)


def test_retention_delay_cannot_dispatch_an_expired_command(tmp_path):
    now = [1000]
    executor = Executor(CONFIG, tmp_path/'worker.sqlite', tmp_path, clock=lambda:now[0])
    queue, sink = Exchange(executor), evidence(S3())
    def delayed_retention(binding):
        sink.retain_dispatch(binding)
        now[0] += 121
    with pytest.raises(PackError, match='expired'):
        Worker(queue, CONFIG, clock=lambda:now[0]).execute_mcp(request()['action'], 'action-1',
            retain_chunk=sink, retain_dispatch=delayed_retention)
    assert queue.requests == []


def test_dispatch_binding_contains_no_raw_command_and_cannot_be_replaced():
    s3 = S3()
    sink = evidence(s3)
    command = {**request(), 'action':{'name':'mcp_call_tool',
        'input':{'name':'configure','arguments':{'secret':'private-command-value'}}}}
    expected = ReplyChunks(command).expected
    reference = sink.retain_dispatch(expected)
    assert sink.retain_dispatch(expected) == reference
    assert sink.load_dispatch() == {'binding':expected, 'reference':reference}
    assert b'private-command-value' not in s3.objects[sink.dispatch_key][0][1]
    with pytest.raises(PackError, match='replace retained'):
        sink.retain_dispatch(ReplyChunks(request()).expected)
    assert len(s3.objects[sink.dispatch_key]) == 1


def test_missing_or_deleted_dispatch_history_cannot_be_loaded():
    s3 = S3()
    sink = evidence(s3)
    with pytest.raises(PackError, match='history'):
        sink.load_dispatch()
    sink.retain_dispatch(ReplyChunks(request()).expected)
    s3.objects[sink.dispatch_key].append(('delete-marker', None))
    with pytest.raises(PackError, match='history'):
        sink.load_dispatch()


@pytest.mark.parametrize('field,value', [('attemptId','foreign'), ('actionId','foreign'),
                                        ('commandDigest','sha256:'+'f'*64)])
def test_foreign_first_chunk_cannot_create_evidence_or_be_acknowledged(field, value):
    s3 = S3()
    part = reply_messages(reply(size=1), request())[0]
    queue = Queue([{**part, field:value}])
    with pytest.raises(PackError):
        drain(queue, evidence(s3))
    assert s3.puts == [] and queue.deleted == []


def test_dispatch_binding_mismatch_blocks_before_queue_access():
    s3 = S3()
    command = {**request(), 'timeoutSeconds':119}
    evidence(s3)(reply_messages(reply(command), command)[0])
    queue = Queue([])
    with pytest.raises(PackError, match='dispatch binding'):
        drain(queue, evidence(s3))
    assert queue.received == []


def test_pending_chunk_cannot_change_payload_already_retained_before_crash():
    s3 = S3()
    evidence(s3)(reply_messages(reply(), request())[0])
    queue = Queue(reply_messages(reply(size=100001), request())[1:])
    before = copy.deepcopy(s3.objects)
    with pytest.raises(PackError, match='binding changed'):
        drain(queue, evidence(s3))
    assert s3.objects == before and queue.deleted == []


@pytest.mark.parametrize('message', [
    {'Body':'x'*60001, 'ReceiptHandle':'receipt'},
    {'Body':'{', 'ReceiptHandle':'receipt'},
    {'Body':'{}', 'ReceiptHandle':''},
    {'Body':None, 'ReceiptHandle':'receipt'},
])
def test_malformed_queue_message_is_not_retained_or_acknowledged(message):
    s3 = S3()
    class MalformedQueue(Queue):
        def receive_message(self, **kwargs):
            return {'Messages':[message]}
    queue = MalformedQueue([])
    with pytest.raises((PackError, json.JSONDecodeError)):
        drain(queue, evidence(s3))
    assert s3.puts == [] and queue.deleted == []


@pytest.mark.parametrize('failure', ['header-missing', 'deleted-part', 'multiple-part', 'truncated-history'])
def test_inconsistent_retained_history_blocks_before_queue_access(failure):
    s3 = S3()
    sink = evidence(s3)
    parts = reply_messages(reply(), request())
    sink(parts[0])
    if failure == 'header-missing':
        s3.objects.pop(sink.prefix+'header.json')
    elif failure == 'deleted-part':
        s3.objects[sink.prefix+'chunk-001.json'] = [('deleted', None)]
    elif failure == 'multiple-part':
        s3.objects[sink.prefix+'chunk-000.json'].append(('other', s3.objects[sink.prefix+'chunk-000.json'][0][1]))
    else:
        s3.mutate_history = lambda value: {**value, 'IsTruncated':True}
    queue = Queue(parts[1:])
    with pytest.raises(PackError, match='history'):
        drain(queue, evidence(s3))
    assert queue.received == queue.deleted == []


def test_storage_or_delete_failure_keeps_complete_result_recoverable_without_dispatch():
    s3 = S3()
    parts = reply_messages(reply(size=1), request())
    def lost_ack(key):
        if 'chunk-' in key:
            raise TimeoutError('retention acknowledgement lost')
    s3.after_write = lost_ack
    queue = Queue(parts)
    with pytest.raises(TimeoutError):
        drain(queue, evidence(s3))
    assert queue.deleted == []
    empty_queue = Queue([])
    assert drain(empty_queue, evidence(s3))['reply'] == reply(size=1)
    assert empty_queue.received == []


def test_queue_delete_failure_preserves_already_retained_reply():
    s3 = S3()
    queue = Queue(reply_messages(reply(size=1), request()))
    def lost_delete():
        raise TimeoutError('delete acknowledgement lost')
    queue.before_delete = lost_delete
    with pytest.raises(TimeoutError):
        drain(queue, evidence(s3))
    assert evidence(s3).recover()['reply'] == reply(size=1)


def test_empty_or_repeated_queue_is_bounded_and_never_reports_partial_success():
    parts = reply_messages(reply(), request())
    for pending in ([], [parts[0]]*10):
        s3, queue = S3(), Queue(pending)
        with pytest.raises(PackError, match='incomplete'):
            drain(queue, evidence(s3), max_polls=3)
        assert len(queue.received) == 3


def test_recovery_deadline_does_not_ack_late_message():
    s3 = S3()
    queue = Queue(reply_messages(reply(size=1), request()))
    ticks = iter([0, 0, 61])
    with pytest.raises(PackError, match='incomplete'):
        drain_mcp_replies(queue, reply_queue_url=CONFIG['replyQueueUrl'],
            binding=ReplyChunks(request()).expected, evidence=evidence(s3), clock=lambda:next(ticks))
    assert queue.deleted == [] and s3.puts == []


@pytest.mark.parametrize('option,value', [('timeout_seconds',0), ('timeout_seconds',301),
    ('max_polls',0), ('max_polls',257), ('max_polls',True)])
def test_unbounded_configuration_rejected_before_queue_or_storage_read(option, value):
    s3, queue = S3(), Queue([])
    with pytest.raises(PackError, match='configuration'):
        drain(queue, evidence(s3), **{option:value})
    assert queue.received == s3.reads == []
