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


def test_model_environment_is_an_allowlist_in_a_real_child(monkeypatch):
    import os
    import subprocess
    import sys
    secrets = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
               "AWS_WEB_IDENTITY_TOKEN_FILE", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
               "ACTIONS_ID_TOKEN_REQUEST_URL", "HONUA_OPS_PROPOSER_KEY",
               "HONUA_OPS_APPROVER_KEY", "UNEXPECTED_SECRET")
    for name in secrets:
        monkeypatch.setenv(name, "planted-secret")
    child = subprocess.check_output([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                                    env=model_environment(dict(os.environ)), text=True)
    observed = json.loads(child)
    assert not set(secrets) & observed.keys()
    assert set(observed) <= {"HOME", "PATH", "LANG", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "LC_CTYPE"}


def test_distinct_approver_and_unwired_drive_never_proposes_or_approves():
    with pytest.raises(ProductError):
        Approver(ENDPOINT, PROPOSER, PROPOSER, lambda request: request)
    client, approver, transport = _pair()
    for scenario in ("O01", "O02", "O05", "O08"):
        product = drive(scenario, client, approver, target_id="cell")
        assert not product["proposals"] and not product["executionIds"]
        assert grade_attempt(scenario, product, {}, None)["status"] == "fail"
    assert transport.calls == []


def test_approval_reports_unavailable_executor():
    _, approver, transport = _pair(approval_status=422)
    with pytest.raises(ProductError, match="operation-executor-unavailable"):
        approver.approve("proposal01")
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {APPROVER_KEY}"


def test_check_stays_closed_and_pins_do_not_match_the_cell_image():
    assert require_ready() is False
    assert forbidden_calls() == []
    assert {gap["id"] for gap in OPEN_PRODUCT_GAPS} == {
        "ecs-convergence-pr", "convergence-image", "restore-142", "restart-config", "controller-cert-cell"}
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
    assert receipt["why"] == "model recording proxy and isolated approval are not wired"
    monkeypatch.setenv("HONUA_OPS_PROPOSER_KEY", "proposer-secret-value")
    monkeypatch.setenv("HONUA_OPS_APPROVER_KEY", "approver-secret-value")
    assert main(["--cell", "--scenario", "O02", "--cell-dir", str(directory)]) == 1
    text = (directory / "operate-batch.json").read_text(encoding="utf-8")
    assert "proposer-secret-value" not in text and "approver-secret-value" not in text
    assert json.loads(text)["why"] == "model recording proxy and isolated approval are not wired"
    assert json.loads(text)["status"] == "fail"


def test_cell_receipt_fails_closed_and_binds_the_run(tmp_path):
    provision = {"cell": "aws-ecs/redis-on", "runId": "99", "runAttempt": 3, "endpoint": ENDPOINT}
    product = drive("O05", *_pair()[:2], target_id="cell")
    receipt = cell_receipt("O05", provision, product, {"server_readback": False})
    assert receipt["status"] == "fail"
    assert receipt["cell"] == "aws-ecs/redis-on"
    assert receipt["runAttempt"] == "3"
    assert receipt["observations"]["serverReadback"] is False
    write_receipt(tmp_path, receipt)
    assert json.loads((tmp_path / "operate-batch.json").read_text())["status"] == "fail"
    with pytest.raises(RuntimeError):
        write_receipt(tmp_path, {**receipt, "status": "pass"})


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


def test_fault_targets_waiter_and_requires_controller_evidence(monkeypatch):
    evidence = "d" * 32
    state = {"evidence_id": evidence, "backend_pid": 99, "migration_failed": True, "journal_absent": True}
    held = _Session(["4242", "99", "t", "0"])
    fault = JournalFault(held, evidence, 99, lambda: state)
    fault.arm()
    receipt = fault.wait_and_fire()
    assert receipt["fired"] and receipt["interrupted"]
    sql = held.queries[2]
    assert "pg_terminate_backend(a.pid)" in sql and "a.pid=99" in sql
    assert "4242 = ANY(pg_blocking_pids(a.pid))" in sql
    assert "h.pid=4242" in sql and "a.datname=current_database()" in sql
    assert held.stopped

    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr("ecs_fault.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("ecs_fault.time.sleep", lambda _: None)
    no_proof = _Session(["4242", "99", "t", "0"])
    fault = JournalFault(no_proof, evidence, 99, lambda: {**state, "migration_failed": False}, hold_seconds=1)
    fault.arm()
    assert fault.wait_and_fire()["fired"] is False
    assert no_proof.stopped


def test_controller_migration_read_requires_matching_script_failure_and_absent_journal():
    from ecs_observe import MigrationStateReader
    class Logs:
        def __init__(self, messages): self.messages = messages
        def get_log_events(self, **kwargs):
            assert kwargs['logStreamName'] == 'candidate-task'
            return {'events': [] if 'nextToken' in kwargs else [{'message': message} for message in self.messages], 'nextForwardToken': 'end'}
    def read(journal, messages):
        return MigrationStateReader(_Session([journal]), Logs(messages), log_group='cell',
            log_stream='candidate-task', script='142_test.sql', evidence_id='d' * 32,
            backend_pid=99, start_time_ms=1000)()
    good = ["Executing Database Server script '142_test.sql'", 'migration failed']
    assert read('0', good)['migration_failed'] is True
    assert read('0', good)['journal_absent'] is True
    assert read('1', good)['journal_absent'] is False
    assert read('0', ["Executing Database Server script '143_other.sql'", 'migration failed'])['migration_failed'] is False
    assert read('0', [good[0], 'migration completed'])['migration_failed'] is False
    with pytest.raises(ValueError):
        MigrationStateReader(_Session([]), Logs([]), log_group='cell', log_stream='candidate-task',
            script="x'; DROP TABLE x;--.sql", evidence_id='d' * 32, backend_pid=99, start_time_ms=0)


def test_recording_proxy_captures_only_successful_actual_proposal_responses(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
    from agent_cli_sessions import RecordingProxy
    from ecs_recording import ProposalCapture
    result = {'status': 'ok', 'result': {'structuredContent': {'proposalId': 'proposal01'}}}
    capture = ProposalCapture(lambda kind, action: result)
    view = {'tools': [{'name': 'honua_propose_platform_release_convergence', 'inputSchema': {'type': 'object'}}]}
    with RecordingProxy(view, capture, tmp_path, 1, 5, server_name='honua') as proxy:
        proxy.rpc('tools/call', {'name': 'honua_propose_platform_release_convergence', 'arguments': {}})
        assert proxy.actions[0]['status'] == 'pass'
    assert capture.records == [{'tool': 'honua_propose_platform_release_convergence', 'proposalId': 'proposal01'}]
    capture('tool_call', {'tool': 'honua_ops_health'})
    assert len(capture.records) == 1
    failed = ProposalCapture(lambda kind, action: {'status': 'fail', 'result': result['result']})
    failed('tool_call', {'tool': 'honua_propose_finding'})
    assert failed.records == []
