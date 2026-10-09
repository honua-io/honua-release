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

# Expectations are written out here rather than read from the module under test, so
# dropping a class, journey or cell from the checker fails these tests.
NIGHTLY_CLASSES = ("build-test", "contract", "sbom", "security", "upgrade", "capacity-soak", "dr",
                   "lambda-certification", "protocol-ledger", "deterministic-journey", "nightly-model-journey",
                   "executable-docs", "installed-clients")
QUALIFYING_CLASSES = ("genuine-model-journey", "update-rollback", "esri-bundle", "cite")
CLASSES = {**dict.fromkeys(NIGHTLY_CLASSES, "nightly"), **dict.fromkeys(QUALIFYING_CLASSES, "qualifying")}
GA_CELLS = ("aws-ecs/redis-off", "aws-ecs/redis-on", "aws-serverless/redis-off", "aws-serverless/redis-on")
JOURNEYS = {
    "deterministic-journey": ("deterministic", GA_CELLS),
    "nightly-model-journey": ("genuine-model", ("aws-ecs/redis-off",)),
    "genuine-model-journey": ("genuine-model", GA_CELLS),
}
MAX_FRESHNESS_DAYS = {**dict.fromkeys(NIGHTLY_CLASSES, 7), "genuine-model-journey": 7,
                      "update-rollback": 7, "esri-bundle": 14, "cite": 14}
JOURNEY_CELL_CASES = [(name, cell) for name, (_, cells) in JOURNEYS.items() for cell in cells]


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path, *, age=54, minted=None):
    """A complete record: minted `minted` hours ago, burning for `age` hours, canaries every
    six hours from the first slot after burn start through now."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock = tmp_path / "platform-lock.json"
    lock.write_text('{"lockVersion":"platform-lock.v1"}\n', encoding="utf-8")
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
                                               "completedAt": row["completedAt"]}]} for cell in cells]
        if name == "update-rollback":
            receipt.update(cells=[{"cell": cell, "updateStatus": "pass", "rollbackStatus": "pass"} for cell in GA_CELLS],
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
    fixture = _fixture(tmp_path)
    def change(receipt):
        for cell in receipt["cells"]:
            attempt = cell["attempts"][0]
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail", "failureAttribution": attribution}, {**attempt, "attempt": 2}]
        receipt["cells"].append({"cell": "aws-eks/redis-off", "status": "fail"})
    for name in JOURNEYS:
        _edit(_receipt(fixture, name), change)
    assert _decision(fixture)["status"] == "pass"


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
                                      "extra-preview-cell"])
def test_update_rollback_status_is_checked_per_ga_cell(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        cells = receipt["cells"]
        if mutation == "dict-all-failed":
            receipt["cells"] = {cell: {"updateStatus": "fail", "rollbackStatus": "fail"} for cell in GA_CELLS}
        elif mutation == "names-only": receipt["cells"] = list(GA_CELLS)
        elif mutation == "cell-update-failed": cells[1]["updateStatus"] = "fail"
        elif mutation == "cell-rollback-failed": cells[2]["rollbackStatus"] = "skipped"
        elif mutation == "cell-status-missing": del cells[3]["rollbackStatus"]
        elif mutation == "duplicate-cell": cells.append(copy.deepcopy(cells[0]))
        elif mutation == "extra-preview-cell":
            cells.append({"cell": "aws-eks/redis-off", "updateStatus": "pass", "rollbackStatus": "pass"})
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
