"""Unit tests for the seam-harness runner.

The harness is the thing that decides PASS/FAIL/BLOCKED for the cross-component seam tier, so its own
verdict logic and parsers must themselves be tested — an untested gate can silently stop gating
(AGENTS.md). These cover the pure, server-independent pieces:

  - report.assemble  — the mechanical verdict (incl. the require_real promotion of BLOCKED/SKIPPED)
  - parse_metric_total — Prometheus counter summing (the honua_geoservices_error_total scrape)
  - manifest loading + pin.is_real / coord — how the manifest drives "real vs placeholder"

Run: python -m pytest e2e/runner/test_runner.py    (or: python e2e/runner/test_runner.py)
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E_DIR))  # make `import runner.*` resolve like run.py does

from runner import harness  # noqa: E402
from runner.manifest import Manifest, ServerPin, SdkPin, load_manifest  # noqa: E402
from runner.report import Result, Status, assemble  # noqa: E402


# ---- report.assemble: the mechanical verdict ------------------------------------------------------
def _r(status: Status) -> Result:
    return Result(scenario=f"s-{status.value}", status=status)


def test_assemble_all_pass_is_pass():
    rep = assemble([_r(Status.PASS), _r(Status.PASS)], require_real=False)
    assert rep["status"] == "pass"
    assert rep["summary"]["pass"] == 2


def test_assemble_any_fail_is_fail():
    rep = assemble([_r(Status.PASS), _r(Status.FAIL)], require_real=False)
    assert rep["status"] == "fail"


def test_blocked_is_tolerated_without_require_real_but_fails_with_it():
    results = [_r(Status.PASS), _r(Status.BLOCKED)]
    assert assemble(results, require_real=False)["status"] == "pass"
    # The whole point of require_real: a BLOCKED (placeholder pin / unpublished image) becomes a red.
    assert assemble(results, require_real=True)["status"] == "fail"


def test_skipped_is_tolerated_without_require_real_but_fails_with_it():
    results = [_r(Status.PASS), _r(Status.SKIPPED)]
    assert assemble(results, require_real=False)["status"] == "pass"
    assert assemble(results, require_real=True)["status"] == "fail"


def test_assemble_summary_counts_every_status():
    results = [_r(Status.PASS), _r(Status.FAIL), _r(Status.BLOCKED), _r(Status.SKIPPED)]
    summ = assemble(results, require_real=False)["summary"]
    assert summ == {"pass": 1, "fail": 1, "skipped": 1, "blocked": 1}


# ---- parse_metric_total: the in-band error-metric scrape ------------------------------------------
def test_parse_metric_absent_is_none():
    body = "# HELP other_total help\nother_total 5\n"
    assert harness.parse_metric_total(body, "honua_geoservices_error_total") is None


def test_parse_metric_sums_label_sets_and_ignores_comments():
    body = (
        "# HELP honua_geoservices_error_total in-band GeoServices errors\n"
        "# TYPE honua_geoservices_error_total counter\n"
        'honua_geoservices_error_total{code="400"} 3\n'
        'honua_geoservices_error_total{code="500"} 4\n'
    )
    assert harness.parse_metric_total(body, "honua_geoservices_error_total") == 7.0


def test_parse_metric_unlabelled_and_scientific_notation():
    body = "honua_geoservices_error_total 1e2\n"
    assert harness.parse_metric_total(body, "honua_geoservices_error_total") == 100.0


def test_parse_metric_does_not_match_prefixed_name():
    # A different metric that merely shares a prefix must NOT be counted.
    body = "honua_geoservices_error_total_created 12345\n"
    assert harness.parse_metric_total(body, "honua_geoservices_error_total") is None


# ---- real honua-server exposition: trailing timestamps + histogram _count (honua-release#5) --------
# Verbatim shape emitted by honua-server's OpenTelemetry Prometheus exporter. Every sample carries a
# trailing millisecond timestamp; the pre-#5 parser anchored the value at end-of-line and therefore
# returned None for ALL of these — the SLO gate could not have read a single metric off a live
# candidate, and "unparseable" was indistinguishable from "metric absent".
REAL_EXPOSITION = (
    "# TYPE honua_geoservices_error_total counter\n"
    'honua_geoservices_error_total{otel_scope_name="Honua",service_type="FeatureServer"} 3 1786929468470\n'
    "# TYPE honua_serving_request_duration_ms histogram\n"
    'honua_serving_request_duration_ms_bucket{honua_protocol="FeatureServer",le="+Inf"} 4 1786929468470\n'
    'honua_serving_request_duration_ms_sum{honua_protocol="FeatureServer"} 42.5 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="FeatureServer"} 4 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="MapServer"} 6 1786929468470\n'
)


def test_parse_metric_reads_samples_with_a_trailing_timestamp():
    assert harness.parse_metric_total(REAL_EXPOSITION, "honua_geoservices_error_total") == 3.0


def test_parse_metric_totals_the_histogram_count_denominator():
    # honua_serving_request_duration_ms_count is the canonical SLO denominator: summed across its
    # protocol label sets it is the total number of classified serving-plane requests.
    assert harness.parse_metric_total(
        REAL_EXPOSITION, "honua_serving_request_duration_ms_count"
    ) == 10.0


def test_parse_metric_histogram_base_name_does_not_absorb_its_children():
    # Guards the denominator against double counting: the bare histogram name has no samples of its
    # own, so it must stay absent rather than silently summing _count + _sum + _bucket.
    assert harness.parse_metric_total(REAL_EXPOSITION, "honua_serving_request_duration_ms") is None


# ---- scope selectors: the denominator must cover the numerator's population --------------------
GEOSERVICES_SELECTOR = (
    'honua_protocol=~"FeatureServer|MapServer|ImageServer|VectorTileServer|GPServer|NAServer|'
    'GeometryService|PrintingTools|StaticMap"'
)

# A candidate that has been up for a day: probe traffic dwarfs the smoke suite.
MIXED_TRAFFIC = (
    'honua_serving_request_duration_ms_count{honua_protocol="Health"} 17280 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="Monitoring"} 5760 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="FeatureServer"} 180 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="MapServer"} 20 1786929468470\n'
    'honua_geoservices_error_total{service_type="FeatureServer"} 20 1786929468470\n'
)


def test_parse_metric_label_selector_scopes_the_denominator():
    filters = harness.parse_label_selector(GEOSERVICES_SELECTOR)
    assert harness.parse_metric_total(
        MIXED_TRAFFIC, "honua_serving_request_duration_ms_count", filters
    ) == 200.0


def test_unscoped_denominator_makes_a_ten_percent_error_rate_look_healthy():
    """The fail-open regression this selector exists to prevent.

    20 errors in 200 GeoServices calls is a 10% error rate. Divided by every label set — which
    includes a day of health probes and metric scrapes — it reads as 0.09% and passes a 1% budget
    with an order of magnitude to spare.
    """
    errors = harness.parse_metric_total(MIXED_TRAFFIC, "honua_geoservices_error_total")
    unscoped = harness.parse_metric_total(MIXED_TRAFFIC, "honua_serving_request_duration_ms_count")
    scoped = harness.parse_metric_total(
        MIXED_TRAFFIC,
        "honua_serving_request_duration_ms_count",
        harness.parse_label_selector(GEOSERVICES_SELECTOR),
    )
    assert errors / unscoped < 0.01      # would PASS a 1% budget
    assert errors / scoped == 0.10       # correctly BREACHES it


def test_parse_metric_returns_none_when_no_sample_is_in_scope():
    # "no in-scope traffic" must be indistinguishable from "absent" so the gate blocks rather than
    # dividing by zero.
    filters = harness.parse_label_selector('honua_protocol="OData"')
    assert harness.parse_metric_total(
        MIXED_TRAFFIC, "honua_serving_request_duration_ms_count", filters
    ) is None


def test_parse_label_selector_handles_equality_and_regex_terms():
    assert harness.parse_label_selector('a="x",b=~"y|z"') == {"a": "x", "b": "y|z"}
    # An equality value is escaped so regex metacharacters in a label value stay literal.
    assert harness.parse_label_selector('a="x.y"') == {"a": re.escape("x.y")}


def test_parse_metric_handles_special_float_literals():
    body = 'honua_x_total{a="b"} +Inf 123\nhonua_y_total 1.5\n'
    assert harness.parse_metric_total(body, "honua_y_total") == 1.5
    assert harness.parse_metric_total(body, "honua_x_total") == float("inf")


def test_scrape_metric_uses_admin_key(monkeypatch):
    seen: dict[str, str] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"honua_geoservices_error_total 2\n"

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        seen["key"] = request.get_header("X-api-key") or ""
        assert timeout == 5
        return Response()

    monkeypatch.setenv("HONUA_ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert harness.scrape_metric("http://server/metrics", "honua_geoservices_error_total") == 2.0
    assert seen["key"] == "test-admin-key"


# ---- manifest loading: what counts as a real (runnable) pin ---------------------------------------
def test_load_real_manifest_has_real_sdk_pins():
    m = load_manifest()  # the committed platform-manifest.yaml carries real SDK versions
    assert m.platform_release.startswith("2026.1")
    # js/python/dotnet versions are real semver in the Phase 0 snapshot -> is_real True.
    for short in ("js", "python", "dotnet"):
        assert m.sdks[short].is_real, f"{short} pin should be real in the committed manifest"
    assert m.sdks["js"].coord == "@honua-io/sdk-js"
    assert m.sdks["python"].coord == "honua-sdk"


def test_placeholder_pins_are_not_real():
    assert not ServerPin(version="TBD", image="TBD").is_real
    assert not ServerPin(version="1.0.0", image="ghcr.io/x:TBD").is_real  # TBD-tagged image
    assert ServerPin(version="1.0.0", image="ghcr.io/x:1.0.0").is_real
    assert not SdkPin(name="x", version="TBD", artifact="npm:x").is_real
    assert SdkPin(name="x", version="1.2.3", artifact="npm:@scope/x").coord == "@scope/x"


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
