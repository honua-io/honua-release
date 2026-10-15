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
                              command_digest, encode, validate_command, validate_reply)


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


class Executor:
    def __init__(self, config, journal, workspace, *, clock=time.time, execute=terminal):
        self.config, self.clock, self.execute = dict(config), clock, execute
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
        row = self.db.execute("SELECT digest,reply FROM actions WHERE id=?", (request["actionId"],)).fetchone()
        if row:
            if row[0] != digest:
                raise PackError("action identity reused with different content")
            if row[1] is not None:
                return json.loads(row[1])
            # A crash after dispatch could have committed external effects.
            result = {"status": "interrupted", "exitCode": -1, "stdout": "", "stderr": ""}
        else:
            self.db.execute("INSERT INTO actions(id,digest) VALUES(?,?)", (request["actionId"], digest))
            self.db.commit()
            remaining = min(request["deadlineEpoch"], self.config["expiresEpoch"]) - self.clock()
            if remaining <= 0:
                result = {"status": "timed-out", "exitCode": -1, "stdout": "", "stderr": ""}
            else:
                # Exceptions leave the durable started row intact for recovery.
                result = self.execute(request["command"], min(seconds, remaining), self.workspace)
        reply = {"protocol": PROTOCOL, "attemptId": request["attemptId"], "actionId": request["actionId"],
                 "commandDigest": digest, **result}
        validate_reply(reply, request)
        self.db.execute("UPDATE actions SET reply=? WHERE id=?", (encode(reply), request["actionId"]))
        self.db.commit()
        return reply


def serve(sqs, config, executor, *, clock=time.time):
    # Reuse the controller configuration validation without sending any command.
    Worker(sqs, config, clock=clock)
    while clock() < config["expiresEpoch"]:
        remaining = config["expiresEpoch"] - clock()
        response = sqs.receive_message(QueueUrl=config["commandQueueUrl"], MaxNumberOfMessages=1,
            WaitTimeSeconds=max(1, min(2, math.floor(remaining))), VisibilityTimeout=330)
        for message in response.get("Messages", []):
            if len(message["Body"].encode()) > 100000:
                raise PackError("oversized container command")
            request = json.loads(message["Body"])
            reply = executor.handle(request)
            sqs.send_message(QueueUrl=config["replyQueueUrl"], MessageBody=encode(reply),
                MessageGroupId=config["attemptId"], MessageDeduplicationId=command_digest(request)[7:])
            # A reply-publication failure leaves the command available; the
            # persisted result handles redelivery without re-execution.
            sqs.delete_message(QueueUrl=config["commandQueueUrl"], ReceiptHandle=message["ReceiptHandle"])


def main():
    import boto3
    from botocore.config import Config
    config = json.loads(os.environ["HONUA_AGENT_TRANSPORT"])
    executor = Executor(config, "/journal/actions.sqlite", "/workspace")
    serve(boto3.client("sqs", config=Config(connect_timeout=5, read_timeout=10,
          retries={"total_max_attempts": 2})), config, executor)


if __name__ == "__main__":
    main()
