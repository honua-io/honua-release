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


CANONICAL_CHECKS = [check_health, check_geoservices_error_surfacing, check_service_catalog]


def run_canonical(endpoint: str, fetch: Fetcher | None = None) -> list[CheckResult]:
    """Run the canonical parity set against a deployed endpoint."""
    f = fetch or _default_fetch
    return [check(endpoint, f) for check in CANONICAL_CHECKS]
