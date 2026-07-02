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
from typing import Callable


@dataclass
class HttpResponse:
    status: int
    body: str


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


def _default_fetch(url: str, timeout: float = 15.0) -> HttpResponse:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return HttpResponse(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:  # a non-2xx still carries a status + body
        return HttpResponse(e.code, e.read().decode("utf-8", "replace") if e.fp else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return HttpResponse(0, f"transport error: {e}")


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
    r = fetch(endpoint.rstrip("/") + "/healthz")
    if r.status == 200:
        return CheckResult("health", "pass", "GET /healthz -> 200")
    if r.status == 0:
        return CheckResult("health", "blocked", f"endpoint unreachable ({r.body})")
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


# The slim parity set every deploy target must exhibit identically (HTTP-level, data-independent).
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


def run_canonical(endpoint: str, fetch: Fetcher | None = None) -> list[CheckResult]:
    """Run the canonical parity set against a deployed endpoint."""
    f = fetch or _default_fetch
    return [check(endpoint, f) for check in CANONICAL_CHECKS]


def run_extended(endpoint: str, fetch: Fetcher | None = None) -> list[CheckResult]:
    """The extended seam scenarios against a cloud endpoint. Until the harness image (honua-release#35)
    runs the real drivers here, each is BLOCKED (honest) — the release train's require_real promotes a
    blocked extended scenario to FAIL, so cloud MCP/Studio/GP/demo cert is gated, not assumed."""
    return [CheckResult(name, "blocked",
                        f"{desc}: needs the cloud harness image (honua-release#35) to drive against {endpoint.rstrip('/')}")
            for name, desc in EXTENDED_SCENARIOS]
