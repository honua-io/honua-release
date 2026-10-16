"""Self-test for the GP process release gate (honua-release#616): every refusal must be able to fire."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gp_process_release as gate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tools/fixtures/gp-process-release"
WORKFLOWS = REPO_ROOT / ".github/workflows"


def _load(name):
    return gate.load_json(FIXTURES / name)


@pytest.fixture
def docs():
    return {
        "v1": _load("lock-v1.json"), "v2": _load("lock-v2.json"), "incompatible": _load("lock-incompatible.json"),
        "batch": _load("lock-batch-only.json"), "index": _load("artifact-index.json"), "state": _load("environment-dev.json"),
        "pin": _load("pin-dev-v2.json"), "pin_stale": _load("pin-dev-stale.json"), "pin_first": _load("pin-dev-first.json"),
    }


# --------------------------------------------------------------------------- documents and digests
def test_committed_fixtures_and_release_files_validate(docs):
    for name in ("v1", "v2", "incompatible", "batch"):
        gate.validate_lock(docs[name], name)
    gate.validate_document(gate.STATE_SCHEMA, docs["state"], "state")
    for pin in ("pin", "pin_stale", "pin_first"):
        gate.validate_pin(docs[pin], REPO_ROOT, pin)
    # the committed reference environment is a real pin bound to committed locks
    gate.validate_pin(gate.load_json(REPO_ROOT / "release/gp-environments/dev/buffer-features.pin.json"), REPO_ROOT)
    gate.validate_document(gate.STATE_SCHEMA, gate.load_json(REPO_ROOT / "release/gp-environments/dev/environment.json"), "dev")


def test_digest_ignores_key_order_and_whitespace_but_not_content(docs):
    v2 = docs["v2"]
    reordered = json.loads(json.dumps(v2, sort_keys=False)[::-1][::-1])  # same content
    shuffled = {k: v2[k] for k in reversed(list(v2))}
    assert gate.digest(v2) == gate.digest(reordered) == gate.digest(shuffled)
    changed = copy.deepcopy(v2)
    changed["artifacts"][0]["sizeBytes"] += 1
    assert gate.digest(changed) != gate.digest(v2)


@pytest.mark.parametrize("mutate, finding", [
    (lambda d: d["compatibility"]["serverContractVersion"].update(minimum=5, maximum=4), "minimum exceeds maximum"),
    (lambda d: d["runtimeImage"].update(digest="sha256:" + "5" * 64), "reference digest and digest disagree"),
    (lambda d: d["artifacts"].append(dict(d["artifacts"][0])), "duplicate artifact names"),
    (lambda d: d["targets"][0].pop("chartVersion"), "kubernetes-job target needs chartVersion"),
    (lambda d: d["configurationReferences"].append({"name": "SECRET", "reference": "hunter2"}), "$.configurationReferences[1].reference"),
    (lambda d: d["source"].update(sha="abc"), "$.source.sha"),
    (lambda d: d.update(schema="honua.gp-process-release-lock/v0"), "$.schema"),
])
def test_lock_validation_refuses_each_defect(docs, mutate, finding):
    lock = copy.deepcopy(docs["v2"])
    mutate(lock)
    with pytest.raises(gate.GateError) as caught:
        gate.validate_lock(lock)
    assert finding in str(caught.value), str(caught.value)


def test_pin_must_name_a_lock_with_the_declared_digest(docs, tmp_path):
    pin = copy.deepcopy(docs["pin"])
    pin["release"]["lockDigest"] = "sha256:" + "0" * 64
    with pytest.raises(gate.GateError, match="lockDigest"):
        gate.validate_pin(pin, REPO_ROOT)
    pin = copy.deepcopy(docs["pin"])
    pin["previous"] = dict(pin["release"])
    with pytest.raises(gate.GateError, match="previous and release are the same lock"):
        gate.validate_pin(pin, REPO_ROOT)
    pin = copy.deepcopy(docs["pin"])
    pin["process"] = "other-process"
    with pytest.raises(gate.GateError, match=r"\$\.process"):
        gate.validate_pin(pin, REPO_ROOT)


# --------------------------------------------------------------------------- preflight
def test_preflight_admits_a_matching_available_compatible_release(docs):
    decision = gate.preflight(docs["v2"], docs["pin"], docs["state"], docs["index"])
    assert decision["admissible"] and decision["refusalReasons"] == []
    assert {c["status"] for n, c in decision["checks"].items() if n != "rollbackTarget"} == {"pass"}
    assert decision["checks"]["rollbackTarget"]["status"] == "skipped"
    assert decision["previousRelease"] == {"lockDigest": docs["pin"]["previous"]["lockDigest"]}
    assert decision["release"]["lockDigest"] == gate.digest(docs["v2"])


def test_preflight_refuses_a_lock_that_is_not_the_pinned_digest(docs):
    decision = gate.preflight(docs["v1"], docs["pin"], docs["state"], docs["index"])
    assert not decision["admissible"]
    assert "LOCK_DIGEST_MISMATCH" in decision["refusalReasons"]
    assert decision["checks"]["lockDigest"]["status"] == "fail"


def test_preflight_refuses_missing_artifacts_and_images(docs):
    index = {"artifacts": [], "images": []}
    decision = gate.preflight(docs["v2"], docs["pin"], docs["state"], index)
    assert not decision["admissible"]
    reasons = decision["refusalReasons"]
    assert any(r.startswith("ARTIFACT_UNAVAILABLE:buffer-features-1.1.0") for r in reasons)
    assert sum(r.startswith("IMAGE_UNAVAILABLE:") for r in reasons) == 2  # runtime image + worker image


def test_preflight_refuses_an_incompatible_server_contract(docs):
    pin = copy.deepcopy(docs["pin"])
    pin["release"] = {"lockPath": pin["release"]["lockPath"], "lockDigest": gate.digest(docs["incompatible"])}
    decision = gate.preflight(docs["incompatible"], pin, docs["state"], docs["index"])
    assert decision["refusalReasons"] == ["INCOMPATIBLE_SERVER_CONTRACT"]
    assert "outside [9, 9]" in decision["checks"]["compatibility"]["detail"]


def test_preflight_refuses_a_target_the_environment_cannot_execute(docs):
    pin = copy.deepcopy(docs["pin"])
    pin["release"]["lockDigest"] = gate.digest(docs["batch"])
    index = dict(docs["index"], artifacts=docs["index"]["artifacts"] + [docs["batch"]["artifacts"][0]["sha256"]],
                 images=docs["index"]["images"] + [docs["batch"]["runtimeImage"]["digest"], docs["batch"]["targets"][0]["workerImageDigest"]])
    decision = gate.preflight(docs["batch"], pin, docs["state"], index)
    assert decision["refusalReasons"] == ["TARGET_UNSUPPORTED:aws-batch"]


def test_preflight_refuses_a_stale_concurrent_attempt(docs):
    decision = gate.preflight(docs["v2"], docs["pin_stale"], docs["state"], docs["index"])
    assert decision["refusalReasons"] == ["STALE_BINDING_VERSION"]
    assert "environment is at 7" in decision["checks"]["bindingVersion"]["detail"]


def test_preflight_refuses_an_environment_mismatch(docs):
    state = dict(docs["state"], environment="stage")
    decision = gate.preflight(docs["v2"], docs["pin"], state, docs["index"])
    assert "ENVIRONMENT_MISMATCH" in decision["refusalReasons"]


def test_rollback_admits_only_the_pins_previous_release(docs):
    decision = gate.preflight(docs["v1"], docs["pin"], docs["state"], docs["index"], rollback_to=docs["v1"])
    assert decision["admissible"] and decision["operation"] == "rollback"
    assert decision["previousRelease"] == {"lockDigest": gate.digest(docs["v2"])}, "a rollback receipt names what it rolled back FROM"
    assert decision["checks"]["rollbackTarget"]["status"] == "pass"

    wrong = gate.preflight(docs["v2"], docs["pin"], docs["state"], docs["index"], rollback_to=docs["v2"])
    assert "LOCK_DIGEST_MISMATCH" in wrong["refusalReasons"] and "ROLLBACK_TARGET_MISMATCH" in wrong["refusalReasons"]

    first = gate.preflight(docs["v1"], docs["pin_first"], docs["state"], docs["index"], rollback_to=docs["v1"])
    assert "ROLLBACK_TARGET_MISSING" in first["refusalReasons"], "the first release of a process has nothing to roll back to"

    missing = gate.preflight(docs["v1"], docs["pin"], docs["state"], {"artifacts": [], "images": []}, rollback_to=docs["v1"])
    assert not missing["admissible"], "a rollback whose artifacts are gone is refused, not attempted"


# --------------------------------------------------------------------------- receipts
def _receipt(decision, **overrides):
    kwargs = dict(operation=decision["operation"], smoke="pass", smoke_evidence="run 1", binding="pending-server-6221",
                  binding_detail="reference", actor="mikemcdougall", run_repository="honua-io/honua-release", run_id="42",
                  run_attempt=1, started_at="2026-10-15T04:00:00Z", finished_at="2026-10-15T04:01:00Z")
    kwargs.update(overrides)
    receipt = gate.build_receipt(decision, **kwargs)
    gate.validate_document(gate.RECEIPT_SCHEMA, receipt, "receipt")
    return receipt


def test_receipts_record_every_outcome_truthfully(docs):
    admissible = gate.preflight(docs["v2"], docs["pin"], docs["state"], docs["index"])
    assert _receipt(admissible)["outcome"] == "reference-only"
    assert _receipt(admissible, binding="activated")["outcome"] == "activated"
    assert _receipt(admissible, binding="activated")["scheduleReconciliation"] == "pending-orchestration-integration"
    assert _receipt(admissible, smoke="fail", binding="unchanged")["outcome"] == "failed"
    refused = gate.preflight(docs["v2"], docs["pin_stale"], docs["state"], docs["index"])
    receipt = _receipt(refused, smoke="skipped", binding="unchanged")
    assert receipt["outcome"] == "refused" and receipt["refusalReasons"] == ["STALE_BINDING_VERSION"]
    assert receipt["runningJobsRetainRelease"] is True
    with pytest.raises(gate.GateError, match="refused attempt cannot report an activated binding"):
        _receipt(refused, binding="activated")
    with pytest.raises(gate.GateError, match="disagrees"):
        _receipt(admissible, operation="rollback")


# --------------------------------------------------------------------------- cli
def _cli(*args):
    return subprocess.run([sys.executable, str(REPO_ROOT / "tools/gp_process_release.py"), *args],
                          capture_output=True, text=True, cwd=REPO_ROOT)


def test_cli_preflight_writes_the_decision_even_when_it_refuses(tmp_path):
    out = tmp_path / "decision.json"
    result = _cli("preflight", "--lock", str(FIXTURES / "lock-v2.json"), "--pin", str(FIXTURES / "pin-dev-stale.json"),
                  "--environment", str(FIXTURES / "environment-dev.json"), "--artifact-index", str(FIXTURES / "artifact-index.json"),
                  "--out", str(out))
    assert result.returncode == 1 and "REFUSED: STALE_BINDING_VERSION" in result.stderr
    assert gate.load_json(out)["admissible"] is False

    receipt = tmp_path / "receipt.json"
    result = _cli("receipt", "--decision", str(out), "--operation", "deploy", "--smoke", "skipped", "--binding", "unchanged",
                  "--actor", "ci", "--run-repository", "honua-io/honua-release", "--run-id", "7", "--out", str(receipt))
    assert result.returncode == 1 and gate.load_json(receipt)["outcome"] == "refused"
    assert _cli("validate-receipt", str(receipt)).returncode == 0

    ok = _cli("preflight", "--lock", str(FIXTURES / "lock-v2.json"), "--pin", str(FIXTURES / "pin-dev-v2.json"),
              "--environment", str(FIXTURES / "environment-dev.json"), "--artifact-index", str(FIXTURES / "artifact-index.json"),
              "--out", str(out))
    assert ok.returncode == 0 and "admissible: deploy buffer-features 1.1.0 -> dev" in ok.stdout


def test_cli_validates_locks_pins_and_prints_digests():
    assert _cli("validate-lock", str(FIXTURES / "lock-v1.json")).returncode == 0
    assert _cli("lock-digest", str(FIXTURES / "lock-v1.json")).stdout.strip() == gate.digest(_load("lock-v1.json"))
    assert _cli("validate-pin", str(FIXTURES / "pin-dev-v2.json"), "--locks-root", str(REPO_ROOT)).returncode == 0
    broken = _cli("validate-lock", str(FIXTURES / "artifact-index.json"))
    assert broken.returncode == 1 and "::error::" in broken.stderr


# --------------------------------------------------------------------------- workflows
def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(workflow):
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            yield job, step


def test_deploy_is_serialized_per_environment_and_never_cancelled():
    deploy = _workflow("gp-deploy-process.yml")
    assert deploy["concurrency"]["cancel-in-progress"] is False
    assert "inputs.environment" in deploy["concurrency"]["group"] and "gp-deploy-" in deploy["concurrency"]["group"]
    triggers = deploy[True] if True in deploy else deploy["on"]
    assert set(triggers) == {"workflow_call", "workflow_dispatch", "push"}
    assert triggers["push"]["branches"] == ["trunk"]
    assert any(path.startswith("release/gp-environments/") for path in triggers["push"]["paths"])
    assert "gh variable set" not in (WORKFLOWS / "gp-deploy-process.yml").read_text(encoding="utf-8")


def test_deploy_preflights_and_records_a_receipt_on_every_path():
    text = (WORKFLOWS / "gp-deploy-process.yml").read_text(encoding="utf-8")
    deploy = _workflow("gp-deploy-process.yml")
    preflight = [s for _, s in _steps(deploy) if "gp_process_release.py preflight" in s.get("run", "")]
    receipt = [s for _, s in _steps(deploy) if "gate.build_receipt(" in s.get("run", "")]
    assert preflight and receipt
    assert all("continue-on-error" not in s for s in preflight)
    assert receipt[0].get("if") == "always()", "a refused or failed attempt still gets a receipt"
    assert "gp_process_deploy.py" in text
    assert "honua-gp-devkit test" not in text
    assert "register-job-definition" not in text
    assert "binding[\"status\"]" in text, "receipt records the actual activation outcome"
    upload = [s for _, s in _steps(deploy) if "upload-artifact" in s.get("uses", "") and "receipt" in str(s.get("with", {}).get("name", ""))]
    assert upload and upload[0].get("if") == "always()"
    attest = [s for _, s in _steps(deploy) if "attest-build-provenance" in s.get("uses", "")]
    assert attest, "the receipt is signed with the workflow's OIDC identity"


def test_rollback_reuses_the_deploy_path_with_the_previous_lock():
    rollback = _workflow("gp-rollback-process.yml")
    assert rollback["concurrency"]["cancel-in-progress"] is False
    deploy_calls = [job for job in rollback["jobs"].values() if job.get("uses", "").endswith("gp-deploy-process.yml")]
    assert deploy_calls and deploy_calls[0]["with"]["operation"] == "rollback"
    plan = [s for _, s in _steps(rollback) if "--rollback-to" in s.get("run", "")]
    assert plan, "the rollback target is checked against the pin's previous release before the deploy path runs"


def test_release_builds_once_and_publishes_an_attested_lock():
    release = _workflow("gp-release-process.yml")
    text = (WORKFLOWS / "gp-release-process.yml").read_text(encoding="utf-8")
    assert "honua-gp-devkit release" in text and "honua-server#6221" in text
    assert [s for _, s in _steps(release) if "gp_process_release.py validate-lock" in s.get("run", "")]
    assert [s for _, s in _steps(release) if "attest-build-provenance" in s.get("uses", "")]
    outputs = release[True]["workflow_call"]["outputs"] if True in release else release["on"]["workflow_call"]["outputs"]
    assert {"lock_digest", "artifact_name"} <= set(outputs)


def test_promote_carries_the_same_digest_into_a_pin_pull_request():
    text = (WORKFLOWS / "gp-promote-process.yml").read_text(encoding="utf-8")
    assert "gp_process_release.py validate-pin" in text and "gh pr create" in text
    assert "lock-digest" in text and "expectedBindingVersion" in text


def test_gp_workflows_pin_every_remote_action():
    import re
    for name in ("gp-release-process.yml", "gp-promote-process.yml", "gp-deploy-process.yml", "gp-rollback-process.yml"):
        for _, step in _steps(_workflow(name)):
            uses = step.get("uses", "")
            if uses and not uses.startswith("./"):
                assert re.search(r"@[0-9a-f]{40}$", uses), f"{name}: {uses} is not SHA-pinned"
