#!/usr/bin/env python3
"""Certify exact signed lock B to retained lock A against a local provider substrate."""
from __future__ import annotations

import argparse
import hashlib
import json
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
    compatible = [str(rollback.pointer(b, schema_path))]
    value = {
        "name": name, "currentLockDigest": rollback.digest(root / "candidate-lock.json"),
        "planes": planes, "schema": {"lockPath": schema_path, "providerId": "database", "compatibleVersions": compatible},
        "provider": {"command": command},
        "sourceInputs": {**source_inputs, "retainedLock": rollback.digest(a_path)},
    }
    return write(root / f"{name}-environment.json", value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--from-lock", type=Path, required=True, help="retained signed lock A")
    parser.add_argument("--to-lock", type=Path, required=True, help="frozen signed candidate lock B")
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--compatibility-matrix", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    a, b = rollback.load(args.from_lock), rollback.load(args.to_lock)
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
        return 1
    write(args.output / "summary.json", {"successOperation": success["id"], "mixedOperation": mixed["id"], "success": "Succeeded", "negative": "ManualInterventionRequired", "sourceInputs": sources})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
