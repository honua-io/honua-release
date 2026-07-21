"""Tests for the demo/cloud canary probe set (honua-release#61).

Proven with injected fetchers only — no live server, no cloud, no AWS. Mirrors the style of
e2e/test_cloud.py: every probe must be classifiable as pass/fail/blocked from a canned HTTP response,
and every probe that needs a seeded id (service, tile layer) or an admin key must report BLOCKED (not
a fake pass, not a fake fail) when that id/key isn't configured.

Run: python -m pytest e2e/test_canary_probes.py    (or: python e2e/test_canary_probes.py)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canary_probes as canary  # noqa: E402
import canonical_checks as cc  # noqa: E402


def _fetcher(routes):
    """routes: list of (url_substr, HttpResponse). First match wins; default = unreachable."""
    def fetch(url):
        for sub, resp in routes:
            if sub in url:
                return resp
        return cc.HttpResponse(0, "no route")
    return fetch


_SEC_HEADERS = {
    "strict-transport-security": "max-age=63072000",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def test_health_live_ready():
    ok = _fetcher([("/healthz/live", cc.HttpResponse(200, "")), ("/healthz/ready", cc.HttpResponse(200, ""))])
    assert canary.check_health_live_ready(ok, "http://x").status == "pass"
    bad = _fetcher([("/healthz/live", cc.HttpResponse(200, "")), ("/healthz/ready", cc.HttpResponse(503, ""))])
    assert canary.check_health_live_ready(bad, "http://x").status == "fail"
    assert canary.check_health_live_ready(_fetcher([]), "http://x").status == "blocked"


def test_security_headers():
    ok = _fetcher([("/", cc.HttpResponse(200, "", dict(_SEC_HEADERS)))])
    assert canary.check_security_headers(ok, "http://x").status == "pass"
    missing = dict(_SEC_HEADERS)
    del missing["strict-transport-security"]
    bad = _fetcher([("/", cc.HttpResponse(200, "", missing))])
    r = canary.check_security_headers(bad, "http://x")
    assert r.status == "fail" and "strict-transport-security" in r.why
    assert canary.check_security_headers(_fetcher([]), "http://x").status == "blocked"


def test_metrics_gated():
    fetch = _fetcher([("/metrics", cc.HttpResponse(401, ""))])
    r = canary.check_metrics_gated(fetch, "http://x")
    assert r.status == "blocked" and "no admin API key" in r.why

    admin_ok = _fetcher([("/metrics", cc.HttpResponse(200, "honua_lambda_memory_limit 1"))])
    assert canary.check_metrics_gated(fetch, "http://x", admin_ok).status == "pass"

    admin_bad = _fetcher([("/metrics", cc.HttpResponse(403, ""))])
    assert canary.check_metrics_gated(fetch, "http://x", admin_bad).status == "fail"

    no_gate = _fetcher([("/metrics", cc.HttpResponse(200, ""))])
    assert canary.check_metrics_gated(no_gate, "http://x").status == "fail"


def test_admin_metrics_health():
    assert canary.check_admin_metrics_health(None, "http://x").status == "blocked"
    ok = _fetcher([("/api/v1/metrics/health", cc.HttpResponse(200, ""))])
    assert canary.check_admin_metrics_health(ok, "http://x").status == "pass"
    bad = _fetcher([("/api/v1/metrics/health", cc.HttpResponse(500, ""))])
    assert canary.check_admin_metrics_health(bad, "http://x").status == "fail"


def test_render_query_smoke():
    r = canary.check_render_query_smoke(_fetcher([]), "http://x", None)
    assert r.status == "blocked" and "no demo service id" in r.why

    ok = _fetcher([
        ("MapServer/export", cc.HttpResponse(200, "")),
        ("FeatureServer/0/query", cc.HttpResponse(200, '{"count": 42}')),
    ])
    assert canary.check_render_query_smoke(ok, "http://x", "maui-roads").status == "pass"

    bad_png = _fetcher([("MapServer/export", cc.HttpResponse(500, ""))])
    assert canary.check_render_query_smoke(bad_png, "http://x", "maui-roads").status == "fail"

    # A non-default layer id (the real demo fixture is layer 3, not 0) must be threaded into the
    # FeatureServer query path — this is exactly the 2026-07-21 bug this test guards against.
    layer3 = _fetcher([
        ("MapServer/export", cc.HttpResponse(200, "")),
        ("FeatureServer/3/query", cc.HttpResponse(200, '{"count": 42}')),
    ])
    r3 = canary.check_render_query_smoke(layer3, "http://x", "maui-roads", layer_id=3)
    assert r3.status == "pass"
    # The same fetcher does NOT satisfy a layer_id=0 query — proves the id is actually used, not ignored.
    assert canary.check_render_query_smoke(layer3, "http://x", "maui-roads", layer_id=0).status == "fail"


def test_deploy_preflight():
    no_key_401 = _fetcher([("deploy/preflight", cc.HttpResponse(401, ""))])
    r = canary.check_deploy_preflight(no_key_401, "http://x")
    assert r.status == "blocked" and "no admin API key" in r.why

    no_key_wrong = _fetcher([("deploy/preflight", cc.HttpResponse(200, ""))])
    assert canary.check_deploy_preflight(no_key_wrong, "http://x").status == "fail"

    admin_ok = _fetcher([("deploy/preflight", cc.HttpResponse(200, '{"readyForCoordinatedDeploy": true}'))])
    assert canary.check_deploy_preflight(no_key_401, "http://x", admin_ok).status == "pass"


def test_stac_collections():
    two = _fetcher([("/stac/collections", cc.HttpResponse(200, json.dumps({"collections": [{"id": "a"}, {"id": "b"}]})))])
    assert canary.check_stac_collections(two, "http://x").status == "pass"

    empty = _fetcher([("/stac/collections", cc.HttpResponse(200, json.dumps({"collections": []})))])
    assert canary.check_stac_collections(empty, "http://x").status == "blocked"
    assert canary.check_stac_collections(empty, "http://x", assert_non_empty=True).status == "fail"

    assert canary.check_stac_collections(_fetcher([]), "http://x").status == "blocked"


def test_ogc_service_capabilities():
    r = canary.check_ogc_service_capabilities(_fetcher([]), "http://x", None, "wms")
    assert r.status == "blocked" and "no service id" in r.why

    ok = _fetcher([("/ogc/services/maui-roads/wms", cc.HttpResponse(200, "<WMS_Capabilities/>"))])
    r2 = canary.check_ogc_service_capabilities(ok, "http://x", "maui-roads", "wms")
    assert r2.status == "pass" and r2.name == "ogc-wms-capabilities"

    bad = _fetcher([("/ogc/services/maui-roads/wcs", cc.HttpResponse(404, ""))])
    assert canary.check_ogc_service_capabilities(bad, "http://x", "maui-roads", "wcs").status == "fail"


def test_edr_odata_ogc_features():
    assert canary.check_edr_collections(
        _fetcher([("/edr/collections", cc.HttpResponse(200, "{}"))]), "http://x").status == "pass"
    assert canary.check_edr_collections(_fetcher([]), "http://x").status == "blocked"

    ok_odata = _fetcher([("/odata", cc.HttpResponse(200, json.dumps({"@odata.context": "x", "value": []})))])
    assert canary.check_odata_service_document(ok_odata, "http://x").status == "pass"
    bad_odata = _fetcher([("/odata", cc.HttpResponse(200, json.dumps({"value": []})))])
    assert canary.check_odata_service_document(bad_odata, "http://x").status == "fail"

    ok_feat = _fetcher([("/ogc/features/collections", cc.HttpResponse(200, json.dumps({"collections": [{"id": "a"}]})))])
    r = canary.check_ogc_features_collections(ok_feat, "http://x")
    assert r.status == "pass" and "1 OGC API Features" in r.why


def test_tiles_tilejson():
    r = canary.check_tiles_tilejson(_fetcher([]), "http://x", None)
    assert r.status == "blocked" and "no numeric layer id" in r.why

    ok = _fetcher([("/tiles/3/tile.json", cc.HttpResponse(200, json.dumps({"tilejson": "3.0.0"})))])
    assert canary.check_tiles_tilejson(ok, "http://x", 3).status == "pass"

    bad = _fetcher([("/tiles/3/tile.json", cc.HttpResponse(404, ""))])
    assert canary.check_tiles_tilejson(bad, "http://x", 3).status == "fail"


def test_geocoding_latency_never_fails():
    # Reachable + fast -> pass.
    fast = _fetcher([("findAddressCandidates", cc.HttpResponse(200, "{}"))])
    r = canary.check_geocoding_latency(fast, "http://x", budget_ms=10_000)
    assert r.status == "pass"

    # Unreachable/erroring -> BLOCKED (report-only), never FAIL.
    broken = _fetcher([])
    r2 = canary.check_geocoding_latency(broken, "http://x", budget_ms=10_000)
    assert r2.status == "blocked" and "2948" in r2.why

    # Over budget -> BLOCKED (report-only), never FAIL.
    slow = _fetcher([("findAddressCandidates", cc.HttpResponse(200, "{}"))])
    r3 = canary.check_geocoding_latency(slow, "http://x", budget_ms=-1)
    assert r3.status == "blocked" and "2948" in r3.why

    assert all(canary.check_geocoding_latency(f, "http://x", budget_ms=b).status != "fail"
              for f, b in [(fast, 10_000), (broken, 10_000), (slow, -1)])


def test_run_canary_generic_mode_blocks_seeded_data_probes_honestly():
    # No ids configured (the generic/cloud-tier mode against a bare terraform cell): probes needing
    # seeded data/ids report BLOCKED, never a fake pass/fail; reachability-only probes still run.
    fetch = _fetcher([
        # Specific paths FIRST — "/" (root, for security-headers) matches every URL as a substring, so
        # it must be last or it would swallow every other route's lookup.
        ("/healthz/live", cc.HttpResponse(200, "")), ("/healthz/ready", cc.HttpResponse(200, "")),
        ("/metrics", cc.HttpResponse(401, "")),
        ("deploy/preflight", cc.HttpResponse(401, "")),
        ("/stac/collections", cc.HttpResponse(200, json.dumps({"collections": []}))),
        ("/edr/collections", cc.HttpResponse(200, "{}")),
        ("/odata", cc.HttpResponse(200, json.dumps({"@odata.context": "x"}))),
        ("/ogc/features/collections", cc.HttpResponse(200, json.dumps({"collections": []}))),
        ("/", cc.HttpResponse(200, "", dict(_SEC_HEADERS))),
    ])
    results = canary.run_canary("http://x", fetch)
    by_name = {r.name: r for r in results}
    assert by_name["render-query-smoke"].status == "blocked"
    assert by_name["tiles-tilejson"].status == "blocked"
    assert by_name["ogc-wms-capabilities"].status == "blocked"
    assert by_name["stac-collections"].status == "blocked"       # empty, non-empty NOT asserted
    assert by_name["health-live-ready"].status == "pass"
    assert by_name["security-headers"].status == "pass"
    assert by_name["edr-collections"].status == "pass"
    assert by_name["odata-service-document"].status == "pass"
    assert by_name["ogc-features-collections"].status == "pass"
    assert all(r.status != "fail" for r in results)  # nothing genuinely broken in this canned run


def test_run_canary_demo_mode_with_ids_configured():
    fetch = _fetcher([
        # Specific paths FIRST — "/" (root, for security-headers) matches every URL as a substring, so
        # it must be last or it would swallow every other route's lookup.
        ("/healthz/live", cc.HttpResponse(200, "")), ("/healthz/ready", cc.HttpResponse(200, "")),
        ("/metrics", cc.HttpResponse(401, "")),
        ("deploy/preflight", cc.HttpResponse(401, "")),
        ("/stac/collections", cc.HttpResponse(200, json.dumps({"collections": [{"id": "x"}]}))),
        ("MapServer/export", cc.HttpResponse(200, "")),
        ("FeatureServer/3/query", cc.HttpResponse(200, '{"count": 1}')),
        ("/ogc/services/maui-roads/wms", cc.HttpResponse(200, "")),
        ("/ogc/services/maui-roads/wmts", cc.HttpResponse(200, "")),
        ("/ogc/services/maui-roads/wcs", cc.HttpResponse(200, "")),
        ("/edr/collections", cc.HttpResponse(200, "{}")),
        ("/odata", cc.HttpResponse(200, json.dumps({"@odata.context": "x"}))),
        ("/ogc/features/collections", cc.HttpResponse(200, json.dumps({"collections": []}))),
        ("/tiles/3/tile.json", cc.HttpResponse(200, json.dumps({"tilejson": "3.0.0"}))),
        ("/", cc.HttpResponse(200, "", dict(_SEC_HEADERS))),
    ])
    results = canary.run_canary("http://x", fetch, service_id="maui-roads", tile_layer_id=3,
                                assert_stac_non_empty=True)
    by_name = {r.name: r for r in results}
    assert by_name["render-query-smoke"].status == "pass"
    assert by_name["tiles-tilejson"].status == "pass"
    assert by_name["ogc-wms-capabilities"].status == "pass"
    assert by_name["stac-collections"].status == "pass"


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
