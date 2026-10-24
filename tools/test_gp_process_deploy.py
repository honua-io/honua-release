"""Behavioral controls for live GP release activation and output qualification."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gp_process_deploy as deploy
import gp_process_release as gate

FIXTURES = Path(__file__).parent / "fixtures/gp-process-release"
ARN = "arn:aws:batch:us-west-2:123456789012:job-definition/honua-gp-buffer-features:7"


@pytest.fixture
def docs():
    lock = gate.load_json(FIXTURES / "lock-batch-only.json")
    pin = gate.load_json(FIXTURES / "pin-dev-first.json")
    pin["release"]["lockDigest"] = gate.digest(lock)
    pin["expectedBindingVersion"] = 0
    state = gate.load_json(FIXTURES / "environment-dev.json")
    state["deploymentTooling"] = {"kind": "aws-batch", "releases": {
        gate.digest(lock): {"jobDefinitionArn": ARN, "smokeFixture": "smoke.json"}}}
    index = {"artifacts": [a["sha256"] for a in lock["artifacts"]],
             "images": [lock["runtimeImage"]["digest"], lock["targets"][0]["workerImageDigest"]]}
    live = {"process": lock["process"]["name"], "bindingVersion": 0, "lockDigest": None,
            "serverContractVersion": 3, "supportedTargets": ["aws-batch"]}
    return lock, pin, state, index, live


class FakeServer:
    def __init__(self, lock, live):
        self.lock, self.live = lock, copy.deepcopy(live)
        self.requests = []
        self.lose_activation_ack = False
        self.refuse_activation = False

    def request(self, method, path, document=None, **kwargs):
        self.requests.append((method, path, document))
        if method == "POST" and path == deploy.BASE:
            return {"lockDigest": gate.digest(self.lock), "process": self.lock["process"]["name"]}
        if path.startswith(deploy.BASE + "/sha256"):
            return self.lock
        if method == "POST" and path.endswith("/activate"):
            if self.refuse_activation:
                raise deploy.DeploymentError("conflict")
            assert document["expectedBindingVersion"] == self.live["bindingVersion"]
            self.live.update(lockDigest=document["lockDigest"], bindingVersion=self.live["bindingVersion"] + 1,
                             targets=document["targets"])
            if self.lose_activation_ack:
                raise deploy.DeploymentError("lost reply")
        return self.live


def test_live_preflight_does_not_trust_committed_version(docs):
    lock, pin, state, index, live = docs
    live["bindingVersion"] = 9
    decision, _ = deploy.live_preflight(FakeServer(lock, live), lock, pin, state, index, "deploy")
    assert not decision["admissible"]
    assert "STALE_BINDING_VERSION" in decision["refusalReasons"]


def test_rollback_reads_current_version_but_requires_exact_predecessor(docs):
    lock, pin, state, index, live = docs
    pin["previous"] = {"lockDigest": gate.digest(lock)}
    pin["release"]["lockDigest"] = "sha256:" + "a" * 64
    live.update(lockDigest=pin["release"]["lockDigest"], previousLockDigest=gate.digest(lock), bindingVersion=8)
    decision, _ = deploy.live_preflight(FakeServer(lock, live), lock, pin, state, index, "rollback")
    assert decision["admissible"]
    live["previousLockDigest"] = "sha256:" + "b" * 64
    refused, _ = deploy.live_preflight(FakeServer(lock, live), lock, pin, state, index, "rollback")
    assert not refused["admissible"]
    assert "ROLLBACK_TARGET_MISMATCH" in refused["refusalReasons"]


@pytest.mark.parametrize("lost_ack", [False, True])
def test_activation_reads_back_exact_version_and_recovers_lost_response(docs, tmp_path, lost_ack):
    lock, _, _, _, live = docs
    server = FakeServer(lock, live)
    server.lose_activation_ack = lost_ack
    result = deploy.activate(server, lock, live, {"kind": "aws-batch", "jobDefinitionArn": ARN}, "deploy", tmp_path / "binding.json")
    assert result["bindingVersion"] == 1
    assert json.loads((tmp_path / "binding.json").read_text())["status"] == "activated"
    assert len([r for r in server.requests if r[1].endswith("/activate")]) == 1


def test_refused_activation_never_claims_unchanged_after_ambiguous_post(docs, tmp_path):
    lock, _, _, _, live = docs
    server = FakeServer(lock, live)
    server.refuse_activation = True
    with pytest.raises(deploy.DeploymentError, match="refused, raced"):
        deploy.activate(server, lock, live, {"kind": "aws-batch", "jobDefinitionArn": ARN}, "deploy", tmp_path / "binding.json")
    assert json.loads((tmp_path / "binding.json").read_text())["status"] == "unknown"



@pytest.mark.parametrize("status", [400, 401, 403, 409, 429])
def test_explicit_native_refusal_cannot_be_relabelled_after_matching_concurrent_activation(docs, tmp_path, status):
    from io import BytesIO
    from urllib.error import HTTPError
    lock, _, _, _, live = docs
    transport = deploy.Client("https://server.example", "private-test-key")
    class RefusingTransport:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status, "refused", {}, BytesIO(b"{}"))
    transport.opener = RefusingTransport()
    class ConcurrentServer(FakeServer):
        def request(self, method, path, document=None, **kwargs):
            if method == "POST" and path.endswith("/activate"):
                self.requests.append((method, path, document))
                # A separate accepted request changes the matching live version;
                # this request still received an explicit native refusal.
                self.live.update(lockDigest=document["lockDigest"], bindingVersion=self.live["bindingVersion"]+1,
                                 targets=document["targets"])
                return transport.request(method, path, document)
            return super().request(method, path, document, **kwargs)
    server = ConcurrentServer(lock, live)
    with pytest.raises(deploy.DeploymentError, match=f"HTTP {status}"):
        deploy.activate(server, lock, live, {"kind":"aws-batch", "jobDefinitionArn":ARN}, "deploy", tmp_path/"binding.json")
    assert json.loads((tmp_path/"binding.json").read_text())["status"] == "unknown"
    assert server.requests[-1][0] == "POST" and server.requests[-1][1].endswith("/activate")



@pytest.mark.parametrize("status", [408, 500, 504])
def test_ambiguous_http_failure_still_observes_exact_accepted_version(docs, tmp_path, status):
    from io import BytesIO
    from urllib.error import HTTPError
    lock, _, _, _, live = docs
    transport = deploy.Client("https://server.example", "private-test-key")
    class AmbiguousTransport:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, status, "uncertain", {}, BytesIO(b"{}"))
    transport.opener = AmbiguousTransport()
    class AcceptedServer(FakeServer):
        def request(self, method, path, document=None, **kwargs):
            if method == "POST" and path.endswith("/activate"):
                super().request(method, path, document, **kwargs)
                return transport.request(method, path, document)
            return super().request(method, path, document, **kwargs)
    result = deploy.activate(AcceptedServer(lock, live), lock, live,
        {"kind":"aws-batch", "jobDefinitionArn":ARN}, "deploy", tmp_path/"binding.json")
    assert result["bindingVersion"] == 1
    assert json.loads((tmp_path/"binding.json").read_text())["status"] == "activated"


def test_verify_existing_iac_revision_does_not_register_or_discard_provider_settings(docs):
    lock, _, state, _, _ = docs
    registration = deploy.registration_for(lock, state)
    image = lock["runtimeImage"]["reference"].split("@")[0] + "@" + lock["targets"][0]["workerImageDigest"]
    definition = {"jobDefinitionArn": ARN, "jobDefinitionName": "honua-gp-buffer-features", "status": "ACTIVE", "platformCapabilities": ["FARGATE"],
                  "containerProperties": {"image": image, "executionRoleArn": "provider-owned", "resourceRequirements": [{"type": "VCPU", "value": "2"}]}}
    def aws(args):
        assert args == ["batch", "describe-job-definitions", "--job-definitions", ARN]
        return {"jobDefinitions": [definition]}
    assert deploy.verify_definition(lock, registration, aws=aws)["jobDefinitionArn"] == ARN
    definition["containerProperties"]["image"] = "wrong:latest"
    with pytest.raises(deploy.DeploymentError, match="does not match"):
        deploy.verify_definition(lock, registration, aws=aws)


@pytest.mark.parametrize("wrong", ["release", "source", "version", "output", "missing", "failed", "identity"])
def test_smoke_requires_real_terminal_job_candidate_and_expected_output_bytes(docs, wrong):
    lock, _, _, _, _ = docs
    expected_hash = "sha256:" + hashlib.sha256(b"expected result").hexdigest()
    fixture = {"params": {"distance": 5}, "declaredScope": [], "expectedArtifacts": [
        {"label": "summary.json", "sha256": expected_hash, "maxBytes": 1024}]}
    class Server:
        def request(self, method, path, body=None, **kwargs):
            if method == "POST":
                assert body == {"params": fixture["params"], "declaredScope": []}
                assert kwargs["idempotency_key"] == "smoke-1"
                return {"jobId": "job-1"}
            if path.endswith("/diagnostics"):
                release = {"lockDigest": gate.digest(lock), "sourceRef": lock["source"]["sha"],
                           "processRelease": lock["process"]["name"], "bindingVersion": "4"}
                field = {"release": "lockDigest", "source": "sourceRef", "version": "bindingVersion"}.get(wrong)
                if field:
                    release[field] = "other"
                return {"jobId": "job-1", "release": release}
            if "artifacts?" in path:
                return {"jobId": "job-1", "items": [] if wrong == "missing" else [
                    {"label": "summary.json", "availability": "available"}]}
            return {"jobId": "other" if wrong == "identity" else "job-1", "status": "Failed" if wrong == "failed" else "Completed"}
    with pytest.raises(deploy.DeploymentError):
        deploy.smoke(Server(), lock, {"bindingVersion": 4}, fixture, "smoke-1",
                     read_artifact=lambda c, j, a, m: "wrong" if wrong == "output" else expected_hash)
    if wrong == "output":
        assert deploy.smoke(Server(), lock, {"bindingVersion": 4}, fixture, "smoke-1",
                            read_artifact=lambda c, j, a, m: expected_hash)["status"] == "pass"


def test_real_http_transport_uses_auth_and_refuses_redirect_without_forwarding():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("X-API-Key")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-receive-token")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = deploy.Client(f"http://127.0.0.1:{server.server_port}", "public-test-key", allow_loopback_http=True)
        assert client.request("GET", "/ok") == {"ok": True}
        with pytest.raises(deploy.DeploymentError, match="redirected"):
            client.request("GET", "/redirect")
        assert requests == [("/ok", "public-test-key"), ("/redirect", "public-test-key")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("url", ["http://example.com", "https://u:p@example.com", "https://example.com?token=x", "file:///tmp/x"])
def test_client_refuses_unsafe_base_urls(url):
    with pytest.raises(deploy.DeploymentError):
        deploy.Client(url, "public-test-key")


@pytest.mark.parametrize("mutation", ["role", "ec2", "credential", "secret", "envfile"])
def test_provider_verification_refuses_ambient_credential_paths(docs, mutation):
    lock, _, state, _, _ = docs
    image = lock["runtimeImage"]["reference"].split("@")[0] + "@" + lock["targets"][0]["workerImageDigest"]
    definition = {"jobDefinitionArn": ARN, "jobDefinitionName": "honua-gp-buffer-features", "status": "ACTIVE",
                  "platformCapabilities": ["FARGATE"], "containerProperties": {"image": image}}
    container = definition["containerProperties"]
    if mutation == "role":
        container["jobRoleArn"] = "shared-role"
    elif mutation == "ec2":
        definition["platformCapabilities"] = ["EC2"]
    elif mutation == "credential":
        container["environment"] = [{"name": "AWS_CONTAINER_CREDENTIALS_FULL_URI", "value": "fake"}]
    elif mutation == "secret":
        container["secrets"] = [{"name": "AWS_SECRET_ACCESS_KEY", "valueFrom": "fake"}]
    else:
        container["environmentFiles"] = [{"value": "fake"}]
    with pytest.raises(deploy.DeploymentError):
        deploy.verify_definition(lock, deploy.registration_for(lock, state), aws=lambda args: {"jobDefinitions": [definition]})


@pytest.mark.parametrize("link", ["https://other.example/output", "//other.example/output", "/other/path",
                                   "/api/geoprocessing/jobs/other/custom-artifacts/result.json",
                                   "/api/geoprocessing/jobs/job-1/custom-artifacts/../secret"])
def test_artifact_reads_never_forward_admin_credentials_to_other_origins_or_jobs(link):
    client = deploy.Client("https://server.example", "public-test-key")
    with pytest.raises(deploy.DeploymentError):
        deploy.artifact_digest(client, "job-1", {"providerLink": link}, 1024)


@pytest.mark.parametrize("binding,smoke,outcome,code", [
    ("activated", "pass", "activated", 0),
    ("activated", "fail", "failed", 1),
    ("activated", "skipped", "failed", 1),
    ("unknown", "skipped", "failed", 1),
    ("unchanged", "pass", "failed", 1),
    ("reference-only", "skipped", "reference-only", 0),
])
def test_executed_workflow_receipt_preserves_actual_binding_and_smoke(docs, tmp_path, binding, smoke, outcome, code):
    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/gp-deploy-process.yml").read_text())
    step = next(s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Record the deployment receipt")
    (tmp_path / "tools").symlink_to(repo / "tools", target_is_directory=True)
    lock, pin, state, index, live = docs
    decision, _ = deploy.live_preflight(FakeServer(lock, live), lock, pin, state, index, "deploy")
    deploy.write(tmp_path / "out/deploy/decision.json", decision)
    deploy.write(tmp_path / "out/deploy/binding.json", {"status": binding, "detail": "fixture"})
    deploy.write(tmp_path / "out/deploy/smoke.json", {"status": smoke, "evidence": "fixture"})
    env = dict(os.environ, OPERATION="deploy", PROCESS=lock["process"]["name"], ENVIRONMENT="dev",
               STARTED_AT="2026-10-16T00:00:00Z", TOOLING="reference" if binding == "reference-only" else "aws-batch",
               ACTOR="test", GITHUB_REPOSITORY="honua-io/honua-release", GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1",
               GITHUB_OUTPUT=str(tmp_path / "outputs"), GITHUB_STEP_SUMMARY=str(tmp_path / "summary"))
    result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == code, result.stderr
    receipt = json.loads((tmp_path / "out/deploy/deployment-receipt.json").read_text())
    assert receipt["outcome"] == outcome
    assert receipt["binding"]["status"] == binding
    assert receipt["smoke"]["status"] == smoke
    assert receipt["scheduleReconciliation"] != "reconciled"


def test_reference_workflow_executes_its_index_script_without_claiming_resolution(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/gp-deploy-process.yml").read_text())
    step = next(s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Build the artifact availability index")
    env = dict(os.environ, TOOLING="reference", LOCK=str(FIXTURES / "lock-v1.json"))
    result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    index = json.loads((tmp_path / "out/deploy/artifact-index.json").read_text())
    assert index["artifacts"] and index["images"]
    assert "nothing was resolved" in index["source"]


def test_optional_batch_definition_name_matches_server_contract(docs):
    lock = copy.deepcopy(docs[0])
    del lock["targets"][0]["jobDefinitionName"]
    gate.validate_lock(lock)


def test_live_lineage_refusal_keeps_a_structured_decision(docs):
    lock, pin, state, index, live = docs
    live["lockDigest"] = "sha256:" + "f" * 64
    decision, _ = deploy.live_preflight(FakeServer(lock, live), lock, pin, state, index, "deploy")
    assert not decision["admissible"]
    assert "LIVE_PREDECESSOR_MISMATCH" in decision["refusalReasons"]
    assert decision["checks"]["bindingVersion"]["status"] == "fail"


def test_main_writes_refused_live_decision_when_live_configuration_refuses(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    pin_path = FIXTURES / "pin-dev-v2.json"
    lock_path = FIXTURES / "lock-v2.json"
    state_path = FIXTURES / "environment-dev.json"
    monkeypatch.chdir(repo)
    monkeypatch.delenv("HONUA_GP_ADMIN_API_KEY", raising=False)
    code = deploy.main(["--lock", str(lock_path), "--pin", str(pin_path), "--environment", str(state_path),
                        "--artifact-index", str(FIXTURES / "artifact-index.json"), "--out", str(tmp_path)])
    assert code == 1
    decision = json.loads((tmp_path / "decision.json").read_text())
    assert not decision["admissible"] and "LIVE_PREFLIGHT_NOT_COMPLETED" in decision["refusalReasons"]
    assert json.loads((tmp_path / "binding.json").read_text())["status"] == "unchanged"


def test_executed_rollback_plan_checks_identity_without_stale_committed_version(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/gp-rollback-process.yml").read_text())
    step = next(s for s in workflow["jobs"]["plan"]["steps"] if s.get("id") == "target")
    (tmp_path / "tools").symlink_to(repo / "tools", target_is_directory=True)
    pin = gate.load_json(FIXTURES / "pin-dev-v2.json")
    pin["expectedBindingVersion"] = 1
    state = gate.load_json(FIXTURES / "environment-dev.json")
    state["bindingVersion"] = 0
    (tmp_path / "release").mkdir()
    (tmp_path / "release/gp-process-locks").symlink_to(repo / "release/gp-process-locks", target_is_directory=True)
    deploy.write(tmp_path / "release/gp-environments/dev/buffer-features.pin.json", pin)
    deploy.write(tmp_path / "release/gp-environments/dev/environment.json", state)
    env = dict(os.environ, PROCESS="buffer-features", ENVIRONMENT="dev", TARGET_LOCK_PATH="",
               GITHUB_OUTPUT=str(tmp_path / "outputs"), GITHUB_STEP_SUMMARY=str(tmp_path / "summary"))
    result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    plan = json.loads((tmp_path / "out/rollback/plan-decision.json").read_text())
    assert plan["livePreflightRequired"] is True
    assert plan["lockDigest"] == pin["previous"]["lockDigest"]
    # An arbitrary rollback target must still fail before calling deployment.
    env["TARGET_LOCK_PATH"] = str(FIXTURES / "lock-v2.json")
    refused = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and "ROLLBACK_TARGET_MISMATCH" in refused.stderr
