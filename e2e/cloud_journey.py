"""Cloud bindings for the owned terminal journey; no stage implementations live here."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "certification/terminal-journey"
EVIDENCE = ROOT / "e2e/cloud-evidence"
GA_CELLS = tuple(f"{target}/redis-{redis}" for target in ("aws-ecs", "aws-serverless")
                 for redis in ("off", "on"))
PREVIEW_TARGETS = ("aws-eks", "aws-mixed")


def drivers():
    # The owned modules use sibling imports. Load the runner under its own name, as the live
    # adapter imports it too; never copy stage logic or invoke a subprocess driver.
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    if "run" not in sys.modules or Path(sys.modules["run"].__file__) != HERE / "run.py":
        spec = importlib.util.spec_from_file_location("run", HERE / "run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["run"] = module
        spec.loader.exec_module(module)
    if "live_driver" not in sys.modules or Path(sys.modules["live_driver"].__file__) != HERE / "live_driver.py":
        spec = importlib.util.spec_from_file_location("live_driver", HERE / "live_driver.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["live_driver"] = module
        spec.loader.exec_module(module)
    return sys.modules["run"], sys.modules["live_driver"]


def manifest():
    return yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())


def candidate_digest():
    return hashlib.sha256((ROOT / "platform-manifest.yaml").read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cell_dir(cell):
    target, redis = cell.split("/")
    if target not in (*PREVIEW_TARGETS, "aws-ecs", "aws-serverless", "stub") or redis not in ("redis-on", "redis-off"):
        raise ValueError("invalid cloud cell")
    return EVIDENCE / os.environ.get("GITHUB_RUN_ID", "local") / os.environ.get("GITHUB_RUN_ATTEMPT", "1") / target / redis


@contextmanager
def admin_credential(value):
    previous = os.environ.get("HONUA_CLOUD_JOURNEY_ADMIN")
    os.environ["HONUA_CLOUD_JOURNEY_ADMIN"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HONUA_CLOUD_JOURNEY_ADMIN", None)
        else:
            os.environ["HONUA_CLOUD_JOURNEY_ADMIN"] = previous


def attempt(cell, number, endpoint, admin_key):
    driver, adapter = drivers()
    directory = cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    workdir = directory / f"work-{number}"
    contract = driver.load(HERE / "journey.v1.json")
    policy = driver.load(HERE / "control-plane-roster.v1.json")
    pinned = manifest()
    target = driver.load(adapter.DEFAULT_TARGET)
    target.update(id=cell, kind="aws-ecs" if cell.startswith("aws-ecs/") else "none")
    target["adminPassword"] = {"env": "HONUA_CLOUD_JOURNEY_ADMIN", "default": ""}
    target["compose"]["notes"] = "Externally provisioned cloud cell; the cloud harness owns teardown."
    target_path = directory / f"target-{number}.json"
    target_path.write_text(json.dumps(target) + "\n")
    notices = [f"Imported adapter protocol: {adapter.PROTOCOL}"]
    results = None
    workspace = driver.pins.ClientWorkspace(status="blocked", root=None,
                                            reason="cloud cell did not reach the driver")
    attribution = None
    try:
        if endpoint is not None:
            with admin_credential(admin_key):
                workspace, results, observed_notices, _ = driver.run_live(
                    target, pinned, contract, workdir, endpoint, True)
            notices.extend(observed_notices)
    except Exception as error:
        # Driver exceptions must still produce an attempt receipt, without retaining credentials
        # or raw tool output. The outer finally still checks cost and destroys infrastructure.
        notices.append(f"Imported journey driver raised {type(error).__name__}")
        attribution = "infrastructure"
    unsupported = target["kind"] == "none"
    if unsupported:
        notices.append("Owned receipt schema lacks this cloud kind and live evidence source; "
                       "this contract receipt cannot qualify the cell (honua-release#377).")
    build_only = results is None or unsupported
    if build_only:
        target["kind"] = "none"
    receipt = driver.build_receipt(
        manifest=pinned, journey=contract, roster=driver.roster_verdict(policy,
            driver.load(Path(os.environ["HONUA_CLOUD_REST_ROSTER"])) if os.environ.get("HONUA_CLOUD_REST_ROSTER") else None,
            driver.load(Path(os.environ["HONUA_CLOUD_MCP_ROSTER"])) if os.environ.get("HONUA_CLOUD_MCP_ROSTER") else None),
        evidence_uri=os.environ.get("HONUA_RUN_URL") or "urn:honua:cloud:" + cell,
        mode="build" if build_only else "live", target=target,
        target_path=target_path, target_base_url=endpoint, workspace=workspace,
        stage_results=None if build_only else results, notices=notices)
    # build_receipt currently labels every live observation Docker. Bind the ECS observation
    # to its actual source; unsupported cloud kinds retain a blocked build receipt above.
    if not build_only:
        for stage in receipt["stages"]:
            stage["evidence"]["source"] = "live-aws-ecs"
    receipt["target"]["composeProject"] = None
    driver.validate_receipt(receipt, HERE / "receipt.schema.json")
    path = directory / f"receipt-{number}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    return {"number": number, "runId": os.environ.get("GITHUB_RUN_ID", "local"),
            "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "cell": cell,
            "candidateDigest": candidate_digest(), "receipt": str(path.relative_to(ROOT / "e2e")),
            "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "failureAttribution": attribution or ("infrastructure" if receipt["status"] != "pass" else None)}


def validate_attempt(record, receipt, cell, *, run_id, run_attempt, at=None):
    driver, _ = drivers()
    driver.validate_receipt(receipt, HERE / "receipt.schema.json")
    contract = driver.load(HERE / "journey.v1.json")
    expected_stages = [(stage["number"], stage["id"], stage["command"]) for stage in contract["stages"]]
    actual_stages = [(stage["number"], stage["stage"], stage["command"]) for stage in receipt["stages"]]
    if actual_stages != expected_stages or receipt["evidenceKey"] != contract["evidenceKey"]:
        raise ValueError("cell receipt does not cover the pinned journey contract")
    if receipt["status"] == "pass" and receipt["roster"]["status"] != "pass":
        raise ValueError("passing receipt lacks authoritative candidate roster evidence")
    pinned = manifest()
    server = pinned["components"]["honua-server"]
    if (record.get("cell") != cell or receipt["target"]["id"] != cell
            or record.get("runId") != run_id or record.get("runAttempt") != run_attempt
            or record.get("candidateDigest") != candidate_digest()
            or receipt["release"] != pinned["platformRelease"]
            or receipt["clientArtifacts"] != driver.pins.receipt_pins(pinned)
            or receipt["server"] != {"sourceSha": server["sha"], "image": f"{server['image']}@{server['digest']}"}):
        raise ValueError("cell receipt is bound to the wrong candidate, run or cell")
    generated = datetime.fromisoformat(receipt["generatedAt"].replace("Z", "+00:00"))
    age = ((at or datetime.now(timezone.utc)) - generated).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError("stale cell receipt")
    if receipt["status"] != "pass":
        if record.get("failureAttribution") not in ("model", "infrastructure"):
            raise ValueError("failed attempt lacks failure attribution")
        return False
    if receipt["mode"] != "live" or receipt["target"]["kind"] != cell.split("/")[0]:
        raise ValueError("passing cell receipt lacks live cloud evidence")
    for stage in receipt["stages"]:
        evidence = stage["evidence"]
        if (evidence["source"] != "live-" + cell.split("/")[0]
                or evidence["freshness"] != "verified-current" or evidence["completeness"] != "complete"):
            raise ValueError("passing cell receipt lacks current complete cloud evidence")
        observed = datetime.fromisoformat(evidence["observedAt"].replace("Z", "+00:00"))
        if not 0 <= (generated - observed).total_seconds() <= 86400:
            raise ValueError("stale stage evidence")
    return True


def check_cost(path, ceiling, *, started_at):
    # A current run-scoped meter must supply cumulative USD before infrastructure is destroyed.
    # AWS Cost Explorer's delayed account totals cannot honestly serve as this meter.
    ceiling = Decimal(str(ceiling))
    if not ceiling.is_finite() or ceiling <= 0:
        raise ValueError("cost ceiling must be finite and positive")
    cost = json.loads(Path(path).read_text())
    if (cost.get("runId") != os.environ.get("GITHUB_RUN_ID", "local")
            or cost.get("runAttempt") != os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            or cost.get("currency") != "USD" or cost.get("scope") != "run"):
        raise ValueError("cost meter is not bound to this run")
    measured = datetime.fromisoformat(cost["measuredAt"].replace("Z", "+00:00"))
    if not started_at <= measured <= datetime.now(timezone.utc):
        raise ValueError("stale cost meter")
    amount = Decimal(str(cost["amount"]))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid cost amount")
    return {"status": "fail" if amount > ceiling else "pass", "amountUsd": str(amount),
            "ceilingUsd": str(ceiling), "measuredAt": cost["measuredAt"], "scope": "run",
            "runId": cost["runId"], "runAttempt": cost["runAttempt"],
            "candidateDigest": candidate_digest()}


def cleanup(cell):
    # run_live with an external URL never starts Compose or a second AWS deployment. Its local
    # registry/install workspace is the only additional resource it creates. Destroying the cell
    # removes its server/database contents as well. Backstop cleanup runs even without a receipt.
    directory = cell_dir(cell)
    for workdir in directory.glob("work-*"):
        if workdir.is_symlink():
            workdir.unlink()
        else:
            shutil.rmtree(workdir)


def aggregate(reports, reports_root, *, require_real, full_scope, run_id, run_attempt):
    cells = []
    failures = []
    by_cell = {}
    for report in reports:
        cell = report.get("cell", report.get("gate", "unknown"))
        preview = cell not in GA_CELLS
        row = {**report, "evidenceTier": "Preview" if preview else "GA"}
        cells.append(row)
        by_cell.setdefault(cell, []).append(row)
        # Preview journey failures are informational. The run-wide spend ceiling remains
        # independent of topology maturity, including a later snapshot from a Preview cell.
        cost = report.get("cost", {})
        if (preview and cost.get("scope") == "run" and cost.get("runId") == run_id
                and cost.get("runAttempt") == run_attempt and cost.get("status") == "fail"):
            failures.append(f"run cost ceiling exceeded during {cell}")
    if full_scope:
        failures.extend(f"full-scope cloud reports missing required cells: {cell}"
                        for cell in GA_CELLS if cell not in by_cell)
    for cell in GA_CELLS:
        rows = by_cell.get(cell, [])
        if not rows:
            continue
        if len(rows) != 1:
            failures.append(f"duplicate cell report: {cell}")
            continue
        report = rows[0]
        try:
            attempts = report.get("journeyAttempts", [])
            if not attempts or [a["number"] for a in attempts] != list(range(1, len(attempts) + 1)) or len(attempts) > 2:
                raise ValueError("missing or invalid attempt history")
            passed = False
            for record in attempts:
                # Uploaded files are loaded relative to the matching report's artifact directory.
                relative = Path(record["receipt"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe receipt path")
                path = Path(report["artifactDirectory"]) / relative
                if not path.resolve().is_relative_to(Path(reports_root).resolve()):
                    raise ValueError("receipt escapes artifact directory")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != record["receiptSha256"]:
                    raise ValueError("receipt digest mismatch")
                if passed:
                    raise ValueError("attempt recorded after a passing journey")
                passed = validate_attempt(record, json.loads(data), cell, run_id=run_id, run_attempt=run_attempt)
            if not passed or report["status"] != "pass":
                raise ValueError("full-scope cloud reports did not all pass: journey/cell failed")
            cost = report.get("cost", {})
            if (cost.get("status") != "pass" or cost.get("scope") != "run"
                    or cost.get("runId") != run_id or cost.get("runAttempt") != run_attempt
                    or cost.get("candidateDigest") != candidate_digest()):
                raise ValueError("missing passing run cost ceiling evidence")
            amount, ceiling = Decimal(cost["amountUsd"]), Decimal(cost["ceilingUsd"])
            if not amount.is_finite() or not ceiling.is_finite() or not 0 <= amount <= ceiling or ceiling <= 0:
                raise ValueError("invalid or over-ceiling run cost evidence")
            measured = datetime.fromisoformat(cost["measuredAt"].replace("Z", "+00:00"))
            if not 0 <= (datetime.now(timezone.utc) - measured).total_seconds() <= 86400:
                raise ValueError("stale run cost evidence")
        except Exception as error:
            failures.append(f"{cell}: {type(error).__name__}: {error}")
    status = "fail" if failures else "pass"
    if not full_scope and status == "pass":
        status = "blocked"
    return {"gate": "cloud-parity", "status": status, "why": "; ".join(failures) if failures else
            ("GA cloud journeys passed" if full_scope else "focused dispatch is diagnostic only"),
            "cells": cells, "canaryProbes": [probe for row in cells for probe in row.get("canaryProbes", [])],
            "certifying": full_scope and require_real and status == "pass",
            "certifyingScope": full_scope, "lambdaGaQualification": "pending", "generatedAt": now()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate cloud artifacts and assemble the GA gate")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for path in sorted(args.reports.rglob("gate-report-cloud.json")):
        try:
            report = json.loads(path.read_text())
        except (ValueError, OSError):
            report = {"gate": "invalid-artifact", "status": "fail", "why": "unreadable cell report"}
        report["artifactDirectory"] = str(path.parent)
        reports.append(report)
    report = aggregate(reports, args.reports,
        require_real=os.environ.get("REQUIRE_REAL") == "true",
        full_scope=os.environ.get("FULL_SCOPE") == "true",
        run_id=os.environ.get("GITHUB_RUN_ID", "local"),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
