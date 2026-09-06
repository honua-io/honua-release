#!/usr/bin/env python3
"""Durable one-operation rollback from exact platform lock B to retained lock A."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RollbackError(ValueError):
    pass


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RollbackError(f"{path}: expected JSON object")
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pointer(value: Any, path: str) -> Any:
    current = value
    for part in path.strip("/").split("/"):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def operation_id(environment: str, from_digest: str) -> str:
    return "rollback-" + hashlib.sha256(f"{environment}\0{from_digest}".encode()).hexdigest()[:24]


def _provider(env: dict[str, Any], *arguments: str) -> dict[str, Any]:
    command = (env.get("provider") or {}).get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise RollbackError("environment provider.command must be a non-empty argv array")
    result = subprocess.run([*command, *arguments], text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RollbackError(f"provider command failed ({result.returncode}): {detail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RollbackError("provider command did not return JSON") from exc
    if not isinstance(value, dict):
        raise RollbackError("provider command returned a non-object")
    return value


def _new_operation(env: dict[str, Any], from_lock: dict[str, Any], to_lock: dict[str, Any], from_digest: str, to_digest: str) -> dict[str, Any]:
    children = []
    for plane in env.get("planes") or []:
        children.append({
            "id": plane["id"], "kind": plane["kind"], "providerId": plane["providerId"],
            "lockPath": plane["lockPath"], "expected": pointer(to_lock, plane["lockPath"]),
            "observedBefore": None, "observed": None, "state": "Pending", "attempts": 0,
            "recovery": "", "providerEvidence": [],
        })
    required = {"serving", "worker", "config", "capability"}
    kinds = {child["kind"] for child in children}
    if not required.issubset(kinds):
        raise RollbackError(f"environment omits required lock-owned planes: {sorted(required - kinds)}")
    schema_path = env["schema"]["lockPath"]
    forward_schema = str(pointer(from_lock, schema_path))
    compatible_versions = env["schema"].get("compatibleVersions")
    if compatible_versions is None:
        compatible_versions = (to_lock.get("rollbackCompatibility") or {}).get("schemaVersions", [])
    compatible = forward_schema in [str(value) for value in compatible_versions]
    children.append({
        "id": "schema", "kind": "schema", "providerId": env["schema"]["providerId"],
        "lockPath": schema_path, "expected": forward_schema, "observedBefore": None, "observed": None,
        "state": "Pending" if compatible else "Failed", "attempts": 0,
        "recovery": "restore a compatible retained lock or follow the database restore runbook" if not compatible else "",
        "providerEvidence": [], "compatibleWithTarget": compatible,
    })
    created = now()
    return {
        "schema": "honua.rollback-operation/v1", "id": operation_id(env["name"], from_digest),
        "environment": env["name"], "fromLockDigest": from_digest, "toLockDigest": to_digest,
        "sourceInputs": env.get("sourceInputs") or {}, "status": "Pending", "children": children,
        "providerMutations": [], "restartCount": 0, "transitions": [{"state": "Pending", "at": created}],
        "createdAt": created, "functionalSmoke": {kind: False for kind in required}, "functionalSmokeEvidence": {},
    }


def _receipt(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "honua.rollback-receipt/v1", "operationId": operation["id"],
        "environment": operation["environment"], "fromLockDigest": operation["fromLockDigest"],
        "toLockDigest": operation["toLockDigest"], "sourceInputs": operation["sourceInputs"],
        "status": operation["status"], "children": operation["children"],
        "providerMutations": operation["providerMutations"], "transitions": operation["transitions"],
        "restartCount": operation["restartCount"], "functionalSmoke": operation["functionalSmoke"],
        "functionalSmokeEvidence": operation["functionalSmokeEvidence"],
        "rollbackClock": {"startedAt": operation["createdAt"], "terminalAt": operation.get("terminalAt")},
        "finalPlanes": {child["id"]: {"state": child["state"], "expected": child["expected"], "observed": child["observed"], "recovery": child["recovery"]} for child in operation["children"]},
    }


def _fail_child(child: dict[str, Any], detail: str) -> None:
    child["state"] = "Failed"
    child["recovery"] = f"reconcile provider {child['providerId']} to {child['expected']} and resume verification: {detail}"


def run(*, environment_path: Path, from_path: Path, to_path: Path, store: Path, receipt_path: Path,
        stop_after: int = 0) -> dict[str, Any]:
    env, from_lock, to_lock = load(environment_path), load(from_path), load(to_path)
    from_digest, to_digest = digest(from_path), digest(to_path)
    if env.get("currentLockDigest") != from_digest:
        raise RollbackError("environment is not on the declared immutable from-lock B")
    op_id = operation_id(env["name"], from_digest)
    state_path = store / f"{op_id}.json"
    if state_path.exists():
        operation = load(state_path)
        if operation["fromLockDigest"] != from_digest or operation["toLockDigest"] != to_digest:
            raise RollbackError("existing rollback operation cannot be retargeted")
        if operation["status"] in ("Succeeded", "ManualInterventionRequired"):
            atomic_write(receipt_path, _receipt(operation))
            return operation
        operation["restartCount"] += 1
        operation["transitions"].append({"state": "Resumed", "at": now()})
    else:
        operation = _new_operation(env, from_lock, to_lock, from_digest, to_digest)
    operation["status"] = "Running"
    operation["transitions"].append({"state": "Running", "at": now()})
    processed = 0
    for child in operation["children"]:
        if child["state"] in ("Verified", "Failed"):
            continue
        child["attempts"] += 1
        try:
            before = _provider(env, "observe", "--provider-id", child["providerId"])
            child["observedBefore"] = before.get("observed")
            child["providerEvidence"].append({"action": "observe-before", **before})
            expected_before = pointer(from_lock, child["lockPath"])
            if child["observedBefore"] != expected_before:
                raise RollbackError(f"observed pre-mutation identity {child['observedBefore']!r} is not lock B {expected_before!r}")
            if child["kind"] != "schema":
                mutation_id = f"{op_id}:{child['id']}"
                mutation = _provider(env, "mutate", "--provider-id", child["providerId"],
                                     "--expected-json", json.dumps(child["expected"], separators=(",", ":")),
                                     "--mutation-id", mutation_id)
                if mutation.get("mutationId") != mutation_id or mutation.get("accepted") is not True:
                    raise RollbackError("provider did not acknowledge the exact mutation id")
                child["providerEvidence"].append({"action": "mutate", **mutation})
                if mutation_id not in operation["providerMutations"]:
                    operation["providerMutations"].append(mutation_id)
            observed = _provider(env, "observe", "--provider-id", child["providerId"])
            child["observed"] = observed.get("observed")
            child["providerEvidence"].append({"action": "observe-after", **observed})
            if child["observed"] != child["expected"]:
                raise RollbackError(f"observed identity {child['observed']!r} does not equal target {child['expected']!r}")
            child["state"] = "Verified"
        except RollbackError as exc:
            _fail_child(child, str(exc))
        processed += 1
        atomic_write(state_path, operation)
        if stop_after and processed >= stop_after:
            operation["transitions"].append({"state": "Interrupted", "at": now()})
            atomic_write(state_path, operation)
            atomic_write(receipt_path, _receipt(operation))
            return operation
    failed = [child for child in operation["children"] if child["state"] == "Failed"]
    pending = [child for child in operation["children"] if child["state"] != "Verified"]
    if not failed and not pending:
        for kind in ("serving", "worker", "config", "capability"):
            expected = {child["providerId"]: child["expected"] for child in operation["children"] if child["kind"] == kind}
            try:
                evidence = _provider(env, "probe", "--kind", kind, "--expected-json", json.dumps(expected, separators=(",", ":")))
                passed = evidence.get("ok") is True
            except RollbackError as exc:
                evidence, passed = {"ok": False, "error": str(exc)}, False
            operation["functionalSmoke"][kind] = passed
            operation["functionalSmokeEvidence"][kind] = evidence
            if not passed:
                for child in operation["children"]:
                    if child["kind"] == kind:
                        _fail_child(child, "bounded functional probe failed")
        failed = [child for child in operation["children"] if child["state"] == "Failed"]
    operation["status"] = "ManualInterventionRequired" if failed else ("Running" if pending else "Succeeded")
    if operation["status"] in ("Succeeded", "ManualInterventionRequired"):
        operation["terminalAt"] = now()
    operation["transitions"].append({"state": operation["status"], "at": now()})
    atomic_write(state_path, operation)
    atomic_write(receipt_path, _receipt(operation))
    return operation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--from-lock", type=Path, required=True)
    parser.add_argument("--to-lock", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = run(environment_path=args.environment, from_path=args.from_lock, to_path=args.to_lock,
                     store=args.store, receipt_path=args.receipt, stop_after=args.stop_after)
    except (OSError, KeyError, json.JSONDecodeError, RollbackError, subprocess.SubprocessError) as exc:
        print(f"release.rollback: REFUSED: {exc}")
        return 2
    print(json.dumps({"operationId": result["id"], "status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "Succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
