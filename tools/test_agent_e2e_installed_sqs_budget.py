"""Installed request bounds; local SDK transport controls are not AWS receipts."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from unittest.mock import Mock

import boto3
from botocore.exceptions import ClientError
import pytest

import agent_e2e_worker as worker
import agent_e2e_container as container
import agent_e2e_installed_container as installed
from agent_e2e_pack import PackError
from hosted_pilot import controller_handoff
from test_agent_e2e_setup_http import server

KEY = 'sha256:'+'a'*64
RUNTIME = 'runtime-source-bound'
TASK = 'arn:aws:ecs:us-east-1:123456789012:task/source-bound/'+'a'*32
E = 'execution-'+'b'*32


@pytest.fixture(autouse=True)
def private_process_counters(monkeypatch):
    # Test isolation only. Production has no reset/remove path.
    monkeypatch.setattr(worker, '_installed_sqs_calls', {})


def bounded(client, kind='controller', identity=E):
    return worker.InstalledSqsBudget(client, kind=kind, candidate_key=KEY,
        runtime_id=RUNTIME, identity=identity)


class Queue:
    def __init__(self): self.calls = 0; self.lock = threading.Lock()
    def call(self, **_):
        with self.lock: self.calls += 1
        return {}
    send_message = receive_message = delete_message = call


def test_actual_source_caps_include_failures_and_survive_client_reconstruction():
    assert worker.installed_sqs_limits() == {
        'workerCallsPerPhysicalExecution': 32768,
        'controllerCallsPerExecutionPerPhysicalProcess': 65536}
    raw = Queue()
    first = bounded(raw)
    for _ in range(65535): first.receive_message()
    broken = Mock()
    broken.send_message.side_effect = TimeoutError('unknown wire outcome')
    with pytest.raises(TimeoutError): bounded(broken).send_message()
    # Another Worker or dispatcher object cannot reset the same process/E cap.
    recovered = worker.Worker(bounded(raw), {'commandQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo',
        'replyQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo',
        'attemptId':E,'expiresEpoch':2000}, clock=lambda:1000)
    with pytest.raises(PackError, match='bound exhausted'): recovered.sqs.delete_message()
    assert raw.calls == 65535 and broken.send_message.call_count == 1
    # A distinct native E has a distinct per-E bound, within shared admissions.
    bounded(raw, identity='execution-'+'c'*32).receive_message()
    assert raw.calls == 65536


def test_concurrent_reconstruction_cannot_exceed_physical_worker_cap():
    raw = Queue()
    def attempt(_):
        try: bounded(raw, 'worker', TASK).send_message(); return True
        except PackError: return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(attempt, range(32776)))
    assert sum(accepted) == raw.calls == 32768
    with pytest.raises(PackError, match='cannot change'):
        bounded(bounded(raw), identity='execution-'+'d'*32)


def test_first_bind_receive_send_delete_share_the_later_worker_budget(tmp_path, monkeypatch):
    configuration = {'schemaVersion':2,'mode':'installed-slot','candidateKey':KEY,'runtimeId':RUNTIME,
        'slotId':'fixed-slot','commandQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo',
        'replyQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo',
        'originalFundingExpiresEpoch':2000,'maxExecutionSeconds':6000}
    raw = Queue()
    counter = bounded(raw, 'worker', TASK)
    for _ in range(32765): counter.receive_message()
    message = {'Body':json.dumps({'protocol':installed.BIND_PROTOCOL,'executionId':E}), 'ReceiptHandle':'original'}
    def receive(**_): raw.calls += 1; return {'Messages':[message]}
    raw.receive_message = receive
    class Binding:
        binding = None
        def __init__(self, *_args, **_kwargs): pass
        def accept(self, value):
            self.binding = {**value,'expiresEpoch':1900}
            return {'protocol':installed.BOUND_PROTOCOL,'executionId':E}
        def close(self): pass
    monkeypatch.setenv('HONUA_AGENT_MCP_CONFIG', '{}')
    monkeypatch.setattr(installed, 'InstalledBinding', Binding)
    monkeypatch.setattr(installed, 'McpOnlyExecutor', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(container, 'serve', lambda queues,*_args,**_kwargs: queues.receive_message())
    with pytest.raises(PackError, match='bound exhausted'):
        installed.serve_installed(raw,configuration,journal=tmp_path/'worker.sqlite',
            workspace=str(tmp_path),task_reader=lambda:TASK,clock=lambda:1000)
    assert raw.calls == 32768  # No uncounted post-bind API reaches the client.


@pytest.mark.parametrize('entry', ['controller', 'worker'])
def test_actual_installed_sdk_makes_one_http_attempt_for_retryable_failure(server, monkeypatch, entry):
    server.respond = lambda *_: (503, {'Content-Type':'application/x-amz-json-1.0'},
        b'{"__type":"ServiceUnavailable","message":"local SDK fixture"}')
    create = boto3.client
    clients = []
    def local_client(service, **kwargs):
        value = create(service, endpoint_url=server.url, aws_access_key_id='local-fixture',
            aws_secret_access_key='local-fixture', **kwargs)
        clients.append(value)
        return value
    monkeypatch.setattr(boto3, 'client', local_client)
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    def request(client):
        bounded(client).receive_message(QueueUrl=server.url+'/queue',MaxNumberOfMessages=1)
    if entry == 'controller':
        invoke = lambda: request(controller_handoff.sdk('sqs','us-east-1'))
    else:
        monkeypatch.setenv('HONUA_AGENT_TRANSPORT', '{"mode":"installed-slot"}')
        monkeypatch.setattr(installed,'serve_installed',lambda client,_:request(client))
        invoke = container.main
    try:
        with pytest.raises(ClientError): invoke()
        assert len(server.seen) == 1
        assert clients[0].meta.config.retries['total_max_attempts'] == 1
        assert list(worker._installed_sqs_calls.values()) == [1]
    finally:
        for client in clients: client.close()
