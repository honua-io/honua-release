"""Goal-loop failure accounting with transport doubles, not qualification receipts."""
import copy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from agent_e2e_budget import update
from agent_e2e_controller import ControllerError, ModelFailure, run_goal, validate_action
from agent_e2e_pack import load_json
from agent_e2e_telemetry import read_events, summarize


ROOT = Path(__file__).resolve().parents[1]
KEY = "sha256:" + "a" * 64
BASIS = "sha256:" + "b" * 64


class Model:
    config = {"modelId": "requested", "returnedModelId": "returned", "pricingDigest": BASIS}
    def __init__(self, content=None):
        self.content = content
        self.inputs = []
    def request(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        return messages
    def quote(self, request):
        return b"request", Decimal("0.02")
    def invoke(self, body):
        content = self.content or [{"type": "text", "text": "Done."}]
        return ({"model": "returned", "stop_reason": "end_turn", "content": content,
                 "usage": {"input_tokens": 10, "output_tokens": 5}}, Decimal("0.01"), "request-id")


class Adapter:
    def __init__(self):
        self.operations = []
        self.public = {"files": [], "documentation": [], "target": "local test"}
        self.verify_status = "pass"
        self.cleanup_error = False
    def preflight(self, context):
        return {"target": "local-docker", "maximumInfrastructureUsd": "0.1",
                "maximumCleanupUsd": "0.1", "liabilityBasisDigest": BASIS}
    def call(self, operation, payload, **kwargs):
        self.operations.append((operation, copy.deepcopy(payload)))
        if operation == "prepare":
            return {"publicInputs": self.public, "secretOracle": "MUST NOT REACH MODEL"}
        if operation == "verify":
            return {"status": self.verify_status, "failedAssertions": []}
        if operation == "cleanup":
            if self.cleanup_error:
                raise ControllerError("credential-bearing error must stay private")
            return {"status": "pass", "observedInfrastructureUsd": "0.01", "observedCleanupUsd": "0.01"}
        if operation == "authorize_retry":
            return {"authorized": False}
        if operation == "agent_action":
            return {"agentResult": {"content": "actual tool output", "isError": False, "errorCategory": None},
                    "operationCategory": "discovery"}
        raise AssertionError(operation)


@pytest.fixture
def environment(tmp_path):
    pack = load_json(ROOT / "certification/agent-e2e/pack.v1.json")
    corpus = load_json(ROOT / "certification/agent-e2e/corpus/manifest.v1.json")
    next(s for s in pack["scenarios"] if s["id"] == "P10")["limits"]["maxTurns"] = 2
    ledger = tmp_path / "budget.json"
    update(ledger, "init", candidate_key=KEY)
    return {"pack": pack, "corpus": corpus, "case_id": "P10:A:local-docker",
            "campaign": {"campaignId": "campaign", "componentBindingDigest": KEY,
                         "qualificationLockDigest": BASIS, "modelId": "requested", "returnedModelId": "returned"},
            "ledger": ledger, "journal_root": tmp_path / "journals", "backend": Adapter(), "model": Model()}


def test_hidden_prepare_payload_not_in_model_input_and_no_qualification_claim(environment):
    result = run_goal(**environment)
    assert result["status"] == "pass" and result["qualification"] is False
    text = json.dumps(environment["model"].inputs)
    assert "secretOracle" not in text and "MUST NOT REACH MODEL" not in text
    assert "assertions" not in text and "expectedOutcome" not in text
    assert summarize(read_events(result["journal"]))["agentEncounteredCorpus"] is True


def test_incorrect_final_state_is_not_retryable_infrastructure(environment):
    environment["backend"].verify_status = "fail"
    result = run_goal(**environment)
    assert result["status"] == "fail" and result["failureAttribution"] == "product"


def test_unknown_model_action_is_model_failure_and_cleanup_runs(environment):
    environment["model"] = Model([{"type": "tool_use", "id": "x", "name": "oracle", "input": {}}])
    result = run_goal(**environment)
    assert result["failureAttribution"] == "model"
    assert [op for op, _ in environment["backend"].operations] == ["prepare", "cleanup"]


def test_turn_exhaustion_is_not_infrastructure_retry(environment):
    class RepeatingModel(Model):
        def invoke(self, body):
            self.content = [{"type": "tool_use", "id": str(len(self.inputs)), "name": "mcp_list_tools", "input": {}}]
            return super().invoke(body)
    environment["model"] = RepeatingModel()
    result = run_goal(**environment)
    assert result["status"] == "fail" and result["failureAttribution"] == "model"
    assert result["usage"] == {"modelTurns": 2, "executedActions": 2}
    assert summarize(read_events(result["journal"]))["repeatedRequests"] == 1


def test_cleanup_error_is_retained_and_reservations_are_not_erased(environment):
    environment["backend"].cleanup_error = True
    result = run_goal(**environment)
    assert result["status"] == "blocked" and result["reason"] == "cleanup-failure"
    assert "credential-bearing" not in json.dumps(result)
    assert sum(e["state"] == "reserved" for e in load_json(environment["ledger"])["entries"]) == 2
    assert read_events(result["journal"])[-1]["event"] == "end"


def test_same_attempt_cannot_overwrite_a_failed_attempt(environment):
    environment["backend"].verify_status = "fail"
    run_goal(**environment)
    environment["backend"].verify_status = "pass"
    with pytest.raises(FileExistsError):
        run_goal(**environment)


def test_retry_requires_independent_authorization_before_paid_work(environment):
    result = run_goal(**environment, attempt=2)
    assert result["status"] == "blocked"
    assert [op for op, _ in environment["backend"].operations] == ["authorize_retry"]
    assert load_json(environment["ledger"])["entries"] == []


@pytest.mark.parametrize("args", [{"command": "echo hi", "timeoutSeconds": True}, {"command": ""},
                                  {"command": "echo hi", "hidden": "oracle"}])
def test_invalid_terminal_arguments_never_reach_backend(args):
    with pytest.raises(ModelFailure):
        validate_action({"type": "tool_use", "id": "one", "name": "terminal", "input": args})
