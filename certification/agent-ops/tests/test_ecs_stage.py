"""Product-path ECS stage. Cloud writes stay out of the harness."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from botocore.stub import Stubber

from ecs_fault import JournalFault
from ecs_observe import EcsObserver, ObservationMissing
from ecs_probes import require_public_cell, score_public_probes
from ecs_stage import (
    OPEN_PRODUCT_GAPS,
    cell_receipt,
    drive,
    finish,
    forbidden_calls,
    grade_attempt,
    main,
    model_environment,
    pins_ready,
    require_ready,
    write_receipt,
)
from product_ops import Approver, ProductClient, ProductError, scrub_model_env

ENDPOINT = "https://cell.cert.demo.honua.io"
PROPOSER = "proposer-key-0123456789abcdef"
APPROVER_KEY = "approver-key-0123456789abcdef"


class Transport:
    def __init__(self, tools: dict, approval_status: int = 200, execution: str | None = "op-1"):
        self.tools = tools
        self.approval_status = approval_status
        self.execution = execution
        self.calls = []

    def __call__(self, request: dict) -> dict:
        self.calls.append(request)
        body = request["body"]
        if request["url"].endswith("/mcp"):
            if body["method"] == "initialize":
                return {"status": 200, "headers": {"Mcp-Session-Id": "session-1"}, "body": {"result": {}}}
            if body["method"] == "notifications/initialized":
                return {"status": 202, "headers": {}, "body": None}
            name = body["params"]["name"]
            return {"status": 200, "headers": {}, "body": {
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"structuredContent": self.tools.get(name, {}), "isError": False},
            }}
        if request["url"].endswith("/approve"):
            if self.approval_status == 422:
                return {"status": 422, "headers": {}, "body": {"code": "operation-executor-unavailable"}}
            return {"status": self.approval_status, "headers": {}, "body": {
                "status": "Executing", "executionOperationId": self.execution,
                "password": "must-not-leak",
            }}
        raise AssertionError(request["url"])


def _pair(tools=None, **kwargs):
    transport = Transport(tools or {
        "honua_ops_health": {},
        "honua_ops_findings": {"findings": [{"id": "finding-1"}]},
        "honua_alert_events": {},
        "honua_operate_events": {},
        "honua_propose_platform_release_convergence": {
            "outcome": "completed",
            "targets": [{"targetId": "cell", "proposalId": "proposal01"}],
        },
        "honua_propose_rollback": {"proposalId": "proposal02"},
        "honua_propose_finding": {"proposalId": "proposal08"},
        "honua_propose_deploy_operation": {},
    }, **kwargs)
    client = ProductClient(ENDPOINT, PROPOSER, transport)
    approver = Approver(ENDPOINT, PROPOSER, APPROVER_KEY, transport)
    return client, approver, transport


def test_model_env_drops_aws_credentials_and_leaves_the_source():
    source = {
        "AWS_ACCESS_KEY_ID": "real",
        "AWS_SECRET_ACCESS_KEY": "real",
        "AWS_SESSION_TOKEN": "real",
        "AWS_PROFILE": "cell",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/creds",
        "AWS_REGION": "us-east-1",
        "PATH": "/usr/bin",
    }
    scrubbed = scrub_model_env(source)
    assert scrubbed == {"PATH": "/usr/bin"}
    assert source["AWS_SECRET_ACCESS_KEY"] == "real"
    assert model_environment(source) == scrubbed


def test_same_principal_cannot_approve_and_approval_is_the_admin_route():
    with pytest.raises(ProductError) as error:
        Approver(ENDPOINT, PROPOSER, PROPOSER, lambda request: request)
    assert error.value.code == "approver"
    client, approver, transport = _pair()
    product = drive("O01", client, approver, target_id="cell", target_version="2026.1-rc.2")
    approve = next(call for call in transport.calls if call["url"].endswith("/approve"))
    assert approve["url"] == ENDPOINT + "/api/v1/admin/proposals/proposal01/approve"
    assert approve["headers"]["Authorization"] == f"Bearer {APPROVER_KEY}"
    assert "password" not in json.dumps(product)
    assert product["executionIds"] == ["op-1"]
    assert product["tools"][:4] == [
        "honua_ops_health", "honua_ops_findings", "honua_alert_events", "honua_operate_events"]


def test_missing_executor_is_a_product_gap_and_not_a_pass():
    client, approver, _transport = _pair(approval_status=422, execution=None)
    product = drive("O01", client, approver, target_id="cell")
    assert "operation-executor-unavailable" in product["why"]
    graded = grade_attempt("O01", product, {"server_readback": True}, None)
    assert graded["status"] == "fail"
    observations = {
        "server_readback": True, "image_digest": "sha256:" + "a" * 64,
        "target_digest": "sha256:" + "a" * 64, "schema": 142, "target_schema": 142,
        "customer_ok": True,
    }
    product["executionIds"] = ["op-1"]
    assert grade_attempt("O01", product, observations, None)["status"] == "pass"
    assert finish("O01", product, observations, None)["blockedPass"] is True
    assert finish("O01", product, observations, None)["status"] == "fail"


def test_restore_and_restart_are_not_simulated():
    client, approver, transport = _pair()
    restore = drive("O05", client, approver, target_id="cell")
    assert "no database restore executor" in restore["why"]
    assert "https://github.com/honua-io/honua-server/issues/6813" in restore["gaps"]
    assert not any(call["url"].endswith("/approve") for call in transport.calls)
    restart = drive("O02", client, approver, target_id="cell")
    assert restart["configExecution"] is None
    assert grade_attempt("O02", restart, {"server_readback": True}, None)["status"] == "fail"
    assert "6814" in "".join(restart["gaps"])
    names = [call["body"]["params"]["name"] for call in transport.calls
             if isinstance(call["body"], dict) and call["body"].get("method") == "tools/call"]
    assert "honua_propose_rollback" in names
    assert all("secret" not in name for name in names)


def test_diagnose_requires_a_fired_fault_and_records_the_finding_proposal():
    client, approver, transport = _pair()
    unfired = drive("O08", client, approver, target_id="cell", fault={"fired": False})
    assert finish("O08", unfired, {}, {"fired": False})["invalid"] is True
    fired = {"fired": True, "upgrade_started": True, "evidence_id": "a" * 32}
    product = drive("O08", client, approver, target_id="cell", fault=fired)
    assert any(call["body"].get("params", {}).get("name") == "honua_propose_finding"
               for call in transport.calls if isinstance(call["body"], dict))
    assert product["executionIds"] == ["op-1"]
    observations = {"server_readback": True, "image_digest": "sha256:" + "b" * 64,
                    "prior_digest": "sha256:" + "b" * 64, "schema": 141, "prior_schema": 141,
                    "customer_ok": True}
    assert grade_attempt("O08", product, observations, fired)["status"] == "pass"
    assert finish("O08", product, observations, fired)["status"] == "fail"


def test_check_stays_closed_and_pins_do_not_match_the_cell_image():
    assert require_ready() is False
    assert forbidden_calls() == []
    assert {gap["id"] for gap in OPEN_PRODUCT_GAPS} == {
        "convergence-image", "restore-142", "restart-config", "controller-cert-cell"}
    digest = "sha256:" + "a" * 64
    other = "sha256:" + "b" * 64
    pins = {"iac_ref": "c" * 40, "helm_ref": "d" * 40,
            "server_image": f"ghcr.io/honua-io/honua-server@{digest}",
            "ecs_architecture": "arm64", "lambda_architecture": "x86_64", "newer_digest": other}
    assert pins_ready(pins) is None
    assert pins_ready({**pins, "newer_digest": digest}) == "newer_digest matches the pinned server image"
    assert pins_ready({**pins, "server_image": "honua:latest"}) == "server_image is not digest-pinned"
    code = __import__("ecs_stage").main([
        "--check", "--iac-ref", "", "--helm-ref", "", "--server-image", "",
        "--ecs-architecture", "", "--lambda-architecture", "", "--newer-digest", ""])
    assert code == 2


def test_cell_cli_fails_closed_without_printing_keys(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HONUA_OPS_PROPOSER_KEY", raising=False)
    monkeypatch.delenv("HONUA_OPS_APPROVER_KEY", raising=False)
    directory = tmp_path / "cell"
    directory.mkdir()
    (directory / "provision.json").write_text(json.dumps({
        "cell": "aws-ecs/redis-on", "runId": "1", "runAttempt": "1"}), encoding="utf-8")
    assert main(["--cell", "--scenario", "O01", "--cell-dir", str(directory)]) == 1
    receipt = json.loads((directory / "operate-batch.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "fail"
    assert receipt["why"] == "cell credentials are not available to this process"
    monkeypatch.setenv("HONUA_OPS_PROPOSER_KEY", "proposer-secret-value")
    monkeypatch.setenv("HONUA_OPS_APPROVER_KEY", "approver-secret-value")
    assert main(["--cell", "--scenario", "O02", "--cell-dir", str(directory)]) == 1
    text = (directory / "operate-batch.json").read_text(encoding="utf-8")
    assert "proposer-secret-value" not in text and "approver-secret-value" not in text
    assert json.loads(text)["why"] == "product executors are not ready"
    assert json.loads(text)["status"] == "fail"


def test_cell_receipt_fails_closed_and_binds_the_run():
    provision = {"cell": "aws-ecs/redis-on", "runId": "99", "runAttempt": 3, "endpoint": ENDPOINT}
    product = drive("O05", *_pair()[:2], target_id="cell")
    receipt = cell_receipt("O05", provision, product, {"server_readback": False})
    assert receipt["status"] == "fail"
    assert receipt["cell"] == "aws-ecs/redis-on"
    assert receipt["runAttempt"] == "3"
    assert receipt["observations"]["serverReadback"] is False
    directory = Path(__file__).parent
    target = directory / "_receipt_probe"
    try:
        write_receipt(target, receipt)
        written = json.loads((target / "operate-batch.json").read_text(encoding="utf-8"))
        assert written["status"] == "fail"
        with pytest.raises(RuntimeError):
            write_receipt(target, {**receipt, "status": "pass"})
    finally:
        probe = target / "operate-batch.json"
        if probe.exists():
            probe.unlink()
        if target.exists():
            target.rmdir()


def test_public_cell_url_and_probe_score_fail_closed():
    assert require_public_cell("https://ab.cert.demo.honua.io") == "https://ab.cert.demo.honua.io"
    for url in ("http://ab.cert.demo.honua.io", "https://user:pw@ab.cert.demo.honua.io/",
                "https://ab.cert.demo.honua.io/extra", "https://ab.cert.demo.honua.io?x=1",
                "https://ab.cert.demo.honua.io#f", "https://127.0.0.1",
                "https://ab.cert.demo.honua.io:8443"):
        with pytest.raises(ValueError):
            require_public_cell(url)
    start, end, stop = 0.0, 1.0, 0.9
    samples = []
    next_id = 1
    for kind in ("read", "write", "writer"):
        offsets = (0.0, 0.8) if kind == "writer" else (0.0, 0.2, 0.4, 0.6, 0.8)
        for offset in offsets:
            sample = {"kind": kind, "ok": True, "started_monotonic": offset,
                      "finished_monotonic": offset + 0.01}
            if kind != "read":
                sample["object_id"] = next_id
                sample["marker"] = f"{kind}-{offset}"
                next_id += 1
            samples.append(sample)
    writes = [sample for sample in samples if sample["kind"] != "read"]
    body = json.dumps({"features": [{"attributes": {"objectid": sample["object_id"],
                                                    "marker": sample["marker"]}}
                                    for sample in writes]}).encode()
    ids = [sample["object_id"] for sample in writes]
    record = {"complete": True, "samples": samples, "started_monotonic": start,
              "ended_monotonic": end, "stop_requested_monotonic": stop,
              "interval": 0.1, "writer_interval": 2.0}
    readback = {"base_url": "https://ab.cert.demo.honua.io", "layer_path": "/layer",
                "started_monotonic": end, "object_id_field": "objectid", "marker_field": "marker",
                "responses": [{"method": "GET", "status": 200, "object_ids": ids,
                               "observed_monotonic": end, "sha256": hashlib.sha256(body).hexdigest(),
                               "body_b64": base64.b64encode(body).decode(),
                               "url": "https://ab.cert.demo.honua.io/layer/query?objectIds=" + ",".join(map(str, ids))}]}
    scored = score_public_probes("https://ab.cert.demo.honua.io", record, readback)
    assert scored["rpo_zero"] is True and scored["rto_seconds"] > 0
    with pytest.raises(ValueError):
        score_public_probes("https://ab.cert.demo.honua.io", record, {**readback, "base_url": "https://other.example"})


def test_observer_reads_task_digests_and_fails_closed(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    import boto3
    client = boto3.client("ecs", region_name="us-east-1")
    stubber = Stubber(client)
    digest = "sha256:" + "c" * 64
    stubber.add_response("describe_services", {
        "services": [{
            "serviceName": "server", "status": "ACTIVE", "runningCount": 2, "desiredCount": 2,
            "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/server:3",
        }],
        "failures": [],
    }, {"cluster": "cell", "services": ["server"]})
    stubber.add_response("list_tasks", {
        "taskArns": ["arn:aws:ecs:us-east-1:1:task/cell/task"],
    }, {"cluster": "cell", "serviceName": "server", "desiredStatus": "RUNNING"})
    stubber.add_response("describe_tasks", {
        "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:1:task/cell/task",
                   "containers": [{"name": "server", "imageDigest": digest}]}],
        "failures": [],
    }, {"cluster": "cell", "tasks": ["arn:aws:ecs:us-east-1:1:task/cell/task"]})
    stubber.add_response("describe_task_definition", {
        "taskDefinition": {"containerDefinitions": [{"name": "server", "image": "repo@" + digest}]},
    }, {"taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/server:3"})
    stubber.activate()
    observed = EcsObserver(client, "cell", "server").task_digests()
    assert observed["image_digest"] == digest
    assert observed["running"] == 2
    stubber.assert_no_pending_responses()
    stubber.add_response("describe_services", {"services": [], "failures": [{"reason": "MISSING"}]},
                         {"cluster": "cell", "services": ["server"]})
    with pytest.raises(ObservationMissing):
        EcsObserver(client, "cell", "server").task_digests()
    stubber.assert_no_pending_responses()
    with pytest.raises(ValueError):
        EcsObserver(client, "cell", "server", region="us-west-2")


class _Session:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.started = []
        self.queries = []
        self.stopped = False

    def start(self, sql: str) -> None:
        self.started.append(sql)

    def query(self, sql: str) -> str:
        self.queries.append(sql)
        return self.answers.pop(0)

    def stop(self) -> None:
        self.stopped = True


def test_fault_receipt_fires_only_after_the_waiter_is_rechecked():
    evidence = "d" * 32
    quiet = _Session(["4242", ""])
    fault = JournalFault(quiet, evidence)
    fault.arm()
    assert fault.wait_and_fire() == {"fired": False, "upgrade_started": False}
    assert quiet.stopped is True
    assert "schema_versions" in quiet.started[0]
    held = _Session(["4242", "99", "t"])
    fired = JournalFault(held, evidence)
    fired.arm()
    receipt = fired.wait_and_fire()
    assert receipt["fired"] is True and receipt["upgrade_started"] is True
    assert receipt["evidence_id"] == evidence
    assert "insert%schema_versions%" in held.queries[-1]
    assert "pg_terminate_backend" in held.queries[-1]
