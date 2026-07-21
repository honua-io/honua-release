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
known-broken pending VPC egress). Every other check/probe can genuinely fail. `--admin-api-key`
(or HONUA_DEMO_API_KEY) is optional; key-gated probes report BLOCKED — not FAIL — without it, mirroring
the original demo-b-probes.sh's "skipped — no API key" behaviour.

  python e2e/demo_canary.py --base https://demo.honua.io \
      [--admin-api-key KEY | env HONUA_DEMO_API_KEY] \
      [--service-id maui-roads] [--tile-layer-id 3] [--assert-stac-non-empty] \
      [--geocode-budget-ms 3000] [--candidate-sha SHA]

Exit code 0 only when overall status is "pass" or "blocked" (a blocked run is surfaced, not silently
green, but does not redden CI by itself — a genuine "fail" always does).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes  # noqa: E402
from canonical_checks import CheckResult, make_fetch, run_canonical  # noqa: E402

REPORT_PATH = E2E_DIR / "gate-report-demo-canary.json"
ENVELOPE_PATH = E2E_DIR / "live-canary-evidence.json"
ENVELOPE_SCHEMA = "honua.live-canary-evidence.v1"


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


def run(base: str, admin_api_key: str | None, service_id: str | None, tile_layer_id: int | None,
        assert_stac_non_empty: bool, geocode_budget_ms: float, candidate_sha: str | None) -> tuple[dict, dict]:
    fetch = make_fetch()
    admin_fetch = make_fetch({"X-API-Key": admin_api_key}) if admin_api_key else None

    canonical = run_canonical(base, fetch, authenticated_fetch=admin_fetch)
    canary = canary_probes.run_canary(base, fetch, admin_fetch=admin_fetch, service_id=service_id,
                                      tile_layer_id=tile_layer_id,
                                      assert_stac_non_empty=assert_stac_non_empty,
                                      geocode_budget_ms=geocode_budget_ms)
    all_results = canonical + canary

    failed = [r.name for r in all_results if r.status == "fail"]
    blocked = [r.name for r in all_results if r.status == "blocked"]
    overall = "fail" if failed else ("blocked" if blocked else "pass")

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
        "status": overall,
        "why": (f"FAILED checks: {failed}" if failed else
                (f"blocked (no admin key configured / no seeded data for): {blocked}" if blocked else
                 "all canonical + canary checks passed")),
    }

    envelope = {
        "schemaVersion": ENVELOPE_SCHEMA,
        "producer": "honua-release/demo-canary",
        "target": {"url": base, "environment": "demo" if "demo.honua.io" in base else "unknown"},
        "generatedAt": generated_at,
        # Mirrors tools/check_evidence_freshness.py's "<sha>@<iso-timestamp>" sourceVersion convention
        # for the server-matrix producer, so honua-evidence#8 can join this the same way once it lands.
        "sourceVersion": f"{candidate_sha or 'unknown'}@{generated_at}",
        "runUrl": run_url,
        "overallStatus": overall,
        "checks": _check_dicts(all_results),
        # Per manifest-capability-id last-verdict, for a future capability-matrix join (honua-evidence#8).
        "capabilityKeys": {
            r.name: {"lastStatus": r.status, "lastGreenAt": generated_at if r.status == "pass" else None}
            for r in all_results
        },
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
        print(f"   [{c['status'].upper():7}] {c['name']}: {c['why']}")
    for c in report["canaryProbes"]:
        print(f"   canary [{c['status'].upper():7}] {c['name']}: {c['why']}")
    print(f"   (report written to {REPORT_PATH})")
    print(f"   (evidence envelope written to {ENVELOPE_PATH})")

    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
