"""Tests for the SLO and upgrade gate verdict logic.

Both gates have real, decidable cores even while their deploy/migration halves stay BLOCKED — and
both must be able to FAIL: an over-budget error rate, and an upgrade that strands an old client or
runs DB migrations backwards.

Run: python -m pytest tools/test_slo_upgrade.py    (or: python tools/test_slo_upgrade.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import check_slo as slo  # noqa: E402
import check_upgrade as up  # noqa: E402
import slo_probe as probe  # noqa: E402
from runner import harness  # noqa: E402


# ---- SLO: the window ------------------------------------------------------------------------------
# The gate measures a DELTA across a bounded probe window, not a raw counter reading. Everything below
# exists because reading the counters raw made the verdict a property of traffic timing: against an
# unchanged candidate on 2026-08-18 the gate said fail (17.6%), blocked, pass and skipped within one
# hour (honua-release#5). Every test name contains "slo" because gate-observability self-tests this
# module with `pytest -k slo`.
_BUDGET = 0.01
_WINDOW_OK = ("pass", "6 per-process counters non-decreasing across the window")


def test_slo_window_within_budget_passes():
    status, why = slo.evaluate_slo_window(10, 11, 5000, 6000, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "pass", why


def test_slo_window_fails_a_breaching_candidate():
    """THE HARD REQUIREMENT. This gate's whole history is people making it look green — it was
    structurally incapable of passing, then fail-open on an unscoped denominator. A candidate that
    breaches its budget inside the window must be RED, and no guard added for reproducibility may
    provide a way around that."""
    status, why = slo.evaluate_slo_window(0, 20, 0, 200, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "fail", why           # 20/200 = 10% against a 1% budget
    assert "exceeds budget" in why
    # ...and it is still red through the full gate, identity and all.
    status, why = slo.evaluate_gate(0, 20, 0, 200, 200, _BUDGET, continuity=_WINDOW_OK,
                                    instance_revision=_PINNED, pinned_sha=_PINNED,
                                    revision_source="commit-sha")
    assert status == "fail", why
    # One error over the smallest window the gate will rate is a breach too — the budget is not
    # rounded away at the boundary.
    assert slo.evaluate_slo_window(0, 1, 0, 100, 100, _BUDGET, continuity=_WINDOW_OK)[0] == "pass"
    assert slo.evaluate_slo_window(0, 2, 0, 100, 100, _BUDGET, continuity=_WINDOW_OK)[0] == "fail"


def test_slo_window_verdict_does_not_move_with_counter_HISTORY():
    """The defect this change exists for, stated as a test.

    Same candidate, same window behaviour, three different histories: a container carrying the twelve
    errors that made the gate say 17.6%, a container that has just recycled and exports nothing at
    all, and a long-lived one with a large clean history. The verdict must be identical, because the
    release is identical. Read raw, those three returned fail, blocked and pass.
    """
    poisoned = slo.evaluate_slo_window(12, 12, 68, 268, 200, _BUDGET, continuity=_WINDOW_OK)
    recycled = slo.evaluate_slo_window(None, None, None, 200, 200, _BUDGET, continuity=_WINDOW_OK)
    long_lived = slo.evaluate_slo_window(4, 4, 900000, 900200, 200, _BUDGET, continuity=_WINDOW_OK)
    assert poisoned[0] == recycled[0] == long_lived[0] == "pass", (poisoned, recycled, long_lived)
    # And the SAME three histories all fail when the window itself is bad.
    for before_e, after_e, before_r, after_r in ((12, 32, 68, 268), (None, 20, None, 200),
                                                 (4, 24, 900000, 900200)):
        assert slo.evaluate_slo_window(before_e, after_e, before_r, after_r, 200, _BUDGET,
                                       continuity=_WINDOW_OK)[0] == "fail"


def test_slo_window_negative_delta_is_blocked_never_clamped():
    """Two scrapes that hit different Lambda containers produce a counter that went backwards.
    Clamping it to zero would silently turn "I measured two unrelated populations" into "no errors"."""
    status, why = slo.evaluate_slo_window(50, 3, 5000, 200, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "blocked", why
    assert "BACKWARDS" in why
    # A negative NUMERATOR delta blocks too, even though it would arithmetically flatter the gate.
    status, why = slo.evaluate_slo_window(50, 3, 0, 500, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "blocked" and "numerator" in why, why


def test_slo_window_vanished_series_is_blocked():
    # A cumulative counter cannot un-exist on a live process; if it did, the process changed.
    status, why = slo.evaluate_slo_window(5, None, 900, 1200, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "blocked" and "un-exist" in why, why


def test_slo_window_absent_before_and_after_is_zero_errors_not_blocked():
    # OTel exports no counter before its first measurement, so absent/absent across a window with a
    # live denominator is genuinely zero errors — the clean-candidate case must be able to pass.
    status, why = slo.evaluate_slo_window(None, None, 0, 200, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "pass", why


def test_slo_window_unattributed_probe_is_blocked():
    """The fleet-sampling hazard, handled rather than assumed away: if the scraped instance did not
    observe the whole burst, the burst was spread across containers and the delta is a fraction of an
    unknown population."""
    status, why = slo.evaluate_slo_window(0, 0, 1000, 1030, 200, _BUDGET, continuity=_WINDOW_OK)
    assert status == "blocked", why
    assert "attribution incomplete" in why
    # No probe traffic at all is blocked as well — that is the empty-denominator state.
    assert slo.evaluate_slo_window(0, 0, 1000, 1000, 0, _BUDGET, continuity=_WINDOW_OK)[0] == "blocked"


def test_slo_window_too_small_to_resolve_the_budget_is_blocked():
    """A five-request window can only read 0% or 20% against a 1% budget, so a `pass` there means
    "no errors happened to land in five requests" — not evidence about a release."""
    status, why = slo.evaluate_slo_window(0, 0, 0, 5, 5, _BUDGET, continuity=_WINDOW_OK)
    assert status == "blocked" and "too small to resolve" in why, why
    assert slo.minimum_resolvable_window(0.01) == 100
    assert slo.minimum_resolvable_window(0.05) == 20


def test_slo_window_without_process_continuity_is_blocked():
    # Perfect-looking numbers, but the two scrapes came from different containers.
    status, why = slo.evaluate_slo_window(
        0, 0, 0, 500, 200, _BUDGET,
        continuity=("blocked", "per-process counters went backwards: process_cpu_time_seconds_total 9 -> 1"))
    assert status == "blocked", why
    assert "not a rate" in why
    # And an unchecked window never defaults to trusted.
    assert slo.parse_continuity("")[0] == "blocked"
    assert slo.parse_continuity("ok: 6 counters non-decreasing")[0] == "pass"
    assert slo.parse_continuity("scrape failed")[0] == "blocked"


def test_slo_continuity_witness_catches_a_recycled_container():
    before = {"process_cpu_time_seconds_total": 7.4, "dotnet_gc_collections_total": 31.0,
              "dotnet_exceptions_total": 111.0, "aspnetcore_routing_match_attempts_total": 12.0}
    assert slo.evaluate_process_continuity(before, dict(before))[0] == "pass"
    grown = {k: v + 1 for k, v in before.items()}
    assert slo.evaluate_process_continuity(before, grown)[0] == "pass"
    # A fresh container resets every per-process counter.
    fresh = {k: 0.0 for k in before}
    status, why = slo.evaluate_process_continuity(before, fresh)
    assert status == "blocked" and "DIFFERENT instances" in why, why
    # A witness that disappears entirely is a regression too.
    assert slo.evaluate_process_continuity(before, {"process_cpu_time_seconds_total": None,
                                                    **{k: v for k, v in grown.items()
                                                       if k != "process_cpu_time_seconds_total"}})[0] == "blocked"
    # Too few comparable witnesses to tell => blocked, not credited.
    assert slo.evaluate_process_continuity({"dotnet_exceptions_total": 1.0},
                                           {"dotnet_exceptions_total": 2.0})[0] == "blocked"


def test_slo_counter_delta_distinguishes_absent_from_zero():
    assert slo.counter_delta(None, None) == (0.0, None)
    assert slo.counter_delta(None, 7.0) == (7.0, None)
    assert slo.counter_delta(3.0, 9.0) == (6.0, None)
    delta, why = slo.counter_delta(9.0, 3.0)
    assert delta is None and "BACKWARDS" in why
    delta, why = slo.counter_delta(9.0, None)
    assert delta is None and "un-exist" in why


# ---- end-to-end: real demo.honua.io expositions, scrape -> window -> verdict -----------------------
# Captured live from the pinned candidate 6b6d3b89 on 2026-08-19, trimmed to the series the gate
# reads. The `before` sample carries the poisoned history the raw-counter gate reported as 17.6%.
_EXPO_BEFORE = (
    'honua_serving_request_duration_ms_count{honua_operation="metadata",honua_protocol="FeatureServer",status_class="2xx"} 40 1787117798204\n'
    'honua_serving_request_duration_ms_count{honua_operation="metadata",honua_protocol="MapServer",status_class="2xx"} 14 1787117798204\n'
    'honua_serving_request_duration_ms_count{honua_operation="query",honua_protocol="FeatureServer",status_class="2xx"} 14 1787117798204\n'
    'honua_serving_request_duration_ms_count{honua_protocol="Health"} 17280 1787117798204\n'
    'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 6 1787117798204\n'
    'honua_geoservices_error_total{error_code="401",operation="services",service_type="GeoServices"} 6 1787117798204\n'
    'process_cpu_time_seconds_total{process_cpu_state="user"} 7.42 1787117798204\n'
    'dotnet_gc_heap_total_allocated_bytes_total 250679392 1787117798204\n'
    'dotnet_gc_collections_total{gc_heap_generation="gen0"} 31 1787117798204\n'
    'dotnet_exceptions_total 111 1787117798204\n'
    'dotnet_thread_pool_work_item_count_total 9004 1787117798204\n'
    'aspnetcore_routing_match_attempts_total{aspnetcore_routing_match_status="success"} 12 1787117798204\n'
)
# 200 clean probe reads later, plus three ambient unauthenticated 401s on the catalog surface.
_EXPO_AFTER = (
    'honua_serving_request_duration_ms_count{honua_operation="metadata",honua_protocol="FeatureServer",status_class="2xx"} 128 1787117858204\n'
    'honua_serving_request_duration_ms_count{honua_operation="metadata",honua_protocol="MapServer",status_class="2xx"} 80 1787117858204\n'
    'honua_serving_request_duration_ms_count{honua_operation="query",honua_protocol="FeatureServer",status_class="2xx"} 60 1787117858204\n'
    'honua_serving_request_duration_ms_count{honua_protocol="Health"} 17284 1787117858204\n'
    'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 6 1787117858204\n'
    'honua_geoservices_error_total{error_code="401",operation="services",service_type="GeoServices"} 9 1787117858204\n'
    'process_cpu_time_seconds_total{process_cpu_state="user"} 7.80 1787117858204\n'
    'dotnet_gc_heap_total_allocated_bytes_total 262109120 1787117858204\n'
    'dotnet_gc_collections_total{gc_heap_generation="gen0"} 33 1787117858204\n'
    'dotnet_exceptions_total 118 1787117858204\n'
    'dotnet_thread_pool_work_item_count_total 9310 1787117858204\n'
    'aspnetcore_routing_match_attempts_total{aspnetcore_routing_match_status="success"} 14 1787117858204\n'
)
_GEOSERVICES_SELECTOR = (
    'honua_protocol=~"FeatureServer|MapServer|ImageServer|VectorTileServer|GPServer|NAServer|'
    'GeometryService|PrintingTools|StaticMap"'
)


def _window_from(before_body: str, after_body: str, probe_requests: int, selector: str | None):
    """Exactly what tools/slo_probe.py does, on captured bytes: scrape -> totals -> verdict inputs."""
    request_filters = harness.parse_label_selector(selector) if selector else None
    error_filters = probe.numerator_filters(selector) if selector else {}

    def totals(body):
        errors = harness.parse_metric_total(body, "honua_geoservices_error_total", error_filters or None)
        every = harness.parse_metric_total(body, "honua_geoservices_error_total")
        requests = harness.parse_metric_total(body, "honua_serving_request_duration_ms_count",
                                              request_filters)
        return errors, requests, (every or 0.0) - (errors or 0.0)

    err_b, req_b, unrated_b = totals(before_body)
    err_a, req_a, unrated_a = totals(after_body)
    continuity = slo.evaluate_process_continuity(probe.witness_totals(before_body),
                                                 probe.witness_totals(after_body))
    return slo.evaluate_slo_window(err_b, err_a, req_b, req_a, probe_requests, _BUDGET,
                                   continuity=continuity,
                                   unrated_errors=max(unrated_a - unrated_b, 0.0))


def test_slo_end_to_end_real_exposition_passes_a_clean_window_despite_a_poisoned_history():
    status, why = _window_from(_EXPO_BEFORE, _EXPO_AFTER, 200, _GEOSERVICES_SELECTOR)
    assert status == "pass", why
    # The six historical FeatureServer 404s that produced the 17.6% reading are outside the window.
    assert "0/200" in why
    # The three ambient catalog 401s are on a surface the denominator emits no request series for.
    # They are named, not rated, and not silently discarded.
    assert "3 error(s) on GeoServices surfaces" in why


def test_slo_end_to_end_real_exposition_fails_when_the_window_itself_breaches():
    """Same bytes, same guards, but the candidate emits in-band FeatureServer errors during the
    window — the HTTP-200-with-{error} class this gate exists to catch (server#2243). RED."""
    breaching = _EXPO_AFTER.replace(
        'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 6',
        'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 26')
    status, why = _window_from(_EXPO_BEFORE, breaching, 200, _GEOSERVICES_SELECTOR)
    assert status == "fail", why
    assert "20/200" in why


def test_slo_end_to_end_scope_symmetry_is_what_makes_the_verdict_reproducible():
    """Numerator and denominator must cover ONE population, and this is the case that proves it.

    The bytes above are a clean candidate: 200 probe reads, zero error envelopes on any surface the
    denominator counts. What also happened during that minute is three unauthenticated 401s from
    somebody port-scanning the public demo, recorded as service_type="GeoServices" — a catalog surface
    that emits NO honua_serving_request_duration_ms_count series at all, so its requests are in no
    denominator anywhere.

    Rated asymmetrically those three strangers fail a healthy release at 1.47%, and whether the gate
    is red depends on who scanned demo.honua.io while it ran. Held to the denominator's population the
    verdict is a property of the candidate, and the three errors are still counted and named in the
    text rather than dropped.
    """
    scoped = _window_from(_EXPO_BEFORE, _EXPO_AFTER, 200, _GEOSERVICES_SELECTOR)
    assert scoped[0] == "pass", scoped
    assert "0/200" in scoped[1] and "3 error(s) on GeoServices surfaces" in scoped[1]

    unscoped = _window_from(_EXPO_BEFORE, _EXPO_AFTER, 200, None)
    assert unscoped[0] == "fail" and "3/204" in unscoped[1], unscoped

    # The narrowing only ever drops surfaces the denominator cannot count. An in-band FeatureServer
    # error — the HTTP-200-with-{error} class this gate was built for — still fails, scoped or not.
    breaching = _EXPO_AFTER.replace(
        'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 6',
        'honua_geoservices_error_total{error_code="404",operation="query",service_type="FeatureServer"} 26')
    assert _window_from(_EXPO_BEFORE, breaching, 200, _GEOSERVICES_SELECTOR)[0] == "fail"

    # And a numerator that cannot be held to the denominator's population is refused outright rather
    # than rated: slo_probe emits no window at all, so check_slo blocks on the missing continuity.
    assert probe.numerator_filters("") == {}
    assert slo.parse_continuity("")[0] == "blocked"


# ---- the probe plan: deterministic, in-scope, and never deliberately wrong ------------------------
_CATALOG = {"folders": [], "services": [
    {"name": "maui-roads", "type": "FeatureServer", "url": "https://demo.honua.io/rest/services/maui-roads/FeatureServer"},
    {"name": "maui-roads", "type": "MapServer", "url": "https://demo.honua.io/rest/services/maui-roads/MapServer"},
    {"name": "maui-roads", "type": "GPServer", "url": "https://demo.honua.io/rest/services/maui-roads/GPServer"},
    {"name": "maui buildings", "type": "FeatureServer", "url": "..."},
]}


def test_slo_probe_plan_is_deterministic_and_in_scope():
    services = probe.catalog_services(_CATALOG)
    assert services == [("maui buildings", "FeatureServer"), ("maui-roads", "FeatureServer"),
                        ("maui-roads", "MapServer")]
    layers = {("maui-roads", "FeatureServer"): [3], ("maui-roads", "MapServer"): [3],
              ("maui buildings", "FeatureServer"): [13]}
    plan = probe.build_plan(services, layers, 12)
    assert len(plan) == 12
    # Same inputs, same plan: two consecutive gate runs must drive the same population or the
    # reproducibility this whole change is for does not hold.
    assert plan == probe.build_plan(services, layers, 12)
    # Service names are URL-encoded, and layer ids come from the service's own metadata rather than
    # being guessed — guessing layer `0` is what produced six of the twelve errors in the 17.6%
    # reading, i.e. the gate measuring its own mistake.
    assert "/rest/services/maui%20buildings/FeatureServer/13?f=json" in plan
    assert not any("/0?f=json" in path for path in plan)
    assert all(path.startswith("/rest/services/") for path in plan)
    assert all("f=json" in path for path in plan)
    # Only the families the denominator counts are driven.
    assert not any("GPServer" in path for path in plan)


def test_slo_probe_plan_is_empty_when_there_is_nothing_in_scope():
    assert probe.catalog_services({"services": []}) == []
    assert probe.catalog_services("<html>401</html>") == []
    assert probe.build_plan([], {}, 200) == []
    assert probe.build_plan([("a", "FeatureServer")], {}, 0) == []


def test_slo_probe_reads_layer_ids_from_service_metadata():
    assert probe.service_layer_ids({"layers": [{"id": 13, "name": "x"}, {"id": 4}]}) == [4, 13]
    assert probe.service_layer_ids({"layers": []}) == []
    assert probe.service_layer_ids({"error": {"code": 401}}) == []
    assert probe.service_layer_ids(None) == []


def test_slo_probe_numerator_covers_the_denominator_population():
    filters = probe.numerator_filters(_GEOSERVICES_SELECTOR)
    assert filters == {"service_type": ("FeatureServer|MapServer|ImageServer|VectorTileServer|"
                                        "GPServer|NAServer|GeometryService|PrintingTools|StaticMap")}
    # A selector with no protocol matcher yields no numerator filter, and slo_probe blocks rather
    # than rating an asymmetric ratio.
    assert probe.numerator_filters("") == {}


# ---- candidate identity binding (honua-release#5) --------------------------------------------------
# An error budget is only the CANDIDATE's if it was scraped from the candidate. Honua's one
# long-lived environment (demo.honua.io) doubles as demo and certification target and routinely runs
# an older build than the manifest pin — so a gate that scrapes without checking identity reports a
# real, correct-looking number about the wrong population. Every test name here contains "slo"
# because gate-observability self-tests this module with `pytest -k slo`.
_PINNED = "6b6d3b898f4abb6b34833d953b50d44f3d38c6c1"      # platform-manifest.yaml honua-server.sha
_DEMO = "6ad71ac701ca709ec671afd09257217e8d17a149"        # what demo.honua.io actually served (2026-08-18)

# Verbatim shapes of the two public responses that carry the identity, both captured live.
_MANIFEST_DOC = {
    "schemaVersion": "1.0.0",
    "server": {
        "serverVersion": "1.0.0",
        "deploymentEnvironment": "Production",
        "deploymentRevision": _DEMO,
        "deploymentRevisionSource": "commit-sha",
    },
}
_STREAMING_DOC = {
    "success": True,
    "data": {"enabled": True, "deploymentRevision": _DEMO, "deploymentRevisionSource": "commit-sha"},
}


def test_slo_identity_matching_revision_proceeds_to_the_budget():
    status, why = slo.evaluate_candidate_identity(_PINNED, _PINNED, "commit-sha")
    assert status == "pass", why
    # ...and the gate then evaluates the error budget normally, in both directions.
    assert slo.evaluate_gate(0, 5, 0, 10000, 200, 0.01, continuity=_WINDOW_OK,
                             instance_revision=_PINNED, pinned_sha=_PINNED,
                             revision_source="commit-sha")[0] == "pass"
    assert slo.evaluate_gate(0, 500, 0, 10000, 200, 0.01, continuity=_WINDOW_OK,
                             instance_revision=_PINNED, pinned_sha=_PINNED,
                             revision_source="commit-sha")[0] == "fail"


def test_slo_identity_abbreviated_revision_still_matches_the_pin():
    status, why = slo.evaluate_candidate_identity(_PINNED[:7], _PINNED, "commit-sha")
    assert status == "pass", why


def test_slo_identity_mismatched_revision_is_blocked_and_names_both_shas():
    """The demo-vs-pin case this binding exists for."""
    status, why = slo.evaluate_candidate_identity(_DEMO, _PINNED, "commit-sha")
    assert status == "blocked"
    assert _DEMO in why and _PINNED in why


def test_slo_gate_cannot_pass_off_a_mismatched_instance():
    # Numbers that would otherwise be a comfortable green.
    status, why = slo.evaluate_gate(0, 5, 0, 10000, 200, 0.01, continuity=_WINDOW_OK,
                                    instance_revision=_DEMO, pinned_sha=_PINNED,
                                    revision_source="commit-sha")
    assert status == "blocked", why
    assert _DEMO in why and _PINNED in why


def test_slo_identity_unreadable_revision_is_blocked_never_assumed_to_match():
    for absent in (None, "", "   "):
        status, why = slo.evaluate_candidate_identity(absent, _PINNED)
        assert status == "blocked", (absent, why)
        assert _PINNED in why
        # And it must never leak through as a pass on the composed verdict either.
        assert slo.evaluate_gate(None, None, 0, 10000, 200, 0.01, continuity=_WINDOW_OK,
                                 instance_revision=absent, pinned_sha=_PINNED)[0] == "blocked"


def test_slo_identity_unreadable_pin_is_blocked():
    # An unreadable platform-manifest.yaml must not degrade into "identity confirmed".
    assert slo.evaluate_candidate_identity(_PINNED, None)[0] == "blocked"
    assert slo.evaluate_candidate_identity(_PINNED, "not-a-sha")[0] == "blocked"


def test_slo_identity_non_commit_revision_source_is_blocked():
    # A build number / image tag / chart version is not comparable to a commit sha.
    status, why = slo.evaluate_candidate_identity("12345", _PINNED, "build-number")
    assert status == "blocked" and "build-number" in why
    # Even a value that happens to be hex is refused when the source says it isn't a commit.
    assert slo.evaluate_candidate_identity(_PINNED, _PINNED, "image-tag")[0] == "blocked"
    # A non-hex revision with no declared source is refused too.
    assert slo.evaluate_candidate_identity("v2026.1-rc.1", _PINNED)[0] == "blocked"


def test_slo_identity_reads_the_real_capability_manifest_shape():
    assert slo.read_instance_revision(_MANIFEST_DOC) == (_DEMO, "commit-sha")
    assert slo.read_instance_revision(_STREAMING_DOC) == (_DEMO, "commit-sha")
    # Absent / wrong-shaped documents yield no revision, which blocks rather than defaulting.
    assert slo.read_instance_revision({"server": {"serverVersion": "1.0.0"}}) == (None, None)
    assert slo.read_instance_revision({"success": False}) == (None, None)
    assert slo.read_instance_revision("<html>401</html>") == (None, None)


def test_slo_identity_url_is_the_same_origin_as_the_scrape():
    # The identity endpoint is DERIVED from HONUA_METRICS_URL, so it cannot point at another host.
    assert slo.capability_manifest_url("https://demo.honua.io/metrics") == \
        "https://demo.honua.io/api/v1/capabilities/manifest"
    assert slo.capability_manifest_url("http://localhost:8080/metrics?foo=1") == \
        "http://localhost:8080/api/v1/capabilities/manifest"
    # Nothing configured / not an http(s) URL -> no identity source -> the gate blocks.
    for bad in (None, "", "   ", "demo.honua.io/metrics", "file:///etc/passwd"):
        assert slo.capability_manifest_url(bad) is None, bad


def test_slo_identity_end_to_end_from_the_live_demo_response_blocks():
    """Full path: real capability-manifest bytes -> revision -> verdict, on today's demo."""
    revision, source = slo.read_instance_revision(_MANIFEST_DOC)
    status, why = slo.evaluate_gate(None, None, 0, 10000, 200, 0.01, continuity=_WINDOW_OK,
                                    instance_revision=revision, pinned_sha=_PINNED,
                                    revision_source=source)
    assert status == "blocked", why
    assert "NOT the pinned candidate" in why


# ---- upgrade -------------------------------------------------------------------------------------
def _manifest(server_db, clients):
    comps = {"honua-server": {"version": "pre-release", "sha": "a" * 40, "dbSchema": server_db}}
    for name, ver in clients.items():
        comps[name] = {"version": ver, "sha": "b" * 40, "artifact": f"npm:{name}"}
    return {"components": comps}


def _matrix(contract, ranges):
    return {"contracts": {contract: {"version": "v1", "clients": ranges}}}


def test_upgrade_old_client_still_supported_passes():
    prior = _manifest("metadata-v1", {"honua-sdk-js": "0.0.14-alpha.0"})
    cand = _manifest("metadata-v1", {"honua-sdk-js": "0.0.20"})
    matrix = _matrix("geoservices", {"honua-sdk-js": ">=0.0.10 <0.1.0"})
    rows, overall = up.evaluate_upgrade(prior, cand, matrix)
    assert overall == "pass"


def test_upgrade_strands_old_client_fails():
    prior = _manifest("metadata-v1", {"honua-sdk-js": "0.0.14-alpha.0"})
    cand = _manifest("metadata-v1", {"honua-sdk-js": "0.1.0"})
    # candidate matrix dropped support for the prior's 0.0.14 client.
    matrix = _matrix("geoservices", {"honua-sdk-js": ">=0.1.0 <0.2.0"})
    rows, overall = up.evaluate_upgrade(prior, cand, matrix)
    assert overall == "fail" and any("strands" in r["why"] for r in rows)


def test_upgrade_db_schema_backwards_fails():
    prior = _manifest("metadata-v3", {})
    cand = _manifest("metadata-v2", {})        # migrations went backwards
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "fail" and any("backwards" in r["why"] for r in rows)


def test_upgrade_db_schema_forward_passes():
    prior = _manifest("metadata-v2", {})
    cand = _manifest("metadata-v3", {})
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "pass"


def test_upgrade_nothing_comparable_is_blocked():
    # All sha-pinned, no db schema -> nothing to decide from manifests alone.
    prior = {"components": {"honua-server": {"version": "pre-release", "sha": "a" * 40}}}
    cand = {"components": {"honua-server": {"version": "pre-release", "sha": "c" * 40}}}
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "blocked" and rows == []


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
