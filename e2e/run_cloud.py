#!/usr/bin/env python3
"""Provision a cloud cell, run the imported pinned journey, meter cost and always tear down.

GA: {aws-ecs, aws-serverless} x Redis off/on; aws-serverless is Lambda + AWS Batch
geoprocessing. EKS is informational Preview under the 2026-10-16 owner amendment.
Redis-on exercises full workflows; Redis-off requires explicitly bound serving/refusal proof. The mixed ECS + Batch cell is out of the 2026.1 rc.3 matrix: its IaC
root does not exist yet (restore it when honua-iac#209 lands).
Missing, blocked or invalid journey evidence cannot certify a GA cloud cell.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes  # noqa: E402
from canonical_checks import CheckResult, is_endpoint_unreachable, make_fetch, run_canonical  # noqa: E402
from runner.cloud import run_drivers, seed_cell  # noqa: E402
from parity import TargetRun, compare  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.base import ProvisionError  # noqa: E402

import cloud_journey  # noqa: E402
import cost_meter  # noqa: E402
import large_import  # noqa: E402

# No aws-mixed entry for 2026.1 rc.3: examples/aws-mixed does not exist in honua-iac, so the cell could
# only ever report a missing root. Restore it (never as the all-ECS root) when honua-iac#209 lands.

REPORT_PATH = E2E_DIR / "gate-report-cloud.json"

# The cloud/OIDC secrets that gate whether this tier can run at all. When NONE are present the gate
# SELF-SKIPS (status: skipped, why: cloud-creds-unset) so a no-cloud local cut is not failed by it —
# it stays ready to enforce per-RC once an org wires the OIDC role for a labelled candidate.
_CLOUD_CRED_ENV = ("HONUA_AWS_ROLE_ARN", "AWS_ROLE_ARN", "AWS_ACCESS_KEY_ID",
                   "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE")

_READY_ATTEMPTS = 36
_READY_DELAY_SECONDS = 5.0
# The journey runner waits for the admit job to open the cell's ingress to its address. EKS admission
# updates the cluster's public-access CIDRs, which alone can take several minutes.
_JOURNEY_READY_ATTEMPTS = 240


def _cloud_creds_present() -> bool:
    return any(os.environ.get(v) for v in _CLOUD_CRED_ENV)


def _mark_provision_attempt() -> None:
    """Record that this cell is about to create real cloud resources.

    The workflow's backstop reaper runs even when the parity step is cancelled mid-apply, where no
    report exists to consult. The marker is what tells it the difference between "nothing was ever
    deployed" and "something may be half-applied and MUST be destroyed".
    """
    marker = os.environ.get("HONUA_CLOUD_PROVISION_MARKER")
    if not marker:
        return
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _check_dicts(results) -> list[dict]:
    return [{"name": r.name, "status": r.status, "why": r.why, **({"evidence": r.evidence} if r.evidence else {})}
            for r in results]


def _wait_for_endpoint(endpoint: str, fetch, *, attempts: int = _READY_ATTEMPTS,
                       delay_seconds: float = _READY_DELAY_SECONDS,
                       sleep=time.sleep) -> tuple[bool, dict]:
    """Wait for the deployed route, Lambda cold start, and application readiness.

    Terraform can finish while an API Gateway auto-deployment is still propagating. A newly
    published Lambda alias also needs one cold start before the canonical probes are meaningful.
    Treat every non-200 response as not-ready and preserve the final response as gate evidence;
    the canonical checks still run after timeout so they retain their detailed verdicts.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    url = endpoint.rstrip("/") + "/healthz/ready"
    last = None
    for attempt in range(1, attempts + 1):
        last = fetch(url)
        if last.status == 200:
            return True, {"url": url, "status": 200, "attempts": attempt}
        if attempt < attempts:
            sleep(delay_seconds)
    assert last is not None
    return False, {
        "url": url,
        "status": last.status,
        "attempts": attempts,
        "body_head": last.body[:200],
        "headers": last.headers,
    }


def _run_identity() -> tuple[str, str]:
    return os.environ.get("GITHUB_RUN_ID", "local"), os.environ.get("GITHUB_RUN_ATTEMPT", "1")


def _results(rows) -> list[CheckResult]:
    return [CheckResult(row["name"], row["status"], row.get("why", ""), row.get("evidence") or {})
            for row in rows or []]


def installed_clients():
    """The published-client endpoint adapter, kept separate from the terminal journey driver."""
    name = "installed_client_endpoint"
    if name not in sys.modules:
        path = E2E_DIR.parent / "certification/installed-clients/endpoint.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def provision_phase(target, target_name: str, *, require_real: bool, redis_enabled: bool,
                    journey_datasource: dict | None = None, installed_context: dict | None = None) -> dict:
    """Credentialed half: provision, probe and seed the cell. Nothing third-party runs here.

    Returns the handoff the credential-free journey job and the teardown job consume. The handoff
    carries identifiers and verdicts only; the cell's application key goes to Secrets Manager.
    `journey_datasource` receives the seed's connection in memory for the sealed journey handoff.
    """
    run_id, run_attempt = _run_identity()
    redis_mode = "redis-on" if redis_enabled else "redis-off"
    cell = f"{target_name}/{redis_mode}"
    avail = target.availability()
    report: dict = {"gate": "cloud-parity", "target": target_name, "redis": redis_mode, "cell": cell,
                    "require_real": require_real,
                    "evidenceTier": "Preview" if target_name in cloud_journey.PREVIEW_TARGETS else "GA",
                    "journeyAttempts": [],
                    "availability": {"ok": avail.ok, "reason": avail.reason, "missing": avail.missing}}
    state: dict = {"cell": cell, "target": target_name, "redis": redis_mode, "runId": run_id,
                   "runAttempt": run_attempt, "candidateDigest": cloud_journey.candidate_digest(),
                   "startedAt": cloud_journey.now(), "requireReal": require_real,
                   "provisionAttempted": False, "endpoint": None, "ready": False,
                   "runningImage": None, "observedServer": None, "seedError": None,
                   "admission": getattr(target, "admission", "none"), "report": report}

    if not avail.ok:
        report["journeyAttempts"].append(cloud_journey.attempt(cell, 1, None, ""))
        # Cloud/OIDC creds unset may self-skip only on the optional bootstrap path. A required cell
        # without credentials is missing required evidence and must be red; otherwise every matrix
        # cell can exit 0 without exercising AWS (honua-release#209).
        if not _cloud_creds_present():
            report["status"] = "fail" if require_real else "skipped"
            report["why"] = (
                "required cloud certification evidence missing: cloud-creds-unset"
                if require_real else "cloud-creds-unset"
            )
            return state
        # Creds present but infra half-wired (no image / no IaC tree) => BLOCKED, promoted to FAIL under
        # require_real so a genuinely broken cloud path is a real red.
        report["status"] = "fail" if require_real else "blocked"
        report["why"] = avail.reason
        return state

    try:
        _mark_provision_attempt()
        state["provisionAttempted"] = True
        configure_tenant_fixture(target, cell)
        state["tenantFixtureEnabled"] = bool(getattr(target, "journey_signing_key", None))
        endpoint = target.provision(redis_enabled=redis_enabled)
        report["endpoint"] = endpoint
        # The scheme the pinned clients will be handed and the host they will see (honua-release#450:
        # an ECS cell with a per-run hostname is https; the receipt's "Candidate transport" notice
        # records the same pair). Identifiers only.
        parsed = urllib.parse.urlsplit(endpoint)
        report["transport"] = {"scheme": parsed.scheme, "host": (parsed.hostname or "").lower()}
        if getattr(target, "migrates_before_serving", False):
            # The serverless root boots the Lambda with skip_migrations=true; migrate the cell's
            # database from this runner before anything is probed. A cell that cannot migrate fails
            # here with the reason, and teardown still destroys what apply created.
            report["migration"] = {"ready": False}
            try:
                report["migration"] = target.migrate(redis_enabled=redis_enabled)
            except ProvisionError as e:
                report["status"] = "fail"
                # A docker or Npgsql error can echo the connection string; the report is public.
                report["why"] = f"migration failed: {_redact_log(str(e))}"
                return state
        fetch = make_fetch(timeout=10.0)
        # The budget is read from the module globals at CALL time so a test can shorten it; the
        # defaults on _wait_for_endpoint are bound at def time and cannot be monkeypatched.
        ready, readiness = _wait_for_endpoint(endpoint, fetch, attempts=_READY_ATTEMPTS,
                                              delay_seconds=_READY_DELAY_SECONDS)
        report["readiness"] = {"ready": ready, **readiness}
        admin_fetch = make_fetch(headers={"X-API-Key": target.admin_api_key}, timeout=10.0)
        checks = run_canonical(endpoint, fetch, authenticated_fetch=admin_fetch,
                               enforcement="strict" if require_real else "bootstrap")
        report["checks"] = _check_dicts(checks)
        # Cloud-tier unblock (honua-release#61): the canary probe set, GENERIC mode (no service/tile id
        # configured — nothing is seeded on a bare terraform cell yet), so data-dependent probes report
        # BLOCKED honestly rather than a fake pass/fail; reachability-only probes run for real.
        # Owner decision 8 of 2026-10-10: an HTTPS load-balancer cell must redirect plain HTTP; any
        # other cell records the redirect probe blocked with its reason.
        expectation = getattr(target, "https_redirect_expectation", None)
        https_redirect = (expectation(redis_enabled) if expectation is not None else
                          (False, f"{target_name} does not provision an HTTP->HTTPS redirect listener"))
        canary_results = canary_probes.run_canary(
            endpoint, fetch, https_redirect=https_redirect,
            redirect_fetch=make_fetch(timeout=10.0, follow_redirects=False))
        report["canaryProbes"] = _check_dicts(canary_results)
        # What the cell advertises before any pinned client ran: teardown holds journey receipts to it.
        identity = fetch(endpoint.rstrip("/") + cloud_journey.CAPABILITY_MANIFEST)
        state["observedServer"] = (cloud_journey.parse_server_identity(identity.body)
                                   if identity.status == 200 else None)
        # The seed needs the cell's database secret, so it runs here. Every seam driver and the
        # journey run later, in the job that holds no cloud credential, before any teardown.
        seed_kwargs = {} if journey_datasource is None else {"datasource": journey_datasource}
        state["seedError"] = seed_cell(endpoint, target=target,
                                       out=cloud_journey.cell_dir(cell) / "extended", **seed_kwargs)
        serving_seed = cloud_journey.cell_dir(cell) / "extended/seed-manifest.json"
        if serving_seed.is_file() and not serving_seed.is_symlink():
            # This digest enters the encrypted provision handoff before candidate packages run.
            state["servingFixtureSha256"] = hashlib.sha256(serving_seed.read_bytes()).hexdigest()
        if (os.environ.get("HONUA_UPDATE_ROLLBACK_ENABLED") == "true" and target_name == "aws-ecs" and redis_enabled):
            from runner.update_rollback_aws import configure_ecs_safety
            configure_ecs_safety(target, endpoint, serving_seed)
        # S10 large-import (honua-release#548): stage the cell's pinned fixture in S3 while this job
        # holds the AWS credentials. Only a target that names an AWS region can stage.
        if getattr(target, "region", None):
            try:
                state["s10"] = large_import.stage(
                    cell, run_id=run_id, run_attempt=run_attempt,
                    bucket=os.environ.get("HONUA_AWS_CELL_FILE_STORAGE_BUCKET"), region=target.region,
                    workdir=cloud_journey.cell_dir(cell) / "s10")
            except Exception as e:
                state["s10"] = {"scenario": large_import.SCENARIO, "staged": False,
                                "why": f"staging the S10 fixture raised {type(e).__name__} ({large_import.ISSUE})"}
        # The owned 1.1-candidate-image check needs the image ECS reports running.
        if target_name == "aws-ecs":
            # Every RUNNING task must report the candidate; the detail names any that disagrees.
            try:
                state["runningImage"], state["runningImageDetail"] = cloud_journey.observe_ecs_image(
                    target, cloud_journey.manifest())
            except Exception as e:
                state["runningImage"] = None
                state["runningImageDetail"] = f"reading the ECS tasks failed: {type(e).__name__}"
        elif target_name == "aws-serverless":
            try:
                state["runningImage"] = cloud_journey.observed_lambda_image(target, cloud_journey.manifest())
            except Exception:
                state["runningImage"] = None
        elif hasattr(target, "observed_image"):
            # EKS: the digest every server pod of the release is running, read from the cluster.
            try:
                state["runningImage"] = target.observed_image(cloud_journey.manifest())
            except Exception:
                state["runningImage"] = None
        state.update(endpoint=endpoint, ready=ready)
        if installed_context is not None and redis_enabled:
            state["installedClients"] = {"enabled": True, "prepared": False}
            try:
                installed_context.update(installed_clients().prepare(target, state, cloud_journey.manifest(),
                    E2E_DIR.parent / "platform-manifest.yaml", admin_key=target.admin_api_key,
                    signing_key=getattr(target, "journey_signing_key", None)))
                state["installedClients"]["prepared"] = True
            except Exception as error:
                # A seed/client exception may carry database coordinates. Only the type is public.
                state["installedClients"]["why"] = f"installed-client fixture preparation raised {type(error).__name__}"
    except ProvisionError as e:
        report["status"] = "fail"
        report["why"] = f"provision failed: {e}"
    except Exception as e:
        report["status"] = "fail"
        report["why"] = f"cloud checks/journey failed: {type(e).__name__}"
    return state


def installed_client_row(state, context, admin_key):
    """The existing matrix is the denominator; missing prerequisites remain visibly blocked."""
    output = cloud_journey.cell_dir(state["cell"]) / "installed-clients/receipt.json"
    if not context:
        return CheckResult("S11-installed-clients", "blocked",
            "published-client endpoint fixture unavailable (honua-release#65); full suite was not qualified")
    adapter = installed_clients()
    try:
        receipt = adapter.run_cloud(state, context, admin_key, output)
        counts = {status: sum(result["status"] == status for result in receipt["results"])
                  for status in ("pass", "fail", "blocked")}
        return CheckResult("S11-installed-clients", receipt["status"],
            "published clients executed the complete installed-client matrix against this endpoint",
            {"receipt": str(output.relative_to(E2E_DIR)), "counts": counts})
    except Exception as error:
        prerequisite = getattr(adapter, "PrerequisiteUnavailable", None)
        if isinstance(prerequisite, type) and isinstance(error, prerequisite):
            return CheckResult("S11-installed-clients", "blocked",
                "approved ALB HTTP cell needs HTTPS for the published-client workflow; no clients executed (honua-release#65)")
        return CheckResult("S11-installed-clients", "fail",
            f"published-client endpoint qualification raised {type(error).__name__} (honua-release#65)")


def journey_phase(state: dict, *, admin_key: str, max_attempts: int = 2,
                  s10_source_url: str | None = None, tenant_signing_key: str | None = None,
                  installed_context: dict | None = None) -> dict:
    """Credential-free half: the npm/browser seam drivers and the manifest-pinned journey.

    The workflow runs this in a job with no id-token permission and no AWS credential, so nothing a
    compromised pinned client can read from /proc holds either (honua-release#381).
    """
    if max_attempts not in (1, 2):
        raise ValueError("journey permits one or two attempts")
    journey: dict = {"cell": state["cell"], "scenarioCoverage": [], "journeyAttempts": []}
    endpoint = state.get("endpoint")
    report = state["report"]
    if not endpoint or report.get("status") == "fail":
        return journey
    cell = state["cell"]
    run_id, run_attempt = _run_identity()
    try:
        if (state.get("runId"), state.get("runAttempt"), state.get("candidateDigest")) != (
                run_id, run_attempt, cloud_journey.candidate_digest()):
            raise ValueError("provision handoff is bound to another run or candidate")
        if bool(state.get("tenantFixtureEnabled")) != (tenant_signing_key is not None):
            raise ValueError("sealed tenant fixture does not match the provisioned cell")
        if not admin_key:
            raise ValueError("provision handoff did not deliver the cell's application key")
        if state.get("ready"):
            # This runner is not the one that provisioned the cell. The admit job opens the cell's
            # ingress to it after it publishes its address, so wait for the endpoint to answer here.
            reachable, readiness = _wait_for_endpoint(endpoint, make_fetch(timeout=10.0),
                attempts=_JOURNEY_READY_ATTEMPTS, delay_seconds=_READY_DELAY_SECONDS)
            journey["readiness"] = {"ready": reachable, **readiness}
            if not reachable:
                journey["status"] = "fail"
                journey["why"] = (f"{cell}: {endpoint} served the provisioning runner but never the "
                                  "credential-free journey runner (ingress admission failed)")
                return journey
        # S10 first: its presigned source URL is short-lived (large_import.PRESIGN_TTL_SECONDS).
        try:
            s10 = large_import.run_scenario(
                state, endpoint, admin_key, source_url=s10_source_url,
                seed_manifest=cloud_journey.cell_dir(cell) / "extended" / "seed-manifest.json")
        except Exception as e:
            s10 = CheckResult(large_import.ROW, "fail",
                              f"{large_import.SCENARIO}: the harness raised {type(e).__name__}")
        extended = run_drivers(endpoint, admin_key=admin_key,
            out=cloud_journey.cell_dir(cell) / "extended", ready=state.get("ready", False),
            require_real=state.get("requireReal", False),
            redis_enabled=state.get("redis") == "redis-on", seed_error=state.get("seedError"))
        if s10 is not None:
            extended = [*extended, s10]
        if state.get("installedClients", {}).get("enabled"):
            extended = [*extended, installed_client_row(state, installed_context, admin_key)]
        journey["scenarioCoverage"] = _check_dicts(extended)
        blockers: list[list[str]] = []
        for number in range(1, max_attempts + 1):
            detail = state.get("runningImageDetail")
            record = cloud_journey.attempt(cell, number, endpoint, admin_key, state.get("runningImage"),
                                           **({"image_detail": detail} if detail else {}),
                                           **({"tenant_signing_key": tenant_signing_key} if tenant_signing_key else {}))
            journey["journeyAttempts"].append(record)
            receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
            try:
                passed = cloud_journey.qualify_attempt(record, receipt, cell,
                    raw_path=E2E_DIR / record["receipt"], run_id=run_id, run_attempt=run_attempt,
                    require_observation=False)
            except ValueError as error:
                # validate_attempt's reasons are fixed strings; they carry no receipt content.
                journey["status"] = "fail"
                journey["why"] = f"journey evidence rejected: {error}"
                break
            if passed:
                break
            blockers.append(cloud_journey.documented_blockers(receipt))
            if blockers[-1] and (not record.get("servingRefusal")
                                 or cloud_journey.profile_blocked(record, E2E_DIR / record["receipt"])):
                # Only a profile that ran gets the retry budget past a raw block; a profile with no
                # admissible prerequisite (a tracked limitation) would meet the same block again.
                break
        else:
            journey["status"] = "fail"
            journey["why"] = "journey did not pass within the recorded attempt budget"
        if journey.get("status") != "fail" and not passed:
            journey.update(_journey_blocked_verdict(blockers, state.get("requireReal", False)))
    except Exception as e:
        journey["status"] = "fail"
        journey["why"] = f"cloud checks/journey failed: {type(e).__name__}"
    finally:
        try:
            cloud_journey.cleanup(cell)
        except Exception as e:
            journey["status"] = "fail"
            journey["why"] = (journey.get("why", "") + "; " if journey.get("why") else "") + \
                f"journey cleanup failed: {type(e).__name__}"
    return journey


def _journey_blocked_verdict(blockers: list[list[str]], require_real: bool) -> dict:
    """Verdict for a journey whose attempts all stopped on BLOCKED receipts.

    Every attempt naming only tracked, documented limitations is `blocked` (honest, never pass)
    with the blockers named; require_real (the per-RC strict mode) promotes it to fail. Any attempt
    that is not a documented block keeps the journey a failure.
    """
    if not blockers or not all(blockers):
        return {"status": "fail", "why": "journey did not pass within the recorded attempt budget"}
    named = sorted({issue for attempt in blockers for issue in attempt})
    if require_real:
        return {"status": "fail", "blockedBy": named,
                "why": f"require_real: journey blocked by tracked limitations {named}"}
    return {"status": "blocked", "blockedBy": named,
            "why": f"journey blocked by tracked limitations {named}"}


_ISSUE_REF = re.compile(r"\b[a-z][a-z0-9-]*#\d+\b|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")


def teardown_phase(state: dict, journey: dict, target, *, reference_endpoint: str | None,
                   cost_ceiling_usd: str = "20", cost_report: Path | None = None,
                   destroy: bool = True) -> dict:
    """Credentialed half: meter cost, destroy the cell, then decide the cell's verdict."""
    report = state["report"]
    cell = state["cell"]
    require_real = state.get("requireReal", False)
    redis_enabled = state.get("redis") == "redis-on"
    report["scenarioCoverage"] = journey.get("scenarioCoverage", report.get("scenarioCoverage", []))
    report["journeyAttempts"] = report.get("journeyAttempts", []) + journey.get("journeyAttempts", [])
    if "readiness" in journey:
        report["journeyReadiness"] = journey["readiness"]
    if journey.get("status") == "blocked" and require_real:
        journey = {**journey, "status": "fail", "why": f"require_real: {journey.get('why')}"}
    if journey.get("status") == "fail" and report.get("status") != "fail":
        report["status"] = "fail"
        report["why"] = journey.get("why", "journey failed")
    journey_blocked = journey.get("status") == "blocked"
    if journey_blocked:
        report["journeyBlockedBy"] = journey.get("blockedBy", [])
    cost_unavailable = None
    report["teardown"] = {"attempted": False, "completed": False}
    report["provisionAttempted"] = bool(state.get("provisionAttempted"))
    if not state.get("provisionAttempted"):
        # Self-skipped, blocked, or never started: nothing was deployed, so nothing is destroyed.
        state["destroyed"] = False
        report["provisionAttempted"] = False  # the run cost meter counts this cell as $0, not missing
        return report

    started_at = datetime.fromisoformat(state["startedAt"].replace("Z", "+00:00"))
    # Metering, receipt persistence, local cleanup and infrastructure destruction are
    # independent obligations. An error in any one must not bypass the following obligations.
    try:
        try:
            if cell in ("aws-ecs/redis-off", "aws-serverless/redis-off") and any(
                    record.get("servingRefusal") for record in report["journeyAttempts"]):
                try:
                    pinned = cloud_journey.manifest()
                    # The teardown runner is fresh: point the target at the restored Terraform root
                    # (both image observations read its outputs) and, on ECS, admit this runner's /32
                    # to the cell ALB so the endpoint's own identity can be read. Without either, the
                    # after-observation was always "unobserved" and every Redis-off ECS cell failed
                    # here even with a passing profile (run 39772292511).
                    if getattr(target, "_workdir", None) is None and hasattr(target, "_iac_root"):
                        target._workdir = target._iac_root()
                    runner_cidr = os.environ.get("HONUA_AWS_RUNNER_CIDR", "")
                    if state["target"] == "aws-ecs" and runner_cidr and hasattr(target, "admit"):
                        try:
                            target.admit(state["endpoint"], runner_cidr, redis_enabled=False)
                        except Exception as error:  # an unreadable identity then fails the check below
                            print(f"{cell}: admitting the teardown runner raised {type(error).__name__}",
                                  file=sys.stderr, flush=True)
                    after_image = (cloud_journey.observe_ecs_image(target, pinned)[0] if state["target"] == "aws-ecs"
                                   else cloud_journey.observed_lambda_image(target, pinned))
                    after = cloud_journey.observed_server(cloud_journey.server_identity(state["endpoint"]), after_image)
                    before = cloud_journey.observed_server(state.get("observedServer"), state.get("runningImage"))
                    for record in report["journeyAttempts"]:
                        record["profileObservation"] = {"source": "aws-control-plane", "before": before,
                            "after": after, "observedAt": cloud_journey.now(),
                            "seedManifestSha256": state.get("servingFixtureSha256")}
                        path = E2E_DIR / record["receipt"]
                        accepted = cloud_journey.qualify_attempt(record, json.loads(path.read_bytes()), cell, raw_path=path,
                            run_id=state["runId"], run_attempt=state["runAttempt"])
                        if (record is report["journeyAttempts"][-1] and not accepted
                                and not cloud_journey.profile_blocked(record, path)):
                            raise ValueError("final serving/refusal profile did not qualify")
                except Exception as exc:
                    report.update(status="fail", why="serving/refusal native observation rejected: " + type(exc).__name__)
            if not report["journeyAttempts"]:
                report["journeyAttempts"].append(cloud_journey.attempt(cell, 1, None, ""))
        except Exception as e:
            report.update(status="fail", why=report.get("why", "") +
                          f"; receipt evidence unavailable: {type(e).__name__}")
        finally:
            meter = cost_report or Path(os.environ.get("HONUA_CLOUD_COST_REPORT",
                                                       "e2e/cloud-evidence/run-cost.json"))
            try:
                try:
                    report["cost"] = cloud_journey.check_cost(meter, cost_ceiling_usd, started_at=started_at)
                except FileNotFoundError:
                    # No external reading: the harness's own live estimate (owner decision 9), read
                    # from the cell's Terraform state while the infrastructure still exists.
                    if not cost_meter.meters(target):
                        raise
                    run_id, run_attempt = (os.environ.get("GITHUB_RUN_ID", "local"),
                                           os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
                    report["cost"] = {**cost_meter.cell_cost(target, state, cost_ceiling_usd, run_id=run_id,
                                                             run_attempt=run_attempt),
                                      "cell": cell, "candidateDigest": cloud_journey.candidate_digest()}
                if report["cost"]["status"] == "unavailable":
                    cost_unavailable = report["cost"].get("why", "run cost meter unavailable")
                    if require_real:
                        report.update(status="fail", why=report.get("why", "") +
                                      f"; cost evidence unavailable: {cost_unavailable}")
                elif report["cost"]["status"] != "pass":
                    report.update(status="fail", why=report.get("why", "") +
                                  "; run cost exceeds ceiling")
            except FileNotFoundError:
                # No meter reading exists for this run. The cost entry is still written, naming
                # what is missing; it never passes. require_real fails the cell on it, as before.
                cost_unavailable = f"run cost meter unavailable: no reading at {meter}"
                report["cost"] = {"status": "unavailable", "why": cost_unavailable, "scope": "run",
                                  "ceilingUsd": str(cost_ceiling_usd),
                                  "runId": os.environ.get("GITHUB_RUN_ID", "local"),
                                  "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")}
                if require_real:
                    report.update(status="fail", why=report.get("why", "") +
                                  "; cost evidence unavailable: FileNotFoundError")
            except Exception as e:
                report.update(status="fail", why=report.get("why", "") +
                              f"; cost evidence unavailable: {type(e).__name__}")
    finally:
        try:
            cloud_journey.cleanup(cell)
        except Exception as e:
            report.update(status="fail", why=report.get("why", "") +
                          f"; journey cleanup failed: {type(e).__name__}")
            report.setdefault("cleanupFailures", []).append("journey-cleanup-failed")
        finally:
            if "s10" in state:
                # The staged S10 fixture lives in the operator-owned bucket, not in the cell: delete
                # it whatever the verdict (the bucket lifecycle on release-cells/ is the backstop).
                report["s10Cleanup"] = large_import.delete_staged(state.get("s10"), cell)
                if report["s10Cleanup"]["status"] == "failed":
                    report.update(status="fail", why=(report.get("why") + "; " if report.get("why") else "")
                                  + f"S10 staged fixture not deleted: {report['s10Cleanup']['why']}")
            state["destroyed"] = False
            if destroy:
                report["teardown"]["attempted"] = True
                try:
                    target.teardown(redis_enabled=redis_enabled)
                    state["destroyed"] = True
                    report["teardown"]["completed"] = True
                except Exception as e:
                    prior = report.get("why")
                    report["status"] = "fail"
                    report["why"] = f"{prior}; teardown failed: {e}" if prior else f"teardown failed: {e}"
                finally:
                    retirement = getattr(target, "retirement_receipt", None)
                    if retirement is not None:
                        report["retirement"] = retirement

    if report.get("status") == "fail":
        return report

    endpoint = report.get("endpoint")
    checks = _results(report.get("checks"))
    canary_results = _results(report.get("canaryProbes"))
    extended = _results(report.get("scenarioCoverage"))
    failed = [c.name for c in checks if c.status == "fail"]
    canary_failed = [c.name for c in canary_results if c.status == "fail"]
    blocked = [c.name for c in checks if c.status == "blocked"]
    ext_blocked = [c.name for c in extended if c.status in ("blocked", "fail")]

    # honua-release#128: a cell whose terraform applied but whose endpoint never served is a FAILED
    # cell, and it is reported as that one fact rather than as a wall of derived probe failures. The
    # readiness poll above already spent its full budget on /healthz/ready; if it never got a 200 and
    # the probes then could not reach the endpoint either, the deployment did not come up. Naming it
    # here keeps the diagnosis at the top of the report instead of leaving the reader to infer it from
    # twenty identical timeouts.
    unreached = [c.name for c in list(checks) + list(canary_results) if is_endpoint_unreachable(c)]
    never_ready = not report.get("readiness", {}).get("ready", True)
    if unreached or never_ready:
        reasons = []
        if never_ready:
            reasons.append("the readiness poll never got a 200 from /healthz/ready within its full "
                           f"budget ({report['readiness'].get('attempts')} attempts, last status "
                           f"{report['readiness'].get('status')})")
        if unreached:
            reasons.append(f"these checks could not reach it at all: {unreached}")
        report["status"] = "fail"
        report["why"] = (
            f"{cell}: terraform provisioned {endpoint} but it never served — " + "; ".join(reasons)
            + ". The endpoint is the thing under test, so this is a cell failure, not a skip "
              "(honua-release#128)."
        )
        return report

    ext_failed = [c.name for c in extended if c.status == "fail"]
    if failed or canary_failed or ext_failed:
        report["status"] = "fail"
        report["why"] = (f"canonical checks failed on {cell}: {failed}; canary probes failed: {canary_failed}; "
                         f"extended scenarios failed: {ext_failed}")
        return report
    if require_real and (blocked or ext_blocked):
        report["status"] = "fail"
        report["why"] = (f"require_real on {cell}: canonical blocked={blocked or '[]'}, "
                         f"scenarios not-certified={ext_blocked}")
        return report

    # Parity vs the reference target, when one was provided.
    if reference_endpoint:
        reference_admin = make_fetch(headers={"X-API-Key": os.environ.get(
            "HONUA_REFERENCE_ADMIN_PASSWORD", "honua-console-dev-key")})
        ref_checks = run_canonical(reference_endpoint, authenticated_fetch=reference_admin,
                                   enforcement="strict" if require_real else "bootstrap")
        report["reference_checks"] = _check_dicts(ref_checks)
        verdict = compare(
            TargetRun("local-docker", provisioned=True, results=ref_checks),
            TargetRun(cell, provisioned=True, results=checks),
        )
        report["parity"] = {"status": verdict.status, "why": verdict.why, "diffs": verdict.diffs}
        if verdict.status == "fail":
            report["status"] = "fail"
            report["why"] = f"parity divergence: {verdict.why}"
            return report

    # Outside require_real, every tracked limitation leaves the cell BLOCKED with its blocker named:
    # honest, never pass. (require_real returned fail above for blocked checks/scenarios, and the
    # journey and cost paths already promoted theirs to fail.)
    scenario_blocked = [f"{c.name} ({', '.join(_ISSUE_REF.findall(c.why)) or 'no tracked issue named'})"
                        for c in extended if c.status == "blocked"]
    blockers = ([f"journey blocked by tracked limitations {journey.get('blockedBy', [])}"] if journey_blocked else [])
    blockers += [f"scenarios blocked: {scenario_blocked}"] if scenario_blocked else []
    blockers += [cost_unavailable] if cost_unavailable else []
    report["status"] = "blocked" if ((blocked or blockers) and not require_real) else "pass"
    # Say what actually happened. The old wording claimed "canonical set passed" even for a cell whose
    # canonical set was entirely BLOCKED — the sentence that made honua-release#128 invisible in the
    # job log for as long as it existed.
    report["why"] = report.get("why") or (
        f"{cell}: canonical set " + (f"blocked on {blocked}" if blocked else "passed")
        + (" + parity ok" if reference_endpoint else " (parity skipped: no reference endpoint)")
        + "".join(f"; {blocker}" for blocker in blockers))
    return report


def run(target_name: str, require_real: bool, reference_endpoint: str | None,
        redis_enabled: bool = False, *, max_attempts: int = 2,
        cost_ceiling_usd: str = "20", cost_report: Path | None = None) -> dict:
    """All three phases in one process (local runs and the self-test). CI runs them as three jobs."""
    if max_attempts not in (1, 2):
        raise ValueError("journey permits one or two attempts")
    cls = REGISTRY.get(target_name)
    if cls is None:
        return {"gate": "cloud-parity", "target": target_name, "status": "fail",
                "why": f"unknown target {target_name!r}; known: {sorted(REGISTRY)}"}
    target = cls(run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    if os.environ.get("HONUA_UPDATE_ROLLBACK_ENABLED") == "true":
        from runner.update_rollback import load_release
        prior_inputs = Path(os.environ.get("HONUA_UPDATE_ROLLBACK_PRIOR_INPUTS", "."))
        target.transition_release = load_release(prior_inputs, os.environ.get("HONUA_UPDATE_ROLLBACK_MODE", "rehearsal"))
    datasource: dict = {}
    state = provision_phase(target, target_name, require_real=require_real, redis_enabled=redis_enabled,
                            journey_datasource=datasource)
    if not state["provisionAttempted"]:
        return state["report"]
    s10_url = None
    if (state.get("s10") or {}).get("staged"):
        try:
            s10_url = large_import.presign(state["s10"], state["cell"])
        except Exception:
            s10_url = None  # the S10 row then reports the missing URL as a blocked precondition
    try:
        with cloud_journey.cell_datasource(datasource or None):
            journey = journey_phase(state, admin_key=target.admin_api_key, max_attempts=max_attempts,
                                    s10_source_url=s10_url, tenant_signing_key=getattr(target, "journey_signing_key", None))
    except Exception as e:
        journey = {"status": "fail", "why": f"cloud checks/journey failed: {type(e).__name__}"}
    report = None
    try:
        report = teardown_phase(state, journey, target, reference_endpoint=reference_endpoint,
                                cost_ceiling_usd=cost_ceiling_usd, cost_report=cost_report)
    finally:
        if os.environ.get("HONUA_CLOUD_TENANT_FIXTURE") == "true":
            try:
                forget_secret(tenant_secret_name(state["cell"]))
            except Exception as error:
                if report is None:
                    raise RuntimeError("run-owned tenant secret cleanup failed") from None
                report["status"] = "fail"
                report["why"] = f"run-owned tenant secret cleanup failed: {type(error).__name__}"
                report.setdefault("cleanupFailures", []).append("tenant-secret-delete-failed")
    return report


# ---- job handoffs ----------------------------------------------------------------------------------
# provision.json and journey.json live in the cell's evidence directory, which each job uploads and
# the next downloads in place. They carry identifiers and verdicts; no credential is ever written.
HANDOFF_NAME = "provision.json"
JOURNEY_NAME = "journey.json"
STATE_SECRET_PREFIX = "honua-cloud-cell-state/"


def _cell(target_name: str, redis: str) -> str:
    return f"{target_name}/redis-{redis}"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def state_secret_name(cell: str) -> str:
    run_id, run_attempt = _run_identity()
    return f"{STATE_SECRET_PREFIX}{run_id}-{run_attempt}-{cell.replace('/', '-')}"


def app_key_secret_name(cell: str) -> str:
    return state_secret_name(cell) + "-app-key"


def tenant_secret_name(cell: str) -> str:
    return state_secret_name(cell) + "-tenant-signing-key"


def configure_tenant_fixture(target, cell: str, *, run=subprocess.run) -> None:
    """Opt-in run-owned issuer; only an existing-secret reference enters Terraform."""
    if os.environ.get("HONUA_CLOUD_TENANT_FIXTURE") != "true":
        return
    if target.name not in ("aws-ecs", "aws-serverless", "aws-eks"):
        raise ValueError("tenant fixture requires an owned AWS cell")
    if target.name != "aws-eks" and not target._root_declares("oidc_static_jwt"):
        raise ValueError("pinned IaC root does not support the typed tenant issuer")
    key = secrets.token_hex(32)
    result = run(["aws", "secretsmanager", "create-secret", "--name", tenant_secret_name(cell),
                  "--description", "Ephemeral cloud journey tenant issuer; deleted by teardown",
                  "--secret-string", "file:///dev/stdin"],
                 input=key, text=True, capture_output=True, check=True)
    observed = json.loads(result.stdout)
    arn, version = observed.get("ARN"), observed.get("VersionId")
    if (not isinstance(arn, str) or not re.fullmatch(r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", arn)
            or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", version)):
        raise ValueError("tenant signing secret has no immutable identity")
    target.journey_signing_key = key
    target.journey_signing_secret = {"arn": arn, "versionId": version}


def _with_secret_fd(run, argv, secret: str, **kwargs):
    """Hand `secret` to a child on an inherited pipe: never argv (/proc/*/cmdline) or a file."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, secret.encode("utf-8"))
        os.close(write_end)
        write_end = None
        return run([part.replace("{fd}", str(read_end)) for part in argv], pass_fds=(read_end,), **kwargs)
    finally:
        os.close(read_end)
        if write_end is not None:
            os.close(write_end)


def store_secret(name: str, value: str, description: str, *, run=subprocess.run) -> None:
    run(["aws", "secretsmanager", "create-secret", "--name", name, "--description", description,
         "--secret-string", "file:///dev/stdin"], input=value, text=True, capture_output=True, check=True)


def read_secret(name: str, *, run=subprocess.run) -> str:
    return run(["aws", "secretsmanager", "get-secret-value", "--secret-id", name, "--query",
                "SecretString", "--output", "text"], capture_output=True, text=True, check=True).stdout.rstrip("\n")


def forget_secret(name: str, *, run=subprocess.run) -> None:
    """Delete without a recovery window. A secret that was never created is already forgotten."""
    result = run(["aws", "secretsmanager", "delete-secret", "--secret-id", name,
                  "--force-delete-without-recovery"], capture_output=True, text=True, check=False)
    if result.returncode and "ResourceNotFoundException" not in (result.stderr or ""):
        raise subprocess.CalledProcessError(result.returncode, result.args)


def seal_state(root: Path, bundle: Path, secret_name: str, *, run=subprocess.run) -> str:
    """Encrypt the Terraform working directory for the teardown job; keep the key in AWS.

    The repository is public, so its artifacts are too: only ciphertext is uploaded. The passphrase
    and the ciphertext digest go to Secrets Manager, which only the credentialed jobs can read.
    """
    # Providers and modules are re-fetched by `terraform init`; a lock left by a cancelled apply
    # must not outlive the process that held it.
    archive = run(["tar", "-czf", "-", "--exclude=./.terraform", "--exclude=./.terraform.tfstate.lock.info",
                   "-C", str(root), "."],
                  capture_output=True, check=True).stdout
    passphrase = secrets.token_urlsafe(48)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    _with_secret_fd(run, ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                          "-pass", "fd:{fd}", "-out", str(bundle)], passphrase, input=archive, check=True)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    store_secret(secret_name, json.dumps({"passphrase": passphrase, "sha256": digest}),
                 "Ephemeral cloud-cell Terraform state key; deleted by teardown", run=run)
    return digest


def open_state(root: Path, bundle: Path, secret_name: str, *, run=subprocess.run) -> None:
    """Restore the sealed Terraform working directory. Fails closed on any digest mismatch."""
    secret = json.loads(read_secret(secret_name, run=run))
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != secret["sha256"]:
        raise ValueError("sealed Terraform state does not match the digest recorded at provision")
    archive = _with_secret_fd(run, ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                                    "-pass", "fd:{fd}", "-in", str(bundle)], secret["passphrase"],
                              capture_output=True, check=True).stdout
    root.mkdir(parents=True, exist_ok=True)
    run(["tar", "-xzf", "-", "-C", str(root)], input=archive, check=True)


# RSA-OAEP(SHA-256) seals at most k - 66 bytes per block (190 for a 2048-bit key). The handoff may
# carry the cell datasource as well as the key, so it is sealed as consecutive blocks of at most
# this many plaintext bytes; a value that fits one block seals exactly as before.
SEAL_BLOCK_BYTES = 190


def _modulus_bytes(private_key: Path, *, run=subprocess.run) -> int:
    text = run(["openssl", "pkey", "-in", str(private_key), "-noout", "-text"],
               capture_output=True, text=True, check=True).stdout
    bits = re.search(r"\((\d+) bit", text)
    if not bits or int(bits.group(1)) < 2048 or int(bits.group(1)) % 8:
        raise ValueError("journey runner key must be RSA of at least 2048 bits")
    return int(bits.group(1)) // 8


def seal_key(secret_name: str, public_key: Path, sealed: Path, *, run=subprocess.run, extend=None) -> None:
    """Encrypt the cell's journey handoff to the journey runner's own public key.

    Job outputs and step env are printed in the public job log, so the key reaches the
    credential-free journey job only as RSA-OAEP ciphertext in an artifact. `extend` adds what the
    admit job minted itself (the S10 presigned source URL) to the plaintext before it is sealed.
    """
    plaintext = read_secret(secret_name, run=run)
    if extend is not None:
        plaintext = extend(plaintext)
    plaintext = plaintext.encode("utf-8")
    ciphertext = b"".join(
        run(["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_key),
             "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256"],
            input=plaintext[offset:offset + SEAL_BLOCK_BYTES], capture_output=True, check=True).stdout
        for offset in range(0, len(plaintext), SEAL_BLOCK_BYTES))
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(ciphertext)


def open_key(sealed: Path, private_key: Path, *, run=subprocess.run) -> str:
    ciphertext = sealed.read_bytes()
    block = _modulus_bytes(private_key, run=run)
    if not ciphertext or len(ciphertext) % block:
        raise ValueError("sealed handoff is not a whole number of RSA blocks")
    return b"".join(
        run(["openssl", "pkeyutl", "-decrypt", "-inkey", str(private_key),
             "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256"],
            input=ciphertext[offset:offset + block], capture_output=True, check=True).stdout
        for offset in range(0, len(ciphertext), block)).decode("utf-8")


def verify_journey(state: dict, journey: dict, journey_dir: Path) -> dict:
    """Re-check the credential-free job's attempts; adopt no verdict it reported about itself.

    Every receipt must be this cell's, intact (digest), bound to this run and candidate
    (validate_attempt), and show the server identity provision observed before any pinned
    client ran. Only the verified files are copied into the cell's evidence directory.
    """
    cell = state["cell"]
    run_id, run_attempt = _run_identity()
    directory = cloud_journey.cell_dir(cell)
    records = journey.get("journeyAttempts")
    if (not isinstance(records, list) or len(records) > 2 or not all(isinstance(r, dict) for r in records)
            or [r.get("number") for r in records] != list(range(1, len(records) + 1))):
        raise ValueError("journey attempt history is not 1..n within two attempts")
    expected = cloud_journey.observed_server(state.get("observedServer"), state.get("runningImage"))
    expected_receipts = {f"receipt-{record['number']}.json" for record in records}
    expected_profiles = {f"serving-refusal-{record['number']}.json" for record in records if record.get("servingRefusal")}
    if (any(path.name not in expected_receipts for path in journey_dir.glob("receipt-*.json"))
            or any(path.name not in expected_profiles for path in journey_dir.glob("serving-refusal-*.json"))):
        raise ValueError("produced journey/profile artifact omitted from attempt history")
    verified: list[tuple[int, bytes, bytes | None]] = []
    blockers: list[list[str]] = []
    passed = False
    for record in records:
        if passed:
            raise ValueError("attempt recorded after a passing journey")
        number = record["number"]
        if record.get("receipt") != str((directory / f"receipt-{number}.json").relative_to(E2E_DIR)):
            raise ValueError("journey receipt path is not this cell's")
        data = (journey_dir / f"receipt-{number}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != record.get("receiptSha256"):
            raise ValueError("journey receipt digest mismatch")
        receipt = json.loads(data)
        if receipt["server"] != expected:
            raise ValueError("journey receipt server differs from what provision observed")
        config = journey_dir / f"target-{number}.json"
        config_bytes = config.read_bytes() if config.is_file() else None
        if receipt["target"].get("configSha256") and (
                config_bytes is None or hashlib.sha256(config_bytes).hexdigest() != receipt["target"]["configSha256"]):
            raise ValueError("journey target config digest mismatch")
        record.pop("profileObservation", None)
        if record.get("servingRefusal"):
            seed = journey_dir / "extended/seed-manifest.json"
            if (not seed.is_file() or seed.is_symlink() or not state.get("servingFixtureSha256")
                    or hashlib.sha256(seed.read_bytes()).hexdigest() != state["servingFixtureSha256"]):
                raise ValueError("journey serving fixture differs from the sealed provision handoff")
        passed = cloud_journey.qualify_attempt(record, receipt, cell,
            raw_path=journey_dir / f"receipt-{number}.json", run_id=run_id, run_attempt=run_attempt,
            require_observation=False)
        if record.get("servingRefusal"):
            for source in (journey_dir / f"serving-refusal-{number}.json",
                           journey_dir / "extended/seed-manifest.json"):
                relative = source.relative_to(journey_dir)
                destination = directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.resolve() != destination.resolve():
                    destination.write_bytes(source.read_bytes())
        if not passed:
            blockers.append(cloud_journey.documented_blockers(receipt))
        verified.append((number, data, config_bytes))
    directory.mkdir(parents=True, exist_ok=True)
    for number, data, config_bytes in verified:
        (directory / f"receipt-{number}.json").write_bytes(data)
        if config_bytes is not None:
            (directory / f"target-{number}.json").write_bytes(config_bytes)
    gate = journey_dir / "extended" / "gate-report.json"
    if gate.is_file() and gate.resolve() != (directory / "extended" / "gate-report.json").resolve():
        (directory / "extended").mkdir(parents=True, exist_ok=True)
        (directory / "extended" / "gate-report.json").write_bytes(gate.read_bytes())
    result = {**journey, "journeyAttempts": records}
    result.pop("blockedBy", None)  # re-derived from the verified receipts below, never adopted
    # A provisioned endpoint the journey recorded no attempt against is a failed journey, whatever
    # the upload claims: an empty history must not leave the cell's verdict to the other rows.
    if not passed and (records or state.get("endpoint")):
        if records and journey.get("status") != "fail":
            # Blocked only when every verified receipt names tracked limitations (see
            # cloud_journey.documented_blockers); the upload's own claim is not adopted.
            result.update(_journey_blocked_verdict(blockers, state.get("requireReal", False)))
            return result
        result["status"] = "fail"
        result["why"] = (journey.get("why") if journey.get("status") == "fail" else None) or (
            "journey did not pass within the recorded attempt budget" if records
            else "journey recorded no attempt against the provisioned endpoint")
    return result


DIAGNOSTICS_NAME = "diagnostics-ecs.json"
LAMBDA_DIAGNOSTICS_NAME = "diagnostics-lambda.json"
EKS_DIAGNOSTICS_NAME = "diagnostics-eks.json"
_LOG_LINES = 300
# Log streams merged per Lambda/Batch log group. A Lambda log group holds one stream per execution
# environment, so the last lines of the group span several streams. CloudWatch updates a stream's
# lastEventTimestamp lazily, so "the N newest by LastEventTime" can miss the environment that served
# last (run 38133126673 redis-on: the tail ended at an 11:04:15 cold start while the journey ran to
# 11:07, and the serving stream the replica check named was not among the five read). A short-lived
# cell has few streams, so every one up to this bound is read and the merge picks the true tail.
_LOG_STREAMS = 40
# Failure signatures counted over the WHOLE log group (filter-log-events), not just the tail: the
# tail of a busy API function covers about a second, so a refusal minutes earlier never shows in it.
_SIGNATURES = (
    ("db-connection-slots", "53300"),
    ("runtime-exit", "Runtime.ExitError"),
    ("unhandled-exception", "Unhandled exception"),
    ("task-timeout", "Task timed out"),
    ("db-timeout", "The operation has timed out"),
    ("preflight-failed", "preflight check failed"),
    ("mcp-tool-unexpected", "MCP tool invocation failed unexpectedly"),
)
_SIGNATURE_EVENTS = 5000
_SIGNATURE_SAMPLES = 60
# API Gateway access-log lines whose status is a 4xx/5xx. A response API Gateway produced itself
# (throttle, Lambda init or invoke failure) carries no CORS header, so a browser reports it as a CORS
# refusal; only the access log tells it apart from a response the server sent.
_ACCESS_ERROR_PATTERN = '{ ($.status = "4*") || ($.status = "5*") }'


def _cell_readers(target, run, redact):
    """`terraform output -raw` and read-only `aws ... --output json` against the cell's root/region."""
    root = target._iac_root()
    if root is None:
        raise ValueError("no Terraform working directory")

    def out(name):
        result = run(["terraform", f"-chdir={root}", "output", "-raw", name], text=True,
                     capture_output=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def aws(*args):
        result = run(["aws", *args, "--region", target.region, "--output", "json"], text=True,
                     capture_output=True, check=False)
        if result.returncode:
            detail = redact(str(result.stderr or "").strip())[-500:]
            raise RuntimeError(f"aws {' '.join(args[:2])} failed" + (f": {detail}" if detail else ""))
        return json.loads(result.stdout or "{}")

    return out, aws


def _log_group_tail(aws, group: str, redact) -> dict:
    """The last _LOG_LINES lines of a log group, merged by timestamp from its newest streams."""
    entry: dict = {"logGroup": group}
    try:
        streams = aws("logs", "describe-log-streams", "--log-group-name", group, "--order-by",
                      "LastEventTime", "--descending", "--max-items", str(_LOG_STREAMS)).get("logStreams", [])
    except RuntimeError as error:
        return {**entry, "error": str(error)}
    entry["logStreams"] = [str(s["logStreamName"]) for s in streams if s.get("logStreamName")]
    events, errors = [], []
    for stream in entry["logStreams"]:
        try:
            page = aws("logs", "get-log-events", "--log-group-name", group, "--log-stream-name", stream,
                       "--limit", str(_LOG_LINES), "--no-start-from-head")
        except RuntimeError as error:
            errors.append(f"{stream}: {error}")
            continue
        events += [(int(e.get("timestamp") or 0), stream, str(e.get("message", "")))
                   for e in page.get("events", [])]
    events.sort(key=lambda event: (event[0], event[1]))
    entry["lines"] = [redact(message.rstrip("\n")) for _, _, message in events][-_LOG_LINES:]
    if errors:
        entry["errors"] = errors
    return entry


def _iso_ms(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _filter_events(aws, group: str, pattern: str) -> list[tuple[int, str, str]]:
    """Every event of the whole log group matching `pattern`, oldest first (bounded)."""
    page = aws("logs", "filter-log-events", "--log-group-name", group, "--filter-pattern", pattern,
               "--max-items", str(_SIGNATURE_EVENTS))
    events = [(int(e.get("timestamp") or 0), str(e.get("logStreamName", "")), str(e.get("message", "")))
              for e in page.get("events", [])]
    events.sort(key=lambda event: (event[0], event[1]))
    return events


def _log_group_signatures(aws, group: str, redact) -> dict:
    """Failure signatures over the whole log group: per signature the matching lines, the streams
    (execution environments) they came from and the first/last time, plus a bounded sample.

    A .NET Lambda writes most entries twice (console text and its JSON twin), so `lines` counts
    log lines, not distinct failures; `streams` and the times are what line up with a request."""
    pattern = " ".join(f'?"{phrase}"' for _, phrase in _SIGNATURES)
    try:
        events = _filter_events(aws, group, pattern)
    except RuntimeError as error:
        return {"error": str(error)}
    summary: dict = {}
    samples = []
    for timestamp, stream, message in events:
        names = [name for name, phrase in _SIGNATURES if phrase in message]
        for name in names:
            row = summary.setdefault(name, {"lines": 0, "streams": set(), "first": timestamp, "last": timestamp})
            row["lines"] += 1
            row["streams"].add(stream)
            row["first"], row["last"] = min(row["first"], timestamp), max(row["last"], timestamp)
        if names and len(samples) < _SIGNATURE_SAMPLES:
            samples.append(f"{_iso_ms(timestamp)} [{stream}] {redact(message.rstrip(chr(10)))[:600]}")
    return {
        "signatures": {name: {"lines": row["lines"], "streams": len(row["streams"]),
                              "first": _iso_ms(row["first"]), "last": _iso_ms(row["last"])}
                       for name, row in summary.items()},
        "truncated": len(events) >= _SIGNATURE_EVENTS,
        "samples": samples,
    }


def _access_log_errors(aws, group: str, redact) -> dict:
    """API Gateway access-log 4xx/5xx lines of the whole group: counts by status and by
    status+method+path, and a bounded sample of the lines themselves."""
    try:
        events = _filter_events(aws, group, _ACCESS_ERROR_PATTERN)
    except RuntimeError as error:
        return {"error": str(error)}
    by_status: dict[str, int] = {}
    by_route: dict[str, int] = {}
    samples = []
    for timestamp, _, message in events:
        try:
            record = json.loads(message)
        except ValueError:
            record = {}
        record = record if isinstance(record, dict) else {}
        status = str(record.get("status", "?"))
        route = f"{status} {record.get('httpMethod', '?')} {str(record.get('path', '?')).split('?')[0]}"
        by_status[status] = by_status.get(status, 0) + 1
        by_route[route] = by_route.get(route, 0) + 1
        if len(samples) < _SIGNATURE_SAMPLES:
            samples.append(f"{_iso_ms(timestamp)} {redact(message.rstrip(chr(10)))[:600]}")
    top = sorted(by_route.items(), key=lambda item: (-item[1], item[0]))[:40]
    return {"byStatus": dict(sorted(by_status.items())), "byRoute": dict(top),
            "truncated": len(events) >= _SIGNATURE_EVENTS, "samples": samples}


def lambda_readiness_diagnostics(target, *, run=subprocess.run, redact=None) -> dict:
    """The serverless counterpart of ecs_readiness_diagnostics: what the cell's Lambda functions and
    Batch jobs logged about their own startup, dependency refusals and job execution.

    Functions are the API function, the control-plane event functions and every other function
    carrying the cell's name stem (`aws lambda list-functions`, filtered by the stem of the
    `lambda_function_name` output); Batch log groups are read when `gp_batch_enabled` is true. Read
    while the cell still exists (teardown, before destroy): its /aws/lambda/* and /aws/batch/* log
    groups are destroyed with it. Every line passes `redact`; only environment variable NAMES are
    recorded, never values.
    """
    redact = redact or (lambda text: text)
    out, aws = _cell_readers(target, run, redact)
    api = out("lambda_function_name")
    if not api:
        raise ValueError("the cell has no lambda_function_name output")
    # The module names every function "<name_prefix>-<environment>-<role>"; the API one is "-honua".
    stem = api[:-len("-honua")] if api.endswith("-honua") else api
    named = [name for name in (api, out("control_plane_reconcile_function_name"),
                               out("control_plane_backstop_function_name")) if name]
    # The CLI follows list-functions pagination itself.
    listed = {str(f["FunctionName"]): f for f in aws("lambda", "list-functions").get("Functions", [])
              if str(f.get("FunctionName", "")).startswith(f"{stem}-")}
    report: dict = {"nameStem": stem, "functions": [], "logs": []}
    for name in sorted(set(listed) | set(named), key=lambda n: (n != api, n)):
        function = listed.get(name, {})
        group = (function.get("LoggingConfig") or {}).get("LogGroup") or f"/aws/lambda/{name}"
        report["functions"].append({
            "functionName": name, "listed": name in listed, "state": function.get("State"),
            "lastUpdateStatus": function.get("LastUpdateStatus"),
            "stateReason": redact(str(function.get("StateReason") or "")),
            "lastModified": function.get("LastModified"), "architectures": function.get("Architectures"),
            "memorySize": function.get("MemorySize"), "timeout": function.get("Timeout"),
            # Names only: a missing or renamed setting shows up here, and no value can leak.
            "environmentNames": sorted(str(key) for key in
                                       (function.get("Environment") or {}).get("Variables") or {}),
            "logGroup": group})
        report["logs"].append({"functionName": name, **_log_group_tail(aws, group, redact),
                               "failureSignatures": _log_group_signatures(aws, group, redact)})
    # The module names the HTTP API after the API function and writes its access log to
    # /aws/apigateway/<api name>: the only record of a response API Gateway produced itself.
    access_group = f"/aws/apigateway/{api}"
    report["logs"].append({"accessLog": True, **_log_group_tail(aws, access_group, redact),
                           "accessErrors": _access_log_errors(aws, access_group, redact)})
    report["gpBatchEnabled"] = out("gp_batch_enabled").lower() == "true"
    if report["gpBatchEnabled"]:
        try:
            groups = aws("logs", "describe-log-groups", "--log-group-name-prefix",
                         f"/aws/batch/{stem}-").get("logGroups", [])
        except RuntimeError as error:
            report["logs"].append({"batch": True, "logGroup": f"/aws/batch/{stem}-*", "error": str(error)})
            groups = []
        for group in sorted(str(g["logGroupName"]) for g in groups if g.get("logGroupName")):
            report["logs"].append({"batch": True, **_log_group_tail(aws, group, redact),
                                   "failureSignatures": _log_group_signatures(aws, group, redact)})
    return report


def eks_readiness_diagnostics(target, *, redis_enabled: bool, redact=None) -> dict:
    """The EKS cell's pods, events, jobs and server log tail, read while the cluster still exists.

    The teardown runner is admitted to the cluster API first (its own /32, as for the destroy). Every
    section is redacted by the target (its generated credentials and the root's connection strings)
    and again by the shared log redaction.
    """
    redact = redact or (lambda text: text)
    target.grant_operator(redis_enabled=redis_enabled)
    return {"sections": [{"title": title, "lines": [redact(line) for line in body.splitlines()[-300:]]}
                         for title, body in target.diagnostic_sections()]}


def ecs_readiness_diagnostics(target, *, run=subprocess.run, redact=None) -> dict:
    """Why an ECS cell's tasks stopped: stop reasons, each stopped task's container log tail, and the
    environment variable NAMES each task definition sets.

    Read from ECS DescribeTasks / DescribeTaskDefinition and CloudWatch Logs while the cell still
    exists (teardown, before destroy). The cell's log group is destroyed with it, so this is the
    only place the exit cause survives. Output lands in the public cell artifact and the job log, so
    every log line passes `redact` and no environment VALUE is ever read into the report.
    """
    redact = redact or (lambda text: text)
    out, aws = _cell_readers(target, run, redact)
    cluster, service = out("ecs_cluster_name"), out("ecs_service_name")
    if not cluster or not service:
        raise ValueError("the cell has no ecs_cluster_name / ecs_service_name output")
    arns = []
    for status in ("RUNNING", "STOPPED"):
        arns += aws("ecs", "list-tasks", "--cluster", cluster, "--service-name", service,
                    "--desired-status", status).get("taskArns", [])
    tasks = aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", *arns).get("tasks", []) if arns else []
    report: dict = {"cluster": cluster, "service": service, "tasks": [], "logs": [], "taskDefinitions": []}
    definitions: dict = {}
    for task in tasks:
        report["tasks"].append({
            "taskArn": task.get("taskArn"), "lastStatus": task.get("lastStatus"),
            "desiredStatus": task.get("desiredStatus"), "healthStatus": task.get("healthStatus"),
            "stopCode": task.get("stopCode"), "stoppedReason": redact(str(task.get("stoppedReason") or "")),
            "startedAt": task.get("startedAt"), "stoppedAt": task.get("stoppedAt"),
            "containers": [{"name": c.get("name"), "lastStatus": c.get("lastStatus"),
                            "exitCode": c.get("exitCode"), "healthStatus": c.get("healthStatus"),
                            "reason": redact(str(c.get("reason") or ""))}
                           for c in task.get("containers", [])]})
        arn = task.get("taskDefinitionArn")
        if arn and arn not in definitions:
            definitions[arn] = aws("ecs", "describe-task-definition", "--task-definition", arn).get(
                "taskDefinition", {})
    # Names only: a missing or renamed setting shows up here, and no value can leak.
    for arn, definition in definitions.items():
        report["taskDefinitions"].append({"taskDefinitionArn": arn, "containers": [
            {"name": container.get("name"),
             "environmentNames": sorted(str(item.get("name")) for item in container.get("environment") or []
                                        if item.get("name")),
             "valueFromNames": sorted(str(item.get("name")) for item in container.get("secrets") or []
                                   if item.get("name"))}
            for container in definition.get("containerDefinitions", [])]})
    # Every stopped task, and the newest task whatever its state. The awslogs stream of a container is
    # <awslogs-stream-prefix>/<container name>/<task id>.
    ordered = sorted(tasks, key=lambda t: str(t.get("createdAt") or ""), reverse=True)
    chosen = [t for t in ordered if t.get("lastStatus") == "STOPPED"]
    if ordered and ordered[0] not in chosen:
        chosen.insert(0, ordered[0])
    for task in chosen:
        task_id = str(task.get("taskArn", "")).rsplit("/", 1)[-1]
        for container in definitions.get(task.get("taskDefinitionArn"), {}).get("containerDefinitions", []):
            options = (container.get("logConfiguration") or {}).get("options") or {}
            group, prefix = options.get("awslogs-group"), options.get("awslogs-stream-prefix")
            if not group or not prefix:
                continue
            stream = f"{prefix}/{container.get('name')}/{task_id}"
            entry = {"taskArn": task.get("taskArn"), "container": container.get("name"),
                     "logGroup": group, "logStream": stream}
            try:
                events = aws("logs", "get-log-events", "--log-group-name", group, "--log-stream-name",
                             stream, "--limit", str(_LOG_LINES), "--no-start-from-head").get("events", [])
            except RuntimeError as error:
                report["logs"].append({**entry, "error": str(error)})
                continue
            report["logs"].append({**entry,
                                   "lines": [redact(str(e.get("message", ""))) for e in events][-_LOG_LINES:]})
    return report


def _redact_log(text: str) -> str:
    """Strip credentials a server log line might carry before it reaches a public artifact."""
    text = re.sub(r"(?i)(password|pwd|masterkey|api[-_]?key|secret|token)(\s*[=:]\s*)[^;\s\"',]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(x-api-key\s*:\s*)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@", r"\1[redacted]@", text)
    # Any other URI userinfo password (redis://, rediss://, amqp://, https://user:pass@...).
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s/]+@", r"\1[redacted]@", text)
    # A whole connection-string setting, whatever its provider syntax.
    text = re.sub(r"(?i)(connectionstrings?(?:__|:)\w+\s*=\s*)\S+", r"\1[redacted]", text)
    # AWS access key ids.
    text = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted-aws-key-id]", text)
    return text


def _phase_diagnose(args) -> int:
    """Teardown-side readiness diagnostics; never changes the verdict, never fails the job."""
    cell = _cell(args.target, args.redis)
    directory = cloud_journey.cell_dir(cell)
    serverless = args.target == "aws-serverless"
    eks = args.target == "aws-eks"
    name = (LAMBDA_DIAGNOSTICS_NAME if serverless else EKS_DIAGNOSTICS_NAME if eks else DIAGNOSTICS_NAME)
    # Runs for a ready cell too: a task that served and later exited (EssentialContainerExited), or a
    # Lambda that refused a dependency, is only explainable from its log, and the log group goes
    # with the cell.
    try:
        if eks:
            diagnostics = eks_readiness_diagnostics(_target(args.target), redis_enabled=args.redis == "on",
                                                    redact=_redact_log)
        else:
            collect = lambda_readiness_diagnostics if serverless else ecs_readiness_diagnostics
            diagnostics = collect(_target(args.target), redact=_redact_log)
    except Exception as error:
        diagnostics = {"error": f"{type(error).__name__}: {_redact_log(str(error))}"}
    diagnostics["cell"] = cell
    _write_json(directory / name, diagnostics)
    if "error" in diagnostics:
        print(f"   diagnostics unavailable: {diagnostics['error']}")
    for task in diagnostics.get("tasks", []):
        print(f"   task {task['taskArn']}: {task['lastStatus']} stopCode={task['stopCode']} "
              f"stoppedReason={task['stoppedReason']!r}")
        for container in task.get("containers", []):
            print(f"     container {container.get('name')}: exitCode={container.get('exitCode')} "
                  f"reason={container.get('reason')!r}")
    for definition in diagnostics.get("taskDefinitions", []):
        for container in definition.get("containers", []):
            print(f"   {definition['taskDefinitionArn']} {container['name']}: environment names "
                  f"{', '.join(container['environmentNames']) or '(none)'}; valueFrom names "
                  f"{', '.join(container['valueFromNames']) or '(none)'}")
    for function in diagnostics.get("functions", []):
        print(f"   function {function['functionName']}: state={function.get('state')} "
              f"lastUpdateStatus={function.get('lastUpdateStatus')} "
              f"stateReason={function.get('stateReason')!r}; environment names "
              f"{', '.join(function.get('environmentNames') or []) or '(none)'}")
    for section in diagnostics.get("sections", []):
        print(f"::group::{section['title']}")
        for line in section["lines"]:
            print(f"   {line}")
        print("::endgroup::")
    for log in diagnostics.get("logs", []):
        owner = (log.get("taskArn") or log.get("functionName") or ("batch" if log.get("batch") else "")
                 or ("access log" if log.get("accessLog") else ""))
        print(f"::group::{log.get('logStream') or log.get('logGroup')} ({owner})")
        if "error" in log:
            print(f"   log unavailable: {log['error']}")
        if log.get("logStreams") is not None:
            print(f"   streams read: {len(log['logStreams'])}"
                  + (" (bound reached; more exist)" if len(log["logStreams"]) >= _LOG_STREAMS else ""))
        signatures = log.get("failureSignatures") or {}
        if "error" in signatures:
            print(f"   failure signatures unavailable: {signatures['error']}")
        for signature, row in (signatures.get("signatures") or {}).items():
            print(f"   signature {signature}: {row['lines']} lines from {row['streams']} stream(s), "
                  f"{row['first']} .. {row['last']}")
        for line in signatures.get("samples", []):
            print(f"   ! {line}")
        access = log.get("accessErrors") or {}
        if "error" in access:
            print(f"   access-log errors unavailable: {access['error']}")
        if access.get("byStatus"):
            print(f"   access-log 4xx/5xx by status: {access['byStatus']}")
            for route, count in access.get("byRoute", {}).items():
                print(f"   access-log {route}: {count}")
        for error in log.get("errors", []):
            print(f"   stream unavailable: {error}")
        for line in log.get("lines", []):
            print(f"   {line}")
        print("::endgroup::")
    print(f"{cell}: readiness diagnostics written to {directory / name}")
    return 0


def _target(target_name: str):
    return REGISTRY[target_name](run_id=os.environ.get("GITHUB_RUN_ID", "local"))


def _print_report(report: dict) -> None:
    print(f"== cloud-parity :: {report['cell']} -> {report['status'].upper()} ==")
    print(f"   {report.get('why', '')}")
    if report["status"] == "skipped":
        # A clear, machine-greppable notice so the self-skip is obvious in the job log / summary.
        print(f"::notice title=cloud-cert self-skipped::{report['cell']}: cloud-creds-unset "
              "(set HONUA_AWS_ROLE_ARN to enforce this tier per-RC)")
    for c in report.get("checks", []):
        print(f"   [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for c in report.get("canaryProbes", []):
        print(f"   canary [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for c in report.get("scenarioCoverage", []):
        print(f"   scenario [{c['status'].upper():7}] {c['name']}: {c['why']}")
    if "parity" in report:
        print(f"   parity: {report['parity']['status']} — {report['parity']['why']}")
    print(f"   (written to {REPORT_PATH})")


def _phase_provision(args) -> int:
    target = _target(args.target)
    cell = _cell(args.target, args.redis)
    # The Actions run id is public, so the cell's application key must not be derived from it.
    # It reaches the journey job only sealed to that runner's own public key.
    os.environ.setdefault("HONUA_ADMIN_PASSWORD", f"Honua-Gate-Aa1!{secrets.token_urlsafe(32)}")
    datasource: dict = {}
    client_context = ({} if os.environ.get("HONUA_CLOUD_INSTALLED_CLIENTS") == "true"
                      and args.redis == "on" and args.target in ("aws-ecs", "aws-serverless") else None)
    state = provision_phase(target, args.target, require_real=args.require_real,
                            redis_enabled=args.redis == "on", journey_datasource=datasource, installed_context=client_context)
    if state["endpoint"]:
        # The admit job seals this to the journey runner's public key (seal_key). With a seeded
        # cell it also carries the seed's connection: the terminal journey's datasource
        # (honua-release#377). It exists only in Secrets Manager and sealed to that runner.
        try:
            store_secret(app_key_secret_name(cell),
                         cloud_journey.pack_handoff(target.admin_api_key, datasource or None,
                             tenant_signing_key=getattr(target, "journey_signing_key", None),
                             installed_clients=client_context or None),
                         "Ephemeral cloud-cell application key; deleted by teardown")
        except Exception as error:
            state["report"].update(status="fail", why="could not hand the cell's application key to "
                                   f"the journey job: {type(error).__name__}")
            state["endpoint"] = None
    _write_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME, state)
    _set_output("endpoint", state["endpoint"] or "")
    _set_output("admission", state["admission"] if state["endpoint"] else "none")
    report = state["report"]
    print(f"== cloud-provision :: {cell} -> {report.get('status', 'provisioned').upper()} ==")
    print(f"   {report.get('why', state['endpoint'] or '')}")
    # A provisioning failure is recorded for teardown, which owns the cell's final verdict.
    return 0


def _phase_journey(args) -> int:
    cell = _cell(args.target, args.redis)
    state = _read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME)
    if state is None or state.get("cell") != cell:
        journey = {"cell": cell, "status": "fail", "why": "provision handoff missing or for another cell"}
    else:
        try:
            handoff = (open_key(args.sealed_key, args.private_key) if args.sealed_key
                       else os.environ.get("HONUA_CLOUD_ADMIN_KEY", ""))
            admin_key, datasource = cloud_journey.unpack_handoff(handoff)
            s10_url = cloud_journey.handoff_source_url(handoff)
            tenant_key = cloud_journey.handoff_tenant_signing_key(handoff)
            client_context = json.loads(handoff).get("installedClients") if handoff.startswith("{") else None
        except Exception:
            admin_key, datasource, s10_url, tenant_key = "", None, None, None
            client_context = None
        with cloud_journey.cell_datasource(datasource):
            journey = journey_phase(state, admin_key=admin_key, max_attempts=args.max_attempts,
                                    s10_source_url=s10_url, tenant_signing_key=tenant_key,
                                    installed_context=client_context)
    _write_json(cloud_journey.cell_dir(cell) / JOURNEY_NAME, journey)
    print(f"== cloud-journey :: {cell} -> {journey.get('status', 'recorded').upper()} ==")
    for c in journey.get("scenarioCoverage", []):
        print(f"   scenario [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for record in journey.get("journeyAttempts", []):
        print(f"   attempt {record['number']}: {record['receipt']}")
    return 1 if journey.get("status") == "fail" else 0


# Approved GA cells; nightly selects ECS Redis-off while promotion can exercise all four.
MODEL_CANARY_CELLS = frozenset(f"{target}/redis-{redis}" for target in ("aws-ecs", "aws-serverless")
                               for redis in ("off", "on"))
MODEL_JOURNEY_DIR = "model-journey"
LOCK_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _passing_deterministic_receipt(cell_directory: Path, *, prerequisite_profile: str | None = None) -> Path | None:
    """The cell journey's passing receipt from this job, which the canary protocol requires."""
    journey = _read_json(cell_directory / JOURNEY_NAME) or {}
    records = journey.get("journeyAttempts")
    if not isinstance(records, list):
        return None
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        path = E2E_DIR / str(record.get("receipt", ""))
        if (path.resolve().parent != cell_directory.resolve()
                or not re.fullmatch(r"receipt-[12]\.json", path.name)):
            continue
        receipt = _read_json(path) if path.is_file() else None
        if receipt and receipt.get("status") in ({"pass", "blocked"} if prerequisite_profile == "redis-off-serving-refusal" else {"pass"}):
            return path
    return None


def model_canary_phase(state: dict, *, admin_key: str, lock_digest: str | None, run=subprocess.run,
                       max_attempts: int = 2, tenant_signing_key: str | None = None) -> dict:
    """Credential-free: run the genuine-model canary on the cell after its deterministic journey.

    Every attempt is bound to the lock digest under certification and chained to this job's passing
    deterministic receipt. Without a lock digest (a manual dispatch: no nightly lock to bind to) the
    harness runs once in its own unbound mode, which writes an honest `blocked` receipt, and the row
    is `blocked`: never a pass, never retried. The cell's application key reaches the harness only
    through the environment of its own process. The result is the journey row the nightly mint retains as
    promotion-receipts/nightly-model-journey (tools/model_journey_report.py).
    """
    import model_journey_report  # noqa: PLC0415 (tools/ is added to sys.path by the caller)

    cell = state["cell"]
    directory = cloud_journey.cell_dir(cell)
    out = directory / MODEL_JOURNEY_DIR
    out.mkdir(parents=True, exist_ok=True)
    run_id, run_attempt = _run_identity()
    receipts: list[Path] = []
    why = None
    if state.get("handoffError"):
        why = str(state["handoffError"])
    elif cell not in MODEL_CANARY_CELLS:
        why = f"{cell} is not an approved genuine-model GA cell"
    elif not state.get("endpoint") or not admin_key:
        why = "the cell has no endpoint or application key for the canary"
    profile = "redis-off-serving-refusal" if cell.endswith("/redis-off") else "full-workflow"
    deterministic = _passing_deterministic_receipt(directory, prerequisite_profile=profile) if why is None else None
    if why is None and deterministic is None:
        why = "the cell's deterministic journey did not pass in this job; the canary is refused"
    target = (deterministic.with_name(deterministic.name.replace("receipt-", "target-"))
              if deterministic is not None else None)
    if why is None and (target is None or not target.is_file()):
        why = "the passing deterministic journey has no retained target config; the canary is refused"
    if why is None:
        config = _read_json(target) or {}
        prerequisite = _read_json(deterministic) or {}
        if (target.resolve().parent != directory.resolve()
                or config.get("id") != cell
                or prerequisite.get("target", {}).get("id") != cell
                or prerequisite.get("target", {}).get("configSha256") != hashlib.sha256(target.read_bytes()).hexdigest()
                or str(config.get("replicaBaseUrl", "")).rstrip("/") != str(state["endpoint"]).rstrip("/")):
            why = "the retained target config is not bound to this deterministic cell and endpoint"
    setup_notices = []
    if why is None:
        driver, _adapter = cloud_journey.drivers()
        # Candidate subprocesses receive application capabilities only, never CI/cloud credentials.
        env = {**driver.pins.candidate_env(),
               **{key: os.environ[key] for key in ("TERMINAL_MODEL_NAME", "TERMINAL_MODEL_RUNTIME",
                   "TERMINAL_MODEL_QUANTIZATION", "TERMINAL_MODEL_SIGNING_MANIFEST_SHA256") if key in os.environ},
               "TERMINAL_MODEL_API_KEY": admin_key, "HONUA_CLOUD_JOURNEY_ADMIN": admin_key,
               "HONUA_CANARY_HTTP_CELL_HOST": urllib.parse.urlsplit(str(state["endpoint"])).hostname or ""}
        if tenant_signing_key is not None:
            env["HONUA_CANARY_TENANT_SIGNING_KEY"] = tenant_signing_key
        binding = ["--lock-digest", lock_digest] if lock_digest is not None else []
        attempts = max_attempts if lock_digest is not None else 1
        for number in range(1, attempts + 1):
            output = out / f"model-canary-{number}.json"
            # Deterministic setup left fixed-name fixture objects. Reset them before every model
            # attempt; the model must issue fresh writes instead of inheriting a completed journey.
            try:
                setup_notices.append({"modelAttempt": number, "notices": cloud_journey.reset_prior_attempt(
                    state["endpoint"], admin_key, config.get("execution"), number + 1)})
            except Exception as error:
                why = f"fixture reset before model attempt {number} failed ({type(error).__name__})"
                break
            output.unlink(missing_ok=True)  # An earlier phase invocation cannot stand in for this attempt.
            endpoint = str(state["endpoint"]).rstrip("/")
            argv = [sys.executable, str(E2E_DIR.parent / "tools" / "terminal_model_canary.py"),
                    "--base-url", endpoint + "/api",
                    # Owner ruling canary-http-cell-2026-10-08: plain HTTP only to this cell's own
                    # harness-provisioned ALB host, never to any other non-loopback host.
                    "--allow-http-cell", urllib.parse.urlsplit(endpoint).hostname or "",
                    "--require-api-key",
                    # The cell's application key goes as X-API-Key, never as a bearer token: a cell
                    # composed with the tenant fixture enables OIDC (oidc_static_jwt), which routes
                    # every bearer token to the JWT handler, so the Studio AI proxy would answer 401
                    # (the local-docker canary's failure, fixed the same way in #795). X-API-Key
                    # is how every other harness call already presents this key (admin_request).
                    "--api-key-header", "x-api-key",
                    "--profile", profile, "--cell", cell, "--attempt", str(number),
                    "--driver-command", str(E2E_DIR.parent / "certification" / "terminal-journey" / "live_driver.py"),
                    "--target", str(target),
                    *binding,
                    "--deterministic-receipt", str(deterministic.relative_to(E2E_DIR.parent)),
                    "--output", str(output)]
            completed = run(argv, env=env, cwd=E2E_DIR.parent, check=False)
            if not output.is_file():
                why = f"canary harness refused the cell before writing attempt {number} (exit {completed.returncode})"
                break
            receipts.append(output)
            if completed.returncode == 0:
                break
    report = model_journey_report.build(receipts, cell=cell, lock_digest=lock_digest,
                                        candidate_digest=cloud_journey.candidate_digest(),
                                        run_id=run_id, run_attempt=run_attempt)
    report["harnessSetup"] = setup_notices
    if why is not None:
        report["cells"][0]["why"] = report["why"] = why
        report["cells"][0]["status"] = report["status"] = report["overallStatus"] = "fail"
    _write_json(out / "gate-report-journey.json", report)
    return report


def _phase_update_rollback(args) -> int:
    """Run on the credentialed retained controller after the ordinary journey, before teardown."""
    from runner.update_rollback import load_release, run_cell, ProofError, preflight
    from runner.update_rollback_aws import AwsRuntime
    cell = _cell(args.target, args.redis)
    directory = cloud_journey.cell_dir(cell)
    output = directory / "update-rollback-cell.json"
    output.unlink(missing_ok=True)
    runtime = None
    receipt = {"schema": "honua.aws-update-rollback-cell/v1", "cell": cell, "status": "fail",
               "mode": args.update_rollback_mode, "runId": _run_identity()[0], "runAttempt": _run_identity()[1]}
    try:
        if not args.prior_inputs or not args.transition_inputs or not args.serving_fixture:
            raise ProofError("prior/candidate authenticated input bundles and retained serving fixture required")
        prior = load_release(args.prior_inputs, args.update_rollback_mode)
        candidate = load_release(args.transition_inputs, args.update_rollback_mode)
        preflight(prior, candidate, cell, args.update_rollback_mode)
        state = _read_json(directory / HANDOFF_NAME)
        if (not state or state.get("cell") != cell or
                (state.get("runId"), state.get("runAttempt")) != _run_identity()):
            raise ProofError("restored provision handoff is bound to another workflow attempt")
        # The actual initial deployment must be A. Never relabel an ordinary B deployment as A.
        if "sha256:" + state.get("candidateDigest", "") != prior.manifest_digest:
            raise ProofError("provisioned baseline manifest differs from the authenticated prior inputs")
        journey = _read_json((args.journey_dir or directory) / JOURNEY_NAME)
        if not journey or journey.get("cell") != cell:
            raise ProofError("ordinary baseline journey must run before update/rollback")
        verify_journey(state, journey, args.journey_dir or directory)
        target = _target(args.target)
        target._workdir = target._iac_root()
        target.transition_release = prior
        if args.target == "aws-ecs":
            target.admit(state["endpoint"], os.environ.get("HONUA_AWS_RUNNER_CIDR", ""), redis_enabled=args.redis == "on")
        # Original application key stays in the owned Secrets Manager handoff; never persist it.
        admin_key, _ = cloud_journey.unpack_handoff(read_secret(app_key_secret_name(cell)))
        os.environ["HONUA_ADMIN_PASSWORD"] = admin_key
        # The controller store and its approver are hermetic and minted per run (AwsRuntime).
        runtime = AwsRuntime(target, state, admin_key=admin_key,
            serving_fixture=args.serving_fixture, controller_env=args.controller_env,
            mirror_repository=os.environ.get("HONUA_UPDATE_ROLLBACK_ECR_REPOSITORY"), vertices=args.gp_vertices, workload_path=args.gp_workload)
        receipt = run_cell(prior, candidate, cell, args.update_rollback_mode, runtime,
                           run_id=_run_identity()[0], run_attempt=_run_identity()[1])
    except ProofError as error:
        receipt["finding"] = str(error)
    except Exception as error:
        receipt["finding"] = "update/rollback prerequisite raised " + type(error).__name__
    finally:
        if runtime:
            cleanup = runtime.close()
            receipt["controllerCleanupFailures"] = cleanup
            if cleanup:
                receipt["status"] = "fail"
        _write_json(output, receipt)
    print(f"{cell}: update/rollback {receipt['status']}; retained {output}")
    return 0 if receipt.get("status") == "pass" else 1


def _phase_model_canary(args) -> int:
    sys.path.insert(0, str(E2E_DIR.parent / "tools"))
    cell = _cell(args.target, args.redis)
    state = _read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME) or {"cell": cell}
    if state.get("cell") != cell:
        state = {"cell": cell, "handoffError": "provision handoff is for another cell; the model is refused"}
    try:
        handoff = open_key(args.sealed_key, args.private_key)
        admin_key, datasource = cloud_journey.unpack_handoff(handoff)
        tenant_key = cloud_journey.handoff_tenant_signing_key(handoff)
    except Exception:
        admin_key, datasource, tenant_key = "", None, None
    with cloud_journey.cell_datasource(datasource):
        report = model_canary_phase(state, admin_key=admin_key, lock_digest=args.lock_digest,
                                    max_attempts=args.max_attempts, tenant_signing_key=tenant_key)
    print(f"== cloud-model-canary :: {cell} -> {report['status'].upper()} ==")
    print(f"   {report['why']}")
    if args.lock_digest is None and report["status"] == "blocked":
        # The honest outcome of an unbound run: recorded, surfaced, and not a red step.
        print(f"::notice::genuine-model canary blocked: {report['why']}")
        return 0
    return 0 if report["status"] == "pass" else 1


def _phase_admit(args) -> int:
    cell = _cell(args.target, args.redis)
    state = _read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME)
    if state is None or not state.get("endpoint"):
        raise SystemExit(f"{cell}: no provisioned endpoint to admit the journey runner to")
    _target(args.target).admit(state["endpoint"], args.cidr, redis_enabled=args.redis == "on")
    print(f"admitted the journey runner to {cell}")
    return 0


def s10_handoff_extension(state: dict | None, cell: str, *, run=subprocess.run):
    """(extend, note): the sealed-handoff extension carrying the S10 presigned source URL, or None.

    Minted here, in the admit job, so its lifetime starts as late as the credentialed jobs allow. A
    presign failure never stops the key delivery: the journey's S10 row then reports the missing URL.
    """
    record = (state or {}).get("s10")
    if not isinstance(record, dict) or not record.get("staged"):
        return None, f"{cell}: no S10 fixture was staged; no source URL to seal"
    try:
        url = large_import.presign(record, cell, run=run)
    except subprocess.CalledProcessError as error:
        return None, f"{cell}: S10 presign failed: {large_import.aws_error(error)}"
    except Exception as error:
        return None, f"{cell}: S10 presign failed: {type(error).__name__}"
    return ((lambda plaintext: cloud_journey.attach_source_url(plaintext, url)),
            f"{cell}: sealed the S10 presigned source URL (valid {large_import.PRESIGN_TTL_SECONDS}s) with the key")


def _phase_deliver_key(args) -> int:
    cell = _cell(args.target, args.redis)
    extend, note = s10_handoff_extension(_read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME), cell)
    seal_key(app_key_secret_name(cell), args.public_key, args.sealed_key, extend=extend)
    print(f"sealed the {cell} application key to the journey runner's public key")
    print(note if extend else f"::warning title=S10 large-import::{note}")
    return 0


def _phase_seal(args) -> int:
    cell = _cell(args.target, args.redis)
    root = _target(args.target)._iac_root()
    if root is None:
        raise SystemExit(f"{cell}: no Terraform working directory to seal")
    seal_state(root, args.bundle, state_secret_name(cell))
    print(f"sealed {cell} Terraform state for teardown")
    return 0


def _phase_open(args) -> int:
    cell = _cell(args.target, args.redis)
    root = _target(args.target)._iac_root()
    if root is None:
        raise SystemExit(f"{cell}: no Terraform working directory to restore into")
    open_state(root, args.bundle, state_secret_name(cell))
    _mark_provision_attempt()
    print(f"restored {cell} Terraform state")
    return 0


def _phase_teardown(args) -> int:
    cell = _cell(args.target, args.redis)
    directory = cloud_journey.cell_dir(cell)
    state = _read_json(directory / HANDOFF_NAME)
    # The journey's upload lives apart from provision's handoff; nothing in it is adopted unverified.
    journey_dir = args.journey_dir or directory
    journey = _read_json(journey_dir / JOURNEY_NAME) or {}
    marker = os.environ.get("HONUA_CLOUD_PROVISION_MARKER")
    restored = bool(marker) and Path(marker).is_file()
    problems = []
    cleanup_unconfirmed = False
    if state is None or state.get("cell") != cell:
        problems.append("provision handoff missing")
        cleanup_unconfirmed = True
        state = {"cell": cell, "redis": f"redis-{args.redis}", "requireReal": args.require_real,
                 "startedAt": cloud_journey.now(), "provisionAttempted": restored,
                 "report": {"gate": "cloud-parity", "target": args.target, "redis": f"redis-{args.redis}",
                            "cell": cell, "require_real": args.require_real,
                            "evidenceTier": "Preview" if args.target in cloud_journey.PREVIEW_TARGETS else "GA",
                            "journeyAttempts": []}}
    elif (state.get("runId"), state.get("runAttempt"), state.get("candidateDigest")) != (
            *_run_identity(), cloud_journey.candidate_digest()):
        problems.append("provision handoff is bound to another run or candidate")
        cleanup_unconfirmed = True
    if not isinstance(state.get("provisionAttempted"), bool):
        cleanup_unconfirmed = True
    if journey and journey.get("cell") != cell:
        problems.append("journey handoff is for another cell")
        journey = {}
    elif state.get("endpoint") and not journey:
        problems.append("journey job left no handoff")
    if journey:
        try:
            journey = verify_journey(state, journey, journey_dir)
        except ValueError as error:
            problems.append(f"journey evidence rejected: {error}")
            journey = {"cell": cell, "status": "fail", "why": "journey evidence rejected"}
        except Exception as error:
            problems.append(f"journey evidence rejected: {type(error).__name__}")
            journey = {"cell": cell, "status": "fail", "why": "journey evidence rejected"}
    attempted = bool(state.get("provisionAttempted")) or restored
    stranded = attempted and not restored
    state["provisionAttempted"] = attempted
    target = _target(args.target)
    if restored and hasattr(target, "grant_operator"):
        # A fresh runner: the cluster API admits only addresses it was told about.
        try:
            target.grant_operator(redis_enabled=args.redis == "on")
        except Exception as error:
            print(f"::warning title=cloud teardown access::{type(error).__name__}")
    if restored:
        init = subprocess.run(["terraform", f"-chdir={target._iac_root()}", "init", "-input=false",
                               "-no-color"], text=True, capture_output=True)
        if init.returncode:
            problems.append("terraform init failed before teardown")
    report = teardown_phase(state, journey, target, reference_endpoint=args.reference_endpoint,
                            cost_ceiling_usd=args.cost_ceiling_usd, cost_report=args.cost_report,
                            destroy=restored)
    if cleanup_unconfirmed and not restored:
        # Absence of both handoff and restored state is not evidence that apply never ran.
        report.setdefault("cleanupFailures", []).append("provisioning-obligation-unconfirmed")
    if stranded:
        problems.append("the cell provisioned infrastructure but its sealed Terraform state never "
                        "reached teardown; reap it by hand (honua-iac#142)")
    secret_names = [app_key_secret_name(cell)] + ([tenant_secret_name(cell)] if
        state.get("tenantFixtureEnabled") or os.environ.get("HONUA_CLOUD_TENANT_FIXTURE") == "true" else []) + ([state_secret_name(cell)] if state.get("destroyed") and not report.get("retirement", {}).get("originalStateKeyRetained") else [])
    for name in secret_names:
        try:
            forget_secret(name)
        except Exception as error:
            problems.append(f"{name} could not be deleted: {type(error).__name__}")
            report.setdefault("cleanupFailures", []).append("owned-secret-delete-failed")
    if problems:
        report["status"] = "fail"
        report["why"] = "; ".join(filter(None, [report.get("why"), *problems]))
    if args.target == "aws-eks" and os.environ.get("HONUA_DEPLOYMENT_PREPARED") == "true":
        try:
            from deployment_cell import write_receipt
            write_receipt(report, directory, E2E_DIR)
        except Exception as error:
            report["status"] = "fail"
            report["why"] = "; ".join(filter(None, [report.get("why"),
                f"before-actuation deployment proof could not be retained: {type(error).__name__}"]))
    report.setdefault("evidence_url", os.environ.get("HONUA_RUN_URL", ""))
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _print_report(report)
    if args.target in cloud_journey.PREVIEW_TARGETS:
        # Keep the qualification outcome in the receipt. Only its informational verdict is
        # tolerated; cleanup and cost failures remain hard process/workflow failures.
        return int(cloud_journey.cleanup_failure(report) is not None
                   or report.get("cost", {}).get("status") == "fail")
    return 1 if report["status"] == "fail" else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=("all", "provision", "seal-state", "journey", "model-canary", "update-rollback",
                                        "deliver-key", "admit", "open-state", "diagnose", "teardown"), default="all",
                    help="CI runs provision, journey and teardown as separate jobs (honua-release#381)")
    ap.add_argument("--bundle", type=Path, help="sealed Terraform state (seal-state / open-state)")
    ap.add_argument("--cidr", help="the journey runner's IPv4 /32 (admit)")
    ap.add_argument("--public-key", type=Path, help="the journey runner's RSA public key (deliver-key)")
    ap.add_argument("--private-key", type=Path, help="this journey runner's RSA private key (journey)")
    ap.add_argument("--journey-dir", type=Path,
                    help="where the journey job's upload was downloaded (teardown); verified, never trusted")
    ap.add_argument("--sealed-key", type=Path,
                    help="the application key sealed to the journey runner (deliver-key writes, journey reads)")
    ap.add_argument("--max-attempts", type=int, choices=(1, 2), default=2)
    ap.add_argument("--lock-digest", help="sha256:<hex> of the platform lock the genuine-model canary binds to; "
                                          "omitted on a manual dispatch, the canary records an unbound `blocked`")
    ap.add_argument("--prior-inputs", type=Path, help="retained authenticated baseline input directory")
    ap.add_argument("--transition-inputs", type=Path, help="retained authenticated destination input directory")
    ap.add_argument("--update-rollback-mode", choices=("rehearsal", "qualification"), default="rehearsal")
    ap.add_argument("--serving-fixture", type=Path, help="baseline extended/seed-manifest.json bound by sealed provision state")
    ap.add_argument("--controller-env", type=Path, help="optional private controller startup supplement (no store, wiring or credentials); never uploaded")
    ap.add_argument("--gp-workload", type=Path, help="reviewed real GP execution+independent feature fixture bound by BOTH retained manifests")
    ap.add_argument("--gp-vertices", type=int, default=65536, help="reviewed symmetric centroid workload size; fast completion fails")
    ap.add_argument("--cost-ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    ap.add_argument("--cost-report", type=Path)
    ap.add_argument("--scenario", choices=("gp_batch_native",), default=None,
                    help="judge gp_batch_native without provisioning; a missing native receipt stays blocked, or fails under --require-real")
    ap.add_argument("--target", default="aws-serverless", choices=sorted(REGISTRY))
    ap.add_argument("--redis", choices=["on", "off"], default="off",
                    help="run the target with Redis enabled or disabled (parity must hold either way)")
    ap.add_argument("--require-real", action="store_true",
                    help="promote BLOCKED to FAIL (the train / a real nightly run)")
    ap.add_argument("--reference-endpoint", default=os.environ.get("HONUA_REFERENCE_ENDPOINT") or None,
                    help="a reference (local-docker) endpoint to assert parity against")
    args = ap.parse_args(argv)
    if args.scenario == "gp_batch_native":
        # Never fall through into provision. A native receipt is supplied by a
        # confirmed candidate run; this flag only judges that it is absent.
        from scenarios.gp_batch_native.scenario import evaluate
        row = evaluate(None)
        if args.require_real and row.get("status") == "blocked":
            row = {**row, "status": "fail", "why": f"require_real: {row.get('why', '')}"}
            print(json.dumps(row))
            return 1
        print(json.dumps(row))
        return 0 if row.get("status") != "fail" else 1
    phases = {"provision": _phase_provision, "seal-state": _phase_seal, "journey": _phase_journey,
              "model-canary": _phase_model_canary, "update-rollback": _phase_update_rollback,
              "deliver-key": _phase_deliver_key, "admit": _phase_admit, "open-state": _phase_open,
              "diagnose": _phase_diagnose, "teardown": _phase_teardown}
    if args.phase in phases:
        if args.phase in ("seal-state", "open-state") and args.bundle is None:
            ap.error(f"--phase {args.phase} requires --bundle")
        if args.phase == "admit" and not args.cidr:
            ap.error("--phase admit requires --cidr")
        if args.phase == "deliver-key" and not (args.public_key and args.sealed_key):
            ap.error("--phase deliver-key requires --public-key and --sealed-key")
        if args.phase == "journey" and bool(args.sealed_key) != bool(args.private_key):
            ap.error("--sealed-key and --private-key go together")
        if args.phase == "model-canary" and not (args.sealed_key and args.private_key):
            ap.error("--phase model-canary requires --sealed-key and --private-key")
        if (args.phase == "model-canary" and args.lock_digest is not None
                and not LOCK_DIGEST.fullmatch(args.lock_digest)):
            # Omit the flag for an unbound run; an empty or malformed digest is a wiring error.
            ap.error("--lock-digest must be sha256:<64 lowercase hex>")
        return phases[args.phase](args)

    report = run(args.target, args.require_real, args.reference_endpoint, redis_enabled=(args.redis == "on"),
                 max_attempts=args.max_attempts, cost_ceiling_usd=args.cost_ceiling_usd, cost_report=args.cost_report)
    report.setdefault("evidence_url", os.environ.get("HONUA_RUN_URL", ""))
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _print_report(report)

    # `run()` already escalates BLOCKED -> "fail" under require_real, so a residual "blocked" here means
    # it is being tolerated (bootstrap, no infra yet) — exit 0, surfaced in the report, not a fake green.
    # Only a real "fail" reddens the job. Mirrors the local-docker tier's honest-bootstrap behaviour.
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
