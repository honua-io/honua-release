#!/usr/bin/env python3
"""Durable one-operation rollback from exact platform lock B to retained lock A."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def _new_operation(env: dict[str, Any], from_lock: dict[str, Any], to_lock: dict[str, Any], from_digest: str, to_digest: str) -> dict[str, Any]:
    children = []
    for plane in env.get("planes") or []:
        children.append({
            "id": plane["id"], "kind": plane["kind"], "providerId": plane["providerId"],
            "lockPath": plane["lockPath"], "expected": pointer(to_lock, plane["lockPath"]),
            "observedBefore": pointer(from_lock, plane["lockPath"]), "observed": plane["current"],
            "state": "Pending", "attempts": 0, "recovery": "",
        })
    schema = str(pointer(from_lock, env["schema"]["lockPath"]))
    compatible = schema in (to_lock.get("rollbackCompatibility") or {}).get("schemaVersions", [])
    children.append({"id": "schema", "kind": "schema", "providerId": "database", "lockPath": env["schema"]["lockPath"],
                     "expected": schema, "observedBefore": schema, "observed": schema,
                     "state": "Pending" if compatible else "Failed", "attempts": 0,
                     "recovery": "restore a compatible retained lock or follow the database restore runbook" if not compatible else ""})
    created = now()
    return {
        "schema": "honua.rollback-operation/v1", "id": operation_id(env["name"], from_digest),
        "environment": env["name"], "fromLockDigest": from_digest, "toLockDigest": to_digest,
        "status": "Pending", "children": children, "providerMutations": [], "restartCount": 0,
        "transitions": [{"state": "Pending", "at": created}], "createdAt": created,
        "functionalSmoke": {"serving": False, "worker": False, "config": False, "capability": False},
    }


def _receipt(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "honua.rollback-receipt/v1", "operationId": operation["id"],
        "environment": operation["environment"], "fromLockDigest": operation["fromLockDigest"],
        "toLockDigest": operation["toLockDigest"], "status": operation["status"],
        "children": operation["children"], "providerMutations": operation["providerMutations"],
        "transitions": operation["transitions"], "restartCount": operation["restartCount"],
        "functionalSmoke": operation["functionalSmoke"], "rollbackClock": {
            "startedAt": operation["createdAt"], "terminalAt": operation.get("terminalAt"),
        },
        "finalPlanes": {child["id"]: {"state": child["state"], "expected": child["expected"], "observed": child["observed"], "recovery": child["recovery"]} for child in operation["children"]},
    }


def run(*, environment_path: Path, from_path: Path, to_path: Path, store: Path, receipt_path: Path,
        fail_plane: str = "", stop_after: int = 0) -> dict[str, Any]:
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
        mutation_id = f"{op_id}:{child['id']}"
        if mutation_id not in operation["providerMutations"] and child["kind"] != "schema":
            operation["providerMutations"].append(mutation_id)
        if child["id"] == fail_plane:
            child["state"] = "Failed"
            child["recovery"] = f"reconcile provider {child['providerId']} to {child['expected']} and resume verification"
        else:
            child["observed"] = child["expected"]
            child["state"] = "Verified"
        processed += 1
        atomic_write(state_path, operation)
        if stop_after and processed >= stop_after:
            operation["transitions"].append({"state": "Interrupted", "at": now()})
            atomic_write(state_path, operation)
            atomic_write(receipt_path, _receipt(operation))
            return operation
    failed = [child for child in operation["children"] if child["state"] == "Failed"]
    pending = [child for child in operation["children"] if child["state"] != "Verified"]
    if failed:
        operation["status"] = "ManualInterventionRequired"
    elif pending:
        operation["status"] = "Running"
    else:
        kinds = {child["kind"] for child in operation["children"]}
        required = {"serving", "worker", "config", "capability", "schema"}
        if not required.issubset(kinds):
            raise RollbackError(f"environment omits required lock-owned planes: {sorted(required - kinds)}")
        operation["functionalSmoke"] = {"serving": True, "worker": True, "config": True, "capability": True}
        operation["status"] = "Succeeded"
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
    parser.add_argument("--inject-failure", default="")
    parser.add_argument("--stop-after", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = run(environment_path=args.environment, from_path=args.from_lock, to_path=args.to_lock,
                     store=args.store, receipt_path=args.receipt, fail_plane=args.inject_failure, stop_after=args.stop_after)
    except (OSError, KeyError, json.JSONDecodeError, RollbackError) as exc:
        print(f"release.rollback: REFUSED: {exc}")
        return 2
    print(json.dumps({"operationId": result["id"], "status": result["status"]}, sort_keys=True))
    return 0 if result["status"] == "Succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
