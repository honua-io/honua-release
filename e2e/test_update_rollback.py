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
        self.job_reads = {}

    def record(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise proof.ProofError("injected " + name)

    def prerequisites(self, prior, candidate):
        self.candidate_build = candidate
        self.record("prerequisites")
    def observe(self, build, phase):
        self.record(phase)
        return {"phase": phase, "observedAt": len(self.calls), "sourceRevision": build.source,
                "native": {"kind": "lambda-alias" if self.cell.startswith("aws-serverless/") else "ecs-native-routing",
                           "imageDigest": build.digest_for(self.cell), "taskCount": 1, "version": "2" if phase == "candidate" else "1"},
                "schemaJournal": {"method": "live-bidirectional-migration-plan", "pendingScripts": 0, "extraScripts": 0,
                                  "readyForCoordinatedDeploy": True, "candidateImageDigest": self.candidate_build.image.rsplit("@", 1)[1]},
                "servingFixture": {"serviceId": "e2e", "layerId": 7, **dict.fromkeys(
                    ("seedManifestSha256", "seedScriptSha256", "seedSqlSha256", "expectedFeaturesSha256"), "1" * 64)}}
    def incompatible_target(self): self.record("negative"); return {"status": "pass"}
    def stage(self, *a): self.record("stage"); return {"previous": "1", "desired": "2"}
    def plan(self, *a): self.record("plan")
    def submit_job(self):
        self.record("submit")
        return {"jobID": "real-boundary-job-" + str(self.calls.count("submit"))}
    def job(self, identity):
        self.reads += 1
        self.job_reads[identity] = self.job_reads.get(identity, 0) + 1
        state = "Succeeded" if self.job_reads[identity] >= 4 else "Running"
        return {"jobId": identity, "status": state, "backend": "local", "claimedBy": "worker-1", "attemptCount": 1,
                "providerOperationId": "provider-1", "completedAt": "2026-10-17T21:00:00Z" if state == "Succeeded" else None}
    def update(self, *a):
        self.record("update")
        if self.cell == "aws-ecs/redis-off":
            return {"actuator": "terraform-exact-apply", "status": "applied", "planSha256": "sha256:"+"3"*64, "imageDigest": self.candidate_build.digest_for(self.cell)}
        return {"operationId": "op-1", "status": "Succeeded", "observedRevision": "2", "providerOperationIdSha256": "sha256:"+"3"*64}
    def rollback(self, *a):
        self.record("rollback-op")
        if self.cell == "aws-ecs/redis-off":
            return {"actuator": "terraform-exact-apply", "status": "applied", "planSha256": "sha256:"+"4"*64, "imageDigest": "sha256:"+"a"*64}
        return {"operationId": "op-1", "status": "RolledBack", "observedRevision": "1", "providerOperationIdSha256": "sha256:"+"4"*64}
    def refusal(self): self.record("refusal"); return {"status": "pass", "outcome": "refused", "code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis", "functionalGpExecuted": False}
    def output(self, *a):
        self.outputs += 1
        return {"sha256": "sha256:" + ("b" if self.changed_output and self.outputs > 1 else "a") * 64,
                "fixtureSha256": "sha256:" + "c" * 64, "oracle": "independent-origin"}
    def execution_events(self, *a): return {"started": 2 if self.duplicate else 1, "completed": 1}


def run(runtime=None, cell="aws-ecs/redis-on", mode="qualification", prior=None, candidate=None):
    runtime = runtime or Runtime()
    runtime.cell = cell
    return proof.run_cell(prior or release(mode=mode), candidate or release("b", mode=mode), cell, mode, runtime,
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
        assert result["jobBeforeUpdate"]["jobId"] == result["jobAfterUpdate"]["jobId"]
        assert result["jobBeforeRollback"]["jobId"] == result["jobAfterRollback"]["jobId"]
        assert result["inFlightJob"]["jobId"] != result["rollbackInFlightJob"]["jobId"]
        assert runtime.calls.count("submit") == 2


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


@pytest.mark.parametrize("at", [1, 2, 3, 6, 7, 8])
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
    assert result["status"] == "fail"
    assert result["functionalGpExecuted"] is (at >= 6)


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
    for cell in sorted(proof.GA_CELLS):
        result = run(cell=cell, mode="rehearsal")
        result["cleanup"] = result["cost"] = {"status": "pass"}
        cells.append(result)
    receipt = proof.aggregate(cells)
    assert receipt["diagnosticOnly"] is True and receipt["qualification"] is False
    assert not any(key in receipt for key in ("class", "kind", "lockDigest", "fromLockDigest", "toLockDigest"))


@pytest.mark.parametrize("missing", ["cleanup", "cost", "cell", "binding"])
def test_no_partial_aggregate_or_unbound_cleanup(missing):
    cells = []
    for cell in sorted(proof.GA_CELLS):
        row = run(cell=cell)
        row["cleanup"] = row["cost"] = {"status": "pass"}
        cells.append(row)
    if missing in ("cleanup", "cost"): del cells[0][missing]
    if missing == "cell": cells.pop()
    if missing == "binding": cells[0]["runAttempt"] = "2"
    with pytest.raises(proof.ProofError): proof.aggregate(cells)


def test_ga_aggregate_is_the_two_ecs_cells_and_refuses_lambda_preview_rows():
    # Founder re-tier 2026-10-25: AWS ECS is the only GA target; a Lambda cell still runs as Preview.
    assert proof.GA_CELLS == {"aws-ecs/redis-off", "aws-ecs/redis-on"}
    assert {"aws-serverless/redis-off", "aws-serverless/redis-on"} <= proof.CELLS
    cells = []
    for cell in sorted(proof.GA_CELLS):
        row = run(cell=cell)
        row["cleanup"] = row["cost"] = {"status": "pass"}
        cells.append(row)
    assert {row["cell"] for row in proof.aggregate(cells)["cells"]} == proof.GA_CELLS
    extra = run(cell="aws-serverless/redis-on")
    extra["cleanup"] = extra["cost"] = {"status": "pass"}
    with pytest.raises(proof.ProofError): proof.aggregate([*cells, extra])
    with pytest.raises(proof.ProofError): proof.aggregate([cells[0], extra])


def test_ecs_preflight_does_not_require_a_distinct_lambda_build():
    prior, candidate = release("a"), release("b")
    same_lambda = proof.Release(**{**candidate.__dict__, "lambda_digest": prior.lambda_digest})
    for cell in sorted(proof.GA_CELLS):
        proof.preflight(prior, same_lambda, cell, "qualification")
    for cell in ("aws-serverless/redis-off", "aws-serverless/redis-on"):
        with pytest.raises(proof.ProofError, match="two distinct immutable server builds"):
            proof.preflight(prior, same_lambda, cell, "qualification")


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


def _attestation(source, subject):
    return [{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": subject.removeprefix("sha256:")}}],
        "predicate": {"buildDefinition": {"resolvedDependencies": [{"digest": {"gitCommit": source}}]}}},
        "signature": {"certificate": {"sourceRepositoryDigest": source, "sourceRepositoryRef": "refs/heads/trunk",
            "buildSignerURI": "https://github.com/honua-io/honua-server/.github/workflows/nightly-container-build.yml@refs/heads/trunk"}}}}]


def bundle(tmp_path):
    import yaml
    directory = tmp_path / "bundle"
    directory.mkdir()
    server = {"sha": "a" * 40, "image": "ghcr.io/honua-io/honua-server:nightly-a",
              "digest": "sha256:" + "b" * 64, "dbSchema": "128",
              "awsLambdaImage": "ghcr.io/honua-io/honua-server:nightly-lambda-aot-a-amd64",
              "awsLambdaDigest": "sha256:" + "c" * 64, "awsLambdaEcrDigest": "sha256:" + "c" * 64,
              "awsLambdaArchitecture": "x86_64"}
    path = directory / "platform-manifest.yaml"
    path.write_text(yaml.safe_dump({"platformRelease": "2026.1-rc.3", "components": {"honua-server": server}}))
    return directory, server


@pytest.mark.parametrize("negative", [None, "wrong-source", "unsigned", "wrong-child", "wrong-platform", "wrong-mirror"])
def test_input_authentication_binds_real_attested_lambda_parent_to_selected_child(tmp_path, negative):
    import yaml
    directory, server = bundle(tmp_path)
    if negative == "wrong-mirror":
        server["awsLambdaEcrDigest"] = "sha256:" + "d" * 64
        (directory / "platform-manifest.yaml").write_text(yaml.safe_dump({"components": {"honua-server": server}}))
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "buildx"]:
            index = {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + ("d" if negative == "wrong-child" else "c") * 64,
                     "platform": {"os": "linux", "architecture": "arm64" if negative == "wrong-platform" else "amd64"}}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(index))
        subject = argv[3].rsplit("@", 1)[1]
        source = "d" * 40 if negative == "wrong-source" else server["sha"]
        return SimpleNamespace(returncode=1 if negative == "unsigned" else 0, stdout=json.dumps(_attestation(source, subject)))
    if negative:
        with pytest.raises(proof.ProofError): proof.load_release(directory, "rehearsal", run=runner)
    else:
        selected = proof.load_release(directory, "rehearsal", run=runner)
        assert selected.lock_digest is None and selected.source == server["sha"]
        assert selected.lambda_digest == server["awsLambdaDigest"]
        gh = [c for c in calls if c[0] == "gh"]
        assert len(gh) == 2 and all("--signer-workflow" in c and "--source-digest" in c for c in gh)
        # The deployed child is authenticated through index membership; no fake child attestation.
        assert gh[1][3] != "oci://" + selected.lambda_image


@pytest.mark.parametrize("deregister", [True, None, False])
def test_lambda_saved_plan_must_retain_prior_worker_revision_before_any_apply(tmp_path, monkeypatch, deregister):
    target = serverless()
    target._workdir = tmp_path
    (tmp_path / "terraform.tfstate").write_text("{}")
    monkeypatch.setattr(target, "_iac_root", lambda: tmp_path)
    monkeypatch.setattr(target, "_root_declares", lambda _: True)
    target._last_vars = ["-var=honua_image_uri=123.dkr.ecr.us-east-1.amazonaws.com/honua@sha256:" + "a" * 64]
    calls = []
    plan = {"resource_changes": [{"type": "aws_batch_job_definition", "change": {"actions": ["update"],
        "before": {"container_properties": "old"}, "after": {"container_properties": "new", "deregister_on_new_revision": deregister}}}]}
    def tf(_root, *args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(plan) if args[0] == "show" else "")
    monkeypatch.setattr(target, "_tf", tf)
    call = lambda: target.stage_lambda("123.dkr.ecr.us-east-1.amazonaws.com/honua@sha256:" + "b" * 64, "3", redis_enabled=True)
    if deregister is False:
        call()
        assert calls[-1][0] == "apply"
        assert "-var=lambda_alias_version=3" in calls[0]
    else:
        with pytest.raises(Exception, match="deregister_on_new_revision=false"): call()
        assert not any(c[0] == "apply" for c in calls)


def test_collector_rehearsal_and_qualification_share_execution_floor_without_invented_lock(tmp_path):
    import collect_update_rollback as collector
    for mode in ("rehearsal", "qualification"):
        root = tmp_path / mode
        for cell in proof.GA_CELLS:
            result = run(cell=cell, mode=mode)
            result["cleanup"] = result["cost"] = {"status": "pass"}
            path = root / cell / "update-rollback-cell.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(result))
        selected = collector.collect(root, release(mode=mode), release("b", mode=mode), mode, "123", "1")
        assert selected["qualification"] is (mode == "qualification")
        if mode == "rehearsal": assert "lockDigest" not in selected
        else: assert selected["class"] == "update-rollback"
        # A retained raw full GP completion claim must never substitute for an off refusal.
        off = next(root.glob("*/redis-off/update-rollback-cell.json"))
        value = json.loads(off.read_text()); value["functionalGpExecuted"] = True; off.write_text(json.dumps(value))
        with pytest.raises(proof.ProofError): collector.collect(root, release(mode=mode), release("b", mode=mode), mode, "123", "1")


@pytest.mark.parametrize("hidden", [{"resourceUri": "honua://jobs/accepted"}, {"nested": {"jobCreatedAt": "2026-10-17T21:00:00Z"}},
                                    {"text": '{"jobId":"accepted"}'}, {"rows": [{"jobStatus": "running"}]}])
def test_actual_off_http_boundary_rejects_hidden_execution_evidence(hidden):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    runtime.workload = native.centroid_workload(4)
    runtime.api = FakeApi([(503, {"code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis", **hidden})])
    with pytest.raises(proof.ProofError, match="typed nonexecuted"): runtime.refusal()
    assert len(runtime.api.calls) == 1


def test_actual_off_http_refusal_needs_independent_manifest():
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    runtime.workload = native.centroid_workload(4)
    for available in (True, False):
        runtime.api = FakeApi([(503, {"code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis"}),
                               (200, {"capabilities": [{"id": "jobs.runner", "available": available, "reasonCode": "dependency-unavailable"}]})])
        if available:
            with pytest.raises(proof.ProofError, match="corroborate"): runtime.refusal()
        else: assert runtime.refusal()["functionalGpExecuted"] is False


@pytest.mark.parametrize("mutation", [None, "pending", "extra", "not-ready", "no-plan", "error"])
def test_real_schema_preflight_requires_both_live_set_differences_empty(mutation):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    view = {"readyForCoordinatedDeploy": True, "migration": {"planAvailable": True, "upgradeRequired": False,
             "pendingScripts": [], "executedButNotDiscoveredScripts": []}}
    if mutation == "pending": view["migration"]["pendingScripts"] = ["next.sql"]
    if mutation == "extra": view["migration"]["executedButNotDiscoveredScripts"] = ["future.sql"]
    if mutation == "not-ready": view["readyForCoordinatedDeploy"] = False
    if mutation == "no-plan": view["migration"]["planAvailable"] = False
    if mutation == "error": view["migration"]["planError"] = "unavailable"
    runtime.api = FakeApi([(200, view)])
    if mutation:
        with pytest.raises(proof.ProofError): runtime.schema_journal()
    else: assert runtime.schema_journal()["pendingScripts"] == 0
    assert runtime.api.calls == [("GET", "/api/v1/admin/deploy/preflight?includeDiagnostics=true", None)]


@pytest.mark.parametrize("bad", [None, "wrong-name", "wrong-geometry", "missing", "duplicate"])
def test_native_serving_probe_compares_authored_literals_not_candidate_parity(bad):
    fixture = {"expectedFeatures": [{"id": "1", "name": "alpha", "geometry": {"type": "Point", "coordinates": [-156.33, 20.75]}},
                                   {"id": "2", "name": "bravo", "geometry": {"type": "Point", "coordinates": [-156.45, 20.88]}}]}
    features = [{"id": int(f["id"]), "properties": {"name": f["name"]}, "geometry": copy.deepcopy(f["geometry"])} for f in fixture["expectedFeatures"]]
    if bad == "wrong-name": features[0]["properties"]["name"] = "different"
    if bad == "wrong-geometry": features[0]["geometry"]["coordinates"][0] += 1
    if bad == "missing": features.pop()
    if bad == "duplicate": features.append(copy.deepcopy(features[0]))
    if bad:
        with pytest.raises(proof.ProofError): native.prove_serving_features({"features": features}, fixture)
    else: native.prove_serving_features({"features": features}, fixture)


@pytest.mark.parametrize("transition", ["update", "rollback"])
@pytest.mark.parametrize("corrupt", ["missing", "duplicate", "identity", "output"])
def test_each_transition_has_independent_exactly_once_terminal_output_proof(transition, corrupt):
    runtime = Runtime()
    original_events, original_job, original_output = runtime.execution_events, runtime.job, runtime.output
    selected = "real-boundary-job-" + ("1" if transition == "update" else "2")
    if corrupt == "duplicate":
        runtime.execution_events = lambda identity, *args: {"started": 1, "completed": 2} if identity == selected else original_events(identity, *args)
    if corrupt == "identity":
        def job(identity):
            row = original_job(identity)
            if identity == selected and runtime.job_reads[identity] >= 3: row["providerOperationId"] = "different"
            return row
        runtime.job = job
    if corrupt == "missing":
        runtime.job = lambda identity: {**original_job(identity), "status": "Failed"} if identity == selected else original_job(identity)
    if corrupt == "output":
        counts = {}
        def output(identity):
            counts[identity] = counts.get(identity, 0) + 1
            value = original_output(identity)
            if identity == selected and counts[identity] > 1: value["sha256"] = "sha256:" + "d" * 64
            return value
        runtime.output = output
    result = run(runtime)
    assert result["status"] == "fail", result
    assert result["updateStatus"] == ("pass" if transition == "rollback" else "fail")


def test_fresh_controller_uses_private_original_tf_inputs_not_mutable_environment(tmp_path, monkeypatch):
    first = ecs(run_id="123")
    prior = ["-input=false", "-var=honua_image=ghcr.io/honua-io/honua-server@sha256:" + "a" * 64,
             "-var=protected_setting=Synthetic-test-secret", "-var=redis_enabled=false"]
    first._write_transition_vars(tmp_path, prior, False)
    private = tmp_path / first._TRANSITION_INPUTS
    assert private.stat().st_mode & 0o777 == 0o600
    target = ecs(run_id="123"); target._workdir = tmp_path; target.transition_release = release(mode="rehearsal")
    (tmp_path / "terraform.tfstate").write_text("{}")
    monkeypatch.setattr(target, "_vars", lambda *a, **k: pytest.fail("must not reconstruct changed operator inputs"))
    calls = []
    monkeypatch.setattr(target, "_tf", lambda root, *a, **k: calls.append(a) or SimpleNamespace(stdout="", returncode=0))
    target.update("ghcr.io/honua-io/honua-server@sha256:" + "b" * 64)
    assert "-var=protected_setting=Synthetic-test-secret" in calls[0]
    target.rollback()
    assert prior[1] in calls[2] and prior[2] in calls[2]
    # A third process restores the updated private argv for unconditional destruction.
    destroy = ecs(run_id="123"); destroy._workdir = tmp_path
    monkeypatch.setattr(destroy, "_tf", lambda root, *a, **k: calls.append(a) or SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(destroy, "_cell_dns_leftovers", lambda *a: None)
    destroy.teardown(False)
    assert calls[-1][0] == "destroy" and prior[1] in calls[-1] and prior[2] in calls[-1]


@pytest.mark.parametrize("bad", ["missing", "wrong-run", "wrong-cell", "wrong-redis", "public", "symlink"])
def test_private_transition_handoff_refuses_before_any_plan(tmp_path, monkeypatch, bad):
    source = ecs(run_id="other" if bad == "wrong-run" else "123")
    source._write_transition_vars(tmp_path, ["-var=honua_image=ghcr.io/honua-io/honua-server@sha256:" + "a" * 64], False)
    private = tmp_path / source._TRANSITION_INPUTS
    if bad == "missing": private.unlink()
    elif bad == "wrong-cell":
        data = json.loads(private.read_text()); data["target"] = "aws-serverless"; private.write_text(json.dumps(data))
    elif bad == "wrong-redis":
        data = json.loads(private.read_text()); data["redisEnabled"] = True; private.write_text(json.dumps(data))
    elif bad == "public": private.chmod(0o644)
    elif bad == "symlink":
        saved = tmp_path / "other"; private.rename(saved); private.symlink_to(saved)
    target = ecs(run_id="123"); target._workdir = tmp_path; target.transition_release = release(mode="rehearsal")
    (tmp_path / "terraform.tfstate").write_text("{}")
    monkeypatch.setattr(target, "_tf", lambda *a, **k: pytest.fail("invalid private handoff must fail before planning"))
    with pytest.raises(Exception): target.update("ghcr.io/honua-io/honua-server@sha256:" + "b" * 64)


@pytest.mark.parametrize("bad", [False, True])
def test_actual_candidate_schema_probe_removes_only_owned_network_admission_and_never_migrates(tmp_path, monkeypatch, bad):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    # The probe needs no operator file: its Production startup profile is minted per run.
    runtime.controller_env = None; runtime.api = SimpleNamespace(credential="synthetic-admin")
    runtime.probe_cleanup_failures = []; runtime.sleep = lambda _: None
    runtime.journal = {"method": "live-bidirectional-migration-plan", "pendingScripts": 0, "extraScripts": 0, "readyForCoordinatedDeploy": True}
    target = ecs(run_id="123"); target._workdir = tmp_path
    monkeypatch.setattr(target, "_connection_string", lambda: "Host=cell-db;Port=5432;Database=honua;Password=synthetic")
    monkeypatch.setattr(target, "_state_secrets", lambda *a: ["synthetic-master"])
    state = {"values": {"root_module": {"resources": [{"type": "aws_db_instance", "values": {
        "address": "cell-db", "publicly_accessible": True, "vpc_security_group_ids": ["owned-db-sg"]}}]}}}
    monkeypatch.setattr(target, "_tf", lambda *a: SimpleNamespace(stdout=json.dumps(state)))
    runtime.target = target
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "203.0.113.1/32")
    monkeypatch.setattr(runtime, "controller_credentials", lambda: {"AWS_ACCESS_KEY_ID": "approved-controller-role"})
    aws_calls, docker_calls = [], []
    def aws(*args):
        aws_calls.append(args)
        if args[1] == "describe-security-group-rules": return {"SecurityGroupRules": []}
        if args[1] == "authorize-security-group-ingress": return {"SecurityGroupRules": [{"SecurityGroupRuleId": "owned-rule"}]}
        return {}
    runtime.aws = aws
    def runner(argv, **kwargs):
        docker_calls.append(argv)
        if argv[0] == "openssl": return SimpleNamespace(stdout="", returncode=0)
        if argv[:2] == ["docker", "run"]:
            env = Path(argv[argv.index("--env-file") + 1]).read_text()
            assert "HONUA_SKIP_MIGRATIONS=true" in env and "Host=cell-db" in env
            assert "ConnectionStrings__redis" not in env and "ControlPlane__" not in env
            assert "Operations__SecretChannel__KeyRingCertificatePath=/keyring/keyring.pfx" in env
            # The cell's own master key (it decrypts the cell database) replaces the profile's.
            assert [l for l in env.splitlines() if l.startswith("Security__ConnectionEncryption__MasterKey=")] == [
                "Security__ConnectionEncryption__MasterKey=synthetic-master"]
            assert any(a.endswith(":/keyring/keyring.pfx:ro") for a in argv)
            assert "AWS_ACCESS_KEY_ID=approved-controller-role" in env and argv[-1] == release("b").image
        return SimpleNamespace(stdout="container", returncode=0)
    runtime.run = runner
    def journal(api):
        if bad: raise proof.ProofError("schema incompatible")
        return dict(runtime.journal)
    monkeypatch.setattr(runtime, "schema_journal", journal)
    if bad:
        with pytest.raises(proof.ProofError, match="before deployment mutation"): runtime.schema_probe(release("b"))
    else: runtime.schema_probe(release("b"))
    assert any(c[:2] == ("ec2", "revoke-security-group-ingress") and c[-1] == "owned-rule" for c in aws_calls)
    assert docker_calls[-1][:3] == ["docker", "rm", "-f"]
    assert not any("apply" in c for c in aws_calls + [tuple(c) for c in docker_calls])
    assert not runtime.probe_cleanup_failures


def test_async_preference_reaches_actual_http_boundary():
    import http.server
    import threading
    seen = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Prefer"), self.headers.get("X-API-Key")))
            self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.dumps({"jobID": "actual-wire-job", "status": "accepted"}).encode()
            self.send_response(201 if self.headers.get("Prefer") == "respond-async" else 200)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args): pass
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            api = proof.Api(f"http://127.0.0.1:{server.server_port}", "synthetic-wire-key", loopback=True)
            status, document = api.request("POST", "/ogc/processes/processes/geometry.centroid/execution", native.centroid_workload(4))
            assert status == 201 and document["status"] == "accepted"
            assert seen == [("/ogc/processes/processes/geometry.centroid/execution", "respond-async", "synthetic-wire-key")]
        finally: server.shutdown(); thread.join()


@pytest.mark.parametrize("confirm", [True, False])
def test_cleanup_requires_terminal_confirmation_before_retained_worker_deregistration(confirm):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    runtime.probe_cleanup_failures = []; runtime.submitted_job = "owned-job"
    runtime.controller_name = None; runtime.registered_tasks = []
    runtime.retained_batch_definitions = ["owned-baseline-definition"]
    runtime.api = FakeApi([(202, {})]); runtime.sleep = lambda _: None
    reads = []
    def job(identity):
        reads.append(identity)
        return {"status": "Cancelled" if confirm and len(reads) > 1 else "Running"}
    runtime.job = job
    calls = []; runtime.aws = lambda *a: calls.append(a)
    failures = runtime.close()
    if confirm:
        assert failures == [] and calls == [("batch", "deregister-job-definition", "--job-definition", "owned-baseline-definition")]
    else:
        assert "owned-inflight-job" in failures and "retained-batch-definition-awaiting-job" in failures
        assert calls == []
    assert runtime.api.calls == [("DELETE", "/ogc/processes/jobs/owned-job", None)]


@pytest.mark.parametrize("mode", ["delayed", "duplicate", "missing"])
def test_cloudwatch_settle_never_turns_missing_or_duplicate_execution_into_success(mode):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime); runtime.sleep = lambda _: None
    reads = []
    def events(*a):
        reads.append(1)
        if mode == "missing" or len(reads) == 1: return {"started": 0, "completed": 0, "eventIdsSha256": "none"}
        if mode == "duplicate" and len(reads) > 2: return {"started": 2, "completed": 1, "eventIdsSha256": "duplicate"}
        return {"started": 1, "completed": 1, "eventIdsSha256": "one"}
    runtime._execution_events = events
    if mode == "missing":
        with pytest.raises(proof.ProofError, match="publication window"): runtime.execution_events("job", "start")
        assert len(reads) == 30
    else:
        result = runtime.execution_events("job", "start")
        assert result["started"] == (2 if mode == "duplicate" else 1) and len(reads) == 3


@pytest.mark.parametrize("wrong_parent", [False, True])
def test_ecs_readback_cannot_use_an_unrelated_parent_with_a_declared_child_digest(wrong_parent):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    build = release("b")
    child = "sha256:" + "c" * 64
    server = build.manifest["components"]["honua-server"]
    server.update(awsEcsArchitecture="x86_64", platformDigests={"amd64": child}, contractVersions={"admin": "v1"})
    runtime.cell = "aws-ecs/redis-off"; runtime.safety = None
    runtime.output_value = lambda name: "owned-cluster" if name == "ecs_cluster_name" else "owned-service"
    runtime.fixture = {"layerId": 1, "expectedFeatures": [{"id": "1", "name": "alpha", "geometry": {}}, {"id": "2", "name": "bravo", "geometry": {}}]}
    runtime.journal = {"proof": "unchanged"}; runtime.schema_journal = lambda: dict(runtime.journal)
    def aws(*args):
        if args[1] == "list-tasks": return {"taskArns": ["owned-task"]}
        return {"tasks": [{"lastStatus": "RUNNING", "containers": [{"lastStatus": "RUNNING", "imageDigest": child,
            "image": "ghcr.io/honua-io/honua-server@sha256:" + "d" * 64 if wrong_parent else build.image}]}]}
    runtime.aws = aws
    runtime.api = FakeApi([(200, {"server": {"deploymentRevision": build.source, "deploymentRevisionSource": "commit-sha"}}),
                          (200, {"data": {"contractVersions": {"admin": "v1"}, "schemaVersions": {"database": build.schema}}}),
                          (200, {"features": [{"id": 1, "properties": {"name": "alpha"}, "geometry": {}},
                                               {"id": 2, "properties": {"name": "bravo"}, "geometry": {}}]})])
    if wrong_parent:
        with pytest.raises(proof.ProofError, match="actual serving ECS image"): runtime.observe(build, "candidate")
        assert runtime.api.calls == []
    else: assert runtime.observe(build, "candidate")["sourceRevision"] == build.source


@pytest.mark.parametrize("key", ["ControlPlane__DeployTargets__0__TargetId", "controlplane:deploytargets:0:targetid",
                                 "CONTROLPLANE__PlatformRelease__Version", "aSpNeTcOrE_uRlS"])
def test_protected_startup_override_guard_uses_dotnet_key_normalization(key):
    normalized = native.env_key(key + "=untrusted")
    assert normalized.startswith(("controlplane__deploytargets__", "controlplane__platformrelease__", "aspnetcore_urls"))


@pytest.mark.parametrize("broken", ["job-read", "controller", "batch"])
def test_unexpected_adapter_failure_does_not_skip_independent_owned_cleanup(broken):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    runtime.probe_cleanup_failures = []; runtime.submitted_job = "owned-job"; runtime.controller_name = "owned-controller"
    runtime.registered_tasks = ["owned-task-1", "owned-task-2"]
    runtime.retained_batch_definitions = ["owned-batch-1", "owned-batch-2"]
    runtime.sleep = lambda _: None
    def job(identity):
        if broken == "job-read": raise ValueError("PRIVATE-ADAPTER-DETAIL")
        return {"status": "Succeeded"}
    runtime.job = job
    calls = []
    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if broken == "controller": raise ValueError("PRIVATE-ADAPTER-DETAIL")
        return SimpleNamespace(stdout="", returncode=0)
    runtime.run = run
    def aws(*args):
        calls.append(args)
        if broken == "batch" and args[-1] == "owned-batch-1": raise ValueError("PRIVATE-ADAPTER-DETAIL")
        return {}
    runtime.aws = aws
    failures = runtime.close()
    assert failures and "PRIVATE-ADAPTER-DETAIL" not in json.dumps(failures)
    assert ("docker", "rm", "-f", "owned-controller") in calls
    assert ("ecs", "deregister-task-definition", "--task-definition", "owned-task-2") in calls
    if broken == "job-read":
        assert "owned-inflight-job" in failures and not any("batch" in c for c in calls)
    else: assert ("batch", "deregister-job-definition", "--job-definition", "owned-batch-2") in calls


@pytest.mark.parametrize('capture', [False, True])
def test_terraform_concrete_operation_hashes_only_the_actual_applied_plan(tmp_path, monkeypatch, capture):
    target = ecs(run_id='123'); target._workdir = tmp_path
    (tmp_path / 'terraform.tfstate').write_text('{}')
    target._last_vars = ['-var=honua_image=ghcr.io/honua-io/honua-server@sha256:' + 'a' * 64]
    plans, applied = [], []
    def tf(root, *args):
        if args[0] == 'plan':
            raw = ('synthetic private plan containing secret=' + repr(args)).encode()
            plans.append(raw); (root / 'honua-update.tfplan').write_bytes(raw)
        elif args[0] == 'apply': applied.append((root / args[-1]).read_bytes())
        return SimpleNamespace(stdout='', returncode=0)
    monkeypatch.setattr(target, '_tf', tf)
    update = target.update('ghcr.io/honua-io/honua-server@sha256:'+'b'*64, capture_evidence=capture)
    rollback = target.rollback(capture_evidence=capture)
    assert plans == applied and len(plans) == 2
    assert not (tmp_path / 'honua-update.tfplan').exists()
    if capture:
        assert update['planSha256'] == proof.digest(plans[0]) and rollback['planSha256'] == proof.digest(plans[1])
        assert update['planSha256'] != rollback['planSha256']
        assert update['imageDigest'] == 'sha256:'+'b'*64 and rollback['imageDigest'] == 'sha256:'+'a'*64
        assert 'secret' not in json.dumps([update, rollback])
    else: assert update is None and rollback is None


def test_terraform_proof_refuses_missing_saved_plan_before_apply(tmp_path, monkeypatch):
    target = ecs(run_id='123'); target._workdir = tmp_path
    (tmp_path / 'terraform.tfstate').write_text('{}')
    target._last_vars = ['-var=honua_image=ghcr.io/honua-io/honua-server@sha256:'+'a'*64]
    calls = []
    monkeypatch.setattr(target, '_tf', lambda root, *args: calls.append(args) or SimpleNamespace(stdout='', returncode=0))
    with pytest.raises(Exception, match='plan identity missing'):
        target.update('ghcr.io/honua-io/honua-server@sha256:'+'b'*64, capture_evidence=True)
    assert not any(args[0] == 'apply' for args in calls)


class StoreApi:
    """Loopback admin API of the image-A migrator / retained controller (no network)."""
    def __init__(self, scenario):
        self.scenario, self.calls = scenario, []

    def __call__(self, endpoint, credential, **kwargs):
        api = SimpleNamespace(endpoint=endpoint, credential=credential)
        def request(method, path, body=None):
            self.calls.append((endpoint, credential, method, path, body))
            if path == "/api/v1/admin/api-keys":
                key = "synthetic-admin" if self.scenario == "same-key" else "minted-approver"
                granted = ["admin"] if self.scenario == "broad" else list(body["permissions"])
                identity = {} if self.scenario == "unechoed" else {"permissions": granted}
                return (201 if self.scenario != "refused" else 403), {"data": {"key": key, "apiKey": identity}}
            pending = ["Honua.Server.Migrations.999_Later.sql"] if self.scenario == "pending" else []
            return 200, {"readyForCoordinatedDeploy": True, "migration": {
                "planAvailable": True, "upgradeRequired": bool(pending), "pendingScripts": pending,
                "executedButNotDiscoveredScripts": [], "planError": None}}
        api.request = request
        api.get = lambda path: request("GET", path)[1]
        return api


def hermetic_runtime(tmp_path, monkeypatch, scenario="ok", supplement=None):
    runtime = native.AwsRuntime.__new__(native.AwsRuntime)
    runtime.cell = "aws-ecs/redis-on"; runtime.sleep = lambda _: None
    runtime.api = SimpleNamespace(credential="synthetic-admin")
    runtime.store_containers, runtime.profile_dir, runtime._profile = [], None, None
    runtime.probe_cleanup_failures, runtime.submitted_job, runtime.controller_name = [], None, None
    runtime.registered_tasks, runtime.retained_batch_definitions = [], []
    runtime.controller_env = supplement
    runtime.safety = {"target_id": "owned-canary", "parameters": {"aws.ecs.canary_service": "owned-canary", "aws.region": "us-east-1"}}
    runtime.target = SimpleNamespace(region="us-east-1", _state_secrets=lambda *a: ["cell-queue.cache.amazonaws.com:6379"])
    monkeypatch.setattr(runtime, "controller_credentials", lambda: {"AWS_ACCESS_KEY_ID": "approved-controller-role"})
    calls, envs = [], {}
    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["docker", "run"] and "--env-file" in argv:
            envs[argv[argv.index("--name") + 1]] = Path(argv[argv.index("--env-file") + 1]).read_text()
        return SimpleNamespace(stdout="", returncode=0)
    runtime.run = runner
    api = StoreApi(scenario)
    monkeypatch.setattr(native, "Api", api)
    return runtime, calls, envs, api


def test_hermetic_store_is_migrated_by_image_a_and_the_controller_never_migrates(tmp_path, monkeypatch):
    runtime, calls, envs, api = hermetic_runtime(tmp_path, monkeypatch)
    prior, candidate = release("a"), release("b")
    runtime.start_controller(prior, candidate)
    runs = [c for c in calls if c[:2] == ["docker", "run"]]
    names = [c[c.index("--name") + 1] for c in runs]
    # Order: store database, store Redis, image-A migrator, then the retained controller.
    assert [n.split("-")[2] for n in names] == ["pg", "redis", "migrate", names[3].split("-")[2]]
    assert runs[0][-1] == native.CONTROLLER_STORE_IMAGES["postgis"] and "127.0.0.1:55432:5432" in runs[0]
    assert runs[1][-4:] == [native.CONTROLLER_STORE_IMAGES["redis"], "redis-server", "--appendonly", "yes"]
    migrator, controller = names[2], names[3]
    assert runs[2][-1] == prior.image and runs[3][-1] == prior.image
    assert "HONUA_SKIP_MIGRATIONS=false" in envs[migrator] and "HONUA_SKIP_MIGRATIONS=true" in envs[controller]
    store_dsn = [l for l in envs[migrator].splitlines() if l.startswith("ConnectionStrings__DefaultConnection=")]
    assert store_dsn and store_dsn[0] in envs[controller].splitlines() and "Host=127.0.0.1;Port=55432" in store_dsn[0]
    assert "ConnectionStrings__redis=127.0.0.1:56379,defaultDatabase=0" in envs[controller]
    # The migrator is removed before the controller starts; it never sees AWS or deploy wiring.
    assert ["docker", "rm", "-f", migrator] in calls and calls.index(["docker", "rm", "-f", migrator]) < calls.index(runs[3])
    assert "AWS_" not in envs[migrator] and "ControlPlane__" not in envs[migrator]
    for name in (migrator, controller):
        assert "Operations__SecretChannel__KeyRingCertificatePath=/keyring/keyring.pfx" in envs[name]
    # A per-run approver minted in the store, distinct from the owned admin key, approves.
    minted = [c for c in api.calls if c[3] == "/api/v1/admin/api-keys"]
    assert len(minted) == 1 and minted[0][4]["permissions"] == ["admin:read", "admin:approve"]
    assert minted[0][0].endswith(":%d" % native.STORE_MIGRATOR_PORT)
    assert runtime.approval_key == "minted-approver" != runtime.api.credential
    assert runtime.deploy.approval_api.credential == "minted-approver"
    # The store images are digest-pinned and recorded for the receipt.
    assert all("@sha256:" in image and len(image.rsplit(":", 1)[1]) == 64 for image in native.CONTROLLER_STORE_IMAGES.values())
    assert runtime.controller_store["images"] == native.CONTROLLER_STORE_IMAGES
    assert runtime.controller_store["migratedBy"] == prior.image and runtime.controller_store["topology"] == "hermetic-per-run"
    # Cleanup removes the controller and the whole store, volumes included.
    assert runtime.close() == []
    assert ["docker", "rm", "-f", "-v", names[0]] in calls and ["docker", "rm", "-f", "-v", names[1]] in calls


@pytest.mark.parametrize("scenario,message", [("pending", "did not migrate the controller store"),
                                              ("same-key", "distinct API key"), ("broad", "exactly admin:read"),
                                              ("unechoed", "exactly admin:read"),
                                              ("refused", "could not be created")])
def test_hermetic_store_refuses_unmigrated_store_and_unsafe_approver(tmp_path, monkeypatch, scenario, message):
    runtime, calls, envs, api = hermetic_runtime(tmp_path, monkeypatch, scenario)
    with pytest.raises(proof.ProofError, match=message):
        runtime.start_controller(release("a"), release("b"))
    assert not any(c[:2] == ["docker", "run"] and c[c.index("--name") + 1].startswith("honua-rollback-") and
                   "HONUA_SKIP_MIGRATIONS=true" in envs.get(c[c.index("--name") + 1], "") for c in calls)
    runtime.close()
    removed = {c[-1] for c in calls if c[:3] == ["docker", "rm", "-f"]}
    started = {c[c.index("--name") + 1] for c in calls if c[:2] == ["docker", "run"]}
    assert started <= removed, "every hermetic store container is removed even when the proof refuses"


def test_hermetic_store_never_shares_the_cell_queue(tmp_path, monkeypatch):
    runtime, calls, envs, api = hermetic_runtime(tmp_path, monkeypatch)
    runtime.target._state_secrets = lambda *a: ["127.0.0.1:56379,defaultDatabase=0"]
    with pytest.raises(proof.ProofError, match="independent durable Redis store"):
        runtime.start_controller(release("a"), release("b"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode privacy check")
@pytest.mark.parametrize("line", ["ConnectionStrings__redis=operator-queue", "ControlPlane__DeployTargets__0__TargetId=x",
                                  "HONUA_ADMIN_PASSWORD=x", "operations:secretchannel:keyringcertificatepath=/x"])
def test_operator_startup_supplement_cannot_override_the_hermetic_store_or_wiring(tmp_path, monkeypatch, line):
    supplement = tmp_path / "controller.env"
    supplement.write_text("Logging__LogLevel__Default=Information\n" + line + "\n")
    supplement.chmod(0o600)
    runtime, calls, envs, api = hermetic_runtime(tmp_path, monkeypatch, supplement=supplement)
    with pytest.raises(proof.ProofError, match="must not override"):
        runtime.startup_profile()


def test_empty_operator_supplement_is_allowed_and_ignored(tmp_path, monkeypatch):
    supplement = tmp_path / "controller.env"
    supplement.write_text("")
    runtime, calls, envs, api = hermetic_runtime(tmp_path, monkeypatch, supplement=supplement)
    lines, mounts = runtime.startup_profile()
    assert lines[0] == "ASPNETCORE_ENVIRONMENT=Production" and mounts[0] == "-v"


def test_receipt_records_the_hermetic_controller_store_identity():
    runtime = Runtime()
    runtime.controller_store = {"topology": "hermetic-per-run", "images": dict(native.CONTROLLER_STORE_IMAGES),
                                "migratedBy": release("a").image, "approverPermissions": ["admin:read", "admin:approve"]}
    receipt = run(runtime)
    assert receipt["controllerStore"]["images"] == native.CONTROLLER_STORE_IMAGES
    assert "controllerStore" not in run(Runtime())
