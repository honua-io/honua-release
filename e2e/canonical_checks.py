"""Canonical (slim) parity checks — the target-agnostic behaviour every deploy target must exhibit
identically.

Per docs/TEST-STRATEGY.md: the full SDK x scenario matrix runs on ONE reference target (local docker);
deploy-target *parity* runs this small, data-independent, HTTP-level set on EVERY target and asserts
identical results. So these checks deliberately avoid SDK toolchains, Prometheus scrapes, or seeded
data — they probe the wire surface honua-server exposes anywhere it runs:

  health            GET /healthz                              -> 200
  geoservices-error a deliberately invalid GeoServices query  -> HTTP 200 + {"error":{...}} envelope
                    (the Esri convention; the same behaviour the SDK guard depends on, checked here at
                     the HTTP layer so it is target-agnostic)
  service-catalog   GET /rest/services?f=json                 -> 200 + JSON object (a catalog shape)
  capabilities      GET /api/v1/admin/capabilities?f=json      -> 200 + JSON (the control-plane surface
                    the MCP tool catalog is derived from; manifest advertises admin contract v1)
  geoprocessing     GET /rest/services?f=json includes a GP    -> a GPServer/GeoProcessing service is
                    catalogued (the surface the GP driver exercises), else blocked
  capability-manifest  GET /api/v1/capabilities/manifest        -> 200 + schemaVersion
                    honua.capability_manifest.v1 + every id in the committed expected-GA list
                    (e2e/expected-ga-manifest.json, honua-release#61) is supported=true. Genuinely
                    target-agnostic: the manifest's `supported` set is a static server declaration, not
                    seeded data, so it holds identically on a terraform-provisioned cell or
                    https://demo.honua.io. `available` is only ASSERTED when an authenticated fetch (an
                    admin X-API-Key) is supplied — unauthenticated callers get available counts as
                    evidence only, since availability legitimately depends on entitlement/policy this
                    check has no key to exercise.

The EXTENDED scenario set (MCP handshake + tool-catalog, Studio authoring, Geoprocessing execute, and
the top demo flow) is the same seam suite the Slice-1 local-docker harness drives. Running it against a
*cloud* endpoint needs the driver toolchain packaged as a harness image (honua-release#35); until that
lands, `run_extended` records those scenarios as BLOCKED (with the #35 reference), so a real per-RC
cloud cert honestly shows cloud MCP/Studio/GP/demo are not-yet-certified rather than green-washing them.

`fetch` is injectable so the result-normalisation is unit-testable without a live server.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

E2E_DIR = Path(__file__).resolve().parent
EXPECTED_GA_PATH = E2E_DIR / "expected-ga-manifest.json"
CAPABILITY_MANIFEST_SCHEMA = "honua.capability_manifest.v1"


@dataclass
class HttpResponse:
    status: int
    body: str
    headers: dict = field(default_factory=dict)   # lower-cased header names -> value


@dataclass
class CheckResult:
    name: str
    status: str            # pass | fail | blocked
    why: str = ""
    evidence: dict = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        """The parity-comparable signature — name + verdict (not the free-text why/timing)."""
        return (self.name, self.status)


Fetcher = Callable[[str], HttpResponse]


def make_fetch(headers: dict[str, str] | None = None, timeout: float = 15.0) -> Fetcher:
    """A `Fetcher` factory that attaches request headers (e.g. an admin `X-API-Key`) and captures
    response headers (lower-cased). Used for authenticated canonical-check calls and the demo-canary
    probe set (e2e/canary_probes.py) — kept here so every caller shares one HTTP implementation."""
    hdrs = dict(headers or {})

    def _fetch(url: str) -> HttpResponse:
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return HttpResponse(r.status, r.read().decode("utf-8", "replace"),
                                    {k.lower(): v for k, v in r.getheaders()})
        except urllib.error.HTTPError as e:  # a non-2xx still carries a status + body
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            hdrs_out = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return HttpResponse(e.code, body, hdrs_out)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return HttpResponse(0, f"transport error: {e}")

    return _fetch


def _default_fetch(url: str) -> HttpResponse:
    return make_fetch()(url)


def load_expected_ga(path: str | Path | None = None) -> dict | None:
    """Load the committed expected-GA manifest (e2e/expected-ga-manifest.json by default).

    Never raises — returns None on anything missing/unreadable/malformed so callers fail CLOSED to
    `blocked`, never a fake pass on a manifest that can't be trusted.
    """
    p = Path(path) if path else EXPECTED_GA_PATH
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("expectedGa"), list):
        return None
    return data


def _is_esri_error_envelope(body: str) -> bool:
    """True when `body` is a GeoServices `{"error":{"code":<int>,...}}` envelope — the same narrow
    shape the SDK guards use (honua-sdk-python#122 / honua-sdk-js#309)."""
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return False
    err = obj.get("error") if isinstance(obj, dict) else None
    return isinstance(err, dict) and isinstance(err.get("code"), int) and not isinstance(err.get("code"), bool)


def check_health(endpoint: str, fetch: Fetcher) -> CheckResult:
    base = endpoint.rstrip("/")
    r = fetch(base + "/healthz")
    if r.status == 200:
        return CheckResult("health", "pass", "GET /healthz -> 200")
    if r.status == 0:
        return CheckResult("health", "blocked", f"endpoint unreachable ({r.body})")
    if r.status == 404:
        # Plain /healthz is Development-only (Honua.ServiceDefaults MapDefaultEndpoints) — a
        # Production/Staging deploy (any real cloud cell, or https://demo.honua.io) 404s here by
        # design (2026-07-20/honua-release#61 finding). Fall back to the always-registered
        # /healthz/live + /healthz/ready pair before calling this a fail.
        live = fetch(base + "/healthz/live")
        ready = fetch(base + "/healthz/ready")
        if live.status == 0 or ready.status == 0:
            return CheckResult("health", "blocked", "endpoint unreachable on /healthz/live or /healthz/ready")
        if live.status == 200 and ready.status == 200:
            return CheckResult("health", "pass",
                               "GET /healthz -> 404 (Development-only route) but "
                               "/healthz/live + /healthz/ready -> 200")
        return CheckResult("health", "fail",
                           f"/healthz -> 404 and /healthz/live -> {live.status}, /healthz/ready -> {ready.status}",
                           {"healthz": 404, "live": live.status, "ready": ready.status})
    return CheckResult("health", "fail", f"GET /healthz -> {r.status}", {"status": r.status})


def check_geoservices_error_surfacing(endpoint: str, fetch: Fetcher) -> CheckResult:
    # A query against a non-existent service/layer with a deliberately malformed predicate. The Esri
    # GeoServices convention (which honua-server implements) returns HTTP 200 with an {error} envelope.
    url = endpoint.rstrip("/") + "/rest/services/__honua_parity_missing__/FeatureServer/0/query?where=1%3D1))&f=json"
    r = fetch(url)
    if r.status == 0:
        return CheckResult("geoservices-error", "blocked", f"endpoint unreachable ({r.body})")
    if r.status == 200 and _is_esri_error_envelope(r.body):
        return CheckResult("geoservices-error", "pass", "invalid query -> HTTP 200 + {error} envelope (Esri convention)")
    if _is_esri_error_envelope(r.body):
        # An error envelope on a non-200 is acceptable too (still surfaced); record the status for parity.
        return CheckResult("geoservices-error", "pass", f"invalid query -> {r.status} + {{error}} envelope",
                           {"status": r.status})
    return CheckResult("geoservices-error", "fail",
                       f"invalid query did not return a GeoServices error envelope (status={r.status})",
                       {"status": r.status, "body_head": r.body[:200]})


def check_service_catalog(endpoint: str, fetch: Fetcher) -> CheckResult:
    r = fetch(endpoint.rstrip("/") + "/rest/services?f=json")
    if r.status == 0:
        return CheckResult("service-catalog", "blocked", f"endpoint unreachable ({r.body})")
    if r.status != 200:
        return CheckResult("service-catalog", "fail", f"GET /rest/services -> {r.status}", {"status": r.status})
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("service-catalog", "fail", "catalog is not valid JSON")
    if not isinstance(obj, dict):
        return CheckResult("service-catalog", "fail", "catalog JSON is not an object")
    return CheckResult("service-catalog", "pass", "GET /rest/services -> 200 + JSON catalog object")


def check_admin_capabilities(endpoint: str, fetch: Fetcher) -> CheckResult:
    """The admin/control-plane capabilities envelope (manifest: admin contract v1). The MCP tool
    catalog is derived from this surface, so a target that can't advertise capabilities can't host a
    coherent MCP layer — this is the target-agnostic HTTP proxy for 'MCP is wireable here'."""
    r = fetch(endpoint.rstrip("/") + "/api/v1/admin/capabilities?f=json")
    if r.status == 0:
        return CheckResult("capabilities", "blocked", f"endpoint unreachable ({r.body})")
    if r.status != 200:
        return CheckResult("capabilities", "fail", f"GET /api/v1/admin/capabilities -> {r.status}",
                           {"status": r.status})
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("capabilities", "fail", "capabilities is not valid JSON")
    if not isinstance(obj, dict):
        return CheckResult("capabilities", "fail", "capabilities JSON is not an object")
    return CheckResult("capabilities", "pass", "GET /api/v1/admin/capabilities -> 200 + JSON envelope")


def check_geoprocessing(endpoint: str, fetch: Fetcher) -> CheckResult:
    """The Geoprocessing surface the GP driver exercises. At the target-agnostic HTTP layer we assert a
    GP service is catalogued (GPServer / a `geoprocessing` service type). Absent => blocked (the catalog
    is reachable but GP isn't exposed on this deploy), never a fake pass."""
    r = fetch(endpoint.rstrip("/") + "/rest/services?f=json")
    if r.status == 0:
        return CheckResult("geoprocessing", "blocked", f"endpoint unreachable ({r.body})")
    if r.status != 200:
        return CheckResult("geoprocessing", "fail", f"GET /rest/services -> {r.status}", {"status": r.status})
    body_l = r.body.lower()
    if "gpserver" in body_l or "geoprocessing" in body_l:
        return CheckResult("geoprocessing", "pass", "service catalog advertises a GPServer / geoprocessing service")
    return CheckResult("geoprocessing", "blocked",
                       "catalog reachable but no GPServer/geoprocessing service advertised on this target")


def check_capability_manifest(endpoint: str, fetch: Fetcher, *, expected: dict | None = None,
                              authenticated_fetch: Fetcher | None = None) -> CheckResult:
    """Live capability-manifest check (honua-release#61): GET {endpoint}/api/v1/capabilities/manifest
    and assert every id in the committed expected-GA list (e2e/expected-ga-manifest.json, minus its
    `excluded` — currently `security.mtls`/`alerts.geofence`, both deliberately gated) is
    `supported=true`. When `authenticated_fetch` is supplied (an admin-X-API-Key-bearing Fetcher) the
    SAME ids must additionally be `available=true`; unauthenticated callers get an `available` count as
    evidence only (never asserted — availability legitimately depends on entitlement/policy this check
    has no key to exercise).

    `expected` overrides the committed file for tests; `None` loads e2e/expected-ga-manifest.json — a
    missing/malformed file reports BLOCKED, never a fake pass.
    """
    url = endpoint.rstrip("/") + "/api/v1/capabilities/manifest"
    r = fetch(url)
    if r.status == 0:
        return CheckResult("capability-manifest", "blocked", f"endpoint unreachable ({r.body})")
    if r.status != 200:
        return CheckResult("capability-manifest", "fail",
                           f"GET /api/v1/capabilities/manifest -> {r.status}", {"status": r.status})
    try:
        obj = json.loads(r.body)
    except (ValueError, TypeError):
        return CheckResult("capability-manifest", "fail", "manifest is not valid JSON")
    if not isinstance(obj, dict):
        return CheckResult("capability-manifest", "fail", "manifest JSON is not an object")
    schema = obj.get("schemaVersion")
    if schema != CAPABILITY_MANIFEST_SCHEMA:
        return CheckResult("capability-manifest", "fail",
                           f"unexpected schemaVersion {schema!r} (want {CAPABILITY_MANIFEST_SCHEMA!r})")
    caps = obj.get("capabilities")
    if not isinstance(caps, list):
        return CheckResult("capability-manifest", "fail", "manifest has no capabilities[] array")
    by_id = {c.get("id"): c for c in caps if isinstance(c, dict) and c.get("id")}

    if expected is None:
        expected = load_expected_ga()
    if expected is None:
        return CheckResult("capability-manifest", "blocked",
                           f"schemaVersion ok ({len(caps)} capabilities advertised) but "
                           f"{EXPECTED_GA_PATH.name} is missing/unreadable — cannot assert GA coverage",
                           {"schemaVersion": schema, "totalCapabilities": len(caps)})

    expected_ids = [i for i in (expected.get("expectedGa") or []) if isinstance(i, str)]
    excluded_ids = {e.get("id") for e in (expected.get("excluded") or []) if isinstance(e, dict) and e.get("id")}
    checked = [i for i in expected_ids if i not in excluded_ids]

    missing = sorted(i for i in checked if i not in by_id)
    unsupported = sorted(i for i in checked if i in by_id and by_id[i].get("supported") is not True)
    available_count = sum(1 for i in checked if by_id.get(i, {}).get("available") is True)

    evidence = {"schemaVersion": schema, "totalCapabilities": len(caps), "expectedGaCount": len(checked),
               "excludedCount": len(excluded_ids), "availableCountUnauthenticated": available_count}

    if missing:
        return CheckResult("capability-manifest", "fail",
                           f"expected-GA ids missing from manifest: {missing}", evidence)
    if unsupported:
        return CheckResult("capability-manifest", "fail",
                           f"expected-GA ids not supported=true: {unsupported}", evidence)

    if authenticated_fetch is not None:
        ar = authenticated_fetch(url)
        if ar.status != 200:
            evidence["authenticated"] = False
            return CheckResult("capability-manifest", "fail",
                               f"authenticated GET /api/v1/capabilities/manifest -> {ar.status}", evidence)
        acaps = None
        try:
            aobj = json.loads(ar.body)
            if isinstance(aobj, dict):
                acaps = aobj.get("capabilities")
        except (ValueError, TypeError):
            acaps = None
        if not isinstance(acaps, list):
            evidence["authenticated"] = False
            return CheckResult("capability-manifest", "fail",
                               "authenticated manifest is not valid JSON / missing capabilities[]", evidence)
        aby_id = {c.get("id"): c for c in acaps if isinstance(c, dict) and c.get("id")}
        # An expected-GA id entirely OMITTED from the authenticated manifest is exactly as bad as one
        # present with available != true — both must fail, not silently drop out of `unavailable`.
        unavailable = sorted(i for i in checked if aby_id.get(i, {}).get("available") is not True)
        avail_auth = sum(1 for i in checked if aby_id.get(i, {}).get("available") is True)
        evidence["authenticated"] = True
        evidence["availableCountAuthenticated"] = avail_auth
        if unavailable:
            return CheckResult("capability-manifest", "fail",
                               f"expected-GA ids not available=true when authenticated: {unavailable}", evidence)
        return CheckResult("capability-manifest", "pass",
                           f"{len(checked)} expected-GA ids supported+available "
                           f"({avail_auth}/{len(checked)} available, authenticated)", evidence)

    return CheckResult("capability-manifest", "pass",
                       f"{len(checked)} expected-GA ids all supported=true "
                       f"({available_count}/{len(checked)} also available=true, unauthenticated)", evidence)


# The slim parity set every deploy target must exhibit identically (HTTP-level, data-independent).
# capability-manifest is deliberately NOT in this list — run_canonical appends it explicitly so it can
# thread an optional authenticated_fetch/expected_ga override through without changing this signature.
CANONICAL_CHECKS = [check_health, check_geoservices_error_surfacing, check_service_catalog,
                    check_admin_capabilities, check_geoprocessing]


# The seam scenarios (MCP / Studio / GP-execute / top-demo) that need the driver toolchain packaged as
# a cloud harness image (honua-release#35). Recorded as BLOCKED against a raw cloud endpoint until #35
# lands, so a real per-RC cloud cert cannot green-wash uncertified cloud MCP/Studio/GP/demo behaviour.
EXTENDED_SCENARIOS = [
    ("mcp-handshake", "MCP initialize + tools/list vs the committed tool-catalog snapshot"),
    ("studio-authoring", "Studio create->style->publish authoring lifecycle"),
    ("gp-execute", "Geoprocessing submitJob->poll->result end-to-end"),
    ("top-demo", "the flagship demo flow end-to-end"),
]


def run_canonical(endpoint: str, fetch: Fetcher | None = None, *,
                  authenticated_fetch: Fetcher | None = None,
                  expected_ga: dict | None = None) -> list[CheckResult]:
    """Run the canonical parity set — plus the live capability-manifest check (honua-release#61) —
    against a deployed endpoint. `authenticated_fetch`/`expected_ga` pass straight through to
    `check_capability_manifest`; both default to the unauthenticated / committed-file behaviour."""
    f = fetch or _default_fetch
    results = [check(endpoint, f) for check in CANONICAL_CHECKS]
    results.append(check_capability_manifest(endpoint, f, expected=expected_ga,
                                             authenticated_fetch=authenticated_fetch))
    return results


def run_extended(endpoint: str, fetch: Fetcher | None = None) -> list[CheckResult]:
    """The extended seam scenarios against a cloud endpoint. Until the harness image (honua-release#35)
    runs the real drivers here, each is BLOCKED (honest) — the release train's require_real promotes a
    blocked extended scenario to FAIL, so cloud MCP/Studio/GP/demo cert is gated, not assumed."""
    return [CheckResult(name, "blocked",
                        f"{desc}: needs the cloud harness image (honua-release#35) to drive against {endpoint.rstrip('/')}")
            for name, desc in EXTENDED_SCENARIOS]
