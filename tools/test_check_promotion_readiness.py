import copy
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import check_promotion_readiness as readiness
from candidate_binding import REQUIRED_RELEASE_GATES
import pytest
import yaml
from jsonschema import Draft202012Validator

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
OTHER_LOCK = "sha256:" + "f" * 64
ECS_IMAGE = "sha256:" + "b" * 64
LAMBDA_IMAGE = "sha256:" + "c" * 64

# Expectations are written out here rather than read from the module under test, so
# dropping a class, journey or cell from the checker fails these tests.
NIGHTLY_CLASSES = ("build-test", "contract", "sbom", "security", "upgrade", "capacity-soak", "dr",
                   "lambda-certification", "protocol-ledger", "deterministic-journey", "nightly-model-journey",
                   "executable-docs", "installed-clients")
QUALIFYING_CLASSES = ("genuine-model-journey", "update-rollback", "cite")
CLASSES = {**dict.fromkeys(NIGHTLY_CLASSES, "nightly"), **dict.fromkeys(QUALIFYING_CLASSES, "qualifying")}
# Founder re-tier 2026-10-25: AWS ECS is the only GA cloud target; Lambda and EKS are Preview.
GA_CELLS = ("aws-ecs/redis-off", "aws-ecs/redis-on")
LAMBDA_PREVIEW_CELLS = ("aws-serverless/redis-off", "aws-serverless/redis-on")
JOURNEYS = {
    "deterministic-journey": ("deterministic", GA_CELLS),
    "nightly-model-journey": ("genuine-model", ("aws-ecs/redis-off",)),
    "genuine-model-journey": ("genuine-model", GA_CELLS),
}
MAX_FRESHNESS_DAYS = {**dict.fromkeys(NIGHTLY_CLASSES, 7), "genuine-model-journey": 7,
                      "update-rollback": 7, "cite": 14}
JOURNEY_CELL_CASES = [(name, cell) for name, (_, cells) in JOURNEYS.items() for cell in cells]


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _update_cell(cell, candidate_lock=None):
    actuator = {"aws-ecs/redis-off": "terraform-exact-apply", "aws-ecs/redis-on": "honua-aws-ecs-alb",
                "aws-serverless/redis-off": "honua-gitops-aws-lambda", "aws-serverless/redis-on": "honua-gitops-aws-lambda"}[cell]
    result = {"cell": cell, "updateStatus": "pass", "rollbackStatus": "pass", "actuator": actuator,
            "fromDigest": "sha256:" + "a" * 64, "toDigest": LAMBDA_IMAGE if cell.startswith("aws-serverless/") else ECS_IMAGE,
            "schemaBoundary": {"status": "pass", "outcome": "serving", "schemaVersion": "128",
                               "imageDigest": "sha256:" + "a" * 64},
            "cleanup": {"status": "pass"}, "cost": {"status": "pass"}}
    result["prior"] = {"manifestDigest": "sha256:" + "a" * 64, "lockDigest": OTHER_LOCK,
                       "sourceRevision": "a" * 40, "schemaVersion": "128", "imageDigest": result["fromDigest"],
                       "lambdaImageDigest": result["fromDigest"]}
    result["candidate"] = {"manifestDigest": "sha256:" + "b" * 64, "lockDigest": candidate_lock,
                           "sourceRevision": "b" * 40, "schemaVersion": "128", "imageDigest": ECS_IMAGE,
                           "lambdaImageDigest": LAMBDA_IMAGE}
    fixture = {"serviceId": "e2e", "layerId": 7, **dict.fromkeys(
        ("seedManifestSha256", "seedScriptSha256", "seedSqlSha256", "expectedFeaturesSha256"), "1" * 64)}
    result["observations"] = [{"phase": phase, "observedAt": i + 1, "sourceRevision": release["sourceRevision"],
        "native": {"kind": "lambda-alias" if cell.startswith("aws-serverless/") else "ecs-native-routing",
                   "imageDigest": image, "version": str(2 if phase == "candidate" else 1), "taskCount": 1},
        "schemaJournal": {"method": "live-bidirectional-migration-plan", "pendingScripts": 0, "extraScripts": 0,
                          "readyForCoordinatedDeploy": True, "candidateImageDigest": ECS_IMAGE},
        "servingFixture": copy.deepcopy(fixture)} for i, (phase, release, image) in enumerate([
            ("baseline", result["prior"], result["fromDigest"]), ("candidate", result["candidate"], result["toDigest"]),
            ("rollback", result["prior"], result["fromDigest"])])]
    if actuator == "terraform-exact-apply":
        result["updateOperation"] = {"actuator": actuator, "status": "applied", "planSha256": "sha256:" + "3" * 64, "imageDigest": result["toDigest"]}
        result["rollbackOperation"] = {"actuator": actuator, "status": "applied", "planSha256": "sha256:" + "4" * 64, "imageDigest": result["fromDigest"]}
    else:
        result["updateOperation"] = {"operationId": "op-1", "status": "Succeeded", "observedRevision": "2", "providerOperationIdSha256": "sha256:" + "3" * 64}
        result["rollbackOperation"] = {"operationId": "op-1", "status": "RolledBack", "observedRevision": "1", "providerOperationIdSha256": "sha256:" + "4" * 64}
    if cell.endswith("redis-off"):
        result.update(executionOutcome="refused", functionalGpExecuted=False, refusals=[{
            "status": "pass", "outcome": "refused", "functionalGpExecuted": False,
            "code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis"} for _ in range(3)])
    else:
        result.update(executionOutcome="succeeded", functionalGpExecuted=True, inFlightJob={
            "jobId": "gp-flight-1", "initialState": "running", "terminalStates": ["succeeded"], "attemptCount": 1,
            "executionEvents": {"started": 1, "completed": 1}, "output": {"sha256": "sha256:" + "1" * 64,
                "fixtureSha256": "sha256:" + "2" * 64, "oracle": "independent-authored-output"}})
        result["rollbackInFlightJob"] = copy.deepcopy(result["inFlightJob"])
        result["rollbackInFlightJob"]["jobId"] = "gp-flight-2"
        for name in ("jobBeforeUpdate", "jobAfterUpdate", "jobBeforeRollback", "jobAfterRollback"):
            result[name] = {"jobId": "gp-flight-2" if "Rollback" in name else "gp-flight-1", "state": "running", "attemptCount": 1}
    return result


def _fixture(tmp_path, *, age=54, minted=None):
    """A complete record: minted `minted` hours ago, burning for `age` hours, canaries every
    six hours from the first slot after burn start through now."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock = tmp_path / "platform-lock.json"
    manifest = {"components": {"honua-server": {"digest": ECS_IMAGE, "awsLambdaEcrDigest": LAMBDA_IMAGE,
                                              "awsLambdaDigest": "sha256:" + "d" * 64}}}
    manifest_bytes = yaml.safe_dump(manifest).encode("utf-8")
    _write(lock, {"lockVersion": "platform-lock.v1",
                  "sourceInputs": {"platformManifest": {"sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()}},
                  "components": {"honua-server": {"artifacts": [{"kind": "image", "digest": ECS_IMAGE}]}}})
    digest = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    burn = NOW - timedelta(hours=age)
    mint = NOW - timedelta(hours=age if minted is None else minted)
    evidence = tmp_path / "evidence"
    train = {"runId": "101", "completedAt": _stamp(mint), "status": "pass", "lockDigest": digest}
    report = {"overallStatus": "pass", "dry_run": False, "generatedAt": _stamp(mint),
              "candidate": {"train": {"runId": "101"}},
              "gates": [{"gate": gate, "status": "pass"} for gate in REQUIRED_RELEASE_GATES],
              "evidenceClasses": list(NIGHTLY_CLASSES)}
    root = evidence / "trains" / "101"
    root.mkdir(parents=True, exist_ok=True)
    (root / "platform-manifest.yaml").write_bytes(manifest_bytes)
    _write(root / "gate-report.json", report)
    _write(root / "run.json", {"updated_at": train["completedAt"], "status": "completed", "conclusion": "success"})
    (root / "platform-lock.json").write_bytes(lock.read_bytes())
    observed = []
    for slot in reversed(range((age - 1) // 6 + 1)):
        run_id = str(300 - slot)
        completed = NOW - timedelta(hours=6 * slot)
        row = {"runId": run_id, "completedAt": _stamp(completed), "status": "pass", "lockDigest": digest}
        observed.append(row)
        _write(evidence / "canaries" / run_id / "live-canary-evidence.json",
               {"runId": run_id, "status": "pass", "candidateLock": {"digest": digest}})
        _write(evidence / "canaries" / run_id / "run.json",
               {"updated_at": row["completedAt"], "status": "completed", "conclusion": "success"})
    canaries = copy.deepcopy(observed[-7:])
    _write(evidence / "canary-sequence.json", {"lockDigest": digest, "runs": observed})
    rows = []
    for index, (name, tier) in enumerate(CLASSES.items()):
        run_id = "101" if tier == "nightly" else str(401 + index)
        completed = mint if tier == "nightly" else burn + timedelta(hours=12)
        row = {"class": name, "runId": run_id, "completedAt": _stamp(completed), "status": "pass", "lockDigest": digest}
        rows.append(row)
        receipt = {**row, "kind": tier, "freshUntil": _stamp(completed + timedelta(days=7))}
        if name in JOURNEYS:
            mode, cells = JOURNEYS[name]
            receipt["cells"] = [{"cell": cell, "mode": mode, "attemptCount": 1,
                                 "attempts": [{"attempt": 1, "status": "pass", "lockDigest": digest,
                                               "completedAt": row["completedAt"],
                                               **({"profile": "redis-off-serving-refusal" if cell.endswith("/redis-off") else "full-workflow",
                                                   "executionOutcome": "refused" if cell.endswith("/redis-off") else "completed",
                                                   "functionalGpExecuted": not cell.endswith("/redis-off")} if mode == "genuine-model"
                                                   or cell.endswith("/redis-off") else {})}]} for cell in cells]
        if name == "update-rollback":
            receipt.update(cells=[_update_cell(cell, digest) for cell in GA_CELLS],
                           fromLockDigest=OTHER_LOCK, toLockDigest=digest,
                           updateStatus="pass", rollbackStatus="pass")
        root = evidence / "evidence" / name / run_id
        _write(root / "receipt.json", receipt)
        _write(root / "run.json", {"updated_at": row["completedAt"], "status": "completed", "conclusion": "success"})
    record = {"schemaVersion": "promotion-evidence.v1", "platformLabel": "2026.1-rc.3",
              "rcTrainRunId": "101", "lock": {"path": "platform-lock.json", "digest": digest,
              "burnStartedAt": _stamp(burn)},
              "strictTrains": [train], "demoCanaries": canaries,
              "evidenceClasses": dict(CLASSES), "evidence": rows}
    history = tmp_path / "lock-history.txt"
    history.write_text("", encoding="utf-8")
    return record, lock, evidence, history


def _decision(fixture, *, now=NOW):
    record, lock, evidence, history = fixture
    return readiness.evaluate(record, lock_path=lock, evidence_dir=evidence, lock_history=history, now=now)[0]


def _edit(path, change):
    value = json.loads(path.read_text())
    change(value)
    _write(path, value)


def _receipt(fixture, name):
    row = next(row for row in fixture[0]["evidence"] if row["class"] == name)
    return fixture[2] / "evidence" / name / row["runId"] / "receipt.json"


def _failed(fixture, check):
    decision = _decision(fixture)
    assert decision["status"] == "refused"
    assert decision["checks"][check]["status"] == "fail"


def test_complete_record_promotes_exact_minting_lock(tmp_path):
    fixture = _fixture(tmp_path)
    assert _decision(fixture)["status"] == "pass"
    assert _decision(fixture)["rcTrainRunId"] == "101"


@pytest.mark.parametrize("age", [48, 73, 96])
def test_promotion_from_hour_48_has_no_72_hour_deadline(tmp_path, age):
    assert _decision(_fixture(tmp_path, age=age))["status"] == "pass"


def test_new_trunk_commits_new_locks_and_other_lock_failures_do_not_reset_burn(tmp_path):
    fixture = _fixture(tmp_path, age=96)
    fixture[3].write_text("new trunk commit\nnew platform lock\nrevert to previous lock\n")
    _write(fixture[2] / "train-sequence.json", [{"workflow_runs": [{"id": 999, "conclusion": "failure"}]}])
    _edit(fixture[2] / "canary-sequence.json", lambda sequence: sequence["runs"].append(
        {"runId": "999", "lockDigest": OTHER_LOCK, "status": "fail", "completedAt": _stamp(NOW)}))
    assert _decision(fixture)["status"] == "pass"


def test_selected_lock_bytes_must_match(tmp_path):
    fixture = _fixture(tmp_path)
    fixture[1].write_text('{"another":"lock"}')
    _failed(fixture, "lock-digest")


def test_burn_under_48_hours_refused(tmp_path):
    _failed(_fixture(tmp_path, age=47), "burn-duration")


@pytest.mark.parametrize("mutation", ["missing", "extra", "dry-run", "failed-gate", "wrong-lock", "after-start", "metadata", "source"])
def test_minting_train_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    root = evidence / "trains" / "101"
    check = "minting-train"
    if mutation == "missing": record["strictTrains"] = []
    elif mutation == "extra": record["strictTrains"].append(copy.deepcopy(record["strictTrains"][0]))
    elif mutation == "dry-run": _edit(root / "gate-report.json", lambda r: r.update(dry_run=True))
    elif mutation == "failed-gate": _edit(root / "gate-report.json", lambda r: r["gates"][0].update(status="skipped"))
    elif mutation == "wrong-lock": (root / "platform-lock.json").write_text('{}')
    elif mutation == "after-start":
        stamp = _stamp(NOW - timedelta(hours=53))
        record["strictTrains"][0]["completedAt"] = stamp
        _edit(root / "run.json", lambda r: r.update(updated_at=stamp))
    elif mutation == "metadata": _edit(root / "run.json", lambda r: r.update(conclusion="failure"))
    elif mutation == "source": record["rcTrainRunId"] = "999"; check = "exact-rc"
    _failed(fixture, check)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong-lock", "receipt-other-lock", "failed", "cadence", "stale", "future", "omitted-failure", "missing-sequence"])
def test_canary_and_lock_failure_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    check = "demo-canaries"
    if mutation == "missing": record["demoCanaries"].pop()
    elif mutation == "duplicate": record["demoCanaries"][3] = copy.deepcopy(record["demoCanaries"][2])
    elif mutation == "wrong-lock": record["demoCanaries"][3]["lockDigest"] = OTHER_LOCK
    elif mutation == "receipt-other-lock":
        _edit(evidence / "canaries" / record["demoCanaries"][3]["runId"] / "live-canary-evidence.json",
              lambda r: r["candidateLock"].update(digest=OTHER_LOCK))
    elif mutation == "failed":
        _edit(evidence / "canaries" / record["demoCanaries"][3]["runId"] / "live-canary-evidence.json",
              lambda r: r.update(status="fail"))
    elif mutation in ("cadence", "stale", "future"):
        for index, row in enumerate(record["demoCanaries"]):
            shift = -7 if mutation == "stale" else (1 if mutation == "future" else (2 if index == 3 else 0))
            row["completedAt"] = _stamp(datetime.fromisoformat(row["completedAt"].replace("Z", "+00:00")) + timedelta(hours=shift))
            _edit(evidence / "canaries" / row["runId"] / "run.json", lambda r: r.update(updated_at=row["completedAt"]))
        _write(evidence / "canary-sequence.json", {"lockDigest": record["lock"]["digest"], "runs": record["demoCanaries"]})
    elif mutation == "omitted-failure":
        _edit(evidence / "canary-sequence.json", lambda r: r["runs"].insert(0,
              {"runId": "199", "completedAt": _stamp(NOW - timedelta(hours=48)), "status": "fail", "lockDigest": record["lock"]["digest"]}))
        check = "lock-burn-health"
    elif mutation == "missing-sequence": (evidence / "canary-sequence.json").unlink()
    _failed(fixture, check)


@pytest.mark.parametrize("name", list(CLASSES))
def test_every_required_class_is_required(tmp_path, name):
    fixture = _fixture(tmp_path)
    fixture[0]["evidence"] = [r for r in fixture[0]["evidence"] if r["class"] != name]
    _failed(fixture, "evidence-declarations")


@pytest.mark.parametrize("mutation", ["undeclared-receipt", "undeclared-train-class", "reclassified", "duplicate"])
def test_evidence_declaration_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    if mutation == "undeclared-receipt": record["evidence"].append({**record["evidence"][0], "class": "unreviewed"})
    elif mutation == "undeclared-train-class": _edit(evidence / "trains/101/gate-report.json", lambda r: r["evidenceClasses"].append("unreviewed"))
    elif mutation == "reclassified": record["evidenceClasses"]["cite"] = "nightly"
    elif mutation == "duplicate": record["evidence"].append(copy.deepcopy(record["evidence"][0]))
    _failed(fixture, "evidence-declarations")


@pytest.mark.parametrize("name", list(CLASSES))
def test_receipt_from_another_lock_is_refused_for_every_class(tmp_path, name):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, name), lambda r: r.update(lockDigest=OTHER_LOCK))
    _failed(fixture, f"evidence:{name}")


@pytest.mark.parametrize("mutation", ["missing", "failed", "stale", "no-bound", "before-burn", "wrong-run", "metadata", "over-14-days"])
def test_qualifying_receipt_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    path = _receipt(fixture, "cite")
    if mutation == "missing": path.unlink()
    elif mutation == "failed": _edit(path, lambda r: r.update(status="skipped"))
    elif mutation == "stale": _edit(path, lambda r: r.update(freshUntil=_stamp(NOW - timedelta(seconds=1))))
    elif mutation == "no-bound": _edit(path, lambda r: r.pop("freshUntil"))
    elif mutation == "before-burn":
        row = next(r for r in fixture[0]["evidence"] if r["class"] == "cite")
        row["completedAt"] = _stamp(NOW - timedelta(hours=55))
        _edit(path, lambda r: r.update(completedAt=row["completedAt"]))
        _edit(path.parent / "run.json", lambda r: r.update(updated_at=row["completedAt"]))
    elif mutation == "wrong-run": _edit(path, lambda r: r.update(runId="999"))
    elif mutation == "metadata": _edit(path.parent / "run.json", lambda r: r.update(conclusion="cancelled"))
    elif mutation == "over-14-days": _edit(path, lambda r: r.update(freshUntil=_stamp(NOW + timedelta(days=15))))
    _failed(fixture, "evidence:cite")


@pytest.mark.parametrize("name", list(JOURNEYS))
@pytest.mark.parametrize("mutation", ["missing-cell", "duplicate-cell", "preview-substitute", "wrong-mode", "third-attempt", "unrecorded-attempt", "unattributed-failure", "skipped", "wrong-lock", "stale-attempt", "gap"])
def test_journey_pass_rule_has_a_negative_per_rule_and_tier(tmp_path, name, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        cell = receipt["cells"][0]
        attempt = cell["attempts"][0]
        if mutation == "missing-cell": receipt["cells"].pop(0)
        elif mutation == "duplicate-cell": receipt["cells"].append(copy.deepcopy(cell))
        elif mutation == "preview-substitute": cell["cell"] = "aws-eks/redis-off"
        elif mutation == "wrong-mode": cell["mode"] = "replayed"
        elif mutation == "third-attempt":
            cell["attemptCount"] = 3
            cell["attempts"] = [{**attempt, "attempt": index} for index in (1, 2, 3)]
        elif mutation == "unrecorded-attempt": cell["attemptCount"] = 2
        elif mutation == "unattributed-failure":
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail"}, {**attempt, "attempt": 2}]
        elif mutation == "skipped": attempt["status"] = "skipped"
        elif mutation == "wrong-lock": attempt["lockDigest"] = OTHER_LOCK
        elif mutation == "stale-attempt": attempt["completedAt"] = _stamp(NOW - timedelta(days=8))
        elif mutation == "gap": attempt["attempt"] = 2
    _edit(_receipt(fixture, name), change)
    _failed(fixture, f"evidence:{name}")


@pytest.mark.parametrize("bound", ["first", "second", "both"])
def test_nightly_model_canary_attempt_bound_to_another_lock_does_not_count(tmp_path, bound):
    # Fix unit J3: each genuine-model canary attempt records the lock digest it ran against. A
    # receipt whose own lockDigest is right still fails when any attempt in its ledger ran
    # against another lock, including an attributed failure that a passing retry follows.
    fixture = _fixture(tmp_path)
    def change(receipt):
        cell = receipt["cells"][0]
        attempt = cell["attempts"][0]
        cell["attemptCount"] = 2
        cell["attempts"] = [{**attempt, "status": "fail", "failureAttribution": "model"}, {**attempt, "attempt": 2}]
        for index in {"first": (0,), "second": (1,), "both": (0, 1)}[bound]:
            cell["attempts"][index]["lockDigest"] = OTHER_LOCK
    _edit(_receipt(fixture, "nightly-model-journey"), change)
    _failed(fixture, "evidence:nightly-model-journey")


@pytest.mark.parametrize("attribution", ["model", "infrastructure"])
def test_second_attempt_pass_and_preview_failure_are_allowed(tmp_path, attribution):
    """Deterministic journeys still qualify on a passing last attempt. Preview cells do not count."""
    fixture = _fixture(tmp_path)
    def change(receipt):
        for cell in receipt["cells"]:
            attempt = cell["attempts"][0]
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail", "failureAttribution": attribution}, {**attempt, "attempt": 2}]
        receipt["cells"].append({"cell": "aws-eks/redis-off", "status": "fail"})
    _edit(_receipt(fixture, "deterministic-journey"), change)
    def preview(receipt):
        receipt["cells"].append({"cell": "aws-eks/redis-off", "status": "fail"})
    for name in ("nightly-model-journey", "genuine-model-journey"):
        _edit(_receipt(fixture, name), preview)
    assert _decision(fixture)["status"] == "pass"


@pytest.mark.parametrize("name", ["nightly-model-journey", "genuine-model-journey"])
@pytest.mark.parametrize("attribution", ["model", "infrastructure", "undetermined"])
def test_genuine_model_promotion_reads_only_the_first_attempt(tmp_path, name, attribution):
    fixture = _fixture(tmp_path)
    def change(receipt):
        for cell in receipt["cells"]:
            attempt = cell["attempts"][0]
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail", "failureAttribution": attribution,
                                 "executionOutcome": "completed", "functionalGpExecuted": True},
                                {**attempt, "attempt": 2}]
    _edit(_receipt(fixture, name), change)
    _failed(fixture, f"evidence:{name}")


def test_update_and_rollback_must_both_pass_all_ga_cells(tmp_path):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "update-rollback"), lambda r: r.update(rollbackStatus="fail"))
    _failed(fixture, "evidence:update-rollback")


def test_cli_refuses_missing_retained_evidence_and_writes_decision(tmp_path):
    fixture = _fixture(tmp_path)
    record, lock, evidence, _ = fixture
    (evidence / "canary-sequence.json").unlink()
    record_path, out = tmp_path / "record.json", tmp_path / "decision.json"
    _write(record_path, record)
    assert readiness.main(["--record", str(record_path), "--lock", str(lock), "--evidence-dir", str(evidence),
                           "--now", _stamp(NOW), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["status"] == "refused"


@pytest.mark.parametrize("label,accepted", [
    ("2026.1-rc.1", True), ("2026.1.0-rc.1", True), ("2026.1.2-rc.3", True),
    ("2026.1.0-rc.0", False), ("2026.1.0", False), ("2026.1.0-rc.latest", False),
    ("../2026.1-rc.1", False), ("2026.1.0.0-rc.1", False),
])
def test_promotion_path_schema_and_readiness_agree_on_patch_rc(tmp_path, label, accepted):
    root = Path(__file__).resolve().parents[1]
    record, lock, evidence, history = _fixture(tmp_path)
    record["platformLabel"] = label
    decision, failures = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                             lock_history=history, now=NOW)
    assert (decision["status"] == "pass") == accepted, failures
    schema = json.loads((root / "certification/promotion-evidence.v1.schema.json").read_text())
    assert Draft202012Validator(schema).is_valid(record) == accepted
    workflow = yaml.safe_load((root / ".github/workflows/promote.yml").read_text())
    command = next(s["run"] for s in workflow["jobs"]["promote"]["steps"]
                   if "canonical committed RC record path" in s.get("run", ""))
    # Execute the actual path admission condition without performing any GitHub calls.
    condition = next(line.strip().split(" ||", 1)[0] for line in command.splitlines()
                     if line.strip().startswith('[[ "$PROMOTION_RECORD"'))
    result = subprocess.run(["bash", "-c", condition], env={**os.environ,
                            "PROMOTION_RECORD": f"certification/promotions/{label}.json"})
    assert (result.returncode == 0) == accepted


@pytest.mark.parametrize("name", list(CLASSES))
def test_any_expired_evidence_closes_promotion_window(tmp_path, name):
    fixture = _fixture(tmp_path, age=96)
    _edit(_receipt(fixture, name), lambda receipt: receipt.update(freshUntil=_stamp(NOW - timedelta(seconds=1))))
    _failed(fixture, f"evidence:{name}")


def test_expiry_boundary_is_inclusive(tmp_path):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "cite"), lambda receipt: receipt.update(freshUntil=_stamp(NOW)))
    assert _decision(fixture)["status"] == "pass"


@pytest.mark.parametrize("mutation", ["missing-cell", "preview-substitute", "update-failed"])
def test_update_rollback_coverage_cannot_be_substituted(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        if mutation == "missing-cell": receipt["cells"].pop()
        elif mutation == "preview-substitute": receipt["cells"][0]["cell"] = "aws-eks/redis-off"
        else: receipt["updateStatus"] = "fail"
    _edit(_receipt(fixture, "update-rollback"), change)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("content", ['{"status":"fail","status":"pass"}', '{"status":NaN}', '[]'])
def test_ambiguous_receipt_json_is_refused(tmp_path, content):
    path = tmp_path / "receipt.json"
    path.write_text(content)
    with pytest.raises(readiness.ReadinessError):
        readiness._load(path, "receipt")


def test_cli_writes_exact_minting_run_and_digest_outputs(tmp_path):
    record, lock, evidence, _ = _fixture(tmp_path, age=96)
    record_path, out, github_output = tmp_path / "record.json", tmp_path / "decision.json", tmp_path / "github-output"
    _write(record_path, record)
    assert readiness.main(["--record", str(record_path), "--lock", str(lock), "--evidence-dir", str(evidence),
                           "--now", _stamp(NOW), "--out", str(out), "--github-output", str(github_output)]) == 0
    assert json.loads(out.read_text())["status"] == "pass"
    assert github_output.read_text() == f"rc_train_run_id=101\nlock_digest={record['lock']['digest']}\n"


def test_schema_rejects_unclassified_and_reclassified_required_classes(tmp_path):
    record, *_ = _fixture(tmp_path)
    schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    Draft202012Validator.check_schema(schema)
    assert validator.is_valid(record)
    record["evidenceClasses"]["cite"] = "nightly"
    assert not validator.is_valid(record)
    del record["evidenceClasses"]["cite"]
    assert not validator.is_valid(record)


def test_checker_policy_matches_the_written_r21_classification():
    assert readiness.EVIDENCE_CLASSES == CLASSES
    assert readiness.GA_CELLS == frozenset(GA_CELLS)
    assert {name: (mode, set(cells)) for name, (mode, cells) in readiness.JOURNEYS.items()} == {
        name: (mode, set(cells)) for name, (mode, cells) in JOURNEYS.items()}
    assert {name: bound.days for name, bound in readiness.MAX_FRESHNESS.items()} == MAX_FRESHNESS_DAYS
    assert all(bound.seconds == 0 for bound in readiness.MAX_FRESHNESS.values())


@pytest.mark.parametrize("name,cell", JOURNEY_CELL_CASES)
def test_every_required_journey_cell_is_required(tmp_path, name, cell):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, name), lambda receipt: receipt.update(
        cells=[row for row in receipt["cells"] if row["cell"] != cell]))
    _failed(fixture, f"evidence:{name}")


def _add_observation(fixture, hours_ago, *, status="fail", lock="selected", run_id="199"):
    record, _, evidence, _ = fixture
    run = {"runId": run_id, "completedAt": _stamp(NOW - timedelta(hours=hours_ago)), "status": status}
    if lock == "selected": run["lockDigest"] = record["lock"]["digest"]
    elif lock == "other": run["lockDigest"] = OTHER_LOCK
    elif lock == "null": run["lockDigest"] = None
    _edit(evidence / "canary-sequence.json", lambda sequence: sequence["runs"].insert(0, run))


# Finding: the lock is minted at T-60h, a canary of it fails at T-55h, and the record
# claims burnStartedAt = T-50h. The scan starts at minting, so the failure still counts.
def test_lock_failure_before_recorded_burn_start_still_ends_the_burn(tmp_path):
    fixture = _fixture(tmp_path, age=50, minted=60)
    assert _decision(fixture)["status"] == "pass"
    _add_observation(fixture, 55)
    _failed(fixture, "lock-burn-health")


@pytest.mark.parametrize("minted", [50, 96])
def test_lock_failure_any_time_after_minting_refuses(tmp_path, minted):
    fixture = _fixture(tmp_path, age=50, minted=minted)
    _add_observation(fixture, minted - 1)
    _failed(fixture, "lock-burn-health")


@pytest.mark.parametrize("status", ["fail", "cancelled", "incomplete"])
def test_unattributed_canary_failure_after_minting_refuses(tmp_path, status):
    fixture = _fixture(tmp_path, age=50, minted=60)
    _add_observation(fixture, 55, status=status, lock="null")
    _failed(fixture, "lock-burn-health")


def test_canary_failure_without_lock_field_is_unattributable(tmp_path):
    fixture = _fixture(tmp_path)
    _add_observation(fixture, 12, lock="absent")
    _failed(fixture, "lock-burn-health")


@pytest.mark.parametrize("lock,hours_ago,status", [
    ("null", 61, "fail"),       # unattributed, before this lock existed
    ("other", 55, "fail"),      # another lock's failure never ends this burn
    ("null", 55, "pass"),       # an unattributed pass is not a failure
])
def test_failures_that_cannot_belong_to_this_lock_do_not_end_its_burn(tmp_path, lock, hours_ago, status):
    fixture = _fixture(tmp_path, age=50, minted=60)
    _add_observation(fixture, hours_ago, status=status, lock=lock)
    assert _decision(fixture)["status"] == "pass"


@pytest.mark.parametrize("shift,accepted", [(timedelta(0), True), (timedelta(minutes=30), True),
                                            (timedelta(minutes=31), False), (timedelta(hours=12), False)])
def test_burn_start_cannot_precede_the_locks_first_canary_by_more_than_a_slot(tmp_path, shift, accepted):
    # The lock's first canary is at T-48h. A burn start more than one slot before it would
    # let the record claim a burn longer than the lock was actually deployed.
    fixture = _fixture(tmp_path, age=54, minted=70)
    first = NOW - timedelta(hours=48)
    fixture[0]["lock"]["burnStartedAt"] = _stamp(first - timedelta(hours=6) - shift)
    decision = _decision(fixture)
    assert (decision["status"] == "pass") == accepted
    assert decision["checks"]["burn-start"]["status"] == ("pass" if accepted else "fail")


@pytest.mark.parametrize("name", list(CLASSES))
def test_evidence_freshness_is_capped_by_class_policy(tmp_path, name):
    fixture = _fixture(tmp_path)
    row = next(row for row in fixture[0]["evidence"] if row["class"] == name)
    completed = datetime.fromisoformat(row["completedAt"][:-1] + "+00:00")
    limit = completed + timedelta(days=MAX_FRESHNESS_DAYS[name])
    _edit(_receipt(fixture, name), lambda receipt: receipt.update(freshUntil=_stamp(limit)))
    assert _decision(fixture)["status"] == "pass"
    _edit(_receipt(fixture, name), lambda receipt: receipt.update(freshUntil=_stamp(limit + timedelta(seconds=1))))
    _failed(fixture, f"evidence:{name}")


def test_nightly_security_receipt_cannot_hold_the_window_open(tmp_path):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "security"), lambda receipt: receipt.update(freshUntil="2099-01-01T00:00:00Z"))
    _failed(fixture, "evidence:security")


def test_declared_class_without_a_freshness_policy_refuses(tmp_path):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    row = {**record["evidence"][0], "class": "extra-scan"}
    record["evidence"].append(row)
    record["evidenceClasses"]["extra-scan"] = "nightly"
    _edit(evidence / "trains/101/gate-report.json", lambda report: report["evidenceClasses"].append("extra-scan"))
    root = evidence / "evidence/extra-scan/101"
    _write(root / "receipt.json", {**json.loads(_receipt(fixture, "build-test").read_text()), "class": "extra-scan"})
    (root / "run.json").write_text((evidence / "evidence/build-test/101/run.json").read_text())
    decision = _decision(fixture)
    assert decision["checks"]["evidence-declarations"]["status"] == "pass"
    assert decision["checks"]["evidence:extra-scan"]["status"] == "fail"


@pytest.mark.parametrize("mutation", ["dict-all-failed", "names-only", "cell-update-failed",
                                      "cell-rollback-failed", "cell-status-missing", "duplicate-cell",
                                      "extra-preview-cell", "extra-lambda-preview-cell"])
def test_update_rollback_status_is_checked_per_ga_cell(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        cells = receipt["cells"]
        if mutation == "dict-all-failed":
            receipt["cells"] = {cell: {"updateStatus": "fail", "rollbackStatus": "fail"} for cell in GA_CELLS}
        elif mutation == "names-only": receipt["cells"] = list(GA_CELLS)
        elif mutation == "cell-update-failed": cells[1]["updateStatus"] = "fail"
        elif mutation == "cell-rollback-failed": cells[0]["rollbackStatus"] = "skipped"
        elif mutation == "cell-status-missing": del cells[1]["rollbackStatus"]
        elif mutation == "duplicate-cell": cells.append(copy.deepcopy(cells[0]))
        elif mutation == "extra-preview-cell":
            cells.append({"cell": "aws-eks/redis-off", "updateStatus": "pass", "rollbackStatus": "pass"})
        elif mutation == "extra-lambda-preview-cell":
            cells.append(_update_cell("aws-serverless/redis-on"))
    _edit(_receipt(fixture, "update-rollback"), change)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("cell", GA_CELLS)
def test_update_rollback_requires_every_ga_cell(tmp_path, cell):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "update-rollback"), lambda receipt: receipt.update(
        cells=[row for row in receipt["cells"] if row["cell"] != cell]))
    _failed(fixture, "evidence:update-rollback")


def test_canary_ledger_matches_record_by_identity_not_json_shape(tmp_path):
    fixture = _fixture(tmp_path)
    def github_shape(sequence):
        for run in sequence["runs"]:
            run["runId"] = int(run["runId"])
            run["completedAt"] = run["completedAt"][:-1] + ".000Z"
            run.update(event="schedule", htmlUrl=f"https://github.com/honua-io/honua-release/actions/runs/{run['runId']}")
    _edit(fixture[2] / "canary-sequence.json", github_shape)
    assert _decision(fixture)["status"] == "pass"


@pytest.mark.parametrize("field,value", [("runId", 999), ("completedAt", "2026-10-02T11:00:00Z"),
                                         ("status", "fail"), ("lockDigest", OTHER_LOCK)])
def test_canary_ledger_identity_fields_still_have_to_match(tmp_path, field, value):
    fixture = _fixture(tmp_path)
    _edit(fixture[2] / "canary-sequence.json", lambda sequence: sequence["runs"][-1].update({field: value}))
    _failed(fixture, "demo-canaries")


def test_schema_no_longer_requires_a_burn_start_commit(tmp_path):
    record, *_ = _fixture(tmp_path)
    schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    assert "burnStartCommit" not in record["lock"]
    assert validator.is_valid(record)
    record["lock"]["burnStartCommit"] = "a" * 40
    assert not validator.is_valid(record)


@pytest.mark.parametrize('position', ['inside', 'before', 'after'])
def test_nightly_receipt_production_must_fall_within_the_minting_run(position, tmp_path):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    row = next(row for row in record['evidence'] if row['class'] == 'contract')
    finished = datetime.fromisoformat(row['completedAt'].replace('Z', '+00:00'))
    produced = finished - timedelta(minutes=1)
    row['completedAt'] = _stamp(produced)
    receipt = _receipt(fixture, 'contract')
    _edit(receipt, lambda value: value.update(completedAt=row['completedAt'],
                                            freshUntil=_stamp(produced + timedelta(days=7))))
    start = finished - timedelta(hours=1)
    end = finished
    if position == 'before':
        start = produced + timedelta(seconds=1)
    elif position == 'after':
        end = produced - timedelta(seconds=1)
    _edit(receipt.with_name('run.json'), lambda value: value.update(created_at=_stamp(start), updated_at=_stamp(end)))
    decision = _decision(fixture)
    assert decision['checks']['evidence:contract']['status'] == ('pass' if position == 'inside' else 'fail')


@pytest.mark.parametrize("cell_name", GA_CELLS)
@pytest.mark.parametrize("mutation", ["missing-actuator", "wrong-actuator", "same-image", "invalid-image",
                                     "missing-job", "empty-job", "not-in-flight", "nonterminal", "empty-terminal", "multiple-terminal",
                                     "missing-boundary", "wrong-boundary-image", "missing-schema", "unproved-refusal"])
def test_update_rollback_requires_actuator_transition_job_and_schema_proof(tmp_path, cell_name, mutation):
    fixture = _fixture(tmp_path)
    def mutate(receipt):
        cell = next(row for row in receipt["cells"] if row["cell"] == cell_name)
        if cell_name.endswith("redis-off") and mutation in {"missing-job", "empty-job", "not-in-flight", "nonterminal", "empty-terminal", "multiple-terminal"}:
            # These historical controls tried to claim execution on a serving-only topology.
            # Any such inFlightJob is now contradictory, including an empty/malformed record.
            cell["inFlightJob"] = {"jobId": " " if mutation == "empty-job" else "wrong-execution",
                                   "initialState": "succeeded" if mutation == "not-in-flight" else "running",
                                   "terminalStates": [] if mutation == "empty-terminal" else ["succeeded"]}
            return
        if mutation == "missing-actuator": del cell["actuator"]
        elif mutation == "wrong-actuator": cell["actuator"] = "fake-deploy"
        elif mutation == "same-image": cell["toDigest"] = cell["fromDigest"]
        elif mutation == "invalid-image": cell["fromDigest"] = "latest"
        elif mutation == "missing-job": del cell["inFlightJob"]
        elif mutation == "empty-job": cell["inFlightJob"]["jobId"] = " "
        elif mutation == "not-in-flight": cell["inFlightJob"]["initialState"] = "succeeded"
        elif mutation == "nonterminal": cell["inFlightJob"]["terminalStates"] = ["running"]
        elif mutation == "empty-terminal": cell["inFlightJob"]["terminalStates"] = []
        elif mutation == "multiple-terminal": cell["inFlightJob"]["terminalStates"] = ["succeeded", "succeeded"]
        elif mutation == "missing-boundary": del cell["schemaBoundary"]
        elif mutation == "wrong-boundary-image": cell["schemaBoundary"]["imageDigest"] = cell["toDigest"]
        elif mutation == "missing-schema": del cell["schemaBoundary"]["schemaVersion"]
        elif mutation == "unproved-refusal": cell["schemaBoundary"]["outcome"] = "refused-cleanly"
    _edit(_receipt(fixture, "update-rollback"), mutate)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("mutation", ["missing-from", "self-lock", "wrong-candidate", "invalid-from"])
def test_update_rollback_requires_two_distinct_candidate_bound_locks(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def mutate(receipt):
        if mutation == "missing-from": del receipt["fromLockDigest"]
        elif mutation == "self-lock": receipt["fromLockDigest"] = receipt["toLockDigest"]
        elif mutation == "wrong-candidate": receipt["toLockDigest"] = "sha256:" + "e" * 64
        else: receipt["fromLockDigest"] = "unattested-tag"
    _edit(_receipt(fixture, "update-rollback"), mutate)
    _failed(fixture, "evidence:update-rollback")


def test_update_rollback_clean_schema_refusal_is_explicit(tmp_path):
    fixture = _fixture(tmp_path)
    def mutate(receipt):
        receipt["cells"][0]["schemaBoundary"].update(outcome="refused-cleanly", reasonCode="schema-newer-than-runtime")
    _edit(_receipt(fixture, "update-rollback"), mutate)
    assert _decision(fixture)["status"] == "pass"


def test_update_rollback_receipt_schema_requires_every_structural_proof_field(tmp_path):
    fixture = _fixture(tmp_path)
    receipt = json.loads(_receipt(fixture, "update-rollback").read_text())
    schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
    validator = Draft202012Validator({"$ref": "#/$defs/updateRollbackReceipt", "$defs": schema["$defs"]})
    Draft202012Validator.check_schema(schema)
    assert validator.is_valid(receipt)
    for field in ("actuator", "fromDigest", "toDigest", "inFlightJob", "schemaBoundary", "prior", "candidate", "observations", "updateOperation", "rollbackOperation"):
        bad = copy.deepcopy(receipt)
        index = next(i for i, cell in enumerate(bad["cells"]) if field in cell)
        del bad["cells"][index][field]
        assert not validator.is_valid(bad), field
    bad = copy.deepcopy(receipt)
    bad["cells"][0]["actuator"] = "honua-gitops-aws-lambda"
    assert not validator.is_valid(bad)
    # Sibling inequality and cell uniqueness are semantic checks, not JSON Schema $data extensions.
    receipt["cells"][0]["toDigest"] = receipt["cells"][0]["fromDigest"]
    assert not readiness._update_rollback(receipt, {cell: _update_cell(cell)["toDigest"] for cell in GA_CELLS})


@pytest.mark.parametrize("cell_name", GA_CELLS)
@pytest.mark.parametrize("digest", ["sha256:" + "e" * 64, "sha256:" + "d" * 64])
def test_update_rollback_rejects_valid_but_unbound_target_images(tmp_path, cell_name, digest):
    fixture = _fixture(tmp_path)
    def change(receipt):
        cell = next(cell for cell in receipt["cells"] if cell["cell"] == cell_name)
        cell["toDigest"] = digest
    _edit(_receipt(fixture, "update-rollback"), change)
    _failed(fixture, "evidence:update-rollback")


def test_update_rollback_rejects_arbitrary_valid_pairs_on_all_cells(tmp_path):
    fixture = _fixture(tmp_path)
    def change(receipt):
        for cell in receipt["cells"]:
            cell.update(fromDigest="sha256:" + "e" * 64, toDigest="sha256:" + "f" * 64)
            cell["schemaBoundary"]["imageDigest"] = cell["fromDigest"]
    _edit(_receipt(fixture, "update-rollback"), change)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("mutation", ["missing-manifest", "tampered-manifest", "missing-binding", "missing-server", "wrong-lock-image"])
def test_update_rollback_requires_retained_candidate_image_inputs(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    manifest = fixture[2] / "trains/101/platform-manifest.yaml"
    if mutation == "missing-manifest":
        manifest.unlink()
    elif mutation == "tampered-manifest":
        manifest.write_text("components: {}\n")
    else:
        lock = json.loads(fixture[1].read_text())
        data = yaml.safe_load(manifest.read_text())
        if mutation == "missing-binding": del lock["sourceInputs"]
        elif mutation == "wrong-lock-image": lock["components"]["honua-server"]["artifacts"][0]["digest"] = OTHER_LOCK
        elif mutation == "missing-server": del data["components"]["honua-server"]
        elif mutation == "missing-mirror": del data["components"]["honua-server"]["awsLambdaEcrDigest"]
        elif mutation == "pending-mirror": data["components"]["honua-server"]["awsLambdaEcrDigest"] = "pending-ecr-mirror"
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        if "sourceInputs" in lock:
            lock["sourceInputs"]["platformManifest"]["sha256"] = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
        _write(fixture[1], lock)
    _failed(fixture, "evidence:update-rollback")


def _drop_lambda_mirror(fixture, value):
    manifest = fixture[2] / "trains/101/platform-manifest.yaml"
    lock = json.loads(fixture[1].read_text())
    data = yaml.safe_load(manifest.read_text())
    if value is None:
        del data["components"]["honua-server"]["awsLambdaEcrDigest"]
    else:
        data["components"]["honua-server"]["awsLambdaEcrDigest"] = value
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    lock["sourceInputs"]["platformManifest"]["sha256"] = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    _write(fixture[1], lock)


@pytest.mark.parametrize("mirror", [None, "pending-ecr-mirror"])
def test_lambda_preview_mirror_pin_does_not_gate_update_rollback(tmp_path, mirror):
    # Founder re-tier 2026-10-25: Lambda is Preview, so its mirror pin cannot block the ECS GA proof.
    fixture = _fixture(tmp_path)
    _drop_lambda_mirror(fixture, mirror)
    assert _decision(fixture)["checks"]["evidence:update-rollback"]["status"] == "pass"


@pytest.mark.parametrize("mirror", [None, "pending-ecr-mirror"])
def test_lambda_mirror_pin_is_required_again_if_lambda_returns_to_ga(tmp_path, monkeypatch, mirror):
    monkeypatch.setattr(readiness, "GA_CELLS", readiness.GA_CELLS | frozenset(LAMBDA_PREVIEW_CELLS))
    fixture = _fixture(tmp_path)
    _drop_lambda_mirror(fixture, mirror)
    with pytest.raises(readiness.ReadinessError, match="Lambda mirror pin"):
        readiness._update_rollback_target_images(fixture[1], fixture[2] / "trains/101/platform-manifest.yaml")


@pytest.mark.parametrize("journey", ["deterministic-journey", "genuine-model-journey"])
def test_lambda_preview_journey_cells_are_neither_required_nor_counted(tmp_path, journey):
    fixture = _fixture(tmp_path)
    receipt = json.loads(_receipt(fixture, journey).read_text())
    assert {row["cell"] for row in receipt["cells"]} == set(GA_CELLS)
    # A failing Lambda Preview row next to the green ECS cells does not change the verdict.
    lambda_row = copy.deepcopy(receipt["cells"][0])
    lambda_row["cell"] = "aws-serverless/redis-off"
    lambda_row["attempts"][-1]["status"] = "fail"
    _edit(_receipt(fixture, journey), lambda r: r["cells"].append(lambda_row))
    assert _decision(fixture)["checks"][f"evidence:{journey}"]["status"] == "pass"


@pytest.mark.parametrize("field", ["runId", "completedAt", "status", "kind", "freshUntil"])
def test_update_rollback_schema_requires_shared_receipt_envelope(tmp_path, field):
    fixture = _fixture(tmp_path)
    receipt = json.loads(_receipt(fixture, "update-rollback").read_text())
    schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
    validator = Draft202012Validator({"$ref": "#/$defs/updateRollbackReceipt", "$defs": schema["$defs"]})
    assert validator.is_valid(receipt)
    del receipt[field]
    assert not validator.is_valid(receipt)


@pytest.mark.parametrize("mutation", ["completed-gp", "missing-refusal", "wrong-dependency", "wrong-capability", "hidden-job", "empty-cost", "empty-cleanup"])
def test_update_rollback_off_requires_actual_nonexecution_profile(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def mutate(receipt):
        cell = next(c for c in receipt["cells"] if c["cell"].endswith("redis-off"))
        if mutation == "completed-gp": cell.update(executionOutcome="succeeded", functionalGpExecuted=True)
        elif mutation == "missing-refusal": cell["refusals"].pop()
        elif mutation == "wrong-dependency": cell["refusals"][1]["missingDependency"] = "database"
        elif mutation == "wrong-capability": cell["refusals"][2]["capability"] = "studio.maps"
        elif mutation == "hidden-job": cell["refusals"][0]["jobId"] = "accepted"
        elif mutation == "empty-cost": cell["cost"] = {}
        else: cell["cleanup"] = {}
    _edit(_receipt(fixture, "update-rollback"), mutate)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("mutation", ["before-completed", "after-completed", "different-job", "duplicate-start", "duplicate-terminal", "retry", "missing-oracle", "wrong-output"])
def test_update_rollback_on_requires_real_switch_and_unique_output_proof(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def mutate(receipt):
        cell = next(c for c in receipt["cells"] if c["cell"].endswith("redis-on"))
        if mutation == "before-completed": cell["jobBeforeUpdate"]["state"] = "succeeded"
        elif mutation == "after-completed": cell["jobAfterRollback"]["state"] = "succeeded"
        elif mutation == "different-job": cell["jobBeforeRollback"]["jobId"] = "other"
        elif mutation == "duplicate-start": cell["inFlightJob"]["executionEvents"]["started"] = 2
        elif mutation == "duplicate-terminal": cell["inFlightJob"]["executionEvents"]["completed"] = 2
        elif mutation == "retry": cell["inFlightJob"]["attemptCount"] = 2
        elif mutation == "missing-oracle": del cell["inFlightJob"]["output"]["oracle"]
        else: cell["inFlightJob"]["output"]["sha256"] = "self-reported"
    _edit(_receipt(fixture, "update-rollback"), mutate)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "retry", "different-job", "bad-output"])
def test_rollback_transition_needs_its_own_complete_execution_proof(mutation):
    import check_promotion_readiness as checks
    cells = [_update_cell(cell) for cell in checks.GA_CELLS]
    cell = next(c for c in cells if c["cell"] == "aws-ecs/redis-on")
    if mutation == "missing": del cell["rollbackInFlightJob"]
    elif mutation == "duplicate": cell["rollbackInFlightJob"]["executionEvents"]["completed"] = 2
    elif mutation == "retry": cell["rollbackInFlightJob"]["attemptCount"] = 2
    elif mutation == "different-job": cell["jobAfterRollback"]["jobId"] = "different"
    else: cell["rollbackInFlightJob"]["output"]["sha256"] = "declared"
    assert not checks._update_rollback_cells(cells, {c["cell"]: c["toDigest"] for c in cells})


@pytest.mark.parametrize('cell_name', GA_CELLS)
@pytest.mark.parametrize('mutation', ['missing-observations', 'short-observations', 'reordered', 'same-time', 'wrong-source',
    'wrong-image', 'wrong-fixture', 'missing-fixture-hash', 'wrong-journal', 'missing-update', 'missing-rollback',
    'status-only-update', 'status-only-rollback', 'wrong-operation', 'same-revision', 'wrong-release', 'same-source', 'missing-journal-image', 'nonfinite-time'])
def test_update_rollback_requires_retained_routing_and_concrete_actuator_proof(cell_name, mutation):
    cells = [_update_cell(name) for name in GA_CELLS]
    targets = {cell['cell']: cell['toDigest'] for cell in cells}
    assert readiness._update_rollback_cells(cells, targets)
    cell = next(c for c in cells if c['cell'] == cell_name)
    if mutation == 'missing-observations': del cell['observations']
    elif mutation == 'short-observations': cell['observations'].pop()
    elif mutation == 'reordered': cell['observations'].reverse()
    elif mutation == 'same-time': cell['observations'][1]['observedAt'] = cell['observations'][0]['observedAt']
    elif mutation == 'wrong-source': cell['observations'][1]['sourceRevision'] = 'c' * 40
    elif mutation == 'wrong-image': cell['observations'][1]['native']['imageDigest'] = cell['fromDigest']
    elif mutation == 'wrong-fixture': cell['observations'][2]['servingFixture']['seedManifestSha256'] = '2' * 64
    elif mutation == 'missing-fixture-hash': del cell['observations'][1]['servingFixture']['expectedFeaturesSha256']
    elif mutation == 'wrong-journal': cell['observations'][2]['schemaJournal']['pendingScripts'] = 1
    elif mutation == 'missing-update': del cell['updateOperation']
    elif mutation == 'missing-rollback': del cell['rollbackOperation']
    elif mutation == 'status-only-update': cell['updateOperation'] = {'status': 'Succeeded'}
    elif mutation == 'status-only-rollback': cell['rollbackOperation'] = {'status': 'RolledBack'}
    elif mutation == 'wrong-operation':
        cell['rollbackOperation']['planSha256' if cell_name == 'aws-ecs/redis-off' else 'operationId'] = 'other'
    elif mutation == 'same-revision':
        if cell_name == 'aws-ecs/redis-off': cell['rollbackOperation']['planSha256'] = cell['updateOperation']['planSha256']
        else: cell['rollbackOperation']['observedRevision'] = cell['updateOperation']['observedRevision']
    elif mutation == 'nonfinite-time': cell['observations'][1]['observedAt'] = float('nan')
    elif mutation == 'missing-journal-image':
        cell['candidate']['imageDigest'] = None
        for row in cell['observations']: row['schemaJournal']['candidateImageDigest'] = None
    elif mutation == 'wrong-release': cell['candidate']['lambdaImageDigest' if cell_name.startswith('aws-serverless/') else 'imageDigest'] = cell['fromDigest']
    else: cell['candidate']['sourceRevision'] = cell['prior']['sourceRevision']
    assert not readiness._update_rollback_cells(cells, targets), (cell_name, mutation)


def test_serving_refusal_is_excluded_from_gp_and_operate_credit(tmp_path):
    decision = _decision(_fixture(tmp_path))
    assert decision["status"] == "pass"
    credit = decision["gpOperateCredit"]
    assert credit["gp"] == ["aws-ecs/redis-on"]
    assert credit["operate"] == []
    assert "aws-ecs/redis-off" in credit["excluded"]
    assert "aws-ecs/redis-on" not in credit["excluded"]


def test_a_false_gp_claim_on_a_refusal_cell_is_refused(tmp_path):
    fixture = _fixture(tmp_path)
    def claim(receipt):
        for cell in receipt["cells"]:
            if cell["cell"].endswith("/redis-off"):
                cell["coversGp"] = True
    _edit(_receipt(fixture, "nightly-model-journey"), claim)
    decision = _decision(fixture)
    assert decision["checks"]["evidence:nightly-model-journey"]["status"] == "pass"
    assert decision["checks"]["gp-operate-credit"]["status"] == "fail"
    assert decision["status"] == "refused"


def test_serving_plus_a_refusal_does_not_become_operate_credit(tmp_path):
    fixture = _fixture(tmp_path)
    def claim(receipt):
        for cell in receipt["cells"]:
            if cell["cell"].endswith("/redis-on"):
                cell["coversOperate"] = True
    _edit(_receipt(fixture, "genuine-model-journey"), claim)
    decision = _decision(fixture)
    assert decision["checks"]["evidence:genuine-model-journey"]["status"] == "pass"
    assert decision["gpOperateCredit"]["operate"] == []
    _failed(fixture, "gp-operate-credit")
