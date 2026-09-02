import json
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
    env = {
        "name": name, "currentLockDigest": rollback.digest(b),
        "planes": [
            {"id": "serving-east", "kind": "serving", "providerId": "deploy/east", "lockPath": "/components/server/artifact", "current": lock_b["components"]["server"]["artifact"]},
            {"id": "serving-west", "kind": "serving", "providerId": "deploy/west", "lockPath": "/components/server/artifact", "current": lock_b["components"]["server"]["artifact"]},
            {"id": "worker-default", "kind": "worker", "providerId": "queue/default", "lockPath": "/components/worker/artifact", "current": lock_b["components"]["worker"]["artifact"]},
            {"id": "config", "kind": "config", "providerId": "projection/config", "lockPath": "/contentDigests/config", "current": lock_b["contentDigests"]["config"]},
            {"id": "capability", "kind": "capability", "providerId": "projection/capability", "lockPath": "/contentDigests/capability", "current": lock_b["contentDigests"]["capability"]},
        ],
        "schema": {"lockPath": "/schema"},
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
    result = _run(tmp_path, a, b, env, fail_plane="serving-west")
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
