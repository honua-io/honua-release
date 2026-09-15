#!/usr/bin/env python3
"""Certify exact signed lock B to retained lock A against a local provider substrate."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import release_rollback as rollback  # noqa: E402


def write(path: Path, value: dict) -> Path:
    rollback.atomic_write(path, value)
    return path


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_path(lock: dict, component: str, kind: str, field: str) -> str:
    artifacts = lock["components"][component]["artifacts"]
    matches = [index for index, value in enumerate(artifacts) if value.get("kind") == kind]
    if len(matches) != 1:
        raise rollback.RollbackError(f"{component} must contain exactly one {kind} artifact")
    return f"/components/{component}/artifacts/{matches[0]}/{field}"


def verify_frozen_sources(candidate: dict, manifest: Path, matrix: Path) -> dict[str, str]:
    observed = {"platformManifest": file_digest(manifest), "compatibilityMatrix": file_digest(matrix)}
    declared = candidate.get("sourceInputs") or {}
    for name, digest in observed.items():
        if (declared.get(name) or {}).get("sha256") != digest:
            raise rollback.RollbackError(f"candidate lock does not bind exact {name} bytes")
    return observed


def environment(root: Path, name: str, a: dict, b: dict, a_path: Path, source_inputs: dict[str, str], fail_provider: str = "") -> Path:
    image_path = artifact_path(b, "honua-server", "image", "platformDigests/amd64")
    try:
        image_digest = rollback.pointer(b, image_path)
    except (KeyError, TypeError) as exc:
        raise rollback.RollbackError("ROLLBACK_CANDIDATE_AMD64_IMAGE_DIGEST_MISSING: candidate lock must retain the exact amd64 image identity") from exc
    if not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise rollback.RollbackError("ROLLBACK_CANDIDATE_AMD64_IMAGE_DIGEST_INVALID")
    schema_path = "/components/honua-server/schemaVersions/database"
    planes = [
        {"id": "serving-east", "kind": "serving", "providerId": "deploy/east", "lockPath": image_path},
        {"id": "serving-west", "kind": "serving", "providerId": "deploy/west", "lockPath": image_path},
        {"id": "worker-default", "kind": "worker", "providerId": "worker/default", "lockPath": image_path},
        {"id": "config", "kind": "config", "providerId": "projection/config", "lockPath": "/sourceInputs/platformManifest/sha256"},
        {"id": "capability", "kind": "capability", "providerId": "projection/capability", "lockPath": "/sourceInputs/compatibilityMatrix/sha256"},
    ]
    state = {"planes": {plane["providerId"]: {"kind": plane["kind"], "value": rollback.pointer(b, plane["lockPath"])} for plane in planes}, "mutations": {}}
    state["planes"]["database"] = {"kind": "schema", "value": str(rollback.pointer(b, schema_path))}
    state_path = write(root / f"{name}-provider-state.json", state)
    provider = Path(__file__).resolve().parent / "rollback_local_provider.py"
    command = [sys.executable, str(provider), "--state", str(state_path)]
    if fail_provider:
        command += ["--fail-provider", fail_provider]
    target_schema = str(rollback.pointer(a, schema_path))
    forward_schema = str(rollback.pointer(b, schema_path))
    compatible = [forward_schema] if target_schema == forward_schema else []
    value = {
        "name": name, "currentLockDigest": rollback.digest(root / "candidate-lock.json"),
        "planes": planes, "schema": {"lockPath": schema_path, "providerId": "database", "compatibleVersions": compatible},
        "provider": {"command": command},
        "sourceInputs": {**source_inputs, "retainedLock": rollback.digest(a_path)},
    }
    return write(root / f"{name}-environment.json", value)


def certify(args, report: dict) -> int:
    a, b = rollback.load(args.from_lock), rollback.load(args.to_lock)
    candidate_digest, target_digest = file_digest(args.to_lock), file_digest(args.from_lock)
    first = report.get("first_lock_bearing_release", False)
    if first:
        if (report.get("no_earlier_lock_exists") is not True
                or report.get("reason") != "no_earlier_attested_platform_lock_exists"
                or candidate_digest != target_digest):
            raise rollback.RollbackError("ROLLBACK_SELF_TARGET_MISMATCH")
    elif candidate_digest == target_digest:
        raise rollback.RollbackError("ROLLBACK_DISTINCT_RETAINED_LOCK_REQUIRED")
    if report.get("candidate_lock_digest", candidate_digest) != candidate_digest or report.get("rollback_target_digest", target_digest) != target_digest:
        raise rollback.RollbackError("ROLLBACK_RESOLVED_BYTES_CHANGED")
    report.update(candidate_lock_digest=candidate_digest, rollback_target_digest=target_digest)
    sources = verify_frozen_sources(b, args.candidate_manifest, args.compatibility_matrix)
    a_path, b_path = args.output / "retained-lock.json", args.output / "candidate-lock.json"
    a_path.write_bytes(args.from_lock.read_bytes())
    b_path.write_bytes(args.to_lock.read_bytes())
    success_env = environment(args.output, "success", a, b, a_path, sources)
    rollback.run(environment_path=success_env, from_path=b_path, to_path=a_path, store=args.output / "success-store",
                 receipt_path=args.output / "success-receipt.interrupted.json", stop_after=2)
    success = rollback.run(environment_path=success_env, from_path=b_path, to_path=a_path, store=args.output / "success-store",
                           receipt_path=args.output / "success-receipt.json")
    mixed_env = environment(args.output, "mixed", a, b, a_path, sources, fail_provider="deploy/west")
    mixed = rollback.run(environment_path=mixed_env, from_path=b_path, to_path=a_path, store=args.output / "mixed-store",
                         receipt_path=args.output / "mixed-state-receipt.json")
    if success["status"] != "Succeeded" or mixed["status"] != "ManualInterventionRequired":
        raise rollback.RollbackError("ROLLBACK_TERMINAL_STATE_MISMATCH")
    if success["restartCount"] != 1 or not all(success["functionalSmoke"].values()):
        raise rollback.RollbackError("ROLLBACK_RESTART_OR_SMOKE_FAILED")
    if first and not all(child["state"] == "Verified" and child["observed"] == rollback.pointer(a, child["lockPath"])
                         for child in success["children"]):
        raise rollback.RollbackError("ROLLBACK_POST_STATE_MISMATCH")
    report.update(overall_status="pass", post_rollback_state_equals_lock=True)
    write(args.output / "summary.json", {"successOperation": success["id"], "mixedOperation": mixed["id"], "success": "Succeeded", "negative": "ManualInterventionRequired", "sourceInputs": sources})
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--from-lock", type=Path, required=True, help="retained signed lock A")
    parser.add_argument("--to-lock", type=Path, required=True, help="frozen signed candidate lock B")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--compatibility-matrix", type=Path, required=True)
    parser.add_argument("--gate-report", type=Path, help="verified target resolution report")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "honua.rollback-gate/v1", "first_lock_bearing_release": False,
              "no_earlier_lock_exists": False, "overall_status": "fail"}
    try:
        if args.gate_report:
            report.update(rollback.load(args.gate_report))
            if report["overall_status"] != "pending":
                raise rollback.RollbackError("ROLLBACK_TARGET_NOT_RESOLVED")
        return certify(args, report)
    except (OSError, ValueError, KeyError) as exc:
        report.update(overall_status="fail", finding=f"ROLLBACK_CERTIFICATION_FAILED: {exc}")
        print(report["finding"])
        return 1
    finally:
        write(args.output / "gate-report.json", report)


if __name__ == "__main__":
    raise SystemExit(main())
