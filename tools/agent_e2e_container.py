"""Container-side terminal executor; never a provisioner or qualification oracle.

Run once per attempt in a non-root Linux container. The trusted lifecycle must
never restart a task on the same queues after loss of its local execution journal.
The independently enforced job expiry remains required if this process is killed.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time

from agent_e2e_worker import (PROTOCOL, MAX_STDOUT, MAX_STDERR, Worker, PackError,
                              MCP_PROTOCOL, MAX_REPLY_BYTES, command_digest, encode,
                              validate_command, validate_reply, reply_messages)


def terminal(command, seconds, workspace):
    if os.name != "posix" or os.geteuid() == 0:
        raise PackError("terminal execution requires a non-root Linux container")
    import resource

    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    # Do not inherit task credentials, transport configuration or the process
    # environment into commands. Network/task identity isolation is still needed.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(workspace),
           "LANG": "C.UTF-8", "TMPDIR": str(workspace)}
    with tempfile.TemporaryFile(dir=workspace) as stdout, tempfile.TemporaryFile(dir=workspace) as stderr:
        process = subprocess.Popen(["/bin/bash", "-s"], cwd=workspace, env=env,
            stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
            preexec_fn=limits)
        status = "completed"
        try:
            process.communicate(command.encode(), timeout=seconds)
        except subprocess.TimeoutExpired:
            status = "timed-out"
        finally:
            # Also stop background descendants when the shell itself exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        stdout.seek(0)
        stderr.seek(0)
        # Drop a partial trailing UTF-8 sequence rather than expanding it beyond
        # the protocol byte limit with replacement characters.
        return {"status": status, "exitCode": process.returncode,
                "stdout": stdout.read(MAX_STDOUT).decode("utf-8", errors="ignore"),
                "stderr": stderr.read(MAX_STDERR).decode("utf-8", errors="ignore")}


def mcp_action(action, seconds, workspace):
    if os.name != 'posix' or os.geteuid() == 0:
        raise PackError('MCP execution requires a non-root Linux container')
    import resource
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_REPLY_BYTES, MAX_REPLY_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    config = os.environ.get('HONUA_AGENT_MCP_CONFIG')
    if not config or len(config.encode()) > 16384:
        raise PackError('missing or oversized agent MCP configuration')
    env = {'PATH':'/usr/local/bin:/usr/bin:/bin', 'LANG':'C.UTF-8', 'HOME':str(workspace),
           'TMPDIR':str(workspace), 'HONUA_AGENT_MCP_CONFIG':config}
    with tempfile.TemporaryFile(dir=workspace) as stdout, tempfile.TemporaryFile(dir=workspace) as stderr:
        process = subprocess.Popen(['/usr/local/bin/python', '-I', '/app/agent_e2e_mcp_entry.py'],
            cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
            start_new_session=True, preexec_fn=limits)
        status = 'completed'
        try:
            process.communicate(encode({'action':action, 'seconds':seconds}).encode(), timeout=seconds)
        except subprocess.TimeoutExpired:
            status = 'timed-out'
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if status == 'timed-out':
            return {'status':status, 'payload':None}
        if process.returncode:
            raise PackError('MCP process failed; outcome remains unverified')
        stdout.seek(0)
        raw = stdout.read(MAX_REPLY_BYTES+1)
        if len(raw) > MAX_REPLY_BYTES:
            raise PackError('oversized MCP process result')
        return {'status':status, 'payload':json.loads(raw)}


class Executor:
    def __init__(self, config, journal, workspace, *, clock=time.time, execute=terminal, execute_mcp=mcp_action):
        self.config, self.clock, self.execute, self.execute_mcp = dict(config), clock, execute, execute_mcp
        self.workspace = Path(workspace).resolve(strict=True)
        self.db = sqlite3.connect(journal)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, digest TEXT NOT NULL, reply TEXT)")
        self.db.commit()

    def handle(self, request):
        # Validate shape independently of freshness so a completed command can
        # return its existing result even when its delivery is late.
        if not isinstance(request, dict) or type(request.get("deadlineEpoch")) is not int:
            raise PackError("malformed container command")
        seconds = request.get("timeoutSeconds")
        if type(seconds) is not int:
            raise PackError("malformed command timeout")
        validate_command(request, self.config["attemptId"], self.config["expiresEpoch"],
                         request["deadlineEpoch"] - seconds)
        digest = command_digest(request)
        structured = request['protocol'] == MCP_PROTOCOL
        def unfinished(status):
            return ({'status':status, 'payload':None} if structured else
                    {'status':status, 'exitCode':-1, 'stdout':'', 'stderr':''})
        row = self.db.execute("SELECT digest,reply FROM actions WHERE id=?", (request["actionId"],)).fetchone()
        if row:
            if row[0] != digest:
                raise PackError("action identity reused with different content")
            if row[1] is not None:
                return json.loads(row[1])
            # A crash after dispatch could have committed external effects.
            result = unfinished('interrupted')
        else:
            self.db.execute("INSERT INTO actions(id,digest) VALUES(?,?)", (request["actionId"], digest))
            self.db.commit()
            remaining = min(request["deadlineEpoch"], self.config["expiresEpoch"]) - self.clock()
            if remaining <= 0:
                result = unfinished('timed-out')
            else:
                # Exceptions leave the durable started row intact for recovery.
                result = (self.execute_mcp(request['action'], min(seconds, remaining), self.workspace) if structured else
                          self.execute(request["command"], min(seconds, remaining), self.workspace))
        reply = {"protocol": request['protocol'], "attemptId": request["attemptId"], "actionId": request["actionId"],
                 "commandDigest": digest, **result}
        validate_reply(reply, request)
        self.db.execute("UPDATE actions SET reply=? WHERE id=?", (encode(reply), request["actionId"]))
        self.db.commit()
        return reply


def serve(sqs, config, executor, *, clock=time.time, control_handler=None):
    # Reuse the controller configuration validation without sending any command.
    Worker(sqs, config, clock=clock)
    while clock() < config["expiresEpoch"]:
        remaining = config["expiresEpoch"] - clock()
        response = sqs.receive_message(QueueUrl=config["commandQueueUrl"], MaxNumberOfMessages=1,
            WaitTimeSeconds=max(1, min(2, math.floor(remaining))), VisibilityTimeout=330)
        for message in response.get("Messages", []):
            if control_handler is not None and control_handler(message):
                continue
            if len(message["Body"].encode()) > 100000:
                raise PackError("oversized container command")
            request = json.loads(message["Body"])
            reply = executor.handle(request)
            for part in reply_messages(reply, request):
                if clock() >= config['expiresEpoch']:
                    raise PackError('worker expired during reply publication; reconcile retained evidence')
                # Each immutable chunk has its own deduplication identity.
                sqs.send_message(QueueUrl=config["replyQueueUrl"], MessageBody=encode(part),
                    MessageGroupId=config["attemptId"], MessageDeduplicationId=command_digest(part)[7:])
            # A reply-publication failure leaves the command available; the
            # persisted result handles redelivery without re-execution.
            sqs.delete_message(QueueUrl=config["commandQueueUrl"], ReceiptHandle=message["ReceiptHandle"])


def main():
    import boto3
    from botocore.config import Config
    config = json.loads(os.environ["HONUA_AGENT_TRANSPORT"])
    if config.get('mode') == 'installed-slot':
        from agent_e2e_installed_container import serve_installed
        serve_installed(boto3.client('sqs', config=Config(connect_timeout=5, read_timeout=10,
            retries={'total_max_attempts': 1})), config)
        return
    executor = Executor(config, "/journal/actions.sqlite", "/workspace")
    serve(boto3.client("sqs", config=Config(connect_timeout=5, read_timeout=10,
          retries={"total_max_attempts": 2})), config, executor)


if __name__ == "__main__":
    main()
