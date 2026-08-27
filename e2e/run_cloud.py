#!/usr/bin/env python3
"""Cross-cloud parity tier entrypoint (docs/TEST-STRATEGY.md Phase B).

Provisions a real honua-server on a cloud deploy target, runs the canonical (slim) parity set —
including the live capability-manifest check (honua-release#61) — against its endpoint, plus the
canary probe set (STAC/EDR/OData/OGC-Features/tiles/per-service-WMS-WMTS-WCS reachability;
e2e/canary_probes.py), tears it down, and — when a reference endpoint is supplied — asserts parity
with the reference (local docker). Emits a machine-readable gate-report.json the release train
consumes.

Honesty (AGENTS.md): when the target's infra isn't wired (no OIDC creds / no deployable image / no
IaC), the run is BLOCKED, never a fake green. `--require-real` (the train / a real nightly run)
promotes BLOCKED to a hard FAIL so the gate can genuinely fail once infra exists.

Cloud-tier unblock (honua-release#61): the canary probes run here in GENERIC mode — no service/tile
id is configured for a bare terraform-provisioned cell (nothing is seeded there yet), so the
data-dependent probes (render+query smoke, per-service WMS/WMTS/WCS, tile.json) honestly report
BLOCKED rather than a fake pass/fail; the reachability-only probes (health, security headers,
metrics-gated, STAC/EDR/OData/OGC-Features reachability) run for real. A genuine FAIL from any canary
probe (a real break, not just "nothing seeded") reddens the run unconditionally — BLOCKED canary
probes are reported but do not gate, since the ephemeral cloud cells have no seed-data story yet
(distinct from the MCP/Studio/GP/demo `scenarioCoverage` scenarios below, which stay hardcoded BLOCKED
pending the driver harness image, honua-release#35).

An UNREACHABLE endpoint is not in that tolerated set (honua-release#128). "Nothing was seeded" is a
missing input; "the deployment never answered" is a missing subject, and a cell that provisioned an
endpoint which then never served fails outright, whatever --require-real says.

  python e2e/run_cloud.py --target aws-serverless [--require-real] [--reference-endpoint URL]

Exit code 0 only when the assembled status is "pass".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes  # noqa: E402
from canonical_checks import (is_endpoint_unreachable, make_fetch, run_canonical,  # noqa: E402
                              run_extended)
from parity import TargetRun, compare  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.base import ProvisionError  # noqa: E402

REPORT_PATH = E2E_DIR / "gate-report-cloud.json"

# The cloud/OIDC secrets that gate whether this tier can run at all. When NONE are present the gate
# SELF-SKIPS (status: skipped, why: cloud-creds-unset) so a no-cloud local cut is not failed by it —
# it stays ready to enforce per-RC once an org wires the OIDC role for a labelled candidate.
_CLOUD_CRED_ENV = ("HONUA_AWS_ROLE_ARN", "AWS_ROLE_ARN", "AWS_ACCESS_KEY_ID",
                   "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE")

_READY_ATTEMPTS = 36
_READY_DELAY_SECONDS = 5.0


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


def run(target_name: str, require_real: bool, reference_endpoint: str | None,
        redis_enabled: bool = False) -> dict:
    cls = REGISTRY.get(target_name)
    if cls is None:
        return {"gate": "cloud-parity", "target": target_name, "status": "fail",
                "why": f"unknown target {target_name!r}; known: {sorted(REGISTRY)}"}

    target = cls(run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    redis_mode = "redis-on" if redis_enabled else "redis-off"
    cell = f"{target_name}/{redis_mode}"
    avail = target.availability()

    report: dict = {"gate": "cloud-parity", "target": target_name, "redis": redis_mode, "cell": cell,
                    "require_real": require_real,
                    "availability": {"ok": avail.ok, "reason": avail.reason, "missing": avail.missing}}

    if not avail.ok:
        # Cloud/OIDC creds unset => SELF-SKIP (not blocked, not fail), even under require_real: without
        # creds this tier literally cannot run, and a local cut must not be reddened by it. Enforcement
        # is per-RC: an org wires HONUA_AWS_ROLE_ARN for a candidate and the cell then runs for real.
        if not _cloud_creds_present():
            report["status"] = "skipped"
            report["why"] = "cloud-creds-unset"
            return report
        # Creds present but infra half-wired (no image / no IaC tree) => BLOCKED, promoted to FAIL under
        # require_real so a genuinely broken cloud path is a real red.
        report["status"] = "fail" if require_real else "blocked"
        report["why"] = avail.reason
        return report

    endpoint = None
    checks = []
    canary_results = []
    try:
        _mark_provision_attempt()
        endpoint = target.provision(redis_enabled=redis_enabled)
        report["endpoint"] = endpoint
        fetch = make_fetch(timeout=10.0)
        # The budget is read from the module globals at CALL time so a test can shorten it; the
        # defaults on _wait_for_endpoint are bound at def time and cannot be monkeypatched.
        ready, readiness = _wait_for_endpoint(endpoint, fetch, attempts=_READY_ATTEMPTS,
                                              delay_seconds=_READY_DELAY_SECONDS)
        report["readiness"] = {"ready": ready, **readiness}
        checks = run_canonical(endpoint, fetch, enforcement="strict" if require_real else "bootstrap")
        report["checks"] = _check_dicts(checks)
        # Cloud-tier unblock (honua-release#61): the canary probe set, GENERIC mode (no service/tile id
        # configured — nothing is seeded on a bare terraform cell yet), so data-dependent probes report
        # BLOCKED honestly rather than a fake pass/fail; reachability-only probes run for real.
        canary_results = canary_probes.run_canary(endpoint, fetch)
        report["canaryProbes"] = _check_dicts(canary_results)
    except ProvisionError as e:
        report["status"] = "fail"
        report["why"] = f"provision failed: {e}"
    finally:
        # Teardown ALWAYS runs, including on the failure path: a cell that created real AWS
        # infrastructure and then failed must not strand it (honua-iac#142 — orphaned VPCs/clusters
        # bill until someone reaps them by hand). A teardown that cannot complete is itself a hard
        # failure of the cell, because the orphan is real — it is never swallowed.
        try:
            target.teardown(redis_enabled=redis_enabled)
        except ProvisionError as e:
            prior = report.get("why")
            report["status"] = "fail"
            report["why"] = f"{prior}; teardown failed: {e}" if prior else f"teardown failed: {e}"

    if report.get("status") == "fail":
        return report

    # Extended seam scenarios (MCP / Studio / GP-execute / top-demo). BLOCKED until the cloud harness
    # image (honua-release#35) drives the real drivers here; require_real promotes that to FAIL so cloud
    # MCP/Studio/GP/demo cert is genuinely gated for a per-RC cut, not assumed.
    extended = run_extended(endpoint)
    report["scenarioCoverage"] = _check_dicts(extended)

    # Verdict from the canonical set + the canary probes' genuine failures.
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

    if failed or canary_failed:
        report["status"] = "fail"
        report["why"] = f"canonical checks failed on {cell}: {failed}; canary probes failed: {canary_failed}"
        return report
    if require_real and (blocked or ext_blocked):
        report["status"] = "fail"
        report["why"] = (f"require_real on {cell}: canonical blocked={blocked or '[]'}, "
                         f"scenarios not-certified={ext_blocked} (needs honua-release#35 harness image)")
        return report

    # Parity vs the reference target, when one was provided.
    if reference_endpoint:
        ref_checks = run_canonical(reference_endpoint,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="aws-serverless", choices=sorted(REGISTRY))
    ap.add_argument("--redis", choices=["on", "off"], default="off",
                    help="run the target with Redis enabled or disabled (parity must hold either way)")
    ap.add_argument("--require-real", action="store_true",
                    help="promote BLOCKED to FAIL (the train / a real nightly run)")
    ap.add_argument("--reference-endpoint", default=os.environ.get("HONUA_REFERENCE_ENDPOINT") or None,
                    help="a reference (local-docker) endpoint to assert parity against")
    args = ap.parse_args(argv)

    report = run(args.target, args.require_real, args.reference_endpoint, redis_enabled=(args.redis == "on"))
    report.setdefault("evidence_url", os.environ.get("HONUA_RUN_URL", ""))
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

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

    # `run()` already escalates BLOCKED -> "fail" under require_real, so a residual "blocked" here means
    # it is being tolerated (bootstrap, no infra yet) — exit 0, surfaced in the report, not a fake green.
    # Only a real "fail" reddens the job. Mirrors the local-docker tier's honest-bootstrap behaviour.
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
