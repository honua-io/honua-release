"""Controller-side container command transport. Infrastructure is Terraform-owned.

The same per-attempt queues connect Fargate or dedicated Docker workers. Replies
are untrusted terminal output, never independent qualification evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from agent_e2e_errors import PackError

PROTOCOL = "honua.agent-e2e.container/v1"
MAX_STDOUT = 16384
MAX_STDERR = 4096


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def command_digest(command):
    return "sha256:" + hashlib.sha256(encode(command).encode()).hexdigest()


def validate_command(value, attempt_id, expires, now):
    required = {"protocol", "attemptId", "actionId", "deadlineEpoch", "command", "timeoutSeconds"}
    if (not isinstance(value, dict) or set(value) != required or value["protocol"] != PROTOCOL
            or value["attemptId"] != attempt_id
            or not isinstance(value["actionId"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", value["actionId"])
            or not isinstance(value["command"], str) or not value["command"].strip()
            or len(value["command"].encode()) > 65536
            or type(value["timeoutSeconds"]) is not int or not 1 <= value["timeoutSeconds"] <= 300
            or type(value["deadlineEpoch"]) is not int
            or not now < value["deadlineEpoch"] <= expires
            or value["deadlineEpoch"] > now + value["timeoutSeconds"] + 1):
        raise PackError("invalid, expired or foreign container command")


def validate_reply(value, command):
    required = {"protocol", "attemptId", "actionId", "commandDigest", "status", "exitCode", "stdout", "stderr"}
    if (not isinstance(value, dict) or set(value) != required or value["protocol"] != PROTOCOL
            or value["attemptId"] != command["attemptId"] or value["actionId"] != command["actionId"]
            or value["commandDigest"] != command_digest(command)
            or value["status"] not in ("completed", "timed-out", "interrupted")
            or type(value["exitCode"]) is not int
            or not isinstance(value["stdout"], str) or len(value["stdout"].encode()) > MAX_STDOUT
            or not isinstance(value["stderr"], str) or len(value["stderr"].encode()) > MAX_STDERR):
        raise PackError("unbound or malformed container reply")


class Worker:
    def __init__(self, sqs, config, *, clock=time.time):
        required = {"commandQueueUrl", "replyQueueUrl", "attemptId", "expiresEpoch"}
        if (not isinstance(config, dict) or set(config) != required
                or not isinstance(config["attemptId"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", config["attemptId"])
                or type(config["expiresEpoch"]) is not int
                or not clock() < config["expiresEpoch"] <= clock() + 6000
                or config["commandQueueUrl"] == config["replyQueueUrl"]):
            raise PackError("invalid container transport configuration")
        for field in ("commandQueueUrl", "replyQueueUrl"):
            if not isinstance(config[field], str) or not re.fullmatch(
                    r"https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+\.fifo", config[field]):
                raise PackError("container transport requires reviewed FIFO queues")
        self.sqs, self.config, self.clock = sqs, dict(config), clock
        self.sent = set()

    def execute(self, command, action_id, timeout=300):
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise PackError("invalid terminal timeout")
        now = self.clock()
        deadline = min(int(now) + timeout, self.config["expiresEpoch"] - 5)
        request = {"protocol": PROTOCOL, "attemptId": self.config["attemptId"], "actionId": action_id,
                   "deadlineEpoch": deadline, "command": command, "timeoutSeconds": timeout}
        validate_command(request, self.config["attemptId"], self.config["expiresEpoch"], now)
        if action_id in self.sent:
            raise PackError("action already dispatched; reconcile durable history before retry")
        # Mark before the API call: an ambiguous network failure must not resend.
        # The backend also persists dispatch before calling this transport.
        self.sent.add(action_id)
        self.sqs.send_message(QueueUrl=self.config["commandQueueUrl"], MessageBody=encode(request),
            MessageGroupId=self.config["attemptId"], MessageDeduplicationId=command_digest(request)[7:])
        while self.clock() < deadline + 5:
            response = self.sqs.receive_message(QueueUrl=self.config["replyQueueUrl"],
                MaxNumberOfMessages=1, WaitTimeSeconds=min(2, max(1, int(deadline + 5 - self.clock()))),
                VisibilityTimeout=30)
            for message in response.get("Messages", []):
                if len(message["Body"].encode()) > 40000:
                    raise PackError("oversized container reply")
                reply = json.loads(message["Body"])
                validate_reply(reply, request)
                self.sqs.delete_message(QueueUrl=self.config["replyQueueUrl"],
                                        ReceiptHandle=message["ReceiptHandle"])
                return reply
        raise PackError("container response missing; outcome remains unverified")
