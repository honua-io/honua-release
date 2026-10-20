"""Finite trusted-controller operations; document code never supplies Python or shell code."""
from __future__ import annotations

import shlex

from python_source_execution import ExecutionRefusal


class SequenceProtocol:
    """Keep the original ordered install requests in private controller memory."""

    def __init__(self, sequence):
        self.sequence = sequence
        self.initialized = False
        self.finished = False
        self.receipts = []
        self.clone_required = "git clone https://github.com/honua-io/honua-sdk-python.git" in sequence.recorded.get("blockBytes", "")
        self.clone_receipt = None

    def request(self, request: dict) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
            raise ExecutionRefusal("controlled request has no finite operation")
        operation = request["operation"]
        expected_keys = ({"operation", "argv", "cwd"} if operation == "clone" else
                         {"operation", "argv"} if operation == "install" else {"operation"})
        if set(request) != expected_keys or operation not in {"clone", "initialize", "install", "audit", "finish"}:
            raise ExecutionRefusal("controlled request contains an unreviewed operation or field")
        if self.finished:
            raise ExecutionRefusal("controlled sequence already finished")
        if operation == "clone":
            if not self.clone_required or self.clone_receipt is not None or self.initialized:
                raise ExecutionRefusal("controlled clone is absent, repeated or out of order")
            if request["argv"] != ["clone", "https://github.com/honua-io/honua-sdk-python.git"]:
                raise ExecutionRefusal("controlled clone differs from the exact original request")
            self.clone_receipt = self.sequence.clone(request["argv"], request["cwd"])
            return self.clone_receipt
        if operation == "initialize":
            if self.initialized:
                raise ExecutionRefusal("controlled sequence initialization was repeated")
            if self.clone_required and self.clone_receipt is None:
                raise ExecutionRefusal("controlled sequence omitted the actual immutable clone")
            result = self.sequence.initialize()
            self.initialized = True
            return result
        if not self.initialized:
            raise ExecutionRefusal("controlled sequence has not initialized")
        if operation == "install":
            ordinal = len(self.receipts)
            if ordinal >= len(self.sequence.recorded["invocations"]):
                raise ExecutionRefusal("controlled install exceeds the original finite requests")
            invocation = self.sequence.recorded["invocations"][ordinal]
            argv = request["argv"]
            if (not isinstance(argv, list) or any(not isinstance(word, str) for word in argv)
                    or argv != shlex.split(invocation["command"])[1:]):
                raise ExecutionRefusal("actual pip request differs from the next original command")
            receipt = self.sequence.install(ordinal)
            if (receipt["ordinal"] != ordinal or receipt["originalCommand"] != invocation["command"]
                    or receipt["originalCommandSha256"] != invocation["commandSha256"]):
                raise ExecutionRefusal("actual installed receipt differs from the original request")
            self.receipts.append(receipt)
            return receipt
        if operation == "finish" and len(self.receipts) != len(self.sequence.recorded["invocations"]):
            raise ExecutionRefusal("controlled sequence omitted an original install request")
        observed = self.sequence.audit(len(self.receipts) - 1)
        if operation == "finish":
            self.finished = True
        return {"operation": operation, "completedInvocations": len(self.receipts),
                "installed": observed, "sourceAdmitted": False,
                "publishedWheelIdentity": False, "candidateQualified": False}
