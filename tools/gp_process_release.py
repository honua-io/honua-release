#!/usr/bin/env python3
"""GP process release gate: locks, environment pins, deployment preflight, receipts (honua-release#616).

The GitHub Actions reference workflows (`gp-release-process.yml`, `gp-promote-process.yml`,
`gp-deploy-process.yml`, `gp-rollback-process.yml`) do no release reasoning in shell. Every
decision that can refuse an activation is taken here, from committed JSON, so it can be
self-tested (`tools/test_gp_process_release.py`) and so a refusal is a verdict, not a crash.

    python3 tools/gp_process_release.py validate-lock LOCK
    python3 tools/gp_process_release.py lock-digest LOCK
    python3 tools/gp_process_release.py validate-pin PIN [--locks-root .]
    python3 tools/gp_process_release.py preflight --lock LOCK --pin PIN --environment STATE \
        --artifact-index INDEX [--rollback-to LOCK] --out DECISION.json
    python3 tools/gp_process_release.py receipt --decision DECISION.json --operation deploy|rollback \
        --smoke pass|fail|skipped --smoke-evidence TEXT --binding activated|unchanged|pending-server-6221 \
        --actor ACTOR --run-repository R --run-id ID --run-attempt N --started-at TS --out RECEIPT.json
    python3 tools/gp_process_release.py validate-receipt RECEIPT

Exit codes: 0 valid / admissible, 1 invalid / refused, 2 usage. `preflight` always writes its
decision file (also on refusal) so the deploy workflow can record a receipt for a refused attempt.

Contract boundary (honua-server#6221): the lock is produced by `honua-gp-devkit release` and the
environment binding is activated through the server's binding API by gp_process_deploy.py, which
reads live versions and checks actual output bytes. This module handles pure validation and receipts;
committed environment state alone is not evidence of live activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = REPO_ROOT / "schemas"

LOCK_SCHEMA = "honua.gp-process-release-lock/v1"
PIN_SCHEMA = "honua.gp-environment-pin/v1"
STATE_SCHEMA = "honua.gp-environment-state/v1"
RECEIPT_SCHEMA = "honua.gp-deployment-receipt/v1"
DECISION_SCHEMA = "honua.gp-deployment-decision/v1"

SCHEMA_FILES = {
    LOCK_SCHEMA: "gp-process-release-lock.v1.schema.json",
    PIN_SCHEMA: "gp-environment-pin.v1.schema.json",
    STATE_SCHEMA: "gp-environment-state.v1.schema.json",
    RECEIPT_SCHEMA: "gp-deployment-receipt.v1.schema.json",
}

CHECK_NAMES = ("lockDigest", "artifactAvailability", "imageAvailability", "compatibility", "targets", "bindingVersion", "rollbackTarget")


class GateError(Exception):
    """A document failed validation; the message lists every finding."""


# ----------------------------------------------------------------------------- documents
def load_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def canonical_bytes(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(document: dict) -> str:
    """Digest of the canonical JSON form, so whitespace and key order cannot change identity."""
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def schema_errors(schema_id: str, document: dict) -> list[str]:
    import jsonschema  # validate job installs it; keep the import local so lock-digest stays stdlib-only

    schema = load_json(SCHEMAS / SCHEMA_FILES[schema_id])
    validator = jsonschema.Draft202012Validator(schema)
    findings = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        pointer = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        findings.append(f"{pointer}: {error.message}")
    return findings


def validate_document(schema_id: str, document: dict, label: str) -> None:
    findings = schema_errors(schema_id, document)
    if document.get("schema") != schema_id:
        findings.insert(0, f"$.schema: expected {schema_id!r}")
    if findings:
        raise GateError(f"{label} is not a valid {schema_id} document:\n  " + "\n  ".join(findings))


def validate_lock(lock: dict, label: str = "lock") -> None:
    validate_document(LOCK_SCHEMA, lock, label)
    findings = []
    compat = lock["compatibility"]["serverContractVersion"]
    if compat["minimum"] > compat["maximum"]:
        findings.append("$.compatibility.serverContractVersion: minimum exceeds maximum")
    if lock["runtimeImage"]["reference"].rsplit("@", 1)[-1] != lock["runtimeImage"]["digest"]:
        findings.append("$.runtimeImage: reference digest and digest disagree")
    names = [a["name"] for a in lock["artifacts"]]
    if len(names) != len(set(names)):
        findings.append("$.artifacts: duplicate artifact names")
    kinds = [t["kind"] for t in lock["targets"]]
    if len(kinds) != len(set(kinds)):
        findings.append("$.targets: a target kind may appear once")
    for index, target in enumerate(lock["targets"]):
        if target["kind"] == "aws-batch" and "jobDefinitionName" not in target:
            findings.append(f"$.targets[{index}]: aws-batch target needs jobDefinitionName")
        if target["kind"] == "kubernetes-job" and "chartVersion" not in target:
            findings.append(f"$.targets[{index}]: kubernetes-job target needs chartVersion")
    if findings:
        raise GateError(f"{label} is internally inconsistent:\n  " + "\n  ".join(findings))


def validate_pin(pin: dict, locks_root: Path, label: str = "pin") -> dict:
    """Validate a pin and prove its lockPath names a lock with the declared digest. Returns the lock."""
    validate_document(PIN_SCHEMA, pin, label)
    findings = []
    lock_path = locks_root / pin["release"]["lockPath"]
    if not lock_path.is_file():
        raise GateError(f"{label}: $.release.lockPath {pin['release']['lockPath']} does not exist")
    lock = load_json(lock_path)
    validate_lock(lock, f"{label} release lock")
    if digest(lock) != pin["release"]["lockDigest"]:
        findings.append(f"$.release.lockDigest: declared {pin['release']['lockDigest']} but the lock at lockPath digests to {digest(lock)}")
    if lock["process"]["name"] != pin["process"]:
        findings.append(f"$.process: pin names {pin['process']!r} but the lock is for {lock['process']['name']!r}")
    if pin["previous"] is not None:
        previous_path = locks_root / pin["previous"]["lockPath"]
        if not previous_path.is_file():
            findings.append(f"$.previous.lockPath: {pin['previous']['lockPath']} does not exist")
        else:
            previous = load_json(previous_path)
            if digest(previous) != pin["previous"]["lockDigest"]:
                findings.append("$.previous.lockDigest: does not match the lock at previous.lockPath")
            if pin["previous"]["lockDigest"] == pin["release"]["lockDigest"]:
                findings.append("$.previous: previous and release are the same lock")
    if findings:
        raise GateError(f"{label} does not bind to its locks:\n  " + "\n  ".join(findings))
    return lock


# ----------------------------------------------------------------------------- preflight
def _check(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail}


def preflight(lock: dict, pin: dict, state: dict, artifact_index: dict, rollback_to: dict | None = None) -> dict:
    """Decide whether activating `lock` in the environment described by `pin` + `state` is admissible.

    Every refusal is recorded as a failed check AND a machine-readable reason, so a receipt for a
    refused attempt explains itself. The rules mirror plan v2 section 4: digest-bound identity,
    artifact and image availability, server-contract compatibility, supported targets, optimistic
    binding version (stale concurrent attempt), and a rollback target that is exactly the pin's
    `previous` lock.
    """
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    lock_digest = digest(lock)
    expected = pin["release"]["lockDigest"] if rollback_to is None else (pin["previous"] or {}).get("lockDigest")
    if expected is None:
        checks["lockDigest"] = _check("fail", "pin has no previous release to roll back to")
        reasons.append("ROLLBACK_TARGET_MISSING")
    elif lock_digest != expected:
        checks["lockDigest"] = _check("fail", f"lock digests to {lock_digest}, pin expects {expected}")
        reasons.append("LOCK_DIGEST_MISMATCH")
    else:
        checks["lockDigest"] = _check("pass", lock_digest)

    available = set(artifact_index.get("artifacts", []))
    missing = [a["name"] for a in lock["artifacts"] if a["sha256"] not in available]
    if missing:
        checks["artifactAvailability"] = _check("fail", "unavailable: " + ", ".join(missing))
        reasons.extend(f"ARTIFACT_UNAVAILABLE:{name}" for name in missing)
    else:
        checks["artifactAvailability"] = _check("pass", f"{len(lock['artifacts'])} artifact digests present in the index")

    images = set(artifact_index.get("images", []))
    wanted = {lock["runtimeImage"]["digest"]} | {t["workerImageDigest"] for t in lock["targets"]}
    missing_images = sorted(wanted - images)
    if missing_images:
        checks["imageAvailability"] = _check("fail", "unavailable: " + ", ".join(missing_images))
        reasons.extend(f"IMAGE_UNAVAILABLE:{d}" for d in missing_images)
    else:
        checks["imageAvailability"] = _check("pass", f"{len(wanted)} image digests present in the index")

    compat = lock["compatibility"]["serverContractVersion"]
    version = state["serverContractVersion"]
    if compat["minimum"] <= version <= compat["maximum"]:
        checks["compatibility"] = _check("pass", f"server contract {version} within [{compat['minimum']}, {compat['maximum']}]")
    else:
        checks["compatibility"] = _check("fail", f"server contract {version} outside [{compat['minimum']}, {compat['maximum']}]")
        reasons.append("INCOMPATIBLE_SERVER_CONTRACT")

    unsupported = sorted({t["kind"] for t in lock["targets"]} - set(state["targets"]))
    if unsupported:
        checks["targets"] = _check("fail", "environment cannot execute: " + ", ".join(unsupported))
        reasons.extend(f"TARGET_UNSUPPORTED:{kind}" for kind in unsupported)
    else:
        checks["targets"] = _check("pass", "every lock target is executable in this environment")

    if pin["expectedBindingVersion"] != state["bindingVersion"]:
        checks["bindingVersion"] = _check("fail", f"pin expects binding version {pin['expectedBindingVersion']}, environment is at {state['bindingVersion']}")
        reasons.append("STALE_BINDING_VERSION")
    else:
        checks["bindingVersion"] = _check("pass", f"binding version {state['bindingVersion']} as expected")

    if rollback_to is None:
        checks["rollbackTarget"] = _check("skipped", "not a rollback")
    elif digest(rollback_to) != lock_digest:
        checks["rollbackTarget"] = _check("fail", "rollback target is not the lock being activated")
        reasons.append("ROLLBACK_TARGET_MISMATCH")
    elif pin["previous"] is None or pin["previous"]["lockDigest"] != lock_digest:
        checks["rollbackTarget"] = _check("fail", "rollback target is not the pin's previous release")
        reasons.append("ROLLBACK_TARGET_MISMATCH")
    else:
        checks["rollbackTarget"] = _check("pass", f"rolling back to the pin's previous release {lock_digest}")

    if pin["environment"] != state["environment"]:
        reasons.append("ENVIRONMENT_MISMATCH")
        checks["targets"] = _check("fail", f"pin is for {pin['environment']}, state describes {state['environment']}")

    ordered_reasons = sorted(set(reasons), key=reasons.index)
    return {
        "schema": DECISION_SCHEMA,
        "admissible": not ordered_reasons,
        "operation": "rollback" if rollback_to is not None else "deploy",
        "environment": pin["environment"],
        "process": lock["process"]["name"],
        "release": {
            "lockDigest": lock_digest,
            "version": lock["process"]["version"],
            "sourceSha": lock["source"]["sha"],
            "runtimeImageDigest": lock["runtimeImage"]["digest"],
            "workerImageDigests": sorted({t["workerImageDigest"] for t in lock["targets"]}),
            "artifactDigests": [a["sha256"] for a in lock["artifacts"]],
        },
        "previousRelease": (
            {"lockDigest": pin["release"]["lockDigest"]} if rollback_to is not None
            else ({"lockDigest": pin["previous"]["lockDigest"]} if pin["previous"] else None)
        ),
        "checks": {name: checks[name] for name in CHECK_NAMES},
        "refusalReasons": ordered_reasons,
    }


# ----------------------------------------------------------------------------- receipts
def build_receipt(decision: dict, *, operation: str, smoke: str, smoke_evidence: str, binding: str, binding_detail: str,
                  actor: str, run_repository: str, run_id: str, run_attempt: int, started_at: str, finished_at: str) -> dict:
    if operation != decision["operation"]:
        raise GateError(f"receipt operation {operation!r} disagrees with the decision's {decision['operation']!r}")
    if decision["admissible"]:
        if binding in {"unchanged", "unknown"} or smoke == "fail" or (binding == "activated" and smoke != "pass"):
            outcome = "failed"
        elif binding == "activated":
            outcome = "activated"
        else:
            outcome = "reference-only"
    else:
        outcome = "refused"
        if binding == "activated":
            raise GateError("a refused attempt cannot report an activated binding")
    return {
        "schema": RECEIPT_SCHEMA,
        "operation": operation,
        "environment": decision["environment"],
        "process": decision["process"],
        "release": decision["release"],
        "previousRelease": decision["previousRelease"],
        "checks": decision["checks"],
        "smoke": {"status": smoke, "evidence": smoke_evidence},
        "binding": {"status": binding, "detail": binding_detail},
        "outcome": outcome,
        "refusalReasons": decision["refusalReasons"],
        "runningJobsRetainRelease": True,
        "scheduleReconciliation": "pending-orchestration-integration" if binding == "activated" else ("pending-server-6221" if binding == "pending-server-6221" else "not-required"),
        "actor": actor,
        "run": {"repository": run_repository, "id": run_id, "attempt": run_attempt, "url": f"https://github.com/{run_repository}/actions/runs/{run_id}"},
        "startedAt": started_at,
        "finishedAt": finished_at,
    }


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- cli
def _write(path: str, document: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-lock"); p.add_argument("lock")
    p = sub.add_parser("lock-digest"); p.add_argument("lock")
    p = sub.add_parser("validate-pin"); p.add_argument("pin"); p.add_argument("--locks-root", default=str(REPO_ROOT))
    p = sub.add_parser("preflight")
    p.add_argument("--lock", required=True); p.add_argument("--pin", required=True); p.add_argument("--environment", required=True)
    p.add_argument("--artifact-index", required=True); p.add_argument("--rollback-to", default=None); p.add_argument("--out", required=True)
    p = sub.add_parser("receipt")
    p.add_argument("--decision", required=True); p.add_argument("--operation", required=True, choices=["deploy", "rollback"])
    p.add_argument("--smoke", required=True, choices=["pass", "fail", "skipped"]); p.add_argument("--smoke-evidence", default="")
    p.add_argument("--binding", required=True, choices=["activated", "unchanged", "unknown", "reference-only", "pending-server-6221"]); p.add_argument("--binding-detail", default="")
    p.add_argument("--actor", required=True); p.add_argument("--run-repository", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--run-attempt", type=int, default=1); p.add_argument("--started-at", default=None); p.add_argument("--out", required=True)
    p = sub.add_parser("validate-receipt"); p.add_argument("receipt")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-lock":
            validate_lock(load_json(args.lock), args.lock)
            print(f"ok: {args.lock} is a valid release lock ({digest(load_json(args.lock))})")
        elif args.command == "lock-digest":
            print(digest(load_json(args.lock)))
        elif args.command == "validate-pin":
            lock = validate_pin(load_json(args.pin), Path(args.locks_root), args.pin)
            print(f"ok: {args.pin} binds {lock['process']['name']} {lock['process']['version']} ({digest(lock)})")
        elif args.command == "preflight":
            lock, pin, state = load_json(args.lock), load_json(args.pin), load_json(args.environment)
            validate_lock(lock, args.lock)
            validate_document(PIN_SCHEMA, pin, args.pin)
            validate_document(STATE_SCHEMA, state, args.environment)
            rollback_to = load_json(args.rollback_to) if args.rollback_to else None
            if rollback_to is not None:
                validate_lock(rollback_to, args.rollback_to)
            decision = preflight(lock, pin, state, load_json(args.artifact_index), rollback_to)
            _write(args.out, decision)
            for name, check in decision["checks"].items():
                print(f"{check['status']:7} {name}: {check['detail']}")
            if not decision["admissible"]:
                print("REFUSED: " + ", ".join(decision["refusalReasons"]), file=sys.stderr)
                return 1
            print(f"admissible: {decision['operation']} {decision['process']} {decision['release']['version']} -> {decision['environment']}")
        elif args.command == "receipt":
            decision = load_json(args.decision)
            if decision.get("schema") != DECISION_SCHEMA:
                raise GateError(f"{args.decision} is not a {DECISION_SCHEMA} document")
            receipt = build_receipt(
                decision, operation=args.operation, smoke=args.smoke, smoke_evidence=args.smoke_evidence,
                binding=args.binding, binding_detail=args.binding_detail, actor=args.actor,
                run_repository=args.run_repository, run_id=args.run_id, run_attempt=args.run_attempt,
                started_at=args.started_at or now(), finished_at=now())
            validate_document(RECEIPT_SCHEMA, receipt, "receipt")
            _write(args.out, receipt)
            print(f"receipt: {receipt['outcome']} ({args.out})")
            return 0 if receipt["outcome"] in ("activated", "reference-only") else 1
        elif args.command == "validate-receipt":
            validate_document(RECEIPT_SCHEMA, load_json(args.receipt), args.receipt)
            print(f"ok: {args.receipt} is a valid deployment receipt")
    except GateError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
