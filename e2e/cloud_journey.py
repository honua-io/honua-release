"""Cloud bindings for the owned terminal journey; no stage implementations live here."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
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
# aws-mixed is out for 2026.1 rc.3 (no examples/aws-mixed root); restore it when honua-iac#209 lands.
PREVIEW_TARGETS = ("aws-eks",)


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


_RUN_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def run_binding(run_id, run_attempt):
    """Identity suffix stored in every stage evidence URI. The owned receipt schema has no run field."""
    if not _RUN_TOKEN.fullmatch(run_id or "") or not _RUN_TOKEN.fullmatch(run_attempt or ""):
        raise ValueError("invalid run identity")
    return f"#honua-run={run_id}/{run_attempt}"


def bound_evidence_uri(cell, run_id, run_attempt):
    base = os.environ.get("HONUA_RUN_URL") or f"urn:honua:cloud:{cell}"
    return base.split("#", 1)[0] + run_binding(run_id, run_attempt)


@contextmanager
def external_image(driver, image_ref):
    # run_live reports no image for an external URL; it cannot see a cloud deployment. Supply
    # the image read back from the cloud control plane to the owned observe(), unchanged otherwise.
    original = driver.observe

    def observe(target, base_url, workspace, bindir, _image_ref, expected_revision):
        return original(target, base_url, workspace, bindir, image_ref, expected_revision)

    driver.observe = observe
    try:
        yield
    finally:
        driver.observe = original


def observed_ecs_image(target, pinned, *, run=subprocess.run):
    """The pinned server image only if every RUNNING task of the deployed ECS service runs it.

    Read from ECS DescribeTasks, never inferred from the apply inputs. Anything else is None.
    """
    server = pinned["components"]["honua-server"]
    root = target._workdir
    if root is None:
        return None
    cluster, service = (target._tf(root, "output", "-raw", name).stdout.strip()
                        for name in ("ecs_cluster_name", "ecs_service_name"))
    if not cluster or not service:
        return None

    def aws(*args):
        return json.loads(run(["aws", "ecs", *args, "--output", "json"], text=True,
                              capture_output=True, check=True).stdout)

    arns = aws("list-tasks", "--cluster", cluster, "--service-name", service,
               "--desired-status", "RUNNING")["taskArns"]
    if not arns:
        return None
    tasks = aws("describe-tasks", "--cluster", cluster, "--tasks", *arns)["tasks"]
    for task in tasks:
        servers = [c for c in task.get("containers", [])
                   if str(c.get("image", "")).split("@", 1)[0] == server["image"]]
        if (task.get("lastStatus") != "RUNNING" or not servers
                or any(c.get("imageDigest") != server["digest"] or c.get("lastStatus") != "RUNNING"
                       for c in servers)):
            return None
    return f"{server['image']}@{server['digest']}" if len(tasks) == len(arns) else None


UNOBSERVED_SHA = "0" * 40

# Notices attempt() writes into a receipt. A driver exception is a failure of this run, never a
# documented limitation; an unsupported cloud kind is the tracked honua-release#377 gap.
DRIVER_ERROR_NOTICE = "Imported journey driver raised "
UNSUPPORTED_KIND_NOTICE = "Owned receipt schema lacks this cloud kind"
UNSUPPORTED_KIND_ISSUE = "https://github.com/honua-io/honua-release/issues/377"


MAX_ERROR_CHARS = 400


def _scrub(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "...(truncated)"


def driver_failure_notices(error, trace_module, secrets):
    """Receipt notices naming what the imported driver raised, where, and its last HTTP exchange.

    The first notice keeps the DRIVER_ERROR_NOTICE prefix that marks the attempt failed. The
    message, step and redacted HTTP summary come from harness-owned diagnostics; any known
    credential value is scrubbed again before it can reach a receipt or the job log.
    """
    message = _scrub(str(error), secrets)
    notices = [f"{DRIVER_ERROR_NOTICE}{type(error).__name__}" + (f": {message}" if message else "")]
    lines = trace_module.describe_trace() if trace_module is not None else ["step: unknown (no driver trace)"]
    notices.extend(f"Imported journey driver failure {_scrub(line, secrets)}" for line in lines)
    return notices


def documented_blockers(receipt):
    """The tracked limitations a BLOCKED journey receipt names, or [] when it is not one.

    A receipt is blocked by documented limitations only when nothing in it failed, the imported
    driver did not raise, and every blocked stage names the issue that blocks it (or the cell kind
    is the tracked #377 gap). Anything else is a failed attempt.
    """
    if not isinstance(receipt, dict) or receipt.get("status") != "blocked":
        return []
    notices = [str(notice) for notice in receipt.get("notices") or []]
    if any(notice.startswith(DRIVER_ERROR_NOTICE) for notice in notices):
        return []
    blockers = set()
    if any(notice.startswith(UNSUPPORTED_KIND_NOTICE) for notice in notices):
        blockers.add(UNSUPPORTED_KIND_ISSUE)
    for stage in receipt.get("stages") or []:
        if stage.get("status") == "fail":
            return []
        if stage.get("status") == "blocked":
            named = [str(b) for b in stage.get("blockedBy") or [] if str(b).strip()]
            if not named:
                return []
            blockers.update(named)
    return sorted(blockers)


CAPABILITY_MANIFEST = "/api/v1/capabilities/manifest"


def parse_server_identity(body):
    """deploymentRevision and its source from an anonymous capability manifest body, or None."""
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        return None
    server = (document.get("server") or document.get("Server") or {}) if isinstance(document, dict) else {}
    if not isinstance(server, dict):
        return None
    revision = server.get("deploymentRevision") or server.get("DeploymentRevision")
    source = server.get("deploymentRevisionSource") or server.get("DeploymentRevisionSource")
    return {"revision": revision, "source": source} if isinstance(revision, str) else None


def server_identity(endpoint, *, opener=urllib.request.urlopen):
    """The revision the live deployment advertises on its anonymous capability manifest, or None."""
    try:
        with opener(endpoint.rstrip("/") + CAPABILITY_MANIFEST, timeout=15) as response:
            return parse_server_identity(response.read().decode("utf-8"))
    except Exception:
        return None


def observed_server(identity, running_image):
    """Receipt `server` pins from observations only: never copied from the manifest.

    sourceSha is the commit the endpoint advertises; image is what the control plane reports
    running. Anything unobserved stays visibly unobserved, so validate_attempt cannot match it.
    """
    revision = (identity or {}).get("revision") or ""
    source = (identity or {}).get("source")
    commit = source in ("commit-sha", "assembly-metadata") and re.fullmatch(r"[0-9a-f]{40}", revision)
    image = running_image or "unobserved"
    if source == "image-digest" and running_image and running_image.rsplit("@", 1)[-1] != revision:
        image = "unobserved"
    return {"sourceSha": revision if commit else UNOBSERVED_SHA, "image": image}


def attempt(cell, number, endpoint, admin_key, running_image=None):
    driver, adapter = drivers()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    evidence_uri = bound_evidence_uri(cell, run_id, run_attempt)
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
    if "discovery" in sys.modules:
        sys.modules["discovery"].reset_trace()
    try:
        if endpoint is not None:
            # pins.CANDIDATE_ENV_ALLOWLIST is the only environment run_live keeps.
            # That drops AWS_*, ACTIONS_ID_TOKEN_REQUEST_*, HONUA_AWS_*, GITHUB_TOKEN
            # and GH_TOKEN before npm install and the candidate CLIs start.
            with admin_credential(admin_key), external_image(driver, running_image):
                with driver.pins.candidate_sandbox():
                    workspace, results, observed_notices, _ = driver.run_live(
                        target, pinned, contract, workdir, endpoint, True)
            notices.extend(observed_notices)
    except Exception as error:
        # Driver exceptions must still produce an attempt receipt, without retaining credentials
        # or raw tool output. The outer finally still checks cost and destroys infrastructure.
        failure = driver_failure_notices(error, sys.modules.get("discovery"), (admin_key,))
        notices.extend(failure)
        print(f"{cell} journey attempt {number}: " + " | ".join(failure), file=sys.stderr, flush=True)
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
        evidence_uri=evidence_uri,
        mode="build" if build_only else "live", target=target,
        target_path=target_path, target_base_url=endpoint, workspace=workspace,
        stage_results=None if build_only else results, notices=notices)
    # build_receipt currently labels every live observation Docker. Bind the ECS observation
    # to its actual source; unsupported cloud kinds retain a blocked build receipt above.
    if not build_only:
        for stage in receipt["stages"]:
            stage["evidence"]["source"] = "live-aws-ecs"
    receipt["target"]["composeProject"] = None
    identity = None
    if endpoint is not None:
        # honua-release#381: a receipt for a real cell states what that cell reported, not what the
        # manifest says it should be, even when the driver failed before any stage ran;
        # validate_attempt then compares the two.
        identity = server_identity(endpoint)
        receipt["server"] = observed_server(identity, running_image)
    driver.validate_receipt(receipt, HERE / "receipt.schema.json")
    path = directory / f"receipt-{number}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    return {"number": number, "runId": run_id, "runAttempt": run_attempt, "cell": cell,
            "candidateDigest": candidate_digest(), "receipt": str(path.relative_to(ROOT / "e2e")),
            "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "failureAttribution": attribution or ("infrastructure" if receipt["status"] != "pass" else None),
            "observedServer": identity}


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
            or receipt["clientArtifacts"] != driver.pins.receipt_pins(pinned)):
        raise ValueError("cell receipt is bound to the wrong candidate, run or cell")
    # A cell receipt's server pins are what the cell advertised and its control plane reported
    # (honua-release#381). No attempt, failed or not, may show a server other than the candidate.
    # Only ECS reports its running image today, so a failed attempt may leave a value visibly
    # unobserved and is still recorded as that cell's failure; a passing one must show the candidate.
    candidate = {"sourceSha": server["sha"], "image": f"{server['image']}@{server['digest']}"}
    if receipt["server"] != candidate:
        if (receipt["server"]["sourceSha"] not in (candidate["sourceSha"], UNOBSERVED_SHA)
                or receipt["server"]["image"] not in (candidate["image"], "unobserved")):
            raise ValueError("cell receipt is bound to the wrong candidate, run or cell")
        if receipt["status"] == "pass":
            raise ValueError("cell receipt server identity was not observed on the live cell")
    suffix = run_binding(run_id, run_attempt)
    uris = [stage["evidence"]["uri"] for stage in receipt["stages"]]
    if len(set(uris)) != 1 or uris[0].count("#") != 1 or not uris[0].endswith(suffix):
        raise ValueError("cell receipt is bound to the wrong run")
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


def aggregate(reports, reports_root, *, require_real, full_scope, run_id, run_attempt,
              final_cost=None):
    cells = []
    failures = []
    blocked = []
    # Per-cell readings predate later cells, teardowns and iac-live. Only a reading taken after
    # every cloud job finished bounds the whole run. A meter that produced no reading at all blocks
    # a non-strict run (named, never pass); require_real fails on it.
    if final_cost is not None and final_cost.get("status") == "unavailable" and not require_real:
        blocked.append("final run cost: " + final_cost.get("why", "no run cost meter reading"))
    elif final_cost is not None and final_cost.get("status") != "pass":
        failures.append("final run cost: " + final_cost.get("why", "run cost ceiling exceeded"))
    elif final_cost is None and full_scope:
        failures.append("final run cost evidence missing after all cloud jobs")
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
            attempt_blockers = []
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
                receipt = json.loads(data)
                passed = validate_attempt(record, receipt, cell, run_id=run_id, run_attempt=run_attempt)
                attempt_blockers.append(documented_blockers(receipt))
            cost = report.get("cost", {})
            # Outside require_real a cell that is BLOCKED only by tracked, documented limitations
            # (journey receipts, scenarios, a missing run cost meter) is reported blocked, never
            # pass. A failed attempt, an over-ceiling or unbound cost reading, or require_real
            # leaves it a failure.
            if (not require_real and report["status"] == "blocked"
                    and (passed or all(attempt_blockers)) and cost.get("status") in ("pass", "unavailable")):
                blocked.append(f"{cell}: {report.get('why') or 'blocked'}")
                continue
            if not passed or report["status"] != "pass":
                raise ValueError("full-scope cloud reports did not all pass: journey/cell failed")
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
    status = "fail" if failures else "blocked" if blocked else "pass"
    if not full_scope and status == "pass":
        status = "blocked"
    return {"gate": "cloud-parity", "status": status, "why": "; ".join(failures) if failures else
            ("blocked by tracked limitations: " + "; ".join(blocked)) if blocked else
            ("GA cloud journeys passed" if full_scope else "focused dispatch is diagnostic only"),
            "cells": cells, "canaryProbes": [probe for row in cells for probe in row.get("canaryProbes", [])],
            "certifying": full_scope and require_real and status == "pass",
            "certifyingScope": full_scope, "lambdaGaQualification": "pending",
            "finalCost": final_cost, "generatedAt": now()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate cloud artifacts and assemble the GA gate")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--final-cost", type=Path,
                        help="run-cost.json refreshed after every cloud job completed")
    parser.add_argument("--final-cost-after",
                        help="ISO time the aggregation began; the final reading must be later")
    parser.add_argument("--cost-ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    args = parser.parse_args()
    final_cost = None
    if args.final_cost is not None:
        try:
            after = datetime.fromisoformat(args.final_cost_after.replace("Z", "+00:00"))
            final_cost = check_cost(args.final_cost, args.cost_ceiling_usd, started_at=after)
        except FileNotFoundError:
            final_cost = {"status": "unavailable", "why": f"unavailable: no reading at {args.final_cost}"}
        except Exception as error:
            final_cost = {"status": "fail", "why": f"unavailable: {type(error).__name__}"}
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
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"), final_cost=final_cost)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
