"""ECS operate stage. Proposals run on the product executor or they fail closed.

Open product gaps, with the file:line evidence in PRODUCT-PATH.md:

- honua-server#6812 convergence cannot apply a release image on ECS or Lambda
- honua-server#6813 no cloud restore across a reader-incompatible migration
- honua-server#6814 no governed restart or workload-config executor
- honua-iac#351 the update/rollback controller cannot restore or act on a cert cell

EKS's native backend remains honua-iac#267. This module does not simulate any of them.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from ecs_fault import JournalFault
from ecs_observe import ObservationMissing
from product_ops import TOOL_NAMES, scrub_model_env

OPEN_PRODUCT_GAPS = (
    {"id": "convergence-image", "issue": "https://github.com/honua-io/honua-server/issues/6812",
     "scenarios": ("O01",)},
    {"id": "restore-142", "issue": "https://github.com/honua-io/honua-server/issues/6813",
     "scenarios": ("O05", "O08")},
    {"id": "restart-config", "issue": "https://github.com/honua-io/honua-server/issues/6814",
     "scenarios": ("O02",)},
    {"id": "controller-cert-cell", "issue": "https://github.com/honua-io/honua-iac/issues/351",
     "scenarios": ("O01", "O02", "O05", "O08")},
)

SCENARIOS = ("O01", "O02", "O05", "O08")
_READS = ("honua_ops_health", "honua_ops_findings", "honua_alert_events", "honua_operate_events")
_FORBIDDEN_CALLS = {
    "update_service", "register_task_definition", "create_db_snapshot",
    "restore_db_instance_from_db_snapshot", "put_secret_value", "delete_db_instance",
    "modify_rule", "update_alias", "publish_version", "force_new_deployment",
}
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"\A\S+@sha256:[0-9a-f]{64}\Z")
_REF = re.compile(r"\A[0-9a-f]{40}\Z")
_ARCH = {"arm64", "x86_64"}
_HERE = Path(__file__).resolve().parent


def require_ready() -> bool:
    """True only when every product gap is closed and this lane has no cloud write call."""
    # The model recording proxy and separate, credential-isolated approval job
    # are not wired. Removing product gaps alone must never enable this path.
    return False


def forbidden_calls() -> list[str]:
    found = []
    for name in ("product_ops.py", "ecs_observe.py", "ecs_fault.py", "ecs_probes.py", "ecs_stage.py"):
        tree = ast.parse((_HERE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _FORBIDDEN_CALLS:
                    found.append(f"{name}:{node.lineno}:{node.func.attr}")
    return found


def pins_ready(pins: dict) -> str | None:
    if not isinstance(pins, dict):
        return "pins"
    for name in ("iac_ref", "helm_ref"):
        if not _REF.fullmatch(str(pins.get(name) or "")):
            return f"{name} is not a 40-hex commit"
    image = str(pins.get("server_image") or "")
    if not _IMAGE.fullmatch(image):
        return "server_image is not digest-pinned"
    for name in ("ecs_architecture", "lambda_architecture"):
        if pins.get(name) not in _ARCH:
            return f"{name} is not arm64 or x86_64"
    newer = str(pins.get("newer_digest") or "")
    if not _SHA.fullmatch(newer):
        return "newer_digest is not sha256:64"
    if newer == image.rsplit("@", 1)[-1]:
        return "newer_digest matches the pinned server image"
    return None


def drive(scenario: str, *args, **kwargs) -> dict:
    """Refuse execution until a trusted recording proxy and isolated jobs are wired.

    A harness must never manufacture proposals or accept a model-supplied ledger.
    """
    if scenario not in SCENARIOS:
        raise ValueError("scenario")
    return {"scenario": scenario, "tools": [], "proposals": [], "executionIds": [],
            "configExecution": None, "why": "model recording proxy and isolated approval are not wired",
            "gaps": [gap["issue"] for gap in OPEN_PRODUCT_GAPS if scenario in gap["scenarios"]]}


def grade_attempt(scenario: str, product: dict, observations: dict, fault: dict | None) -> dict:
    if scenario == "O08" and not _fault_fired(fault):
        return {"status": "fail", "invalid": True, "why": "fault did not fire"}
    if scenario == "O02" and not product.get("configExecution"):
        return {"status": "fail", "why": product.get("why") or "no governed restart or config executor"}
    if scenario == "O05" and not product.get("restoreExecution"):
        return {"status": "fail", "why": product.get("why") or "no database restore executor"}
    executions = product.get("executionIds")
    if not isinstance(executions, list) or not executions:
        return {"status": "fail", "why": product.get("why") or "product executor did not run"}
    if not isinstance(observations, dict) or observations.get("server_readback") is not True:
        return {"status": "fail", "why": "observation missing"}
    if scenario == "O01":
        matched = (observations.get("image_digest") == observations.get("target_digest")
                   and observations.get("schema") == observations.get("target_schema")
                   and observations.get("customer_ok") is True)
        return {"status": "pass" if matched else "fail", "why": "" if matched else "live state did not converge"}
    if scenario == "O08":
        matched = (observations.get("image_digest") == observations.get("prior_digest")
                   and observations.get("schema") == observations.get("prior_schema")
                   and observations.get("customer_ok") is True)
        return {"status": "pass" if matched else "fail", "invalid": False,
                "why": "" if matched else "service did not return to the prior revision"}
    return {"status": "fail", "why": "scenario was not recovered by the product"}


def finish(scenario: str, product: dict, observations: dict, fault: dict | None) -> dict:
    graded = grade_attempt(scenario, product, observations, fault)
    if graded.get("status") == "pass" and not require_ready():
        return {**graded, "status": "fail", "blockedPass": True,
                "why": "product executors are not ready"}
    return graded


def observe_live(observer, schema, invariants) -> dict:
    try:
        digests = observer.task_digests()
        schema_version = schema()
        invariant = invariants()
    except (ObservationMissing, OSError, ValueError):
        return {"server_readback": False}
    if not isinstance(schema_version, int) or not isinstance(invariant, dict) or invariant.get("ok") is not True:
        return {"server_readback": False, **digests, "schema": schema_version}
    return {"server_readback": True, **digests, "schema": schema_version, "invariants": invariant}


def write_receipt(directory: Path, receipt: dict) -> Path:
    if receipt.get("status") == "pass" and not require_ready():
        raise RuntimeError("refusing a pass receipt while product executors are missing")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "operate-batch.json"
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return path


def cell_receipt(scenario: str, provision: dict, product: dict, observations: dict,
                 fault: dict | None = None) -> dict:
    graded = finish(scenario, product, observations, fault)
    return {
        "cell": provision.get("cell"),
        "runId": provision.get("runId"),
        "runAttempt": str(provision.get("runAttempt")),
        "scenario": scenario,
        "status": graded["status"],
        "why": graded.get("why") or product.get("why") or "",
        "productExecutions": list(product.get("executionIds") or []),
        "proposals": list(product.get("proposals") or []),
        "tools": list(product.get("tools") or []),
        "observations": {"serverReadback": observations.get("server_readback") is True},
        "fault": fault,
        "gaps": list(product.get("gaps") or []),
        "invalid": graded.get("invalid") is True,
    }


def model_environment(source: dict) -> dict:
    return scrub_model_env(source)


def _fault_fired(fault: dict | None) -> bool:
    return bool(isinstance(fault, dict) and fault.get("fired") is True
                and fault.get("upgrade_started") is True and fault.get("interrupted") is True
                and isinstance(fault.get("evidence_id"), str)
                and fault["evidence_id"])


def _finding_id(findings: dict | None) -> str | None:
    if not isinstance(findings, dict):
        return None
    items = findings.get("findings")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    identifier = items[0].get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def _check(pins: dict | None) -> int:
    problems = []
    if not require_ready():
        problems.append("product executors are missing")
        problems.extend(gap["issue"] for gap in OPEN_PRODUCT_GAPS)
    problems.extend(forbidden_calls())
    missing = [name for name in TOOL_NAMES if name not in (_HERE / "product_ops.py").read_text(encoding="utf-8")]
    if missing:
        problems.append("mcp tools missing from the client")
    if pins is not None:
        pin_error = pins_ready(pins)
        if pin_error:
            problems.append(pin_error)
    if problems:
        print("\n".join(problems))
        return 2
    return 0


def _cell_block() -> str:
    """Distinct principals are necessary. They do not make a missing executor real."""
    return "model recording proxy and isolated approval are not wired"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ECS product-path operate stage")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--cell", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--cell-dir", type=Path)
    parser.add_argument("--iac-ref", default=None)
    parser.add_argument("--helm-ref", default=None)
    parser.add_argument("--server-image", default=None)
    parser.add_argument("--ecs-architecture", default=None)
    parser.add_argument("--lambda-architecture", default=None)
    parser.add_argument("--newer-digest", default=None)
    args = parser.parse_args(argv)
    pins = None
    if any(value is not None for value in (
            args.iac_ref, args.helm_ref, args.server_image, args.ecs_architecture,
            args.lambda_architecture, args.newer_digest)):
        pins = {
            "iac_ref": args.iac_ref, "helm_ref": args.helm_ref, "server_image": args.server_image,
            "ecs_architecture": args.ecs_architecture, "lambda_architecture": args.lambda_architecture,
            "newer_digest": args.newer_digest,
        }
    if args.check:
        return _check(pins)
    if not args.cell or not args.scenario:
        return 2
    directory = args.cell_dir
    provision_path = directory / "provision.json" if directory else None
    provision = {}
    if provision_path and provision_path.is_file():
        provision = json.loads(provision_path.read_text(encoding="utf-8"))
    product = {"executionIds": [], "proposals": [], "tools": [], "why": _cell_block(),
               "gaps": [gap["issue"] for gap in OPEN_PRODUCT_GAPS if args.scenario in gap["scenarios"]]}
    receipt = cell_receipt(args.scenario or "O01", provision if isinstance(provision, dict) else {},
                           product, {"server_readback": False})
    if directory:
        write_receipt(directory, receipt)
    print(receipt["why"])
    return 1


def arm_fault(session, evidence_id: str, target_pid: int, read_migration_state) -> dict:
    """Used by a cell run that already holds a database session. Not called while gaps block --check."""
    fault = JournalFault(session, evidence_id, target_pid, read_migration_state)
    fault.arm()
    return fault.wait_and_fire()


if __name__ == "__main__":
    sys.exit(main())
