"""Producer refusal controls; these are never AWS qualification evidence."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gp_native_cancellation import AwsObserver, ProofError, efs_mount, observation, run_cell

NOW = 1792350000
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
STORE = {"reference": "qualification", "configurationDigest": "a" * 64, "attestationSha256": "b" * 64}
EMPTY = {"state": "absent", "files": [], "bytes": 0}
PROCESS = {"workerId": "worker-1", "worker": {"pid": 100, "parentPid": 1, "startTicks": 10},
           "child": {"pid": 200, "parentPid": 100, "startTicks": 20}, "bootId": "12345678-1234-1234-1234-123456789abc", "pidNamespace": 1,
           "barrierSha256": "c" * 64, "scratchRelative": "job-1"}


def record(phase="before"):
    return {"schema": "honua.gp-native-observation/v1", "status": "observed", "qualification": False,
            "jobId": "job-1", "nonce": "d" * 32, "phase": phase, "observedAtUnixNs": NOW * 10**9,
            "policySha256": "e" * 64, "store": STORE.copy(), "outputInventory": EMPTY.copy(),
            "scratchInventory": {"state": "present", "files": [], "bytes": 0, "device": 1, "inode": 2}, "barrier": {"operationId": "job-1", "workerId": "worker-1",
            "childProcessId": 200, "workerProcessId": 100, "readyAt": STAMP, "sha256": "c" * 64},
            "process": copy.deepcopy(PROCESS)}


def validate(value):
    return observation(value, job="job-1", nonce="d" * 32, phase="before", start=NOW * 10**9,
                       end=NOW * 10**9 + 10**9, policy_sha="e" * 64, store=STORE)


@pytest.mark.parametrize("field,value", [("jobId", "wrong"), ("nonce", "0" * 32), ("phase", "after"),
    ("status", "blocked"), ("qualification", True), ("observedAtUnixNs", (NOW - 300) * 10**9),
    ("policySha256", "0" * 64), ("store", {**STORE, "reference": "other"}), ("outputInventory", {}),
    ("outputInventory", {"state": "absent", "files": [], "bytes": True}),
    ("outputInventory", {"state": "present", "files": [{"path": "orphan"}], "bytes": 4}),
    ("barrier", {})])
def test_observation_missing_stale_partial_wrong_identity_and_orphans_fail(field, value):
    candidate = record()
    candidate[field] = value
    with pytest.raises((ProofError, KeyError)):
        validate(candidate)


def test_observation_accepts_complete_exact_fresh_record_only():
    assert validate(record())["jobId"] == "job-1"


class ObserverControl:
    def __init__(self, mode="positive"):
        self.mode, self.phases, self.authenticated = mode, [], False

    def authenticate(self):
        self.authenticated = True

    def preflight(self):
        assert self.authenticated
        if self.mode == "preflight":
            raise ProofError("wrong mount")
        return {"worker": "provider-task", "observer": "read-only-observer"}

    def observe(self, job, phase, expected=None):
        self.phases.append(phase)
        value = record(phase)
        if phase in ("after", "store"):
            if self.mode == "live-child":
                raise ProofError("native child still present")
            value["process"] = {**PROCESS, "childAbsent": self.mode != "partial-after"}
        if self.mode == "wrong-claim":
            value["barrier"]["workerId"] = "other"
        if self.mode == "scratch" and phase == "after":
            value["scratchInventory"] = {"state": "present", "files": [{"path": "orphan"}], "bytes": 4}
        return value

    def bind_batch(self, job, provider):
        if self.mode == "wrong-task":
            raise ProofError("wrong task")
        return self.preflight()

    def batch(self, job, provider, stopped=False):
        if stopped and self.mode == "batch-running":
            raise ProofError("task not stopped")
        return {"providerJobId": provider, "attempt": 1, "status": "FAILED" if stopped else "RUNNING"}


class ApiControl:
    def __init__(self, mode="positive"):
        self.mode, self.cancelled, self.results = mode, False, 0

    def get(self, path):
        if path.startswith("/api/"):
            return {"jobId": "wrong" if self.mode == "wrong-job" else "job-1", "createdAt": STAMP,
                    "status": "Succeeded" if self.mode == "succeeded" else "Cancelled" if self.cancelled else "Running",
                    "attemptCount": 2 if self.mode == "retry" else 1, "claimedBy": "worker-1", "providerOperationId": "batch-job"}
        return {"jobID": "job-1", "status": "successful" if self.mode == "ogc-success" else "dismissed"}

    def request(self, method, path):
        self.results += 1
        return (200, {"outputs": {"output": "late"}}) if self.mode == "readable" or self.mode == "late-result" and self.results > 1 else (410, {})


class McpControl:
    def __init__(self, api, mode="positive"):
        self.api, self.mode, self.calls = api, mode, []

    def call(self, mode, argument):
        self.calls.append(mode)
        if mode == "submit":
            return {"jobId": "job-1", "status": "Queued"}
        self.api.cancelled = True
        ack = {"jobId": "job-1", "status": "cancellation_requested", "cancellationRequested": self.mode != "false-ack"}
        return {"jobId": "job-1", "responses": [ack, ack]}


def run_control(cell="aws-ecs/redis-on", mode="positive"):
    config = {"cell": cell, "source": "f" * 40, "workerImage": "worker@sha256:" + "a" * 64}
    observer, api = ObserverControl(mode), ApiControl(mode)
    mcp = McpControl(api, mode)
    result = run_cell(config, observer, api, mcp, {"plan": {"planId": "test"}}, sleep=lambda _: None, clock=lambda: NOW, polls=2)
    return result, observer, mcp


@pytest.mark.parametrize("cell", ["aws-ecs/redis-on", "aws-serverless/redis-on"])
def test_both_required_cells_submit_cancel_and_observe_late_without_qualification_claim(cell):
    result, observer, mcp = run_control(cell)
    assert result["status"] == "pass" and result["candidateQualified"] is False
    assert mcp.calls == ["submit", "cancel"]
    assert observer.phases == (["before", "after", "after"] if cell.startswith("aws-ecs") else ["started", "store", "store"])
    if cell.startswith("aws-serverless"):
        assert result["batchBefore"]["status"] == "RUNNING"
        assert result["batchAfter"]["status"] == "FAILED"


@pytest.mark.parametrize("mode", ["preflight", "live-child", "partial-after", "wrong-claim", "scratch", "wrong-job", "succeeded", "retry", "false-ack", "ogc-success", "readable", "late-result"])
def test_ecs_faults_never_get_pass_receipt(mode):
    with pytest.raises(ProofError):
        run_control(mode=mode)


@pytest.mark.parametrize("mode", ["wrong-task", "batch-running", "readable", "late-result"])
def test_batch_entry_or_stopped_task_and_late_result_are_required(mode):
    with pytest.raises(ProofError):
        run_control("aws-serverless/redis-on", mode)


def mount_fixture():
    definition = {"volumes": [{"name": "output", "efsVolumeConfiguration": {"fileSystemId": "fs-1", "rootDirectory": "/",
                    "transitEncryption": "ENABLED", "authorizationConfig": {"accessPointId": "fsap-1"}}}]}
    container = {"mountPoints": [{"containerPath": "/output", "sourceVolume": "output", "readOnly": True}]}
    return definition, container


@pytest.mark.parametrize("fault", ["writable", "missing", "unattested", "wrong-source", "ambiguous"])
def test_mount_observation_refuses_incomplete_or_writable_efs(fault):
    definition, container = mount_fixture()
    if fault == "writable": container["mountPoints"][0]["readOnly"] = False
    if fault == "missing": container["mountPoints"] = []
    if fault == "unattested": definition["volumes"][0]["efsVolumeConfiguration"]["authorizationConfig"] = {}
    if fault == "wrong-source": container["mountPoints"][0]["sourceVolume"] = "fresh-empty-volume"
    if fault == "ambiguous": container["mountPoints"] *= 2
    with pytest.raises(ProofError):
        efs_mount(definition, container, "/output", readonly=True)


def test_efs_identity_includes_access_point_and_root_directory():
    definition, container = mount_fixture()
    assert efs_mount(definition, container, "/output", readonly=True) == {
        "fileSystemId": "fs-1", "accessPointId": "fsap-1", "rootDirectory": "/"}


def test_missing_live_flag_writes_blocked_receipt_and_nonzero(tmp_path):
    output = tmp_path / "receipt.json"
    result = subprocess.run([sys.executable, "tools/gp_native_cancellation.py", "--config", "absent", "--plan", "absent",
                             "--output", str(output)], text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert json.loads(output.read_text()) == {"schema": "honua.aws-native-gp-cancellation/v1", "status": "blocked",
        "candidateQualified": False, "reason": "native-cancellation-proof-incomplete"}


def observer_fixture():
    config = {"cell": "aws-ecs/redis-on", "source": "f" * 40, "region": "us-west-2", "cluster": "cluster",
              "workerImage": "worker@sha256:" + "a" * 64, "workerTask": "task", "workerContainer": "worker",
              "observerTask": "task", "observerContainer": "diagnostic", "policySha256": "e" * 64,
              "store": STORE.copy(), "barrierRoot": "/barriers", "scratchRoot": "/scratch",
              "outputRoot": "/outputs", "keyPrefix": "gp/outputs"}
    worker = {"name": "worker", "image": config["workerImage"], "readonlyRootFilesystem": True,
              "environment": [{"name": k, "value": v} for k, v in {
                  "HONUA_GP_QUALIFICATION_BARRIER_ROOT": "/barriers", "HONUA_GP_QUALIFICATION_PAUSE_AT": "native-process-started",
                  "Geoprocessing__OutputStaging__Provider": "local", "Geoprocessing__OutputStaging__LocalRootPath": "/outputs",
                  "Geoprocessing__OutputStaging__KeyPrefix": "gp/outputs", "Geoprocessing__OutputStaging__StoreReference": "qualification",
                  "Geoprocessing__OutputStaging__PersistenceClass": "shared-persistent", "GdalWorker__ScratchRoot": "/scratch"}.items()],
              "mountPoints": [{"containerPath": path, "sourceVolume": name, "readOnly": False}
                              for path, name in (("/outputs", "output"), ("/barriers", "barrier"), ("/scratch", "scratch"))]}
    diagnostic = {"name": "diagnostic", "image": config["workerImage"], "entryPoint": ["/usr/bin/python3"],
                  "command": ["-I", "-B", "-c", "import time; time.sleep(86400)"], "readonlyRootFilesystem": False,
                  "mountPoints": [{**row, "readOnly": True} for row in worker["mountPoints"]] + [
                      {"containerPath": "/run/honua/observer-policy", "sourceVolume": "policy", "readOnly": True}]}
    volumes = [{"name": name, "efsVolumeConfiguration": {"fileSystemId": "fs-1", "transitEncryption": "ENABLED",
                "authorizationConfig": {"accessPointId": "fsap-" + name}, "rootDirectory": "/"}} for name in ("output", "barrier")]
    definition = {"taskDefinitionArn": "definition", "pidMode": "task", "containerDefinitions": [worker, diagnostic],
                  "volumes": volumes + [{"name": "scratch"}, {"name": "policy"}]}
    task = {"taskArn": "task", "clusterArn": "cluster", "taskDefinitionArn": "definition", "lastStatus": "RUNNING",
            "enableExecuteCommand": True, "containers": [{"name": name, "containerArn": name + "-arn", "runtimeId": name + "-runtime",
            "imageDigest": "sha256:" + "a" * 64, "lastStatus": "RUNNING", "managedAgents": [
                {"name": "ExecuteCommandAgent", "lastStatus": "RUNNING"}]} for name in ("worker", "diagnostic")]}
    observer = AwsObserver(config)
    observer.authenticated, observer.runtime_digests, observer.provenance = True, {"amd64": "sha256:" + "a" * 64}, {}
    observer.aws = lambda service, method, *args: {"tasks": [task]} if method == "describe-tasks" else {"taskDefinition": definition}
    return observer, task, definition, worker, diagnostic


def test_actual_task_definition_admission_preserves_worker_readonly_root():
    observer, task, definition, worker, diagnostic = observer_fixture()
    admitted = observer.preflight()
    assert admitted["worker"]["runtimeId"] == "worker-runtime"
    assert admitted["observer"]["runtimeId"] == "diagnostic-runtime"
    assert admitted["volumes"]["/outputs"]["accessPointId"] == "fsap-output"


@pytest.mark.parametrize("fault", ["agent", "worker-root", "pid-mode", "writable-policy", "shared-policy", "writable-output",
                                  "changed-image", "changed-runtime-image", "changed-task", "wrong-output-root", "missing-selector", "override-root"])
def test_actual_provider_or_mount_contradictions_prevent_admission(fault):
    observer, task, definition, worker, diagnostic = observer_fixture()
    if fault == "agent": task["containers"][1]["managedAgents"] = []
    if fault == "worker-root": worker["readonlyRootFilesystem"] = False
    if fault == "pid-mode": definition["pidMode"] = "host"
    if fault == "writable-policy": diagnostic["mountPoints"][-1]["readOnly"] = False
    if fault == "shared-policy": worker["mountPoints"].append({"containerPath": "/policy", "sourceVolume": "policy"})
    if fault == "writable-output": diagnostic["mountPoints"][0]["readOnly"] = False
    if fault == "changed-image": worker["image"] = "worker:latest"
    if fault == "changed-runtime-image": task["containers"][0]["imageDigest"] = "sha256:" + "0" * 64
    if fault == "changed-task": task["taskArn"] = "replacement-task"
    if fault == "wrong-output-root": worker["environment"][3]["value"] = "/fresh-empty"
    if fault == "missing-selector": worker["environment"][1]["value"] = "claimed"
    if fault == "override-root": task["overrides"] = {"containerOverrides": [{"name": "worker", "environment": [
        {"name": "Geoprocessing:OutputStaging:LocalRootPath", "value": "/fresh-empty"}]}]}
    with pytest.raises(ProofError):
        observer.preflight()


@pytest.mark.parametrize("fault", ["positive", "wrong-job", "wrong-nonce", "duplicate", "stale", "wrong-scope", "blocked"])
def test_real_exec_transport_parses_one_exact_nonce_bound_scoped_response(fault):
    import shlex
    import hashlib
    observer, *_ = observer_fixture()
    observer.preflight()
    observer.clock = lambda: NOW * 10**9
    def run(argv, **kwargs):
        assert argv[:3] == ["aws", "ecs", "execute-command"]
        assert argv[argv.index("--task") + 1] == "task"
        assert argv[argv.index("--container") + 1] == "diagnostic"
        command = shlex.split(argv[-1])
        assert command[:4] == ["/usr/bin/python3", "-I", "-B", "/app/gp_native_observer.py"]
        nonce = command[command.index("--nonce") + 1]
        value = record()
        value["nonce"] = nonce
        scope = {"role": "worker", "barrierRoot": "/barriers", "scratchRoot": "/scratch", "outputRoot": "/outputs", "keyPrefix": "gp/outputs"}
        value["scopeSha256"] = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fault == "wrong-job": value["jobId"] = "other"
        if fault == "wrong-nonce": value["nonce"] = "0" * 32
        if fault == "stale": value["observedAtUnixNs"] -= 300 * 10**9
        if fault == "wrong-scope": value["scopeSha256"] = "0" * 64
        if fault == "blocked": value["status"] = "blocked"
        raw = json.dumps(value) + "\n"
        return subprocess.CompletedProcess(argv, 0, raw * (2 if fault == "duplicate" else 1), "")
    observer.run = run
    if fault == "positive":
        assert observer.observe("job-1", "before")["jobId"] == "job-1"
    else:
        with pytest.raises(ProofError):
            observer.observe("job-1", "before")


@pytest.mark.parametrize("fault", ["positive", "wrong-job", "wrong-attempt", "wrong-task", "two-attempts", "wrong-definition", "no-exit", "no-stop", "task-running"])
def test_batch_provider_attempt_and_exact_ecs_stopped_task_are_both_required(fault):
    observer, task, definition, worker, diagnostic = observer_fixture()
    observer.preflight()
    observer.config.update(cell="aws-serverless/redis-on", batchQueue="queue", batchDefinition="batch-definition:1")
    actual = {"jobId": "batch-1", "jobQueue": "queue", "jobDefinition": "batch-definition:1", "status": "FAILED",
              "container": {"environment": [{"name": "HONUA_OPERATION_ID", "value": "job-1"},
                                              {"name": "HONUA_EXECUTION_ATTEMPT", "value": "1"}]},
              "attempts": [{"startedAt": NOW * 1000, "stoppedAt": (NOW + 1) * 1000,
                            "container": {"taskArn": "task", "exitCode": 1}}]}
    task["lastStatus"] = "STOPPED"
    for row in task["containers"]: row["lastStatus"] = "STOPPED"
    if fault == "wrong-job": actual["container"]["environment"][0]["value"] = "other"
    if fault == "wrong-attempt": actual["container"]["environment"][1]["value"] = "2"
    if fault == "wrong-task": actual["attempts"][0]["container"]["taskArn"] = "other-task"
    if fault == "two-attempts": actual["attempts"] *= 2
    if fault == "wrong-definition": actual["jobDefinition"] = "batch-definition:2"
    if fault == "no-exit": del actual["attempts"][0]["container"]["exitCode"]
    if fault == "no-stop": del actual["attempts"][0]["stoppedAt"]
    if fault == "task-running": task["lastStatus"] = "RUNNING"
    ecs = observer.aws
    observer.aws = lambda service, *args: {"jobs": [actual]} if service == "batch" else ecs(service, *args)
    if fault == "positive":
        assert observer.batch("job-1", "batch-1", stopped=True)["stoppedAt"] == (NOW + 1) * 1000
    else:
        with pytest.raises(ProofError):
            observer.batch("job-1", "batch-1", stopped=True)


@pytest.mark.parametrize("field,value", [("process", {}), ("process", {"workerId": "worker-1"}),
                                       ("scratchInventory", {}), ("scratchInventory", EMPTY)])
def test_partial_process_or_scratch_cannot_replace_native_os_observation(field, value):
    candidate = record()
    candidate[field] = value
    with pytest.raises(ProofError):
        validate(candidate)


@pytest.mark.parametrize("fault", ["positive", "wrong-source", "wrong-subject", "wrong-ref", "changed-manifest", "duplicate-architecture"])
def test_native_authentication_binds_actual_attestation_and_index_membership(fault):
    import hashlib
    observer, *_ = observer_fixture()
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]}
    if fault == "duplicate-architecture": manifest["manifests"] *= 2
    raw = json.dumps(manifest)
    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    observer.config["workerImage"] = "native@" + digest
    observer.expected_digest = digest
    source = observer.config["source"]
    certificate = {"sourceRepositoryDigest": source, "sourceRepositoryRef": "refs/heads/trunk",
                   "buildSignerURI": "https://github.com/honua-io/honua-server/.github/workflows/nightly-container-build.yml@refs/heads/trunk"}
    statement = {"subject": [{"digest": {"sha256": digest.removeprefix("sha256:")}}], "predicate": {
        "buildDefinition": {"resolvedDependencies": [{"digest": {"gitCommit": source}}]}}}
    if fault == "wrong-source": certificate["sourceRepositoryDigest"] = "0" * 40
    if fault == "wrong-subject": statement["subject"][0]["digest"]["sha256"] = "0" * 64
    if fault == "wrong-ref": certificate["sourceRepositoryRef"] = "refs/heads/unreviewed"
    attested = [{"verificationResult": {"statement": statement, "signature": {"certificate": certificate}}}]
    def run(argv, **kwargs):
        if argv[0] == "gh":
            assert argv[argv.index("--source-digest") + 1] == source
            assert argv[argv.index("--signer-workflow") + 1].endswith("nightly-container-build.yml")
            return subprocess.CompletedProcess(argv, 0, json.dumps(attested), "")
        assert argv[:4] == ["docker", "buildx", "imagetools", "inspect"]
        return subprocess.CompletedProcess(argv, 0, raw + (" " if fault == "changed-manifest" else ""), "")
    observer.run = run
    if fault == "positive":
        observer.authenticate()
        assert observer.runtime_digests == {"amd64": "sha256:" + "a" * 64}
        assert observer.provenance["manifestSha256"] == digest.removeprefix("sha256:")
    else:
        with pytest.raises(ProofError):
            observer.authenticate()


@pytest.mark.parametrize("target,path", [("diagnostic", "/app"), ("diagnostic", "/app/gp_native_observer.py"),
    ("worker", "/app"), ("diagnostic", "/usr/bin"), ("diagnostic", "/outputs/gp"), ("worker", "/outputs/gp"),
    ("diagnostic", "/barriers/job-1"), ("worker", "/scratch/job-1"), ("diagnostic", "/var/run"),
    ("diagnostic", "/outputs/../app"), ("diagnostic", "/unrelated")])
def test_extra_code_import_or_nested_effect_mounts_cannot_replace_attested_execution(target, path):
    observer, task, definition, worker, diagnostic = observer_fixture()
    spec = diagnostic if target == "diagnostic" else worker
    spec["mountPoints"].append({"containerPath": path, "sourceVolume": "fresh-empty-or-replacement-code", "readOnly": True})
    with pytest.raises(ProofError):
        observer.preflight()


@pytest.mark.parametrize("target,name", [("worker", "PATH"), ("worker", "LD_PRELOAD"), ("worker", "DOTNET_STARTUP_HOOKS"),
    ("diagnostic", "PATH"), ("diagnostic", "PYTHONPATH"), ("diagnostic", "PYTHONHOME"), ("diagnostic", "LD_PRELOAD")])
def test_definition_environment_cannot_override_executable_or_import_paths(target, name):
    observer, task, definition, worker, diagnostic = observer_fixture()
    spec = diagnostic if target == "diagnostic" else worker
    spec.setdefault("environment", []).append({"name": name, "value": "/replacement"})
    with pytest.raises(ProofError):
        observer.preflight()


@pytest.mark.parametrize("target,name", [("worker", "PATH"), ("diagnostic", "PYTHONPATH"), ("worker", "LD_PRELOAD")])
def test_task_overrides_cannot_replace_bootstrap_environment(target, name):
    observer, task, definition, worker, diagnostic = observer_fixture()
    task["overrides"] = {"containerOverrides": [{"name": target, "environment": [{"name": name, "value": "/replacement"}]}]}
    with pytest.raises(ProofError):
        observer.preflight()


@pytest.mark.parametrize("name", ["LD_PRELOAD", "DOTNET_STARTUP_HOOKS", "HONUA_GP_QUALIFICATION_EXECUTOR_MODE", "Geoprocessing:OutputStaging:LocalRootPath"])
def test_secret_names_cannot_hide_bootstrap_or_scope_overrides(name):
    observer, task, definition, worker, diagnostic = observer_fixture()
    worker["secrets"] = [{"name": name, "valueFrom": "not-read-by-control"}]
    with pytest.raises(ProofError):
        observer.preflight()


@pytest.mark.parametrize("target,kind,path", [("diagnostic", "tmpfs", "/outputs/gp"), ("worker", "tmpfs", "/scratch/job-1"),
    ("diagnostic", "devices", "/app/gp_native_observer.py")])
def test_linux_tmpfs_and_device_mount_surfaces_cannot_shadow_effects_or_code(target, kind, path):
    observer, task, definition, worker, diagnostic = observer_fixture()
    spec = diagnostic if target == "diagnostic" else worker
    spec["linuxParameters"] = {kind: [{"containerPath": path, "size": 16}]}
    with pytest.raises(ProofError):
        observer.preflight()


def test_double_leading_slash_cannot_bypass_executable_mount_floor():
    observer, task, definition, worker, diagnostic = observer_fixture()
    worker["mountPoints"].append({"containerPath": "//app", "sourceVolume": "replacement", "readOnly": True})
    with pytest.raises(ProofError):
        observer.preflight()
