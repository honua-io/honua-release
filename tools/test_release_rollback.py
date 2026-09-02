import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import release_rollback as rollback  # noqa: E402


def _write(path: Path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path, compatible=True, name="prod"):
    lock_a = {
        "platform": {"id": "honua-2026.1-rc.1"},
        "components": {
            "server": {"artifact": "sha256:" + "a" * 64},
            "worker": {"artifact": "sha256:" + "b" * 64},
        },
        "contentDigests": {"config": "sha256:" + "c" * 64, "capability": "sha256:" + "d" * 64},
        "schema": "106", "rollbackCompatibility": {"schemaVersions": ["107"] if compatible else ["106"]},
    }
    lock_b = {
        "platform": {"id": "honua-2026.1-rc.2"},
        "components": {
            "server": {"artifact": "sha256:" + "e" * 64},
            "worker": {"artifact": "sha256:" + "f" * 64},
        },
        "contentDigests": {"config": "sha256:" + "1" * 64, "capability": "sha256:" + "2" * 64},
        "schema": "107", "rollbackCompatibility": {"schemaVersions": ["107"]},
    }
    a, b = _write(tmp_path / f"{name}-a.json", lock_a), _write(tmp_path / f"{name}-b.json", lock_b)
    planes = [
        {"id": "serving-east", "kind": "serving", "providerId": "deploy/east", "lockPath": "/components/server/artifact"},
        {"id": "serving-west", "kind": "serving", "providerId": "deploy/west", "lockPath": "/components/server/artifact"},
        {"id": "worker-default", "kind": "worker", "providerId": "queue/default", "lockPath": "/components/worker/artifact"},
        {"id": "config", "kind": "config", "providerId": "projection/config", "lockPath": "/contentDigests/config"},
        {"id": "capability", "kind": "capability", "providerId": "projection/capability", "lockPath": "/contentDigests/capability"},
    ]
    state = {"planes": {plane["providerId"]: {"kind": plane["kind"], "value": rollback.pointer(lock_b, plane["lockPath"])} for plane in planes}, "mutations": {}}
    state["planes"]["database"] = {"kind": "schema", "value": lock_b["schema"]}
    state_path = _write(tmp_path / f"{name}-provider.json", state)
    provider = Path(__file__).resolve().parent / "rollback_local_provider.py"
    env = {
        "name": name, "currentLockDigest": rollback.digest(b),
        "planes": planes,
        "schema": {"lockPath": "/schema", "providerId": "database", "compatibleVersions": ["107"] if compatible else ["106"]},
        "provider": {"command": [os.sys.executable, str(provider), "--state", str(state_path)]},
        "sourceInputs": {"candidateManifest": "sha256:" + "3" * 64, "compatibilityMatrix": "sha256:" + "4" * 64},
    }
    return a, b, _write(tmp_path / f"{name}-env.json", env)


def _run(tmp_path, a, b, env, **kwargs):
    return rollback.run(environment_path=env, from_path=b, to_path=a, store=tmp_path / "store",
                        receipt_path=tmp_path / "receipt.json", **kwargs)


def test_one_invocation_converges_every_lock_owned_plane(tmp_path):
    a, b, env = _fixture(tmp_path)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "Succeeded"
    assert {c["kind"] for c in result["children"]} == {"serving", "worker", "config", "capability", "schema"}
    assert all(c["state"] == "Verified" for c in result["children"])
    assert all(result["functionalSmoke"].values())


def test_controller_restart_resumes_same_parent_operation(tmp_path):
    a, b, env = _fixture(tmp_path)
    interrupted = _run(tmp_path, a, b, env, stop_after=2)
    assert interrupted["status"] == "Running"
    resumed = _run(tmp_path, a, b, env)
    assert resumed["id"] == interrupted["id"] and resumed["restartCount"] == 1
    assert resumed["status"] == "Succeeded"


def test_duplicate_invocation_folds_without_provider_mutations(tmp_path):
    a, b, env = _fixture(tmp_path)
    first = _run(tmp_path, a, b, env)
    duplicate = _run(tmp_path, a, b, env)
    assert duplicate["id"] == first["id"]
    assert duplicate["providerMutations"] == first["providerMutations"]


def test_existing_operation_cannot_be_retargeted(tmp_path):
    a, b, env = _fixture(tmp_path)
    _run(tmp_path, a, b, env, stop_after=1)
    changed = json.loads(a.read_text()); changed["components"]["server"]["artifact"] = "sha256:" + "9" * 64
    _write(a, changed)
    with pytest.raises(rollback.RollbackError, match="cannot be retargeted"):
        _run(tmp_path, a, b, env)


def test_target_failure_is_explicit_mixed_state_with_recovery(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text())
    value["provider"]["command"][4:4] = ["--fail-provider", "deploy/west"]
    _write(env, value)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    failed = next(c for c in result["children"] if c["id"] == "serving-west")
    assert failed["state"] == "Failed" and failed["recovery"]
    assert not all(result["functionalSmoke"].values())


def test_incompatible_forward_schema_never_claims_rollback(tmp_path):
    a, b, env = _fixture(tmp_path, compatible=False)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    assert next(c for c in result["children"] if c["id"] == "schema")["state"] == "Failed"


def test_exact_from_lock_bytes_are_required(tmp_path):
    a, b, env = _fixture(tmp_path)
    b.write_text(b.read_text() + "\n")
    with pytest.raises(rollback.RollbackError, match="not on the declared"):
        _run(tmp_path, a, b, env)


def test_missing_worker_plane_cannot_reach_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text()); value["planes"] = [p for p in value["planes"] if p["kind"] != "worker"]
    _write(env, value)
    with pytest.raises(rollback.RollbackError, match="worker"):
        _run(tmp_path, a, b, env)


def test_receipt_is_bound_to_both_exact_lock_digests(tmp_path):
    a, b, env = _fixture(tmp_path)
    _run(tmp_path, a, b, env)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["fromLockDigest"] == rollback.digest(b)
    assert receipt["toLockDigest"] == rollback.digest(a)
    assert receipt["rollbackClock"]["terminalAt"]
    assert receipt["sourceInputs"]["candidateManifest"].startswith("sha256:")


def test_provider_is_actuated_and_observed_before_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    result = _run(tmp_path, a, b, env)
    assert all(child["providerEvidence"] for child in result["children"])
    provider_state = json.loads((tmp_path / "prod-provider.json").read_text())
    assert len(provider_state["mutations"]) == 5
    assert all(evidence["ok"] for evidence in result["functionalSmokeEvidence"].values())


def test_failed_functional_probe_cannot_claim_success(tmp_path):
    a, b, env = _fixture(tmp_path)
    value = json.loads(env.read_text())
    value["provider"]["command"][4:4] = ["--fail-probe", "worker"]
    _write(env, value)
    result = _run(tmp_path, a, b, env)
    assert result["status"] == "ManualInterventionRequired"
    assert result["functionalSmoke"]["worker"] is False
    assert next(child for child in result["children"] if child["kind"] == "worker")["state"] == "Failed"


def test_certifier_consumes_exact_frozen_source_bytes(tmp_path):
    manifest = tmp_path / "platform-manifest.yaml"
    matrix = tmp_path / "compatibility-matrix.yaml"
    manifest.write_text("platformRelease: 2026.1\n", encoding="utf-8")
    matrix.write_text("contracts: {}\n", encoding="utf-8")

    def sha(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def exact_lock(image, schema):
        return {
            "sourceInputs": {
                "platformManifest": {"path": manifest.name, "sha256": sha(manifest)},
                "compatibilityMatrix": {"path": matrix.name, "sha256": sha(matrix)},
            },
            "components": {"honua-server": {
                "schemaVersions": {"database": schema},
                "artifacts": [{"kind": "image", "platformDigests": {"amd64": image}}],
            }},
        }

    retained = _write(tmp_path / "retained.json", exact_lock("sha256:" + "a" * 64, "107"))
    candidate = _write(tmp_path / "candidate.json", exact_lock("sha256:" + "b" * 64, "107"))
    output = tmp_path / "certification"
    script = Path(__file__).resolve().parent / "certify_release_rollback.py"
    result = subprocess.run([
        os.sys.executable, str(script), "--output", str(output), "--from-lock", str(retained),
        "--to-lock", str(candidate), "--candidate-manifest", str(manifest),
        "--compatibility-matrix", str(matrix),
    ], check=False, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads((output / "success-receipt.json").read_text())
    assert receipt["sourceInputs"]["platformManifest"] == sha(manifest)
    assert receipt["sourceInputs"]["compatibilityMatrix"] == sha(matrix)
