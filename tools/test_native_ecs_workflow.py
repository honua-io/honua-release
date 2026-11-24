"""Offline checks for the dispatch and evidence boundary."""
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
import yaml

import nightly_receipts

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
REPO = "honua-io/honua-release"
SNAPSHOT = "a" * 40


def workflow(name):
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())


def test_nightly_orders_native_producer_after_ledger_and_pins_run():
    jobs = workflow("nightly-certification")["jobs"]
    producer = jobs["native_qualification"]
    assert "protocol_ledger" in producer["needs"]
    assert producer["steps"][-1]["env"]["CANDIDATE_REF"] == (
        "${{ needs.protocol_ledger.outputs.candidate_ref }}"
    )
    train = jobs["train"]
    assert "native_qualification" in train["needs"]
    assert "needs.native_qualification.result == 'success'" in train["if"]
    assert train["with"]["native_qualification_run_id"] == (
        "${{ needs.native_qualification.outputs.run_id }}"
    )
    gate = workflow("release-train")["jobs"]["gate_cloud_parity"]
    assert gate["with"]["producer_run_id"] == "${{ inputs.native_qualification_run_id }}"


def test_manual_train_accepts_the_native_producer_run():
    train = workflow("release-train")
    dispatch_input = train[True]["workflow_dispatch"]["inputs"]["native_qualification_run_id"]
    assert dispatch_input["type"] == "string"
    assert dispatch_input["required"] is False


@pytest.mark.parametrize("run_id", ["", "invalid"])
def test_train_gate_rejects_missing_or_invalid_producer_run(run_id):
    steps = workflow("gate-native-ecs-qualification")["jobs"]["qualification"]["steps"]
    resolve = next(step for step in steps if step.get("id") == "producer")
    env = {**os.environ, "PRODUCER_RUN_ID": run_id, "CANDIDATE_DIGEST": "a" * 64,
           "LOCK_DIGEST": "sha256:" + "b" * 64}
    result = subprocess.run(["bash", "-c", resolve["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "An exact native ECS producer run is required" in result.stdout


@pytest.mark.parametrize("branch", ["trunk", "another-branch"])
def test_native_dispatch_checks_returned_run(branch):
    api = Mock(side_effect=[
        {"workflow_run_id": 42},
        {"id": 42, "event": "workflow_dispatch", "head_branch": branch,
         "path": ".github/workflows/native-ecs-qualification.yml"},
    ])
    if branch == "trunk":
        assert nightly_receipts.dispatch_native_ecs(api, REPO, SNAPSHOT, "nightly-label") == 42
    else:
        with pytest.raises(LookupError, match="branch"):
            nightly_receipts.dispatch_native_ecs(api, REPO, SNAPSHOT, "nightly-label")
    method, path, body = api.call_args_list[0].args
    assert method == "POST"
    assert path.endswith("/native-ecs-qualification.yml/dispatches")
    assert body == {
        "ref": "refs/heads/trunk",
        "inputs": {"candidate_ref": SNAPSHOT, "platform_label": "nightly-label"},
        "return_run_details": True,
    }


def test_pending_ledger_stops_preflight(tmp_path):
    (tmp_path / "platform-manifest.yaml").write_text("protocolCertification: {ledger: {status: pending}}")
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
           "CANDIDATE_REF": SNAPSHOT, "PLATFORM_LABEL": "nightly-label"}
    result = subprocess.run(
        ["bash", str(ROOT / "tools/native_ecs_preflight.sh")], cwd=tmp_path,
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "requires the bound protocol ledger" in result.stderr
    assert not (tmp_path / "native-ecs-diagnostics").exists()


def test_blocked_producer_fails_and_only_uploads_diagnostics():
    job = workflow("native-ecs-qualification")["jobs"]["qualification"]
    assert job["if"] == "github.ref == 'refs/heads/trunk'"
    blocked, failure, diagnostics = job["steps"][-3:]
    assert failure["if"] == "failure()"
    result = subprocess.run(["bash", "-c", blocked["run"]], capture_output=True, text=True)
    assert result.returncode == 1
    assert "#1546" in result.stdout
    assert diagnostics["if"] == "always()"
    assert diagnostics["with"]["name"].startswith("native-ecs-diagnostics-")
