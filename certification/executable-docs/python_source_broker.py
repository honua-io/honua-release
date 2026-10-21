"""Host-owned finite bridge. Document requests never become arbitrary controller code."""
from __future__ import annotations

import base64
import hashlib
import shlex

from python_source_execution import ExecutionRefusal


def validate_original_phases(code, invocations):
    """Refuse interpreter use concurrent with arbitrary document code.

    Only exact finite setup and trusted bridges may precede the final pip call.
    After that call, every real nonpip command runs in the reader and no further
    install/audit is admitted until that entire reader is actually terminal.
    """
    if not isinstance(invocations, list) or not invocations:
        raise ExecutionRefusal("original source block has no finite install declarations")
    ordinal, arbitrary_started, clones = 0, False, 0
    for line in code.splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[:2] == ["pip", "install"]:
            if (arbitrary_started or ordinal >= len(invocations)
                    or line != invocations[ordinal]["command"]):
                raise ExecutionRefusal("original source block has an unsafe or reordered install phase")
            ordinal += 1
        elif words == ["git", "clone", "https://github.com/honua-io/honua-sdk-python.git"]:
            if arbitrary_started or ordinal or clones:
                raise ExecutionRefusal("original clone is outside its finite setup phase")
            clones += 1
        elif words == ["cd", "honua-sdk-python"] and clones == 1 and ordinal == 0 and not arbitrary_started:
            continue
        else:
            if ordinal != len(invocations):
                raise ExecutionRefusal("document code can write the environment before a trusted install audit")
            arbitrary_started = True
    if ordinal != len(invocations):
        raise ExecutionRefusal("original source block omitted a finite install phase")
    return {"cloneRequired": clones == 1, "installCount": ordinal,
            "nonpipTailPresent": arbitrary_started, "finalAuditRequiresTerminalReader": True}


class BlockBroker:
    """Bind a single live document container to one original ordered block.

    The caller serializes dispatch, initializes the private controller before
    admitting requests, and closes the socket only after the actual reader is
    terminal. Its worker transport must authenticate every returned receipt.
    Clone success requires an actual immutable clone/transposition receipt; no
    operation marks a non-pip development command successful.
    """

    def __init__(self, *, context_index, block_sha256, checkout, invocations, dispatch, clone_required=False):
        self.context_index = context_index
        self.block_sha256 = block_sha256
        self.checkout = str(checkout)
        self.invocations = invocations
        self.dispatch = dispatch
        self.ordinal = 0
        self.failed = False
        self.clone_required = clone_required
        self.cloned = False
        self.finished = False

    def request(self, request):
        if self.failed or self.finished:
            raise ExecutionRefusal("controlled block already refused a request")
        try:
            return self._request(request)
        except Exception:
            self.failed = True
            raise

    def _request(self, request):
        if (not isinstance(request, dict)
                or set(request) != {"operation", "contextIndex", "blockSha256", "argv", "cwd"}
                or request["operation"] not in {"clone", "install"}
                or type(request["contextIndex"]) is not int
                or request["contextIndex"] != self.context_index
                or request["blockSha256"] != self.block_sha256):
            raise ExecutionRefusal("controlled request differs from its live original block")
        if request["operation"] == "clone":
            return self._clone(request)
        if request["cwd"] != self.checkout or self.clone_required and not self.cloned:
            raise ExecutionRefusal("controlled install preceded clone or changed its original cwd")
        if self.ordinal >= len(self.invocations):
            raise ExecutionRefusal("controlled block exceeded its original invocation count")
        expected = shlex.split(self.invocations[self.ordinal]["command"])[1:]
        if (not isinstance(request["argv"], list)
                or any(not isinstance(word, str) for word in request["argv"])
                or request["argv"] != expected):
            raise ExecutionRefusal("controlled request differs from the next original pip argv")
        result = self.dispatch({"contextIndex": self.context_index, "operation": "install", "argv": expected})
        if (not isinstance(result, dict)
                or type(result.get("contextIndex")) is not int
                or result.get("contextIndex") != self.context_index or result.get("operation") != "install"
                or type(result.get("completedInvocations")) is not int
                or result.get("completedInvocations") != self.ordinal + 1
                or any(result.get(key) is not False for key in
                       ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))
                or result.get("successfulStderrCaptured") is not False):
            raise ExecutionRefusal("private controller response differs from the actual request")
        try:
            stdout = base64.b64decode(result["stdoutBase64"], validate=True)
        except (KeyError, ValueError, TypeError) as error:
            raise ExecutionRefusal("private controller output has invalid bytes") from error
        if hashlib.sha256(stdout).hexdigest() != result.get("stdoutSha256"):
            raise ExecutionRefusal("private controller output bytes changed")
        self.ordinal += 1
        # The unchanged canonical engine captures successful stdout and discards
        # successful stderr. The explicit false field prevents claiming that an
        # empty stream was independently observed.
        return {"exitCode": 0, "stdoutBase64": result["stdoutBase64"],
                "stdoutSha256": result["stdoutSha256"], "stderrBase64": "",
                "stderrSha256": hashlib.sha256(b"").hexdigest(), "successfulStderrCaptured": False}

    def _clone(self, request):
        from pathlib import Path

        if (not self.clone_required or self.cloned or self.ordinal
                or request["cwd"] != str(Path(self.checkout).parent)
                or request["argv"] != ["clone", "https://github.com/honua-io/honua-sdk-python.git"]):
            raise ExecutionRefusal("controlled clone differs from its original position or request")
        result = self.dispatch({"contextIndex": self.context_index, "operation": "clone",
                                "argv": request["argv"], "cwd": request["cwd"]})
        self._join(result, "clone", 0)
        if result.get("mutableNetworkCloneEquivalent") is not False:
            raise ExecutionRefusal("immutable clone was incorrectly represented as a network clone")
        for stream in ("stdout", "stderr"):
            data = base64.b64decode(result[stream + "Base64"], validate=True)
            if hashlib.sha256(data).hexdigest() != result[stream + "Sha256"]:
                raise ExecutionRefusal("actual clone stream bytes changed")
        initialized = self.dispatch({"contextIndex": self.context_index, "operation": "initialize"})
        self._join(initialized, "initialize", 0)
        self.cloned = True
        return {"exitCode": 0, **{key: result[key] for key in
            ("stdoutBase64", "stdoutSha256", "stderrBase64", "stderrSha256")},
            "successfulStderrCaptured": True}

    def _join(self, result, operation, count):
        if (not isinstance(result, dict) or type(result.get("contextIndex")) is not int
                or result["contextIndex"] != self.context_index or result.get("operation") != operation
                or type(result.get("completedInvocations")) is not int or result["completedInvocations"] != count
                or any(result.get(key) is not False for key in
                       ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))):
            raise ExecutionRefusal("private controller response differs from the actual request")

    def finish(self):
        if self.failed or self.finished or self.ordinal != len(self.invocations):
            raise ExecutionRefusal("controlled block omitted or refused an original pip request")
        result = self.dispatch({"contextIndex": self.context_index, "operation": "finish"})
        self._join(result, "finish", self.ordinal)
        self.finished = True
        return result
