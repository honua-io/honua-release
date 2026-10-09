#!/usr/bin/env python3
"""Provision a cloud cell, run the imported pinned journey, meter cost and always tear down.

GA: {aws-ecs, aws-serverless} x Redis off/on; aws-serverless is Lambda + AWS Batch geoprocessing.
EKS is informational Preview. The mixed ECS + Batch cell is out of the 2026.1 rc.3 matrix: its IaC
root does not exist yet (restore it when honua-iac#209 lands).
Missing, blocked or invalid journey evidence cannot certify a GA cloud cell.
"""
from __future__ import annotations

import argparse
import hashlib
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


def provision_phase(target, target_name: str, *, require_real: bool, redis_enabled: bool) -> dict:
    """Credentialed half: provision, probe and seed the cell. Nothing third-party runs here.

    Returns the handoff the credential-free journey job and the teardown job consume. The handoff
    carries identifiers and verdicts only; the cell's application key goes to Secrets Manager.
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
        endpoint = target.provision(redis_enabled=redis_enabled)
        report["endpoint"] = endpoint
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
        canary_results = canary_probes.run_canary(endpoint, fetch)
        report["canaryProbes"] = _check_dicts(canary_results)
        # What the cell advertises before any pinned client ran: teardown holds journey receipts to it.
        identity = fetch(endpoint.rstrip("/") + cloud_journey.CAPABILITY_MANIFEST)
        state["observedServer"] = (cloud_journey.parse_server_identity(identity.body)
                                   if identity.status == 200 else None)
        # The seed needs the cell's database secret, so it runs here. Every seam driver and the
        # journey run later, in the job that holds no cloud credential, before any teardown.
        state["seedError"] = seed_cell(endpoint, target=target,
                                       out=cloud_journey.cell_dir(cell) / "extended")
        # The owned 1.1-candidate-image check needs the image ECS reports running.
        if target_name == "aws-ecs":
            try:
                state["runningImage"] = cloud_journey.observed_ecs_image(target, cloud_journey.manifest())
            except Exception:
                state["runningImage"] = None
        state.update(endpoint=endpoint, ready=ready)
    except ProvisionError as e:
        report["status"] = "fail"
        report["why"] = f"provision failed: {e}"
    except Exception as e:
        report["status"] = "fail"
        report["why"] = f"cloud checks/journey failed: {type(e).__name__}"
    return state


def journey_phase(state: dict, *, admin_key: str, max_attempts: int = 2) -> dict:
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
        extended = run_drivers(endpoint, admin_key=admin_key,
            out=cloud_journey.cell_dir(cell) / "extended", ready=state.get("ready", False),
            require_real=state.get("requireReal", False),
            redis_enabled=state.get("redis") == "redis-on", seed_error=state.get("seedError"))
        journey["scenarioCoverage"] = _check_dicts(extended)
        for number in range(1, max_attempts + 1):
            record = cloud_journey.attempt(cell, number, endpoint, admin_key, state.get("runningImage"))
            journey["journeyAttempts"].append(record)
            receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
            try:
                passed = cloud_journey.validate_attempt(record, receipt, cell,
                                                        run_id=run_id, run_attempt=run_attempt)
            except ValueError as error:
                # validate_attempt's reasons are fixed strings; they carry no receipt content.
                journey["status"] = "fail"
                journey["why"] = f"journey evidence rejected: {error}"
                break
            if passed:
                break
        else:
            journey["status"] = "fail"
            journey["why"] = "journey did not pass within the recorded attempt budget"
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
    if journey.get("status") == "fail" and report.get("status") != "fail":
        report["status"] = "fail"
        report["why"] = journey.get("why", "journey failed")
    if not state.get("provisionAttempted"):
        # Self-skipped, blocked, or never started: nothing was deployed, so nothing is destroyed.
        state["destroyed"] = False
        return report

    started_at = datetime.fromisoformat(state["startedAt"].replace("Z", "+00:00"))
    # Metering, receipt persistence, local cleanup and infrastructure destruction are
    # independent obligations. An error in any one must not bypass the following obligations.
    try:
        try:
            if not report["journeyAttempts"]:
                report["journeyAttempts"].append(cloud_journey.attempt(cell, 1, None, ""))
        except Exception as e:
            report.update(status="fail", why=report.get("why", "") +
                          f"; receipt evidence unavailable: {type(e).__name__}")
        finally:
            try:
                report["cost"] = cloud_journey.check_cost(
                    cost_report or Path(os.environ.get("HONUA_CLOUD_COST_REPORT",
                                                      "e2e/cloud-evidence/run-cost.json")),
                    cost_ceiling_usd, started_at=started_at)
                if report["cost"]["status"] != "pass":
                    report.update(status="fail", why=report.get("why", "") +
                                  "; run cost exceeds ceiling")
            except Exception as e:
                report.update(status="fail", why=report.get("why", "") +
                              f"; cost evidence unavailable: {type(e).__name__}")
    finally:
        try:
            cloud_journey.cleanup(cell)
        except Exception as e:
            report.update(status="fail", why=report.get("why", "") +
                          f"; journey cleanup failed: {type(e).__name__}")
        finally:
            state["destroyed"] = False
            if destroy:
                try:
                    target.teardown(redis_enabled=redis_enabled)
                    state["destroyed"] = True
                except Exception as e:
                    prior = report.get("why")
                    report["status"] = "fail"
                    report["why"] = f"{prior}; teardown failed: {e}" if prior else f"teardown failed: {e}"

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

    report["status"] = "blocked" if (blocked and not require_real) else "pass"
    # Say what actually happened. The old wording claimed "canonical set passed" even for a cell whose
    # canonical set was entirely BLOCKED — the sentence that made honua-release#128 invisible in the
    # job log for as long as it existed.
    report["why"] = report.get("why") or (
        f"{cell}: canonical set " + (f"blocked on {blocked}" if blocked else "passed")
        + (" + parity ok" if reference_endpoint else " (parity skipped: no reference endpoint)"))
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
    state = provision_phase(target, target_name, require_real=require_real, redis_enabled=redis_enabled)
    if not state["provisionAttempted"]:
        return state["report"]
    try:
        journey = journey_phase(state, admin_key=target.admin_api_key, max_attempts=max_attempts)
    except Exception as e:
        journey = {"status": "fail", "why": f"cloud checks/journey failed: {type(e).__name__}"}
    return teardown_phase(state, journey, target, reference_endpoint=reference_endpoint,
                          cost_ceiling_usd=cost_ceiling_usd, cost_report=cost_report)


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


def seal_key(secret_name: str, public_key: Path, sealed: Path, *, run=subprocess.run) -> None:
    """Encrypt the cell's application key to the journey runner's own public key.

    Job outputs and step env are printed in the public job log, so the key reaches the
    credential-free journey job only as RSA-OAEP ciphertext in an artifact.
    """
    ciphertext = run(["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_key),
                      "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256"],
                     input=read_secret(secret_name, run=run).encode("utf-8"),
                     capture_output=True, check=True).stdout
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(ciphertext)


def open_key(sealed: Path, private_key: Path, *, run=subprocess.run) -> str:
    return run(["openssl", "pkeyutl", "-decrypt", "-inkey", str(private_key),
                "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256", "-in", str(sealed)],
               capture_output=True, check=True).stdout.decode("utf-8")


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
    verified: list[tuple[int, bytes, bytes | None]] = []
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
        passed = cloud_journey.validate_attempt(record, receipt, cell, run_id=run_id, run_attempt=run_attempt)
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
    # A provisioned endpoint the journey recorded no attempt against is a failed journey, whatever
    # the upload claims: an empty history must not leave the cell's verdict to the other rows.
    if not passed and (records or state.get("endpoint")):
        result["status"] = "fail"
        result["why"] = (journey.get("why") if journey.get("status") == "fail" else None) or (
            "journey did not pass within the recorded attempt budget" if records
            else "journey recorded no attempt against the provisioned endpoint")
    return result


DIAGNOSTICS_NAME = "diagnostics-ecs.json"
_LOG_LINES = 200


def ecs_readiness_diagnostics(target, *, run=subprocess.run, redact=None) -> dict:
    """Why an ECS cell never became ready: task stop reasons and the server task's last log lines.

    Read from ECS DescribeTasks and CloudWatch Logs while the cell still exists (teardown, before
    destroy). Output lands in the public cell artifact, so every log line passes `redact`.
    """
    redact = redact or (lambda text: text)
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
            raise RuntimeError(f"aws {' '.join(args[:2])} failed")
        return json.loads(result.stdout or "{}")

    cluster, service = out("ecs_cluster_name"), out("ecs_service_name")
    if not cluster or not service:
        raise ValueError("the cell has no ecs_cluster_name / ecs_service_name output")
    arns = []
    for status in ("RUNNING", "STOPPED"):
        arns += aws("ecs", "list-tasks", "--cluster", cluster, "--service-name", service,
                    "--desired-status", status).get("taskArns", [])
    tasks = aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", *arns).get("tasks", []) if arns else []
    report: dict = {"cluster": cluster, "service": service, "tasks": [], "logs": []}
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
    # The newest task's server container log: the stream is <prefix>/<container>/<task id>.
    for task in sorted(tasks, key=lambda t: str(t.get("createdAt") or ""), reverse=True)[:1]:
        task_id = str(task.get("taskArn", "")).rsplit("/", 1)[-1]
        for container in definitions.get(task.get("taskDefinitionArn"), {}).get("containerDefinitions", []):
            options = (container.get("logConfiguration") or {}).get("options") or {}
            group, prefix = options.get("awslogs-group"), options.get("awslogs-stream-prefix")
            if not group or not prefix:
                continue
            stream = f"{prefix}/{container.get('name')}/{task_id}"
            try:
                events = aws("logs", "get-log-events", "--log-group-name", group, "--log-stream-name",
                             stream, "--limit", str(_LOG_LINES), "--no-start-from-head").get("events", [])
            except RuntimeError as error:
                report["logs"].append({"container": container.get("name"), "error": str(error)})
                continue
            report["logs"].append({"container": container.get("name"), "logGroup": group,
                                   "logStream": stream,
                                   "lines": [redact(str(e.get("message", ""))) for e in events][-_LOG_LINES:]})
    return report


def _redact_log(text: str) -> str:
    """Strip credentials a server log line might carry before it reaches a public artifact."""
    text = re.sub(r"(?i)(password|pwd|masterkey|api[-_]?key|secret|token)(\s*[=:]\s*)[^;\s\"',]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(x-api-key\s*:\s*)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@", r"\1[redacted]@", text)
    return text


def _phase_diagnose(args) -> int:
    """Teardown-side readiness diagnostics; never changes the verdict, never fails the job."""
    cell = _cell(args.target, args.redis)
    directory = cloud_journey.cell_dir(cell)
    state = _read_json(directory / HANDOFF_NAME) or {}
    if state.get("ready") is True:
        print(f"{cell}: provision reported ready; no readiness diagnostics needed")
        return 0
    try:
        diagnostics = ecs_readiness_diagnostics(_target(args.target), redact=_redact_log)
    except Exception as error:
        diagnostics = {"error": f"{type(error).__name__}: {_redact_log(str(error))}"}
    diagnostics["cell"] = cell
    _write_json(directory / DIAGNOSTICS_NAME, diagnostics)
    for task in diagnostics.get("tasks", []):
        print(f"   task {task['taskArn']}: {task['lastStatus']} stopCode={task['stopCode']} "
              f"stoppedReason={task['stoppedReason']!r}")
    print(f"{cell}: readiness diagnostics written to {directory / DIAGNOSTICS_NAME}")
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
    state = provision_phase(target, args.target, require_real=args.require_real,
                            redis_enabled=args.redis == "on")
    if state["endpoint"]:
        # The admit job seals this to the journey runner's public key (seal_key).
        try:
            store_secret(app_key_secret_name(cell), target.admin_api_key,
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
            admin_key = (open_key(args.sealed_key, args.private_key) if args.sealed_key
                         else os.environ.get("HONUA_CLOUD_ADMIN_KEY", ""))
        except Exception:
            admin_key = ""
        journey = journey_phase(state, admin_key=admin_key, max_attempts=args.max_attempts)
    _write_json(cloud_journey.cell_dir(cell) / JOURNEY_NAME, journey)
    print(f"== cloud-journey :: {cell} -> {journey.get('status', 'recorded').upper()} ==")
    for c in journey.get("scenarioCoverage", []):
        print(f"   scenario [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for record in journey.get("journeyAttempts", []):
        print(f"   attempt {record['number']}: {record['receipt']}")
    return 1 if journey.get("status") == "fail" else 0


# The nightly genuine-model cell (check_promotion_readiness.JOURNEYS["nightly-model-journey"]).
MODEL_CANARY_CELL = "aws-ecs/redis-off"
MODEL_JOURNEY_DIR = "model-journey"


def _passing_deterministic_receipt(cell_directory: Path) -> Path | None:
    """The cell journey's passing receipt from this job, which the canary protocol requires."""
    journey = _read_json(cell_directory / JOURNEY_NAME) or {}
    for record in reversed(journey.get("journeyAttempts") or []):
        path = E2E_DIR / str(record.get("receipt", ""))
        receipt = _read_json(path) if path.is_file() else None
        if receipt and receipt.get("status") == "pass":
            return path
    return None


def model_canary_phase(state: dict, *, admin_key: str, lock_digest: str, run=subprocess.run,
                       max_attempts: int = 2) -> dict:
    """Credential-free: run the genuine-model canary on the cell after its deterministic journey.

    Every attempt is bound to the lock digest under certification and chained to this job's passing
    deterministic receipt. The cell's application key reaches the harness only through the
    environment of its own process. The result is the journey row the nightly mint retains as
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
    if cell != MODEL_CANARY_CELL:
        why = f"{cell} is not the genuine-model cell ({MODEL_CANARY_CELL})"
    elif not state.get("endpoint") or not admin_key:
        why = "the cell has no endpoint or application key for the canary"
    deterministic = _passing_deterministic_receipt(directory) if why is None else None
    if why is None and deterministic is None:
        why = "the cell's deterministic journey did not pass in this job; the canary is refused"
    if why is None:
        env = {**os.environ, "TERMINAL_MODEL_API_KEY": admin_key}
        for number in range(1, max_attempts + 1):
            output = out / f"model-canary-{number}.json"
            endpoint = str(state["endpoint"]).rstrip("/")
            argv = [sys.executable, str(E2E_DIR.parent / "tools" / "terminal_model_canary.py"),
                    "--base-url", endpoint + "/api",
                    # Owner ruling canary-http-cell-2026-10-08: plain HTTP only to this cell's own
                    # harness-provisioned ALB host, never to any other non-loopback host.
                    "--allow-http-cell", urllib.parse.urlsplit(endpoint).hostname or "",
                    "--require-api-key", "--cell", cell, "--attempt", str(number),
                    "--lock-digest", lock_digest,
                    "--deterministic-receipt", str(deterministic.relative_to(E2E_DIR.parent)),
                    "--output", str(output)]
            completed = run(argv, env=env, cwd=E2E_DIR.parent, check=False)
            if not output.is_file():
                why = f"canary harness refused the cell before writing attempt {number} (exit {completed.returncode})"
                break
            receipts.append(output)
            if completed.returncode == 0:
                break
    report = model_journey_report.build(receipts, cell=MODEL_CANARY_CELL, lock_digest=lock_digest,
                                        candidate_digest=cloud_journey.candidate_digest(),
                                        run_id=run_id, run_attempt=run_attempt)
    if why is not None:
        report["cells"][0]["why"] = report["why"] = why
        report["cells"][0]["status"] = report["status"] = report["overallStatus"] = "fail"
    _write_json(out / "gate-report-journey.json", report)
    return report


def _phase_model_canary(args) -> int:
    sys.path.insert(0, str(E2E_DIR.parent / "tools"))
    cell = _cell(args.target, args.redis)
    state = _read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME) or {"cell": cell}
    try:
        admin_key = open_key(args.sealed_key, args.private_key)
    except Exception:
        admin_key = ""
    report = model_canary_phase(state, admin_key=admin_key, lock_digest=args.lock_digest,
                                max_attempts=args.max_attempts)
    print(f"== cloud-model-canary :: {cell} -> {report['status'].upper()} ==")
    print(f"   {report['why']}")
    return 0 if report["status"] == "pass" else 1


def _phase_admit(args) -> int:
    cell = _cell(args.target, args.redis)
    state = _read_json(cloud_journey.cell_dir(cell) / HANDOFF_NAME)
    if state is None or not state.get("endpoint"):
        raise SystemExit(f"{cell}: no provisioned endpoint to admit the journey runner to")
    _target(args.target).admit(state["endpoint"], args.cidr, redis_enabled=args.redis == "on")
    print(f"admitted the journey runner to {cell}")
    return 0


def _phase_deliver_key(args) -> int:
    cell = _cell(args.target, args.redis)
    seal_key(app_key_secret_name(cell), args.public_key, args.sealed_key)
    print(f"sealed the {cell} application key to the journey runner's public key")
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
    if state is None or state.get("cell") != cell:
        problems.append("provision handoff missing")
        state = {"cell": cell, "redis": f"redis-{args.redis}", "requireReal": args.require_real,
                 "startedAt": cloud_journey.now(), "provisionAttempted": restored,
                 "report": {"gate": "cloud-parity", "target": args.target, "redis": f"redis-{args.redis}",
                            "cell": cell, "require_real": args.require_real,
                            "evidenceTier": "Preview" if args.target in cloud_journey.PREVIEW_TARGETS else "GA",
                            "journeyAttempts": []}}
    elif (state.get("runId"), state.get("runAttempt"), state.get("candidateDigest")) != (
            *_run_identity(), cloud_journey.candidate_digest()):
        problems.append("provision handoff is bound to another run or candidate")
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
    if stranded:
        problems.append("the cell provisioned infrastructure but its sealed Terraform state never "
                        "reached teardown; reap it by hand (honua-iac#142)")
    secret_names = [app_key_secret_name(cell)] + ([state_secret_name(cell)] if state.get("destroyed") else [])
    for name in secret_names:
        try:
            forget_secret(name)
        except Exception as error:
            problems.append(f"{name} could not be deleted: {type(error).__name__}")
    if problems:
        report["status"] = "fail"
        report["why"] = "; ".join(filter(None, [report.get("why"), *problems]))
    report.setdefault("evidence_url", os.environ.get("HONUA_RUN_URL", ""))
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _print_report(report)
    return 1 if report["status"] == "fail" else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=("all", "provision", "seal-state", "journey", "model-canary",
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
    ap.add_argument("--lock-digest", help="sha256:<hex> of the platform lock the genuine-model canary binds to")
    ap.add_argument("--cost-ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    ap.add_argument("--cost-report", type=Path)
    ap.add_argument("--target", default="aws-serverless", choices=sorted(REGISTRY))
    ap.add_argument("--redis", choices=["on", "off"], default="off",
                    help="run the target with Redis enabled or disabled (parity must hold either way)")
    ap.add_argument("--require-real", action="store_true",
                    help="promote BLOCKED to FAIL (the train / a real nightly run)")
    ap.add_argument("--reference-endpoint", default=os.environ.get("HONUA_REFERENCE_ENDPOINT") or None,
                    help="a reference (local-docker) endpoint to assert parity against")
    args = ap.parse_args(argv)
    phases = {"provision": _phase_provision, "seal-state": _phase_seal, "journey": _phase_journey,
              "model-canary": _phase_model_canary,
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
        if args.phase == "model-canary" and not (args.sealed_key and args.private_key and args.lock_digest):
            ap.error("--phase model-canary requires --sealed-key, --private-key and --lock-digest")
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
