"""Worker integration contracts; fixture HTTP and queues are not qualification."""
import copy
import json

import pytest

from agent_e2e_container import Executor, serve
from agent_e2e_mcp_runner import run
from agent_e2e_worker import (Worker, MCP_PROTOCOL, MAX_REPLY_BYTES, CHUNK_BYTES, ReplyChunks,
                              PackError, command_digest, reply_messages, validate_command)
from test_agent_e2e_worker import CONFIG
from test_agent_e2e_mcp import server, standard, result


def request():
    return {'protocol':MCP_PROTOCOL, 'attemptId':'attempt-1', 'actionId':'action-1',
            'deadlineEpoch':1120, 'timeoutSeconds':120, 'action':{'name':'mcp_list_tools','input':{}}}


def reply(command=None, size=100000):
    command = command or request()
    return {'protocol':MCP_PROTOCOL, 'attemptId':command['attemptId'], 'actionId':command['actionId'],
            'commandDigest':command_digest(command), 'status':'completed', 'payload':{'content':'x'*size}}


class Exchange:
    def __init__(self, executor):
        self.executor, self.requests, self.pending, self.deleted = executor, [], [], []
    def send_message(self, **kwargs):
        command = json.loads(kwargs['MessageBody'])
        self.requests.append(command)
        self.pending.extend(reply_messages(self.executor.handle(command), command))
    def receive_message(self, **kwargs):
        value = self.pending.pop(0)
        return {'Messages':[{'Body':json.dumps(value), 'ReceiptHandle':str(value['index'])}]}
    def delete_message(self, **kwargs):
        self.deleted.append(kwargs['ReceiptHandle'])


def test_complete_large_catalog_crosses_worker_transport_without_truncation(server, tmp_path, monkeypatch):
    tools = [{'name':f'tool-{index}', 'inputSchema':{'type':'object','description':'observed '*100}} for index in range(200)]
    def respond(request):
        if request and request.get('method') == 'tools/list':
            return result(request, {'tools':tools})
        return standard(request)
    server.respond = respond
    monkeypatch.setenv('HONUA_AGENT_MCP_TOKEN', 'preexisting-worker-value')
    config = {'endpoint':server.url,'credential':'own-actor-token','credentialKind':'bearer','workflowView':None}
    executor = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1000,
                        execute_mcp=lambda action, seconds, _: {'status':'completed','payload':run(action, config, seconds)})
    queue = Exchange(executor)
    retained = []
    def retain(value):
        assert len(queue.deleted) == len(retained)
        retained.append(copy.deepcopy(value))
    observed = Worker(queue, CONFIG, clock=lambda:1000).execute_mcp(request()['action'], 'action-1', retain_chunk=retain)
    assert observed['payload']['result'] == {'tools':tools}
    assert observed['payload']['error'] is None
    assert observed['payload']['sessionCleanup'] == {'status':'pass'}
    assert len(retained) > 10 and len(queue.deleted) == len(retained)
    assert len(observed['payload']['observations']) == 4
    assert 'own-actor-token' not in json.dumps(observed)
    assert __import__('os').environ['HONUA_AGENT_MCP_TOKEN'] == 'preexisting-worker-value'
    assert all(entry[2]['Authorization'] == 'Bearer own-actor-token' for entry in server.seen)
    # Recovery assembles the exact retained result with no new command or model.
    recovered = ReplyChunks(queue.requests[0])
    for part in reversed(retained):
        restored = recovered.accept(part)
    assert restored == observed


def test_lost_mcp_write_reply_and_worker_redelivery_do_not_repeat_write(server, tmp_path):
    writes = []
    def respond(request):
        if request and request['method'] == 'tools/call':
            writes.append(request['params'])
            return None
        return standard(request)
    server.respond = respond
    config = {'endpoint':server.url,'credential':None,'credentialKind':'bearer','workflowView':None}
    calls = []
    def execute(action, seconds, _):
        calls.append(action)
        return {'status':'completed','payload':run(action, config, seconds)}
    command = {**request(), 'action':{'name':'mcp_call_tool','input':{'name':'publish','arguments':{'map':'chosen'}}}}
    executor = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1000, execute_mcp=execute)
    first = executor.handle(command)
    assert first['payload']['error']['category'] == 'transport-outcome-unknown'
    executor.db.close()
    restored = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1001, execute_mcp=execute)
    assert restored.handle(command) == first
    assert writes == [{'name':'publish','arguments':{'map':'chosen'}}]
    assert len(calls) == 1


def test_mcp_crash_preserves_interrupted_identity(tmp_path):
    calls = []
    def crash(*args):
        calls.append(args)
        raise OSError('process disappeared')
    executor = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1000, execute_mcp=crash)
    with pytest.raises(OSError):
        executor.handle(request())
    assert executor.handle(request())['status'] == 'interrupted'
    assert executor.handle(request())['payload'] is None
    assert len(calls) == 1


def test_failed_chunk_retention_prevents_queue_acknowledgement(tmp_path):
    executor = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1000,
                        execute_mcp=lambda *_: {'status':'completed','payload':{'data':'large'*30000}})
    queue = Exchange(executor)
    def unavailable(_):
        raise OSError('durable storage unavailable')
    worker = Worker(queue, CONFIG, clock=lambda:1000)
    with pytest.raises(OSError):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=unavailable)
    assert not queue.deleted
    with pytest.raises(PackError, match='already dispatched'):
        worker.execute_mcp(request()['action'], 'action-1', retain_chunk=unavailable)
    assert len(queue.requests) == 1


@pytest.mark.parametrize('field,value', [('attemptId','other'), ('actionId','other'),
    ('commandDigest','sha256:'+'a'*64), ('index',-1), ('index',True), ('count',99999),
    ('byteLength',MAX_REPLY_BYTES+1), ('data','not-base64'), ('data','A'*50000)],
    ids=lambda value: 'large-value' if isinstance(value, str) and len(value) > 100 else str(value))
def test_foreign_or_unbounded_chunk_rejected(field, value):
    part = reply_messages(reply(), request())[0]
    with pytest.raises(PackError):
        ReplyChunks(request()).accept({**part, field:value})


def test_chunk_duplicates_are_idempotent_but_changed_bytes_or_identity_fail():
    parts = reply_messages(reply(), request())
    collector = ReplyChunks(request())
    assert collector.accept(parts[0]) is None
    assert collector.accept(parts[0]) is None
    altered = {**parts[0], 'data':parts[0]['data'].replace('e', 'f')}
    assert altered != parts[0]
    with pytest.raises(PackError, match='duplicate content'):
        collector.accept(altered)
    with pytest.raises(PackError, match='binding changed'):
        collector.accept({**parts[1], 'payloadDigest':'sha256:'+'f'*64})


def test_missing_or_tampered_chunks_cannot_create_complete_result():
    parts = reply_messages(reply(), request())
    collector = ReplyChunks(request())
    for part in parts[:-1]:
        assert collector.accept(part) is None
    corrupt = ReplyChunks(request())
    for part in parts[:-1]:
        assert corrupt.accept({**part, 'payloadDigest':'sha256:'+'0'*64}) is None
    with pytest.raises(PackError, match='digest mismatch'):
        corrupt.accept({**parts[-1], 'payloadDigest':'sha256:'+'0'*64})


@pytest.mark.parametrize('action', [{'name':'terminal','input':{'command':'echo bad'}},
    {'name':'mcp_list_tools','input':{'credential':'injected'}},
    {'name':'mcp_call_tool','input':{'name':'write','arguments':[], 'endpoint':'https://other'}}])
def test_model_cannot_override_mcp_identity_endpoint_or_operation(action):
    with pytest.raises(PackError, match='MCP action'):
        validate_command({**request(), 'action':action}, 'attempt-1', 1600, 1000)


def test_partial_chunk_publication_keeps_worker_result_for_redelivery(tmp_path):
    calls = []
    executor = Executor(CONFIG, tmp_path/'journal.sqlite', tmp_path, clock=lambda:1000,
        execute_mcp=lambda *args: calls.append(args) or {'status':'completed','payload':{'data':'x'*100000}})
    class BrokenReply:
        sent = []
        def receive_message(self, **kwargs):
            return {'Messages':[{'Body':json.dumps(request()),'ReceiptHandle':'command'}]}
        def send_message(self, **kwargs):
            self.sent.append(kwargs)
            if len(self.sent) == 2:
                raise TimeoutError('publication lost')
        def delete_message(self, **kwargs):
            pytest.fail('command deleted before all reply chunks were published')
    queue = BrokenReply()
    with pytest.raises(TimeoutError):
        serve(queue, CONFIG, executor, clock=lambda:1000)
    assert len(calls) == 1
    retained = executor.handle(request())
    assert len(calls) == 1
    assert reply_messages(retained, request())[0] == json.loads(queue.sent[0]['MessageBody'])
    assert queue.sent[0]['MessageDeduplicationId'] != queue.sent[1]['MessageDeduplicationId']
