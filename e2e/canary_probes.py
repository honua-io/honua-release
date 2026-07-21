"""Demo/cloud canary probe set (honua-release#61) — the broader protocol-reachability set the
scheduled demo canary (`.github/workflows/demo-canary.yml`) runs against https://demo.honua.io, and
that `run_cloud.py` also runs (in a data-independent, no-service-id mode) against a terraform-
provisioned cloud cell.

This is deliberately a SEPARATE, wider set from `canonical_checks.CANONICAL_CHECKS` (the tiny
data-independent parity slim set every target must match identically): several probes here need a
seeded service/layer id to be meaningful (render+query smoke, per-service WMS/WMTS/WCS
GetCapabilities, tile.json). Honesty rule (AGENTS.md): when no id is configured for a probe, it
reports BLOCKED ("no <thing> id configured to probe"), never a fake pass and never a fake fail — a
bare/ephemeral cloud cell with no seeded data is legitimately unprovable here, not broken.

Ported/corrected from honua-server's docs/internal/demo/scripts/demo-b-probes.sh (Beats 1-3, 6:
health/security-headers/admin-gate/telemetry/deploy-preflight), reusing `canonical_checks.HttpResponse`
/ `Fetcher` / `make_fetch` so both modules share one HTTP implementation. `fetch` is always the plain
(unauthenticated) caller; `admin_fetch` (built via `canonical_checks.make_fetch({"X-API-Key": key})`)
is optional — key-gated probes report BLOCKED, not FAIL, when it is absent, mirroring the original
script's "skipped — no API key" behaviour.

`check_geocoding_latency` is REPORT-ONLY by design (honua-server#2948: geocoding is known-broken
pending VPC egress) — it never returns "fail", only "pass" or "blocked", so a real, known, tracked gap
does not redden every canary run.
"""
from __future__ import annotations

import json
import time

from canonical_checks import CheckResult, Fetcher

# Demo-specific defaults (docs/internal/demo runbook fixtures on https://demo.honua.io) — NOT used by
# default for the generic/cloud-tier invocation (run_cloud.py), which passes no ids and gets honest
# BLOCKED verdicts for the probes that need seeded data on a bare terraform cell. Layer 3 is the same
# maui-roads MapServer/FeatureServer layer id demo-b-probes.sh queries — reused here for both the
# tile.json probe and the FeatureServer count query (check_render_query_smoke).
DEMO_SERVICE_ID = "maui-roads"
DEMO_TILE_LAYER_ID = 3
DEMO_ASSERT_STAC_NON_EMPTY = True

_SECURITY_HEADERS = (
    ("strict-transport-security", None),
    ("x-frame-options", "deny"),
    ("cross-origin-opener-policy", None),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", None),
)


def check_health_live_ready(fetch: Fetcher, base: str) -> CheckResult:
    """Beat 1 (demo-b-probes.sh): /healthz/live + /healthz/ready -> 200. Plain /healthz is Development-
    only (Honua.ServiceDefaults) and 404s in Production, so this — not check_health — is what a
    Production/demo target must satisfy."""
    live = fetch(base.rstrip("/") + "/healthz/live")
    ready = fetch(base.rstrip("/") + "/healthz/ready")
    if live.status == 0 or ready.status == 0:
        return CheckResult("health-live-ready", "blocked", "endpoint unreachable")
    if live.status == 200 and ready.status == 200:
        return CheckResult("health-live-ready", "pass", "/healthz/live + /healthz/ready -> 200")
    return CheckResult("health-live-ready", "fail",
                       f"/healthz/live -> {live.status}, /healthz/ready -> {ready.status} (want 200/200)",
                       {"live": live.status, "ready": ready.status})


def check_security_headers(fetch: Fetcher, base: str) -> CheckResult:
    """Beat 2 (demo-b-probes.sh): the security header baseline on the root response."""
    r = fetch(base.rstrip("/") + "/")
    if r.status == 0:
        return CheckResult("security-headers", "blocked", "endpoint unreachable")
    missing = [name for name, want in _SECURITY_HEADERS
              if name not in r.headers or (want is not None and want.lower() not in r.headers[name].lower())]
    if missing:
        return CheckResult("security-headers", "fail", f"missing/incorrect headers: {missing}",
                           {"headers": r.headers})
    return CheckResult("security-headers", "pass", "all baseline security headers present")


def check_metrics_gated(fetch: Fetcher, base: str, admin_fetch: Fetcher | None = None) -> CheckResult:
    """Beat 2/3 (demo-b-probes.sh): /metrics is 401 without a key; 200 + honua_lambda_* with one."""
    url = base.rstrip("/") + "/metrics"
    no_key = fetch(url)
    if no_key.status == 0:
        return CheckResult("metrics-gated", "blocked", "endpoint unreachable")
    if no_key.status != 401:
        return CheckResult("metrics-gated", "fail", f"/metrics without a key -> {no_key.status} (want 401)")
    if admin_fetch is None:
        return CheckResult("metrics-gated", "blocked",
                           "no admin API key configured — cannot check the authenticated 200 leg",
                           {"unauthenticated": "401 (ok)"})
    with_key = admin_fetch(url)
    if with_key.status != 200:
        return CheckResult("metrics-gated", "fail", f"/metrics with a key -> {with_key.status} (want 200)")
    return CheckResult("metrics-gated", "pass", "/metrics: 401 without a key, 200 with one")


def check_admin_metrics_health(admin_fetch: Fetcher | None, base: str) -> CheckResult:
    """Beat 3 (demo-b-probes.sh): /api/v1/metrics/health with the admin key -> 200. Key-gated."""
    if admin_fetch is None:
        return CheckResult("admin-metrics-health", "blocked", "no admin API key configured")
    r = admin_fetch(base.rstrip("/") + "/api/v1/metrics/health")
    if r.status == 0:
        return CheckResult("admin-metrics-health", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("admin-metrics-health", "fail", f"-> {r.status} (want 200)")
    return CheckResult("admin-metrics-health", "pass", "/api/v1/metrics/health -> 200")


def check_render_query_smoke(fetch: Fetcher, base: str, service_id: str | None = None,
                             layer_id: int = 0) -> CheckResult:
    """Render + query smoke (demo-b-probes.sh): MapServer export PNG + FeatureServer count query,
    proving the target actually serves data (not just reachable routes). BLOCKED (not fail) when no
    service id is configured — a bare/ephemeral cloud cell legitimately has nothing seeded yet.
    `layer_id` must name a REAL FeatureServer layer on `service_id` (demo-b-probes.sh's maui-roads
    fixture is layer 3, not 0 — the demo default threads the same id used for the tiles probe)."""
    if not service_id:
        return CheckResult("render-query-smoke", "blocked", "no demo service id configured to probe")
    png = fetch(base.rstrip("/") + f"/rest/services/{service_id}/MapServer/export"
               "?bbox=-180,-90,180,90&size=200,200&format=png&f=image")
    if png.status == 0:
        return CheckResult("render-query-smoke", "blocked", "endpoint unreachable")
    if png.status != 200:
        return CheckResult("render-query-smoke", "fail", f"MapServer export -> {png.status} (want 200 PNG)")
    cnt = fetch(base.rstrip("/") + f"/rest/services/{service_id}/FeatureServer/{layer_id}/query"
               "?where=1%3D1&returnCountOnly=true&f=json")
    if cnt.status != 200 or '"count"' not in cnt.body:
        return CheckResult("render-query-smoke", "fail",
                           f"FeatureServer/{layer_id} count query -> {cnt.status}, body={cnt.body[:200]!r}")
    return CheckResult("render-query-smoke", "pass",
                       f"{service_id}/{layer_id}: MapServer export 200 PNG + FeatureServer count query ok")


def check_deploy_preflight(fetch: Fetcher, base: str, admin_fetch: Fetcher | None = None) -> CheckResult:
    """Beat 6 (demo-b-probes.sh): deploy-control preflight — 401 without a key, and (with one) a
    readyForCoordinatedDeploy verdict. Key-gated."""
    url = base.rstrip("/") + "/api/v1/admin/deploy/preflight"
    no_key = fetch(url)
    if no_key.status == 0:
        return CheckResult("deploy-preflight", "blocked", "endpoint unreachable")
    if no_key.status != 401:
        return CheckResult("deploy-preflight", "fail", f"without a key -> {no_key.status} (want 401)")
    if admin_fetch is None:
        return CheckResult("deploy-preflight", "blocked", "no admin API key configured",
                           {"unauthenticated": "401 (ok)"})
    r = admin_fetch(url)
    if r.status != 200 or "readyForCoordinatedDeploy" not in r.body:
        return CheckResult("deploy-preflight", "fail",
                           f"preflight -> {r.status}, body={r.body[:200]!r}")
    return CheckResult("deploy-preflight", "pass", "deploy/preflight returned coordination readiness")


def check_stac_collections(fetch: Fetcher, base: str, assert_non_empty: bool = False) -> CheckResult:
    """GET /stac/collections -> 200 + collections[]. Empty is BLOCKED by default (an ephemeral target
    legitimately has no STAC collections seeded); `assert_non_empty=True` (the demo canary, which is
    known to carry real STAC collections) turns an empty result into a genuine FAIL — the exact drift
    the 2026-07-20 audit found (empty STAC on a deployment that should have data)."""
    r = fetch(base.rstrip("/") + "/stac/collections")
    if r.status == 0:
        return CheckResult("stac-collections", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("stac-collections", "fail", f"-> {r.status} (want 200)")
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("stac-collections", "fail", "response is not valid JSON")
    cols = obj.get("collections") if isinstance(obj, dict) else None
    if not isinstance(cols, list):
        return CheckResult("stac-collections", "fail", "response has no collections[] array")
    if not cols:
        status = "fail" if assert_non_empty else "blocked"
        return CheckResult("stac-collections", status,
                           "STAC catalog reachable but 0 collections" +
                           (" (expected non-empty on this target)" if assert_non_empty else
                            " (no data seeded on this ephemeral target)"))
    return CheckResult("stac-collections", "pass", f"{len(cols)} STAC collection(s)", {"count": len(cols)})


def check_ogc_service_capabilities(fetch: Fetcher, base: str, service_id: str | None, kind: str) -> CheckResult:
    """GET /ogc/services/{service_id}/{kind} (kind in wms|wmts|wcs) -> 200 GetCapabilities (the route
    defaults REQUEST=GetCapabilities when the query string is omitted). BLOCKED when no service id is
    configured."""
    name = f"ogc-{kind}-capabilities"
    if not service_id:
        return CheckResult(name, "blocked", f"no service id configured to probe {kind.upper()} capabilities")
    r = fetch(base.rstrip("/") + f"/ogc/services/{service_id}/{kind}")
    if r.status == 0:
        return CheckResult(name, "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult(name, "fail", f"{service_id}/{kind} -> {r.status} (want 200)")
    return CheckResult(name, "pass", f"{service_id}/{kind} GetCapabilities -> 200")


def check_edr_collections(fetch: Fetcher, base: str) -> CheckResult:
    """GET /edr/collections -> 200 + JSON (reachability; EDR collections may legitimately be empty)."""
    r = fetch(base.rstrip("/") + "/edr/collections")
    if r.status == 0:
        return CheckResult("edr-collections", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("edr-collections", "fail", f"-> {r.status} (want 200)")
    try:
        json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("edr-collections", "fail", "response is not valid JSON")
    return CheckResult("edr-collections", "pass", "GET /edr/collections -> 200 + JSON")


def check_odata_service_document(fetch: Fetcher, base: str) -> CheckResult:
    """GET /odata -> 200 + the OData v4 service document (@odata.context + value[])."""
    r = fetch(base.rstrip("/") + "/odata")
    if r.status == 0:
        return CheckResult("odata-service-document", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("odata-service-document", "fail", f"-> {r.status} (want 200)")
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("odata-service-document", "fail", "response is not valid JSON")
    if not isinstance(obj, dict) or "@odata.context" not in obj:
        return CheckResult("odata-service-document", "fail", "response missing @odata.context")
    return CheckResult("odata-service-document", "pass", "GET /odata -> 200 + service document")


def check_ogc_features_collections(fetch: Fetcher, base: str) -> CheckResult:
    """GET /ogc/features/collections -> 200 + collections[] (OGC API Features)."""
    r = fetch(base.rstrip("/") + "/ogc/features/collections")
    if r.status == 0:
        return CheckResult("ogc-features-collections", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("ogc-features-collections", "fail", f"-> {r.status} (want 200)")
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("ogc-features-collections", "fail", "response is not valid JSON")
    if not isinstance(obj, dict) or not isinstance(obj.get("collections"), list):
        return CheckResult("ogc-features-collections", "fail", "response has no collections[] array")
    return CheckResult("ogc-features-collections", "pass",
                       f"{len(obj['collections'])} OGC API Features collection(s)")


def check_tiles_tilejson(fetch: Fetcher, base: str, layer_id: int | None = None) -> CheckResult:
    """GET /tiles/{layer_id}/tile.json (numeric layer id, per the corrected 2026-07-20 audit route) ->
    200 + a TileJSON document. BLOCKED when no numeric layer id is configured."""
    if layer_id is None:
        return CheckResult("tiles-tilejson", "blocked", "no numeric layer id configured to probe")
    r = fetch(base.rstrip("/") + f"/tiles/{layer_id}/tile.json")
    if r.status == 0:
        return CheckResult("tiles-tilejson", "blocked", "endpoint unreachable")
    if r.status != 200:
        return CheckResult("tiles-tilejson", "fail", f"layer {layer_id} -> {r.status} (want 200)")
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("tiles-tilejson", "fail", "response is not valid JSON")
    if not isinstance(obj, dict) or "tilejson" not in obj:
        return CheckResult("tiles-tilejson", "fail", "response is missing the tilejson field")
    return CheckResult("tiles-tilejson", "pass", f"layer {layer_id}: tile.json -> 200")


def check_geocoding_latency(fetch: Fetcher, base: str, budget_ms: float = 3000.0,
                           locator: str = "GeocodeServer") -> CheckResult:
    """REPORT-ONLY geocoding latency budget probe (never FAILs — geocoding is known-broken pending VPC
    egress, honua-server#2948). Reports `pass` inside budget, `blocked` (not fail) when unreachable,
    erroring, or over budget, always with the measured latency as evidence so drift is visible without
    reddening the canary for a tracked, known gap."""
    url = base.rstrip("/") + f"/rest/services/{locator}/findAddressCandidates?SingleLine=Kahului%2C+HI&f=json"
    t0 = time.perf_counter()
    r = fetch(url)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    evidence = {"elapsedMs": round(elapsed_ms, 1), "budgetMs": budget_ms, "status": r.status}
    if r.status != 200:
        return CheckResult("geocoding-latency", "blocked",
                           f"geocoding unreachable/erroring (status={r.status}, {elapsed_ms:.0f}ms) — "
                           "report-only, known gap: honua-server#2948", evidence)
    if elapsed_ms > budget_ms:
        return CheckResult("geocoding-latency", "blocked",
                           f"geocoding responded in {elapsed_ms:.0f}ms, over the {budget_ms:.0f}ms budget "
                           "— report-only, known gap: honua-server#2948", evidence)
    return CheckResult("geocoding-latency", "pass", f"geocoding responded in {elapsed_ms:.0f}ms "
                       f"(budget {budget_ms:.0f}ms)", evidence)


def run_canary(base: str, fetch: Fetcher, *, admin_fetch: Fetcher | None = None,
              service_id: str | None = None, tile_layer_id: int | None = None,
              assert_stac_non_empty: bool = False, geocode_budget_ms: float = 3000.0) -> list[CheckResult]:
    """Assemble the full canary probe set. Called with no ids/assertions (the generic/cloud-tier mode,
    run_cloud.py against a bare terraform cell) or with the demo defaults (the scheduled demo canary,
    `.github/workflows/demo-canary.yml`, against https://demo.honua.io)."""
    results = [
        check_health_live_ready(fetch, base),
        check_security_headers(fetch, base),
        check_metrics_gated(fetch, base, admin_fetch),
        check_admin_metrics_health(admin_fetch, base),
        check_render_query_smoke(fetch, base, service_id, tile_layer_id if tile_layer_id is not None else 0),
        check_deploy_preflight(fetch, base, admin_fetch),
        check_stac_collections(fetch, base, assert_stac_non_empty),
        check_ogc_service_capabilities(fetch, base, service_id, "wms"),
        check_ogc_service_capabilities(fetch, base, service_id, "wmts"),
        check_ogc_service_capabilities(fetch, base, service_id, "wcs"),
        check_edr_collections(fetch, base),
        check_odata_service_document(fetch, base),
        check_ogc_features_collections(fetch, base),
        check_tiles_tilejson(fetch, base, tile_layer_id),
        check_geocoding_latency(fetch, base, geocode_budget_ms),
    ]
    return results


__all__ = [
    "DEMO_SERVICE_ID", "DEMO_TILE_LAYER_ID", "DEMO_ASSERT_STAC_NON_EMPTY",
    "check_health_live_ready", "check_security_headers", "check_metrics_gated",
    "check_admin_metrics_health", "check_render_query_smoke", "check_deploy_preflight",
    "check_stac_collections", "check_ogc_service_capabilities", "check_edr_collections",
    "check_odata_service_document", "check_ogc_features_collections", "check_tiles_tilejson",
    "check_geocoding_latency", "run_canary",
]
