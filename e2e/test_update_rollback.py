"""Boundary-injected producer tests; these receipts are synthetic controls, never cloud evidence."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import update_rollback as proof
from runner import update_rollback_aws as native
from targets.terraform_target import ecs, serverless


def release(letter="a", *, mode="qualification", schema="128"):
    value = "sha256:" + letter * 64
    return proof.Release({"components": {"honua-server": {"digest": value, "schemaVersions": {"database": schema}}}},
                         value, value if mode == "qualification" else None, letter * 40, schema,
                         "ghcr.io/honua-io/honua-server@" + value,
                         "ghcr.io/honua-io/honua-lambda@" + value, value)


class Runtime:
    def __init__(self):
        self.calls = []
        self.reads = 0
        self.fail_at = None
        self.duplicate = False
        self.changed_output = False
        self.outputs = 0

    def record(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise proof.ProofError("injected " + name)

    def prerequisites(self, *a): self.record("prerequisites")
    def observe(self, build, phase): self.record(phase); return {"phase": phase, "image": build.image}
    def incompatible_target(self): self.record("negative"); return {"status": "pass"}
    def stage(self, *a): self.record("stage"); return {"previous": "1", "desired": "2"}
    def plan(self, *a): self.record("plan")
    def submit_job(self): self.record("submit"); return {"jobID": "real-boundary-job"}
    def job(self, identity):
        self.reads += 1
        state = "Succeeded" if self.reads >= 6 else "Running"
        return {"jobId": identity, "status": state, "backend": "local", "claimedBy": "worker-1", "attemptCount": 1,
                "providerOperationId": "provider-1", "completedAt": "2026-10-17T21:00:00Z" if state == "Succeeded" else None}
    def update(self, *a): self.record("update"); return {"status": "Succeeded"}
    def rollback(self, *a): self.record("rollback-op"); return {"status": "RolledBack"}
    def refusal(self): self.record("refusal"); return {"code": "dependency-unavailable", "missingDependency": "redis", "functionalGpExecuted": False}
    def output(self, *a):
        self.outputs += 1
        return {"sha256": "sha256:" + ("b" if self.changed_output and self.outputs > 1 else "a") * 64}
    def execution_events(self, *a): return {"started": 2 if self.duplicate else 1, "completed": 1}


def run(runtime=None, cell="aws-ecs/redis-on", mode="qualification", prior=None, candidate=None):
    return proof.run_cell(prior or release(mode=mode), candidate or release("b", mode=mode), cell, mode, runtime or Runtime(),
                          run_id="123", run_attempt="1", sleep=lambda _: None, polls=8)


@pytest.mark.parametrize("cell", sorted(proof.CELLS))
def test_real_adapters_are_required_for_every_topology(cell):
    runtime = Runtime()
    result = run(runtime, cell)
    assert result["status"] == "pass", result
    assert result["actuator"] == proof.ACTUATORS[cell]
    assert runtime.calls.index("negative") < runtime.calls.index("stage") < runtime.calls.index("update")
    if cell.endswith("redis-off"):
        assert "submit" not in runtime.calls
        assert "inFlightJob" not in result
        assert result["executionOutcome"] == "refused" and result["functionalGpExecuted"] is False
        assert runtime.calls.count("refusal") == 3
    else:
        assert result["functionalGpExecuted"] is True
        assert result["inFlightJob"]["terminalStates"] == ["succeeded"]
        assert result["jobBeforeUpdate"]["jobId"] == result["jobAfterRollback"]["jobId"]


@pytest.mark.parametrize("bad", ["same-build", "schema", "missing-lock", "same-lock", "preview"])
def test_preflight_refuses_before_any_adapter_mutation(bad):
    prior, candidate, cell = release(), release("b"), "aws-ecs/redis-on"
    if bad == "same-build": candidate = prior
    if bad == "schema": candidate = release("b", schema="129")
    if bad == "missing-lock": candidate = release("b", mode="rehearsal")
    if bad == "same-lock": candidate = proof.Release(**(candidate.__dict__ | {"lock_digest": prior.lock_digest}))
    if bad == "preview": cell = "aws-eks/redis-on"
    runtime = Runtime()
    result = run(runtime, prior=prior, candidate=candidate, cell=cell)
    assert result["status"] == "fail" and runtime.calls == []


@pytest.mark.parametrize("at", [1, 2, 3, 4, 5])
def test_job_must_be_still_running_at_every_switch_boundary(at):
    runtime = Runtime()
    original = runtime.job
    def job(identity):
        row = original(identity)
        if runtime.reads >= at:
            row["status"] = "Succeeded"
        return row
    runtime.job = job
    result = run(runtime)
    assert result["status"] == "fail" and result["functionalGpExecuted"] is False


@pytest.mark.parametrize("field,value", [("jobId", "different"), ("providerOperationId", "different"),
                                        ("attemptCount", 2), ("claimedBy", "different"), ("backend", "different")])
def test_handoff_does_not_rebind_execution_identity(field, value):
    runtime = Runtime()
    original = runtime.job
    def job(identity):
        row = original(identity)
        if runtime.reads > 1:
            row[field] = value
        return row
    runtime.job = job
    assert run(runtime)["status"] == "fail"


@pytest.mark.parametrize("negative", ["duplicate", "changed_output"])
def test_exactly_once_requires_independent_events_and_stable_output(negative):
    runtime = Runtime()
    setattr(runtime, negative, True)
    assert run(runtime)["status"] == "fail"


def test_rehearsal_does_not_get_promotion_evidence_credit():
    cells = []
    for cell in sorted(proof.CELLS):
        result = run(cell=cell, mode="rehearsal")
        result["cleanup"] = result["cost"] = {"status": "pass"}
        cells.append(result)
    receipt = proof.aggregate(cells)
    assert receipt["diagnosticOnly"] is True and receipt["qualification"] is False
    assert not any(key in receipt for key in ("class", "kind", "lockDigest", "fromLockDigest", "toLockDigest"))


@pytest.mark.parametrize("missing", ["cleanup", "cost", "cell", "binding"])
def test_no_partial_aggregate_or_unbound_cleanup(missing):
    cells = []
    for cell in sorted(proof.CELLS):
        row = run(cell=cell)
        row["cleanup"] = row["cost"] = {"status": "pass"}
        cells.append(row)
    if missing in ("cleanup", "cost"): del cells[0][missing]
    if missing == "cell": cells.pop()
    if missing == "binding": cells[0]["runAttempt"] = "2"
    with pytest.raises(proof.ProofError): proof.aggregate(cells)


def test_finalize_binds_actual_teardown_and_meter_to_same_run():
    row = run()
    report = {"cell": row["cell"], "cost": {"status": "pass", "runId": "123", "runAttempt": "1"},
              "teardown": {"attempted": True, "completed": True}}
    final = proof.finalize_cell(row, report)
    assert final["cleanup"]["status"] == "pass" and final["cloudReportSha256"].startswith("sha256:")
    for bad in ({"teardown": {"attempted": True, "completed": False}}, {"cleanupFailures": ["orphan"]},
                {"cost": {"status": "pass", "runId": "other", "runAttempt": "1"}}):
        with pytest.raises(proof.ProofError): proof.finalize_cell(row, report | bad)


def test_exact_apply_keeps_all_other_inputs_and_uses_saved_plan(tmp_path, monkeypatch):
    target = ecs()
    target._workdir = tmp_path
    (tmp_path / "terraform.tfstate").write_text("{}")
    monkeypatch.setattr(target, "_iac_root", lambda: tmp_path)
    original = ["-var=honua_image=ghcr.io/honua-io/honua-server@sha256:" + "a" * 64, "-var=redis_enabled=false", "-var-file=protected.json"]
    target._last_vars = original[:]
    calls = []
    monkeypatch.setattr(target, "_tf", lambda _root, *args, **kw: calls.append(args) or SimpleNamespace(stdout="", returncode=0))
    target.update("ghcr.io/honua-io/honua-server@sha256:" + "b" * 64)
    assert calls[0][0] == "plan" and calls[1] == ("apply", "-input=false", "-auto-approve", "honua-update.tfplan")
    assert "-var-file=protected.json" in calls[0] and "-var=redis_enabled=false" in calls[0]
    target.rollback()
    assert original[0] in calls[2]


def test_no_native_backend_is_silently_replaced_by_terraform(tmp_path):
    for target, enabled in ((serverless(), False), (ecs(), True)):
        with pytest.raises(Exception, match="only the ECS Redis-off"):
            target.update("ghcr.io/honua-io/honua-server@sha256:" + "b" * 64, redis_enabled=enabled)


class FakeApi:
    def __init__(self, rows): self.rows, self.calls = list(rows), []
    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.rows.pop(0)
    def get(self, path): return self.request("GET", path)[1]


def test_native_lambda_and_ecs_use_actual_supported_deploy_operation_contract():
    for backend in ("honua-gitops-aws-lambda", "honua-aws-ecs-alb"):
        api = FakeApi([(200, {"backendRegistered": True, "target": {"backend": backend, "targetId": "target"},
                              "readyToSubmit": True, "capabilities": {"supportsRollback": True}}),
                       (201, {"operationId": "op-1"}),
                       (200, {"operationId": "op-1", "target": {"backend": backend}, "status": "Succeeded",
                              "observedState": "2", "providerOperationId": "native"}),
                       (200, {"operationId": "op-1", "target": {"backend": backend}, "status": "RolledBack",
                              "observedState": "1", "providerOperationId": "native"})])
        approval = FakeApi([(200, {}), (200, {})])
        adapter = proof.DeployApi(api, "target", backend, approval_api=approval, sleep=lambda _: None)
        assert adapter.submit("1", "2", "idempotent")["observedRevision"] == "2"
        assert adapter.rollback("1")["status"] == "RolledBack"
        assert approval.calls[0][1].endswith("/op-1/submit") and approval.calls[1][1].endswith("/op-1/rollback")
        assert api.calls[1][2]["submitImmediately"] is False
        assert not any(path.endswith("/promote") for _, path, _ in api.calls + approval.calls)


def test_auth_endpoint_redirect_credentials_and_cross_origin_fail_closed():
    for endpoint in ("http://public.example", "https://key@host", "https://host/?token=x"):
        with pytest.raises(proof.ProofError): proof.Api(endpoint, "key")
    with pytest.raises(proof.ProofError): proof.Api("https://host", "key\nASPNETCORE_ENVIRONMENT=Development")


def test_authored_workload_is_real_symmetric_wkb_with_independent_oracle():
    import base64
    import struct
    payload = native.centroid_workload(4)
    raw = base64.b64decode(payload["inputs"]["wkb"])
    assert struct.unpack("<BIII", raw[:13]) == (1, 3, 1, 5)
    points = [struct.unpack("<dd", raw[i:i+16]) for i in range(13, len(raw), 16)]
    assert points[0] == points[-1]
    assert points[0] == (10, 0)
    assert payload["inputs"]["srid"] == "4326"
