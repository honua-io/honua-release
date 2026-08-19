#!/usr/bin/env python3
"""Drive the bounded SLO probe window against a deployed candidate and report what it measured.

WHY A PROBE AT ALL. honua-server's SLO series are cumulative counters scoped to one server process's
lifetime. On Lambda that makes a raw `/metrics` reading a function of traffic timing rather than of
the release: measured against an unchanged candidate on 2026-08-18 the gate said `fail` (17.6%, from
six bad requests an operator had made minutes earlier, which no amount of clean traffic can dilute
because a counter has no window), then `blocked` (container recycled, both series gone), then `pass`,
then `skipped`. Four verdicts, one release. See honua-release#5.

So the gate stops reading a number and starts measuring an INTERVAL it defines:

    scrape  ->  deliver a fixed, deterministic burst of ordinary GeoServices reads  ->  scrape

and evaluates the difference. Everything that happened before the window cancels out, the denominator
cannot be empty, and the window is the same shape on every run — which is what makes two consecutive
dispatches agree.

WHAT THE VERDICT THEREFORE MEANS, said plainly: this is a synthetic canary over a bounded window, NOT
a production SLO. It answers "does the pinned candidate serve its own catalogue without emitting error
envelopes, right now" — it does not answer "what error rate have real users seen". Ambient traffic that
lands on the same instance during the window is included (it is real signal about the same process),
but the window is minutes long and gate-driven, so nobody should read a `pass` here as production
evidence. The workflow summary says so in the verdict text.

WHAT THIS FILE DOES NOT DECIDE. All network I/O lives here; every verdict lives in tools/check_slo.py,
which is pure and unit-tested. This module only reports observations. It emits `KEY=value` lines on
stdout for GITHUB_ENV and human-readable progress on stderr.

The probe is READ-ONLY and self-describing: it walks the candidate's own `/rest/services` catalogue,
learns real layer ids from each service's metadata (they differ per service — hardcoding `0` is what
produced six of the twelve errors in the poisoned reading above), and issues only metadata and
`returnCountOnly` queries. It never writes, and it never deliberately issues a bad request.

  python tools/slo_probe.py            # env: HONUA_METRICS_URL, HONUA_ADMIN_PASSWORD,
                                       #      HONUA_SLO_PROBE_REQUESTS, REQUEST_METRIC, ERROR_METRIC
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "e2e"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_slo import (CONTINUITY_WITNESS_SERIES, evaluate_process_continuity,  # noqa: E402
                       service_root_url)
from runner.harness import parse_label_selector, parse_metric_total  # noqa: E402

# Default burst size. It is not arbitrary: check_slo.minimum_resolvable_window says a 1% budget needs
# at least 100 requests before a single error is inside the budget's resolution, and the probe must
# clear that floor with headroom for the attribution check. 200 sequential reads take ~65s against
# demo.honua.io.
DEFAULT_PROBE_REQUESTS = 200

# Protocol families that are in scope for the GeoServices error budget, in the order the plan cycles
# them. Both are covered by the denominator selector honua-server publishes in
# observability/slo-metric-contract.json; the probe drives traffic at the same population the gate
# then measures, which is the point.
PROBE_SERVICE_TYPES = ("FeatureServer", "MapServer")


# --------------------------------------------------------------------------------------------------
# Pure plan construction (unit-tested in tools/test_slo_upgrade.py)
# --------------------------------------------------------------------------------------------------
def catalog_services(document: object) -> list[tuple[str, str]]:
    """[(name, type), ...] from a GeoServices catalogue response, sorted for determinism.

    Determinism matters more than it looks: the burst must be the SAME set of requests on every run,
    or two consecutive dispatches measure two different populations and the reproducibility this whole
    change exists for is lost.
    """
    if not isinstance(document, dict):
        return []
    found = set()
    for entry in document.get("services") or []:
        if not isinstance(entry, dict):
            continue
        name, kind = entry.get("name"), entry.get("type")
        if isinstance(name, str) and isinstance(kind, str) and kind in PROBE_SERVICE_TYPES:
            found.add((name, kind))
    return sorted(found)


def service_layer_ids(document: object) -> list[int]:
    """Layer ids advertised by a service's own metadata. Empty when it advertises none."""
    if not isinstance(document, dict):
        return []
    ids = []
    for layer in document.get("layers") or []:
        if isinstance(layer, dict) and isinstance(layer.get("id"), int):
            ids.append(layer["id"])
    return sorted(set(ids))


def build_plan(services: list[tuple[str, str]], layers: dict[tuple[str, str], list[int]],
               target: int) -> list[str]:
    """The deterministic request plan: a fixed cycle of in-scope reads, repeated up to `target`.

    Every path is a read the candidate is expected to answer successfully. A probe that deliberately
    issued bad requests would be measuring its own mistakes, which is exactly the reading that made
    the gate say 17.6% about a healthy build.
    """
    cycle: list[str] = []
    for name, kind in services:
        quoted = urllib.parse.quote(name, safe="")
        cycle.append(f"/rest/services/{quoted}/{kind}?f=json")
        for layer_id in layers.get((name, kind), []):
            cycle.append(f"/rest/services/{quoted}/{kind}/{layer_id}?f=json")
            cycle.append(f"/rest/services/{quoted}/{kind}/{layer_id}/query"
                         f"?where=1%3D1&returnCountOnly=true&f=json")
    if not cycle or target <= 0:
        return []
    plan = []
    while len(plan) < target:
        plan.extend(cycle)
    return plan[:target]


def numerator_filters(request_selector: str) -> dict[str, str]:
    """The error-series label filter that covers the SAME population as the denominator selector.

    The denominator selector honua-server publishes scopes `honua_serving_request_duration_ms_count`
    by `honua_protocol`; the error counter carries the same family vocabulary under `service_type`.
    Translating one to the other keeps numerator and denominator over one population, which is the
    contract's own stated rule ("an error-budget ratio is only meaningful when its denominator covers
    the same population as its numerator") — and it fixes an asymmetry that was pointing the WRONG
    way. Verified live on demo.honua.io: catalog-level errors are recorded as
    service_type="GeoServices", and the exposition contains no honua_protocol="GeoServices" series at
    all, so those requests are in no denominator. A window's verdict therefore moved with how many
    strangers had port-scanned the demo while it ran (three unauthenticated 401s in a 200-request
    window read as 1.5% against a 1% budget), which is exactly the traffic-timing dependence the
    window exists to remove.

    This NARROWS the numerator only to surfaces the denominator provably cannot count. Every error on
    a counted surface still rates — including the HTTP-200-with-{error} in-band class this gate was
    built for. What drops out is counted separately and named in the verdict, never dropped silently.
    """
    protocols = parse_label_selector(request_selector).get("honua_protocol")
    return {"service_type": protocols} if protocols else {}


def witness_totals(body: str | None) -> dict[str, float | None]:
    """The per-process continuity witness, read out of one exposition."""
    if body is None:
        return {}
    return {name: parse_metric_total(body, name) for name in CONTINUITY_WITNESS_SERIES}


# --------------------------------------------------------------------------------------------------
# Network I/O
# --------------------------------------------------------------------------------------------------
def _fetch(url: str, api_key: str | None = None, timeout: int = 30) -> tuple[int, str]:
    """(http_status, body). Status 0 means the request never got an HTTP response at all."""
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json")
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, f"{exc.__class__.__name__}: {exc}"


def _json(url: str, api_key: str | None = None) -> object | None:
    status, body = _fetch(url, api_key)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def discover(root: str) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[int]], int]:
    """Walk the candidate's catalogue. Returns (services, layers, counted_requests).

    Per-service metadata reads are ordinary in-scope traffic on the instance under measurement, so
    they are counted into the attribution floor — leaving them out would understate it and let a
    window that only just met the floor look like it cleared it.

    The catalogue ROOT read is deliberately NOT counted. Honua classifies `/rest/services` to a
    catalog-level surface that emits no honua_serving_request_duration_ms_count series at all (checked
    live: the demo exposes honua_protocol values FeatureServer and MapServer only), so it is delivered
    and served but appears in no denominator. Counting it made the attribution check fail by exactly
    one on every single run — 199 observed against 200 delivered — which would have turned a working
    guard into a permanent block. The floor must count the same population the denominator does.
    """
    counted = 0
    catalog = _json(f"{root}?f=json")   # served, but in no denominator series — see above
    services = catalog_services(catalog)
    layers: dict[tuple[str, str], list[int]] = {}
    for name, kind in services:
        quoted = urllib.parse.quote(name, safe="")
        document = _json(f"{root}/{quoted}/{kind}?f=json")
        counted += 1
        layers[(name, kind)] = service_layer_ids(document)
    return services, layers, counted


def main(argv: list[str] | None = None) -> int:
    metrics_url = os.environ.get("HONUA_METRICS_URL", "").strip()
    api_key = os.environ.get("HONUA_ADMIN_PASSWORD", "") or None
    error_metric = os.environ.get("ERROR_METRIC", "honua_geoservices_error_total")
    request_metric = os.environ.get("REQUEST_METRIC", "honua_serving_request_duration_ms_count")
    selector = os.environ.get("REQUEST_SELECTOR", "").strip()
    try:
        target = int(os.environ.get("HONUA_SLO_PROBE_REQUESTS") or DEFAULT_PROBE_REQUESTS)
    except ValueError:
        target = DEFAULT_PROBE_REQUESTS

    out: dict[str, str] = {
        "PROBE_ERR_BEFORE": "", "PROBE_ERR_AFTER": "",
        "PROBE_REQ_BEFORE": "", "PROBE_REQ_AFTER": "",
        "PROBE_REQUESTS": "0", "PROBE_CONTINUITY": "", "PROBE_NOTE": "",
        "PROBE_UNRATED_ERRORS": "0", "PROBE_UNRATED_NOTE": "",
    }

    def emit(note: str = "") -> int:
        if note:
            out["PROBE_NOTE"] = note
        # GITHUB_ENV is line-oriented: every value must collapse to one line or the whole block is
        # mis-parsed and the gate silently evaluates the wrong inputs.
        for key, value in out.items():
            print(f"{key}={' '.join(str(value).split())}")
        return 0

    root = service_root_url(metrics_url)
    if root is None:
        return emit("HONUA_METRICS_URL unset or not an http(s) URL - no candidate to probe")
    if not selector:
        # Falling back to an unscoped denominator would be fail-open; the gate blocks instead.
        return emit("scope selector unresolved - refusing to probe against an unscoped denominator")
    filters = parse_label_selector(selector)
    error_filters = numerator_filters(selector)
    if not error_filters:
        return emit(f"denominator selector {selector!r} carries no honua_protocol matcher, so the "
                    "numerator cannot be held to the same population - refusing to rate an "
                    "asymmetric ratio")

    def sample() -> tuple[str | None, dict[str, float | None], float | None, float | None, float]:
        """(body, witness, in-scope errors, in-scope requests, errors the denominator cannot count)"""
        status, body = _fetch(metrics_url, api_key)
        if status != 200:
            return None, {}, None, None, 0.0
        errors = parse_metric_total(body, error_metric, error_filters)
        every_error = parse_metric_total(body, error_metric)
        requests = parse_metric_total(body, request_metric, filters)
        unrated = (every_error or 0.0) - (errors or 0.0)
        return body, witness_totals(body), errors, requests, unrated

    before_body, before_witness, err_before, req_before, unrated_before = sample()
    if before_body is None:
        return emit(f"baseline scrape of {metrics_url} failed - nothing to difference against")
    out["PROBE_ERR_BEFORE"] = "" if err_before is None else repr(err_before)
    out["PROBE_REQ_BEFORE"] = "" if req_before is None else repr(req_before)
    print(f"baseline: error={err_before} request={req_before}", file=sys.stderr)

    services, layers, discovery_requests = discover(root)
    if not services:
        return emit(f"{root} advertised no in-scope services - the probe has nothing to drive")
    print(f"discovered {len(services)} in-scope services "
          f"({discovery_requests} counted discovery requests)", file=sys.stderr)

    plan = build_plan(services, layers, max(target - discovery_requests, 0))
    started = time.time()
    delivered, transport_failures, codes = discovery_requests, 0, {}
    for path in plan:
        status, _ = _fetch(root.rsplit("/rest/services", 1)[0] + path)
        codes[status] = codes.get(status, 0) + 1
        if status == 0:
            transport_failures += 1
        else:
            delivered += 1
    print(f"burst: {len(plan)} requests in {time.time() - started:.1f}s codes={codes}", file=sys.stderr)

    after_body, after_witness, err_after, req_after, unrated_after = sample()
    if after_body is None:
        return emit(f"closing scrape of {metrics_url} failed - the window has no end")
    out["PROBE_ERR_AFTER"] = "" if err_after is None else repr(err_after)
    out["PROBE_REQ_AFTER"] = "" if req_after is None else repr(req_after)
    out["PROBE_REQUESTS"] = str(delivered)
    unrated = max(unrated_after - unrated_before, 0.0)
    out["PROBE_UNRATED_ERRORS"] = repr(unrated)
    if unrated:
        out["PROBE_UNRATED_NOTE"] = (
            f"{unrated:g} error(s) landed on GeoServices surfaces that emit no request series "
            f"({request_metric} has no matching label set), so they are counted and named but not "
            "rated against a denominator that excludes their requests")
    print(f"closing: error={err_after} request={req_after} delivered={delivered}", file=sys.stderr)

    if transport_failures:
        # A candidate that cannot answer its own catalogue is not evidence of a healthy release, and
        # it is not a rate either: those requests were never counted by any instance. Block.
        return emit(f"{transport_failures} of {len(plan) + discovery_requests} probe requests never "
                    "reached the candidate - it is not reliably serving, and the window is incomplete")

    status, why = evaluate_process_continuity(before_witness, after_witness)
    out["PROBE_CONTINUITY"] = f"ok: {why}" if status == "pass" else why
    return emit()


if __name__ == "__main__":
    raise SystemExit(main())
