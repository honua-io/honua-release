"""Regression tests for driver verify import-crash attribution in terminal_model_canary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import terminal_model_canary as canary  # noqa: E402

MANIFEST = REPO_ROOT / "platform-manifest.yaml"
JOURNEY = REPO_ROOT / "certification" / "terminal-journey" / "journey.v1.json"
PROTOCOL = REPO_ROOT / "certification" / "terminal-model-canary" / "driver-protocol.v1.json"
LOCK = "sha256:" + "a" * 64
CRASH_RESPONSE = {
    "status": "fail",
    "operation": "verify",
    "error": "ModuleNotFoundError: No module named 'no_redirect'",
}


def _endpoint(*, key: str | None = None) -> canary.EndpointConfig:
    return canary.EndpointConfig(
        base_url="http://127.0.0.1:8000/v1",
        model="qwen-local",
        api_key=key,
        api_key_env="TERMINAL_MODEL_API_KEY",
        runtime="vllm",
        quantization="awq-4bit",
    )


class _StubModel:
    def __init__(self, *_):
        pass

    def ensure_admission(self):
        return [{"operation": "admin.ai.admission.initialize", "status": "Completed"}]

    def complete(self, *_):
        return json.dumps({"kind": "tool_call", "tool": "honua_read", "arguments": {}}), {}, 0, {}


class _VerifyCrashDriver:
    """Replays all stages cleanly and raises a configured error when verify is called."""

    def __init__(self, builder, verify_error_factory):
        self.stages = {stage["id"]: stage for stage in builder.journey["stages"]}
        self.approval = {stage["id"] for stage in builder.journey["stages"][-2:]}
        self.injection = builder.receipt["errorInjection"]["stageId"]
        self.error_id = builder.receipt["errorInjection"]["id"]
        self.attempts: dict[str, int] = {}
        self.approved: set[str] = set()
        self.verify_calls = 0
        self.verify_error_factory = verify_error_factory

    def _observe(self, stage_id: str) -> dict:
        needed = 2 if stage_id == self.injection else 1
        if self.attempts.get(stage_id, 0) < needed:
            return {"status": "ready", "stageStatus": "ready", "observation": {}, "toolView": {}}
        if stage_id in self.approval and stage_id not in self.approved:
            return {"status": "ready", "stageStatus": "awaiting_approval", "proposalId": stage_id}
        stage = self.stages[stage_id]
        return {"status": "pass", "stageStatus": {
            "id": stage_id, "number": stage["number"], "command": stage["command"],
            "status": "pass", "blockedBy": [], "checks": [{"status": "pass"}]}}

    def _execute(self, stage_id: str) -> dict:
        self.attempts[stage_id] = self.attempts.get(stage_id, 0) + 1
        if stage_id == self.injection and self.attempts[stage_id] == 1:
            return {"status": "fail", "result": {"injectedError": {"id": self.error_id, "recoverable": True}}}
        if stage_id == self.injection:
            return {"status": "pass", "result": {"recoveredError": {"id": self.error_id, "recovered": True}}}
        return {"status": "pass"}

    def invoke(self, operation: str, payload: dict) -> dict:
        stage_id = payload.get("stage")
        handlers = {
            "setup": lambda: {"status": "ready", "workspaceId": "w", "toolView": {"v": 1},
                              "credentialReferences": [{"envVar": "K"}]},
            "inject_error": lambda: {"status": "armed", "errorId": self.error_id, "recoverable": True},
            "observe": lambda: self._observe(stage_id),
            "execute": lambda: self._execute(stage_id),
            "approve": lambda: self._approve(stage_id),
            "verify": self._verify,
        }
        return handlers.get(operation, lambda: {"status": "complete"})()

    def _approve(self, stage_id: str) -> dict:
        self.approved.add(stage_id)
        return {"status": "approved", "proposalId": stage_id, "resolvedBy": "approver",
                "auditId": "audit", "proposerSelfApproval": "denied"}

    def _verify(self) -> dict:
        self.verify_calls += 1
        raise self.verify_error_factory()


def test_import_error_diagnostics_and_crash_classification():
    assert canary.driver_failure_diagnostic(CRASH_RESPONSE) == (
        "; driverError=ModuleNotFoundError: No module named 'no_redirect'"
    )
    assert canary.driver_failure_diagnostic(
        {"status": "fail", "error": "ImportError: cannot import name 'x'"}
    ) == "; driverError=ImportError: cannot import name 'x'"
    assert canary._driver_crashed(CRASH_RESPONSE, 1)
    assert canary._driver_crashed({"status": "fail", "error": "ImportError: x"}, 1)
    assert not canary._verify_oracle_failed(CRASH_RESPONSE)


def test_verify_import_crash_reaches_verify_and_never_attributes_to_model(monkeypatch):
    def run_with(factory):
        endpoint = _endpoint(key="test-key")
        builder = canary.build_receipt_builder(
            manifest_path=MANIFEST, journey_path=JOURNEY, protocol_path=PROTOCOL,
            endpoint=endpoint, driver_command=None, lock_digest=LOCK,
        )
        driver = _VerifyCrashDriver(builder, factory)
        monkeypatch.setattr(canary, "DriverAdapter", lambda *_: driver)
        monkeypatch.setattr(canary, "CandidateProxyClient", _StubModel)
        receipt = canary.execute_live(builder, endpoint=endpoint, driver_command=canary.DEFAULT_DRIVER,
                                      max_actions_per_stage=4)
        assert driver.verify_calls == 1
        return receipt

    def infra_error():
        return canary.InfrastructureFailure("driver verify crashed; driverError=ModuleNotFoundError")

    def envelope_error():
        error = canary.CanaryError("driver verify failed with exit code 1")
        error.driver_response = CRASH_RESPONSE
        return error

    infra_receipt = run_with(infra_error)
    assert infra_receipt["status"] == "fail" and infra_receipt["failureAttribution"] == "infrastructure"
    assert not any("final #123 verification did not pass" in notice for notice in infra_receipt["notices"])

    env_receipt = run_with(envelope_error)
    assert env_receipt["status"] == "fail" and env_receipt["failureAttribution"] == "undetermined"
    assert not any("final #123 verification did not pass" in notice for notice in env_receipt["notices"])
