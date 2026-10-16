"""Container transport/executor contract tests; never live qualification."""
import json

import pytest

from agent_e2e_pack import PackError
from agent_e2e_worker import Worker, PROTOCOL, command_digest, validate_command
from agent_e2e_container import Executor, serve

CONFIG = {"commandQueueUrl":"https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo",
          "replyQueueUrl":"https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo",
          "attemptId":"attempt-1", "expiresEpoch":1600}


def request():
    return {"protocol":PROTOCOL, "attemptId":"attempt-1", "actionId":"action-1",
            "deadlineEpoch":1300, "timeoutSeconds":300, "command":"echo hello"}


def result():
    return {"status":"completed", "exitCode":0, "stdout":"hello\n", "stderr":""}


class Queue:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.mutate = lambda reply: reply
    def send_message(self, **kwargs):
        self.sent.append(kwargs)
    def receive_message(self, **kwargs):
        command = json.loads(self.sent[-1]["MessageBody"])
        reply = {"protocol":PROTOCOL, "attemptId":command["attemptId"],
                 "actionId":command["actionId"], "commandDigest":command_digest(command), **result()}
        return {"Messages":[{"Body":json.dumps(self.mutate(reply)), "ReceiptHandle":"receipt"}]}
    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


def test_transport_uses_bound_queues_and_does_not_provision():
    queue = Queue()
    worker = Worker(queue, CONFIG, clock=lambda:1000)
    assert worker.execute("echo hello", "action-1")["stdout"] == "hello\n"
    assert queue.sent[0]["QueueUrl"] == CONFIG["commandQueueUrl"]
    assert queue.deleted[0]["QueueUrl"] == CONFIG["replyQueueUrl"]
    with pytest.raises(PackError, match="already dispatched"):
        worker.execute("echo hello", "action-1")


@pytest.mark.parametrize('now,timeout,expected', [(1000.99, 1, 1002), (1000.01, 300, 1301), (1000, 1, 1001), (1590.99, 10, 1595)])
def test_deadline_rounding_preserves_requested_duration_within_absolute_expiry(now, timeout, expected):
    queue = Queue()
    Worker(queue, CONFIG, clock=lambda:now).execute('echo hello', 'action-1', timeout=timeout)
    command = json.loads(queue.sent[0]['MessageBody'])
    assert command['deadlineEpoch'] == expected
    assert command['deadlineEpoch'] <= CONFIG['expiresEpoch'] - 5
    if now + timeout <= CONFIG['expiresEpoch'] - 5:
        assert timeout <= command['deadlineEpoch'] - now < timeout + 1


@pytest.mark.parametrize("field,value", [("attemptId","other"), ("actionId","other"),
    ("commandDigest","sha256:"+"f"*64), ("exitCode",True), ("stdout","x"*16385),
    ("status","pass")])
def test_foreign_or_malformed_reply_never_becomes_a_result(field, value):
    queue = Queue()
    queue.mutate = lambda reply: {**reply, field:value}
    with pytest.raises(PackError, match="reply"):
        Worker(queue, CONFIG, clock=lambda:1000).execute("echo hello", "action-1")
    assert queue.deleted == []


def test_ambiguous_send_is_not_automatically_retried():
    queue = Queue()
    def send(**kwargs):
        raise TimeoutError("provider detail")
    queue.send_message = send
    worker = Worker(queue, CONFIG, clock=lambda:1000)
    with pytest.raises(TimeoutError):
        worker.execute("create map", "action-1")
    with pytest.raises(PackError, match="already dispatched"):
        worker.execute("create map", "action-1")


@pytest.mark.parametrize("change", [{"attemptId":"other"}, {"deadlineEpoch":2000},
    {"timeoutSeconds":True}, {"command":""}, {"actionId":"a/../../b"}])
def test_invalid_command_rejected_before_execution(change):
    with pytest.raises(PackError):
        validate_command({**request(), **change}, "attempt-1", 1600, 1000)


def test_completed_delivery_is_replayed_without_executing_again(tmp_path):
    calls = []
    def execute(*args):
        calls.append(args)
        return result()
    executor = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1000, execute=execute)
    first = executor.handle(request())
    executor.db.close()
    restarted = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1400, execute=execute)
    assert restarted.handle(request()) == first
    assert len(calls) == 1


def test_crash_after_dispatch_is_interrupted_not_reexecuted(tmp_path):
    calls = []
    def crash(*args):
        calls.append(args)
        raise RuntimeError("lost worker")
    executor = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1000, execute=crash)
    with pytest.raises(RuntimeError):
        executor.handle(request())
    executor.db.close()
    restarted = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1001, execute=crash)
    assert restarted.handle(request())["status"] == "interrupted"
    assert len(calls) == 1


def test_reused_action_with_changed_command_is_rejected(tmp_path):
    executor = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1000, execute=lambda *args: result())
    executor.handle(request())
    with pytest.raises(PackError, match="reused"):
        executor.handle({**request(), "command":"delete everything"})


def test_expired_command_does_not_execute(tmp_path):
    def unexpected(*args):
        pytest.fail("expired command executed")
    executor = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1400, execute=unexpected)
    assert executor.handle(request())["status"] == "timed-out"


def test_response_send_failure_retains_command_and_completed_journal(tmp_path):
    calls = []
    executor = Executor(CONFIG, tmp_path/"journal.sqlite", tmp_path, clock=lambda:1000,
                        execute=lambda *args: calls.append(args) or result())
    class BrokenReply:
        def receive_message(self, **kwargs):
            return {"Messages":[{"Body":json.dumps(request()), "ReceiptHandle":"receipt"}]}
        def send_message(self, **kwargs):
            raise TimeoutError("reply unavailable")
        def delete_message(self, **kwargs):
            pytest.fail("deleted command before durable reply")
    with pytest.raises(TimeoutError):
        serve(BrokenReply(), CONFIG, executor, clock=lambda:1000)
    assert executor.handle(request())["status"] == "completed"
    assert len(calls) == 1


def test_image_context_cannot_export_private_controller_sources(tmp_path):
    from bundle_agent_e2e_container import bundle, RUNTIME
    context = tmp_path / "context"
    manifest = bundle(context)
    assert set(manifest["files"]) == set(RUNTIME) | {"Dockerfile", "requirements.txt"}
    assert not (context / "agent_e2e_pack.py").exists()
    assert not (context / "agent_e2e_worker_evidence.py").exists()
    assert not (context / "agent_e2e_reply_recovery.py").exists()
    assert not (context / "corpus").exists()
    with pytest.raises(FileExistsError):
        bundle(context)
