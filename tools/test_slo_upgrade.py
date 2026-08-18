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
from runner import harness  # noqa: E402


# ---- SLO -----------------------------------------------------------------------------------------
def test_slo_within_budget_passes():
    assert slo.evaluate_slo(5, 10000, 0.01)[0] == "pass"


def test_slo_over_budget_fails():
    status, why = slo.evaluate_slo(500, 10000, 0.01)   # 5% > 1%
    assert status == "fail" and "exceeds budget" in why


def test_slo_absent_denominator_is_blocked_not_pass():
    # No denominator => no candidate / nothing scrapeable / no in-scope traffic => never a green.
    assert slo.evaluate_slo(None, None, 0.01)[0] == "blocked"
    assert slo.evaluate_slo(5, None, 0.01)[0] == "blocked"
    assert slo.evaluate_slo(0, 0, 0.01)[0] == "blocked"      # no traffic


def test_slo_absent_error_counter_with_live_traffic_is_zero_errors_not_blocked():
    """A clean candidate exports no error counter at all — OTel omits a counter until its first
    measurement. Blocking on that made the gate unable to pass a healthy release, and made strict
    enforcement fail it outright."""
    status, why = slo.evaluate_slo(None, 10000, 0.01)
    assert status == "pass", why
    assert "zero errors" in why


def test_slo_absent_error_counter_still_blocks_without_a_denominator():
    # Absent numerator only means zero once the denominator proves the candidate is serving.
    assert slo.evaluate_slo(None, 0, 0.01)[0] == "blocked"


# ---- end-to-end: scrape + verdict, the fail-open regression ---------------------------------------
# A candidate up for 24h with liveness+readiness probes every 10s and a 15s Prometheus scrape, plus a
# 200-call GeoServices smoke suite in which 20 calls returned an error envelope: a real 10% error
# rate on the surface this gate exists to protect.
_CANDIDATE_EXPOSITION = (
    'honua_serving_request_duration_ms_count{honua_protocol="Health"} 17280 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="Monitoring"} 5760 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="FeatureServer"} 180 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="MapServer"} 20 1786929468470\n'
    'honua_geoservices_error_total{service_type="FeatureServer"} 20 1786929468470\n'
)
_GEOSERVICES_SELECTOR = (
    'honua_protocol=~"FeatureServer|MapServer|ImageServer|VectorTileServer|GPServer|NAServer|'
    'GeometryService|PrintingTools|StaticMap"'
)


def _slo_verdict(selector: str | None) -> str:
    errors = harness.parse_metric_total(_CANDIDATE_EXPOSITION, "honua_geoservices_error_total")
    requests = harness.parse_metric_total(
        _CANDIDATE_EXPOSITION,
        "honua_serving_request_duration_ms_count",
        harness.parse_label_selector(selector) if selector else None,
    )
    return slo.evaluate_slo(errors, requests, 0.01)[0]


def test_slo_gate_fails_a_ten_percent_geoservices_error_rate():
    """The hard requirement: this gate must not wave through a badly broken release."""
    assert _slo_verdict(_GEOSERVICES_SELECTOR) == "fail"


def test_slo_gate_would_be_fail_open_without_a_scoped_denominator():
    """Why the selector is mandatory rather than a nicety.

    A GeoServices-only numerator over an unscoped denominator is not merely imprecise — it inverts
    the gate's verdict on the exact scenario it exists to catch, which is worse than the no-signal
    state it replaced. Kept as a test so nobody 'simplifies' the selector away.
    """
    assert _slo_verdict(None) == "pass"


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
    assert slo.evaluate_gate(5, 10000, 0.01, instance_revision=_PINNED, pinned_sha=_PINNED,
                             revision_source="commit-sha")[0] == "pass"
    assert slo.evaluate_gate(500, 10000, 0.01, instance_revision=_PINNED, pinned_sha=_PINNED,
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
    status, why = slo.evaluate_gate(5, 10000, 0.01, instance_revision=_DEMO, pinned_sha=_PINNED,
                                    revision_source="commit-sha")
    assert status == "blocked", why
    assert _DEMO in why and _PINNED in why


def test_slo_identity_unreadable_revision_is_blocked_never_assumed_to_match():
    for absent in (None, "", "   "):
        status, why = slo.evaluate_candidate_identity(absent, _PINNED)
        assert status == "blocked", (absent, why)
        assert _PINNED in why
        # And it must never leak through as a pass on the composed verdict either.
        assert slo.evaluate_gate(None, 10000, 0.01, instance_revision=absent,
                                 pinned_sha=_PINNED)[0] == "blocked"


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
    status, why = slo.evaluate_gate(None, 10000, 0.01, instance_revision=revision,
                                    pinned_sha=_PINNED, revision_source=source)
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
