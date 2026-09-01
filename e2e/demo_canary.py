#!/usr/bin/env python3
"""Scheduled demo canary entrypoint (honua-release#61).

Runs the canonical slim set (incl. the live capability-manifest check, honua-release#61 item 1) plus
the full canary probe set (e2e/canary_probes.py — the corrected 2026-07-20 protocol-reachability probes,
ported from honua-server's docs/internal/demo/scripts/demo-b-probes.sh, plus STAC/EDR/OData/OGC-Features/
tiles/per-service-WMS-WMTS-WCS/geocoding-latency) against a deployed target — https://demo.honua.io by
default (`.github/workflows/demo-canary.yml`, cron every 6h + workflow_dispatch).

Emits:
  1. gate-report-demo-canary.json — a human/machine-readable {gate,status,why,checks,...} report the
     workflow uses for its step summary and its open/update-a-single-issue-on-failure step.
  2. live-canary-evidence.json — a VERSIONED evidence envelope (schemaVersion
     honua.live-canary-evidence.v1) for honua-evidence#8's "cross-repo evidence joins... live canary
     results... joined into the [capability] matrix" ask. honua-evidence#8 had not yet defined this
     schema as of this writing — treat this as a MINIMAL PROPOSAL for that issue to adopt/refine, not a
     frozen contract; it deliberately mirrors the `sourceVersion: "<sha>@<timestamp>"` convention
     tools/check_evidence_freshness.py already uses for the server-matrix producer, so it can join the
     same way once honua-evidence ingests it.

Honesty (AGENTS.md): `geocoding-latency` is REPORT-ONLY (never fails — honua-server#2948, geocoding is
known-broken pending VPC egress). Every other check/probe can genuinely fail — and a failure that is
KNOWN and OWNED is quarantined, never deleted: `e2e/canary-quarantine.yaml` maps a probe to an owning
issue, `e2e/quarantine.py` downgrades that probe's FAIL to `quarantined` (green run, issue linked in the
step summary) until its `reviewBy` expires, and the live-canary envelope published to honua-evidence
still reports it `red`. Quarantine moves a CI verdict, never an evidence verdict. `--admin-api-key`
(or HONUA_DEMO_API_KEY) is optional; key-gated probes report BLOCKED — not FAIL — without it, mirroring
the original demo-b-probes.sh's "skipped — no API key" behaviour.

  python e2e/demo_canary.py --base https://demo.honua.io \
      [--admin-api-key KEY | env HONUA_DEMO_API_KEY] \
      [--service-id maui-roads] [--tile-layer-id 3] [--assert-stac-non-empty] \
      [--geocode-budget-ms 3000] [--candidate-sha SHA]

Exit code 0 only when overall status is "pass" or "blocked" (a blocked run is surfaced, not silently
green, but does not redden CI by itself — a genuine "fail" always does). Since honua-release#128 an
unreachable demo.honua.io is one of those genuine fails rather than a blocked run: `blocked` here means
"no admin key / no seeded data", never "the site did not answer".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes  # noqa: E402
from canonical_checks import CheckResult, make_fetch, run_canonical  # noqa: E402
from quarantine import QUARANTINED, apply_quarantine, load_quarantine  # noqa: E402

REPORT_PATH = E2E_DIR / "gate-report-demo-canary.json"
ENVELOPE_PATH = E2E_DIR / "live-canary-evidence.json"
ENVELOPE_SCHEMA = "honua-evidence.live-canary-envelope/v1"

# Probe/check name -> capability-matrix key(s) (honua-evidence capability-matrix.v1.json
# vocabulary). Only mapped results become evidence-envelope probes; the ingester requires
# non-empty capabilityKeys per probe (docs/producer-contracts.md in honua-evidence).
PROBE_CAPABILITY_KEYS: dict[str, list[str]] = {
    "health-live-ready": ["ops.health"],
    "metrics-gated": ["ops.observability"],
    "admin-metrics-health": ["ops.observability"],
    "render-query-smoke": ["serve.geoservices-mapserver", "serve.geoservices-featureserver"],
    "stac-collections": ["serve.stac"],
    "ogc-features-collections": ["serve.ogc-api-features"],
    "edr-collections": ["serve.ogc-api-edr"],
    "odata-service-document": ["serve.odata"],
    "tiles-tilejson": ["serve.vector-tiles"],
    "geocoding-latency": ["geocoding.forward"],
    "ogc-wms-capabilities": ["serve.wms"],
    "ogc-wmts-capabilities": ["serve.wmts"],
    "ogc-wcs-capabilities": ["serve.wcs"],
    "capability-manifest": ["discovery.capability-manifest"],
}


def _check_dicts(results: list[CheckResult]) -> list[dict]:
    return [{"name": r.name, "status": r.status, "why": r.why, **({"evidence": r.evidence} if r.evidence else {})}
            for r in results]


def _best_effort_candidate_sha() -> str | None:
    """Best-effort read of platform-manifest.yaml's pinned honua-server sha, so the evidence envelope
    can (when possible) record what candidate the canary ran against — never fatal if unavailable."""
    try:
        import yaml
        data = yaml.safe_load((REPO_ROOT / "platform-manifest.yaml").read_text(encoding="utf-8")) or {}
        return ((data.get("components") or {}).get("honua-server") or {}).get("sha") or None
    except Exception:  # noqa: BLE001 - genuinely best-effort, never fatal
        return None


def _live_deployment_revision(base: str, fetch, expected_sha: str | None) -> tuple[CheckResult, str]:
    """Read the revision from the deployment itself and bind evidence only when it matches."""
    response = fetch(base.rstrip("/") + "/api/v1/capabilities/manifest")
    try:
        manifest = json.loads(response.body) if response.status == 200 else {}
        server = manifest.get("server") if isinstance(manifest, dict) else {}
        revision = server.get("deploymentRevision") if isinstance(server, dict) else None
    except (TypeError, ValueError):
        revision = None
    evidence = {"liveDeploymentRevision": revision or "", "expectedDeploymentRevision": expected_sha or ""}
    if not revision:
        return CheckResult("deployment-revision", "fail",
                           "live capability manifest does not advertise server.deploymentRevision", evidence), ""
    if not expected_sha or revision != expected_sha:
        return CheckResult("deployment-revision", "fail",
                           f"live deployment revision {revision!r} does not match candidate {expected_sha!r}",
                           evidence), revision
    return CheckResult("deployment-revision", "pass",
                       f"live deployment advertises candidate revision {revision}", evidence), revision



CANONICAL_DEMO_HOST = "demo.honua.io"


def _target_environment(base: str) -> str:
    """Label the environment this canary actually ran against.

    Compares the parsed HOSTNAME, not a substring of the URL. `"demo.honua.io" in base` also
    matches https://demo.honua.io.attacker.test/ and https://evil.test/demo.honua.io, so a run
    against an unrelated host could be stamped into the evidence envelope as though it came from
    the real demo environment. This envelope is release evidence -- mislabelling which environment
    produced it is exactly the kind of quiet inaccuracy the certification chain exists to prevent.

    Subdomains are NOT folded into the canonical label: demo.honua.io and anything.demo.honua.io
    are different deployments and should not be reported as the same environment.
    """
    host = (urlparse(base).hostname or "").lower()
    return CANONICAL_DEMO_HOST if host == CANONICAL_DEMO_HOST else base


def run(base: str, admin_api_key: str | None, service_id: str | None, tile_layer_id: int | None,
        assert_stac_non_empty: bool, geocode_budget_ms: float, candidate_sha: str | None) -> tuple[dict, dict]:
    fetch = make_fetch()
    admin_fetch = make_fetch({"X-API-Key": admin_api_key}) if admin_api_key else None

    canonical = run_canonical(base, fetch, authenticated_fetch=admin_fetch,
                              frozen_server_sha=candidate_sha, enforcement="bootstrap")
    revision_check, live_candidate_sha = _live_deployment_revision(base, fetch, candidate_sha)
    canonical.append(revision_check)
    canary = canary_probes.run_canary(base, fetch, admin_fetch=admin_fetch, service_id=service_id,
                                      tile_layer_id=tile_layer_id,
                                      assert_stac_non_empty=assert_stac_non_empty,
                                      geocode_budget_ms=geocode_budget_ms)
    all_results = canonical + canary

    # Issue-linked quarantine (honua-release#84, e2e/canary-quarantine.yaml): a KNOWN, OWNED failure is
    # downgraded to `quarantined` so it neither reddens the run nor sits red and unowned — but the probe
    # keeps running and keeps telling the truth. Expired entries fail the run again by design.
    quarantine_entries = load_quarantine()
    all_results, quarantine_audit = apply_quarantine(all_results, quarantine_entries)
    n_canonical = len(canonical)
    canonical, canary = all_results[:n_canonical], all_results[n_canonical:]

    failed = [r.name for r in all_results if r.status == "fail"]
    quarantined = [r.name for r in all_results if r.status == QUARANTINED]
    blocked = [r.name for r in all_results if r.status == "blocked"]
    overall = "fail" if failed else ("blocked" if (blocked or quarantined) else "pass")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_url = os.environ.get("HONUA_RUN_URL", "")

    report = {
        "gate": "demo-canary",
        "target": base,
        "adminKeyConfigured": admin_fetch is not None,
        "generatedAt": generated_at,
        "evidence_url": run_url,
        "checks": _check_dicts(canonical),
        "canaryProbes": _check_dicts(canary),
        "quarantine": quarantine_audit,
        "status": overall,
        "why": (f"FAILED checks: {failed}" if failed else
                ((f"quarantined (known, issue-owned): {quarantined}; " if quarantined else "") +
                 (f"blocked (no admin key configured / no seeded data for): {blocked}" if blocked else
                  ("no unowned failures" if quarantined else "all canonical + canary checks passed")))),
    }

    # honua-evidence#8 landed its producer contract (honua-io/honua-evidence#9,
    # docs/producer-contracts.md: schema honua-evidence.live-canary-envelope/v1) after this
    # emitter was first drafted — the envelope below conforms to that contract exactly.
    # Required top-level fields: schema, manifestId, targetEnvironment, runAt, probes.
    # Probes without capabilityKeys are skipped by the ingester, so only capability-mapped
    # results are emitted here; the FULL check list (including unmapped operational checks
    # like security-headers/deploy-preflight) lives in the gate report above.
    # Quarantine is a CI-verdict concession, NOT an evidence concession: a quarantined probe is still a
    # genuinely failing capability, so honua-evidence must see it as `red`. Downgrading it here would
    # let a known gap render green on the public evidence site — the exact dishonesty quarantine exists
    # to avoid. The owning issue travels with it in `detail`.
    status_map = {"pass": "green", "fail": "red", QUARANTINED: "red"}
    probes = []
    for r in all_results:
        keys = PROBE_CAPABILITY_KEYS.get(r.name)
        if not keys or r.status not in status_map:
            continue  # unmapped or blocked results are operational detail, not capability evidence
        probes.append({
            "probeName": r.name,
            "capabilityKeys": keys,
            "status": status_map[r.status],
            "lastGreenAt": generated_at if r.status == "pass" else "",
            "detail": r.why,
        })

    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "manifestId": f"demo-canary-{generated_at}",
        "targetEnvironment": _target_environment(base),
        "targetUrl": base,
        "runAt": generated_at,
        "overallStatus": ("partial" if quarantined else
                          {"pass": "green", "fail": "red"}.get(overall, "partial")),
        "sourceRepo": "honua-io/honua-release",
        "sourceRef": os.environ.get("GITHUB_SHA", ""),
        "sourceRunUrl": run_url,
        # Extra (contract tolerates unknown fields): revision advertised by the deployment exercised.
        "candidateServerSha": live_candidate_sha,
        "probes": probes,
    }
    return report, envelope


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("HONUA_DEMO_BASE_URL", "https://demo.honua.io"))
    ap.add_argument("--admin-api-key", default=os.environ.get("HONUA_DEMO_API_KEY") or None,
                    help="admin X-API-Key (env HONUA_DEMO_API_KEY also honored); absent -> key-gated "
                         "checks/probes report BLOCKED, never a fake pass")
    ap.add_argument("--service-id", default=os.environ.get("HONUA_DEMO_SERVICE_ID", canary_probes.DEMO_SERVICE_ID))
    ap.add_argument("--tile-layer-id", type=int,
                    default=int(os.environ.get("HONUA_DEMO_TILE_LAYER_ID", canary_probes.DEMO_TILE_LAYER_ID)))
    ap.add_argument("--assert-stac-non-empty", action="store_true",
                    default=os.environ.get("HONUA_DEMO_ASSERT_STAC_NON_EMPTY", "1") not in ("0", "false", "False"),
                    help="fail (not just report) if STAC has 0 collections — on by default for the demo target")
    ap.add_argument("--geocode-budget-ms", type=float, default=3000.0)
    ap.add_argument("--candidate-sha", default=_best_effort_candidate_sha())
    args = ap.parse_args(argv)

    report, envelope = run(args.base, args.admin_api_key, args.service_id, args.tile_layer_id,
                           args.assert_stac_non_empty, args.geocode_budget_ms, args.candidate_sha)

    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    ENVELOPE_PATH.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")

    print(f"== demo-canary :: {report['target']} -> {report['status'].upper()} ==")
    print(f"   {report['why']}")
    print(f"   admin key configured: {report['adminKeyConfigured']}")
    for c in report["checks"]:
        print(f"   [{c['status'].upper():11}] {c['name']}: {c['why']}")
    for c in report["canaryProbes"]:
        print(f"   canary [{c['status'].upper():11}] {c['name']}: {c['why']}")
    audit = report["quarantine"]
    for e in audit["applied"]:
        print(f"   quarantine: {e['probe']} is a known failure owned by {e['issue']} "
              f"(since {e['since']}, review by {e['reviewBy']})")
    for e in audit["expired"]:
        print(f"   quarantine EXPIRED: {e['probe']} passed its reviewBy {e['reviewBy']} — "
              f"failing the run again; fix or re-justify {e['issue']}")
    for e in audit["stale"]:
        print(f"   quarantine STALE: {e['probe']} is now {e['observedStatus']} — delete its entry from "
              f"e2e/canary-quarantine.yaml so the next real regression is not hidden ({e['issue']})")
    for e in audit["unknown"]:
        print(f"   quarantine UNKNOWN: {e['probe']} is not a probe this canary emits (renamed?) "
              f"— {e['issue']}")
    print(f"   (report written to {REPORT_PATH})")
    print(f"   (evidence envelope written to {ENVELOPE_PATH})")

    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
