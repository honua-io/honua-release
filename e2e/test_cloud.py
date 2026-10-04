"""Tests for the cross-cloud parity tier.

The cloud gate must (a) compare targets correctly, (b) classify each canonical check correctly, and
(c) report BLOCKED — never a fake green — when the AWS infra isn't wired. All proven here with no
cloud, no terraform, no live server (injected fetchers + an unset environment).

Run: python -m pytest e2e/test_cloud.py    (or: python e2e/test_cloud.py)
"""
from __future__ import annotations

import contextlib
import json
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canonical_checks as cc  # noqa: E402
import parity as par  # noqa: E402
import run_cloud  # noqa: E402
from runner import cloud as cloud_driver  # noqa: E402
import demo_canary  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.base import ProvisionError  # noqa: E402
from targets.terraform_target import ecs, serverless  # noqa: E402

# The parity job runs this self-test with the live run's GITHUB_RUN_ID before the live cell, and
# uploads e2e/cloud-evidence/**/receipt-*.json. Fixture receipts must never land in that tree.
SELFTEST_EVIDENCE = E2E_DIR / ".cloud-evidence-selftest" / str(os.getpid())
run_cloud.cloud_journey.EVIDENCE = SELFTEST_EVIDENCE
atexit.register(shutil.rmtree, SELFTEST_EVIDENCE.parent, True)

_AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_ROLE_ARN", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "HONUA_LAMBDA_IMAGE_URI", "HONUA_ECS_IMAGE", "HONUA_IAC_DIR", "HONUA_HELM_DIR",
            "HONUA_AWS_DB_INGRESS_CIDR", "HONUA_LAMBDA_ARCHITECTURE", "HONUA_ECS_ARCHITECTURE",
            "HONUA_AWS_RUNNER_CIDR")

TEST_SERVER_SHA = "a" * 40


def _expected_ga(ids, excluded=None, revision=TEST_SERVER_SHA):
    expected = {"expectedGa": ids, "excluded": excluded or [], "sourceSnapshot": {}}
    if revision is not None:
        expected["sourceSnapshot"]["deploymentRevision"] = revision
    return expected


# ---- canonical checks: result normalisation -------------------------------------------------------
def _fetcher(routes):
    """routes: list of (url_substr, HttpResponse). First match wins; default = unreachable."""
    def fetch(url):
        for sub, resp in routes:
            if sub in url:
                return resp
        return cc.HttpResponse(0, "no route")
    return fetch


def test_health_pass_fail_unreachable():
    assert cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(200, "ok"))])).status == "pass"
    assert cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(503, "down"))])).status == "fail"
    unreached = cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(0, "conn refused"))]))
    assert unreached.status == "fail" and cc.is_endpoint_unreachable(unreached)


def test_health_falls_back_to_live_ready_on_404():
    # Plain /healthz is Development-only (Honua.ServiceDefaults.MapDefaultEndpoints); a Production/
    # Staging deploy (any real cloud cell, or https://demo.honua.io) 404s there by design — the
    # always-registered /healthz/live + /healthz/ready pair must be checked as a fallback (2026-07-21
    # live-canary finding, honua-release#61). More-specific routes are listed first — "/healthz" is a
    # substring of "/healthz/live"/"/healthz/ready" so it must be checked last.
    ok = _fetcher([
        ("/healthz/live", cc.HttpResponse(200, "")),
        ("/healthz/ready", cc.HttpResponse(200, "")),
        ("/healthz", cc.HttpResponse(404, "")),
    ])
    r = cc.check_health("http://x", ok)
    assert r.status == "pass" and "404" in r.why

    bad = _fetcher([
        ("/healthz/live", cc.HttpResponse(200, "")),
        ("/healthz/ready", cc.HttpResponse(503, "")),
        ("/healthz", cc.HttpResponse(404, "")),
    ])
    assert cc.check_health("http://x", bad).status == "fail"

    def unreachable_fallback(url):
        # Exact-match fetch (not the substring _fetcher) so /healthz -> 404 but /healthz/live and
        # /healthz/ready are genuinely unreachable (status 0), distinct from the 404 case above.
        if url == "http://x/healthz":
            return cc.HttpResponse(404, "")
        return cc.HttpResponse(0, "conn refused")

    assert cc.check_health("http://x", unreachable_fallback).status == "fail"


def test_geoservices_error_envelope_detection():
    env = cc.HttpResponse(200, '{"error":{"code":400,"message":"Invalid where"}}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", env)])).status == "pass"
    # A 200 that is NOT an error envelope (e.g. an empty featureset) means the convention isn't surfaced.
    ok = cc.HttpResponse(200, '{"features":[]}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", ok)])).status == "fail"
    # bool code must NOT be treated as an envelope (mirrors the SDK guards).
    boolcode = cc.HttpResponse(200, '{"error":{"code":true}}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", boolcode)])).status == "fail"
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([])).status == "fail"


def test_service_catalog():
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(200, '{"services":[]}'))])).status == "pass"
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(200, "not json"))])).status == "fail"
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(500, ""))])).status == "fail"


def test_admin_capabilities():
    ok = cc.HttpResponse(200, '{"contractVersions":{"admin":"v1"}}')
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", ok)])).status == "pass"
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", cc.HttpResponse(200, "no"))])).status == "fail"
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", cc.HttpResponse(404, ""))])).status == "fail"
    assert cc.check_admin_capabilities("http://x", _fetcher([])).status == "fail"


def test_geoprocessing_catalog():
    gp = cc.HttpResponse(200, '{"services":[{"name":"Buffer","type":"GPServer"}]}')
    assert cc.check_geoprocessing("http://x", _fetcher([("/rest/services", gp)])).status == "pass"
    # catalog reachable but no GP advertised => blocked (honest), never a fake pass.
    nogp = cc.HttpResponse(200, '{"services":[{"name":"roads","type":"FeatureServer"}]}')
    assert cc.check_geoprocessing("http://x", _fetcher([("/rest/services", nogp)])).status == "blocked"
    assert cc.check_geoprocessing("http://x", _fetcher([])).status == "fail"


def test_capability_manifest_pass_unauthenticated():
    expected = _expected_ga(["a.one", "a.two"], [{"id": "b.gated", "reason": "gated"}])
    body = json.dumps({
        "schemaVersion": "honua.capability_manifest.v1",
        "capabilities": [
            {"id": "a.one", "supported": True, "available": True},
            {"id": "a.two", "supported": True, "available": False},
            {"id": "b.gated", "supported": True, "available": False},
        ],
    })
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected=expected, frozen_server_sha=TEST_SERVER_SHA)
    assert r.status == "pass"
    assert r.evidence["expectedGaCount"] == 2
    assert r.evidence["availableCountUnauthenticated"] == 1


def test_capability_manifest_revision_match_passes():
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    r = cc.check_capability_manifest(
        "http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
        expected=_expected_ga([]), frozen_server_sha=TEST_SERVER_SHA, enforcement="strict")
    assert r.status == "pass"


def test_demo_canary_binds_revision_advertised_by_live_deployment():
    body = json.dumps({"server": {"deploymentRevision": TEST_SERVER_SHA}})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))])
    result, revision = demo_canary._live_deployment_revision("http://x", fetch, TEST_SERVER_SHA)
    assert result.status == "pass"
    assert revision == TEST_SERVER_SHA


def test_demo_canary_refuses_missing_or_stale_live_revision():
    missing = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, "{}"))])
    result, revision = demo_canary._live_deployment_revision("http://x", missing, TEST_SERVER_SHA)
    assert result.status == "fail" and revision == ""

    stale_sha = "b" * 40
    stale_body = json.dumps({"server": {"deploymentRevision": stale_sha}})
    stale = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, stale_body))])
    result, revision = demo_canary._live_deployment_revision("http://x", stale, TEST_SERVER_SHA)
    assert result.status == "fail" and revision == stale_sha


def test_capability_manifest_stale_revision_fails_closed_by_default():
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))])
    expected = _expected_ga([], revision="b" * 40)

    result = cc.check_capability_manifest(
        "http://x", fetch, expected=expected, frozen_server_sha=TEST_SERVER_SHA)

    assert result.status == "fail"
    assert "b" * 40 in result.why
    assert TEST_SERVER_SHA in result.why
    assert "honua-release#183" in result.why


def test_capability_manifest_missing_snapshot_revision_fails_closed_in_all_modes():
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))])

    strict = cc.check_capability_manifest(
        "http://x", fetch, expected=_expected_ga([], revision=None),
        frozen_server_sha=TEST_SERVER_SHA, enforcement="strict")
    bootstrap = cc.check_capability_manifest(
        "http://x", fetch, expected=_expected_ga([], revision=None),
        frozen_server_sha=TEST_SERVER_SHA, enforcement="bootstrap")

    assert strict.status == "fail"
    assert bootstrap.status == "fail"
    assert "sourceSnapshot.deploymentRevision=None" in strict.why


def test_capability_manifest_fail_on_missing_or_unsupported_id():
    expected = _expected_ga(["a.one", "a.missing"])
    body = json.dumps({
        "schemaVersion": "honua.capability_manifest.v1",
        "capabilities": [{"id": "a.one", "supported": False, "available": False}],
    })
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected=expected, frozen_server_sha=TEST_SERVER_SHA)
    assert r.status == "fail"
    assert "a.missing" in r.why


def test_capability_manifest_fail_on_wrong_schema_version():
    body = json.dumps({"schemaVersion": "wrong.v0", "capabilities": []})
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected={"expectedGa": [], "excluded": []})
    assert r.status == "fail" and "schemaVersion" in r.why


def test_capability_manifest_fails_when_unreachable():
    r = cc.check_capability_manifest("http://x", _fetcher([]))
    assert r.status == "fail" and cc.is_endpoint_unreachable(r)


def test_load_expected_ga_returns_none_for_missing_or_malformed_file():
    import tempfile
    assert cc.load_expected_ga("/nonexistent/path.json") is None
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert cc.load_expected_ga(bad) is None
        wrong_shape = Path(d) / "wrong.json"
        wrong_shape.write_text(json.dumps({"noExpectedGaKey": []}), encoding="utf-8")
        assert cc.load_expected_ga(wrong_shape) is None


def test_committed_expected_ga_manifest_loads_and_is_well_formed():
    data = cc.load_expected_ga()
    assert data is not None, "e2e/expected-ga-manifest.json must exist and be well-formed"
    assert data["expectedGa"], "expectedGa must be non-empty"
    excluded_ids = {e["id"] for e in data.get("excluded", [])}
    assert {"security.mtls", "alerts.geofence"} <= excluded_ids


def test_capability_manifest_blocked_when_expected_ga_file_missing(monkeypatch):
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    # Force the default-lookup branch (expected=None) to miss, simulating an absent/unfetchable
    # committed manifest — must report BLOCKED, never a fake pass.
    monkeypatch.setattr(cc, "EXPECTED_GA_PATH", Path("/nonexistent/does-not-exist.json"))
    r = cc.check_capability_manifest(
        "http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]))
    assert r.status == "blocked" and "does-not-exist.json is missing/unreadable" in r.why


def test_capability_manifest_authenticated_asserts_available():
    expected = _expected_ga(["a.one"])
    unauth_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1",
                              "capabilities": [{"id": "a.one", "supported": True, "available": False}]})
    auth_ok_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1",
                               "capabilities": [{"id": "a.one", "supported": True, "available": True}]})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, unauth_body))])
    auth_fetch_ok = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, auth_ok_body))])
    r = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_ok,
                                     frozen_server_sha=TEST_SERVER_SHA)
    assert r.status == "pass" and r.evidence["authenticated"] is True

    auth_fetch_stale = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, unauth_body))])
    r2 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_stale,
                                      frozen_server_sha=TEST_SERVER_SHA)
    assert r2.status == "fail" and "available=true when authenticated" in r2.why

    # An expected-GA id entirely OMITTED from the authenticated manifest (not just present-but-
    # unavailable) must also fail, not silently drop out of the `unavailable` list.
    auth_omitted_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    auth_fetch_omitted = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, auth_omitted_body))])
    r3 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_omitted,
                                      frozen_server_sha=TEST_SERVER_SHA)
    assert r3.status == "fail" and "a.one" in r3.why and "available=true when authenticated" in r3.why


def test_capability_manifest_availability_gated_ids_still_assert_supported():
    expected = _expected_ga(["a.one", "a.gated"])
    expected["availabilityGated"] = [{"id": "a.gated", "reason": "off by configuration"}]

    def body(gated_supported=True, gated_present=True):
        caps = [{"id": "a.one", "supported": True, "available": True}]
        if gated_present:
            caps.append({"id": "a.gated", "supported": gated_supported, "available": False})
        return json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": caps})

    def run(b):
        f = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, b))])
        return cc.check_capability_manifest("http://x", f, expected=expected, authenticated_fetch=f,
                                            frozen_server_sha=TEST_SERVER_SHA)

    ok = run(body())
    assert ok.status == "pass" and ok.evidence["availabilityGatedCount"] == 1
    # Gating relaxes ONLY the authenticated available=true leg: unsupported or absent still fails.
    assert run(body(gated_supported=False)).status == "fail"
    missing = run(body(gated_present=False))
    assert missing.status == "fail" and "a.gated" in missing.why
    # The authenticated manifest must not contradict the public one for a gated id.
    public = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body()))])
    for contradicting in (body(gated_supported=False), body(gated_present=False)):
        auth = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, contradicting))])
        r = cc.check_capability_manifest("http://x", public, expected=expected, authenticated_fetch=auth,
                                         frozen_server_sha=TEST_SERVER_SHA)
        assert r.status == "fail" and "a.gated" in r.why and "when authenticated" in r.why
    # A non-gated id that is unavailable when authenticated still fails.
    expected["availabilityGated"] = []
    assert run(body()).status == "fail"


def test_run_canonical_includes_capability_manifest():
    names = {r.name for r in cc.run_canonical("http://x", _fetcher([]))}
    assert "capability-manifest" in names


# ---- parity comparator ----------------------------------------------------------------------------
def _results(statuses):
    return [cc.CheckResult(n, s) for n, s in statuses]


def test_parity_pass_when_identical():
    ref = par.TargetRun("local-docker", True, _results([("health", "pass"), ("service-catalog", "pass")]))
    oth = par.TargetRun("aws-serverless", True, _results([("health", "pass"), ("service-catalog", "pass")]))
    assert par.compare(ref, oth).status == "pass"


def test_parity_fail_on_divergence():
    ref = par.TargetRun("local-docker", True, _results([("health", "pass")]))
    oth = par.TargetRun("aws-serverless", True, _results([("health", "fail")]))
    v = par.compare(ref, oth)
    assert v.status == "fail" and any("health" in d for d in v.diffs)


def test_parity_blocked_when_target_not_provisioned():
    ref = par.TargetRun("local-docker", True, _results([("health", "pass")]))
    oth = par.TargetRun("aws-serverless", False, [], note="no AWS creds")
    assert par.compare(ref, oth).status == "blocked"


def test_parity_fail_when_reference_itself_failing():
    ref = par.TargetRun("local-docker", True, _results([("health", "fail")]))
    oth = par.TargetRun("aws-serverless", True, _results([("health", "fail")]))
    # Identical, but the reference is broken — parity to a broken baseline is not a pass.
    assert par.compare(ref, oth).status == "fail"


# ---- BLOCKED honesty: no AWS infra => not a green (all 3 targets) ----------------------------------
def test_all_three_aws_targets_registered():
    assert set(REGISTRY) == {"aws-serverless", "aws-ecs", "aws-eks"}


def _tf_vars(argv):
    """Parse `-var=k=v` flags from a terraform arg list into a {k: v} dict."""
    out = {}
    for a in argv:
        if a.startswith("-var="):
            k, _, v = a[len("-var="):].partition("=")
            out[k] = v
    return out


def test_prefix_distinct_per_redis_mode_no_collision(monkeypatch):
    # Regression guard for the strict-cloud-parity collision: the redis-on and redis-off cells run
    # against the same AWS account with the SAME run_id (one GITHUB_RUN_ID across the matrix), so their
    # name_prefix MUST differ or RDS/Lambda/etc. names collide and the redis-on cell fails spuriously.
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    for factory in (serverless, ecs):
        t = factory(run_id="run1234567890")
        on = _tf_vars(t._vars(True))
        off = _tf_vars(t._vars(False))
        assert on["name_prefix"] != off["name_prefix"], (t.name, on["name_prefix"], off["name_prefix"])
        # redis toggle is still correctly threaded to the module var.
        assert on["redis_enabled"] == "true" and off["redis_enabled"] == "false"
        # both bounded for RDS(63)/Lambda(64) identifiers once the module suffixes ("<=18>-it-...").
        for p in (on["name_prefix"], off["name_prefix"]):
            assert 0 < len(p) <= 18 and p.isalnum() and p.islower(), (t.name, p)

    # EKS derives its prefix independently (cluster, not a tf output) — same non-collision guarantee,
    # and teardown must reconstruct the exact prefix it applied (stored on provision, not recomputed).
    eks = REGISTRY["aws-eks"](run_id="run1234567890")
    assert eks._name_prefix(True) != eks._name_prefix(False)
    assert 0 < len(eks._name_prefix(True)) <= 18
    assert eks._prefix is None  # unset until provision; teardown falls back safely


def test_ecs_forces_alb_deletion_protection_off_serverless_has_no_alb(monkeypatch):
    # The ECS ALB defaults deletion_protection=true and would strand the ALB on `terraform destroy`;
    # the ephemeral cert harness must force it off. Serverless has no ALB, so it must NOT pass the var
    # (the serverless root doesn't declare it — passing it would be a terraform error).
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    assert _tf_vars(ecs(run_id="r1")._vars(False)).get("alb_deletion_protection") == "false"
    assert "alb_deletion_protection" not in _tf_vars(serverless(run_id="r1")._vars(False))


def test_ecs_uses_the_proven_x86_64_aot_manifest(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")

    values = _tf_vars(ecs(run_id="r1")._vars(False))

    assert values["task_cpu_architecture"] == "X86_64"


def test_ecs_explicitly_selects_new_connection_encryption_key(monkeypatch):
    # The IAC ECS root is fail-closed: callers must choose between adopting the
    # current key and generating one for a new deployment. This harness always
    # creates a fresh, ephemeral database, so it must pass a typed JSON null.
    # `-var=name=null` is insufficient for a string-constrained Terraform input:
    # it is coerced to the literal string "null".
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    args = ecs(run_id="r1")._vars(False)
    var_files = [Path(a.removeprefix("-var-file=")) for a in args if a.startswith("-var-file=")]
    assert len(var_files) == 1
    values = json.loads(var_files[0].read_text(encoding="utf-8"))
    assert values["honua_connection_encryption_master_key"] is None
    assert "honua_connection_encryption_master_key" not in _tf_vars(args)


def test_ephemeral_admin_password_meets_iac_contract(monkeypatch):
    monkeypatch.delenv("HONUA_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    password = _tf_vars(serverless(run_id="r1")._vars(False))["honua_admin_password"]
    assert len(password) >= 32
    assert any(c.isupper() for c in password)
    assert any(c.islower() for c in password)
    assert any(c.isdigit() for c in password)
    assert any(not c.isalnum() for c in password)


def test_aws_tf_targets_expose_only_runner_ip_for_postgis_bootstrap(monkeypatch):
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    for factory in (serverless, ecs):
        values = _tf_vars(factory(run_id="r1")._vars(False))
        assert values["db_publicly_accessible"] == "true"
        assert json.loads(values["db_additional_ingress_cidrs"]) == ["192.0.2.10/32"]
    assert json.loads(_tf_vars(serverless(run_id="r1")._vars(False))["lambda_architectures"]) == ["arm64"]


def test_serverless_rejects_broad_db_ingress(monkeypatch):
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "0.0.0.0/0")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    with __import__("pytest").raises(ProvisionError, match="single IPv4 /32"):
        serverless(run_id="r1")._vars(False)


def test_teardown_reconstructs_redis_mode_vars(monkeypatch):
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    target = serverless(run_id="run123456")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    calls = []

    def _record(root, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(target, "_tf", _record)
    target.teardown(redis_enabled=True)
    values = _tf_vars(calls[0])
    assert values["redis_enabled"] == "true"
    assert values["name_prefix"].startswith("honuar")


def test_serverless_blocked_without_infra(monkeypatch):
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    avail = serverless().availability()
    assert not avail.ok
    assert any("AWS credentials" in m for m in avail.missing)
    assert any("HONUA_LAMBDA_IMAGE_URI" in m for m in avail.missing)


def test_ecs_blocked_without_infra(monkeypatch):
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    avail = ecs().availability()
    assert not avail.ok and any("HONUA_ECS_IMAGE" in m for m in avail.missing)


def test_eks_needs_helm_chart_and_image(monkeypatch):
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    avail = REGISTRY["aws-eks"]().availability()
    assert not avail.ok
    # EKS is the heavy cell: beyond AWS/iac it needs the helm chart + a k8s image (deterministic envs;
    # CLI presence varies by machine so we don't assert on kubectl/helm being absent).
    assert any("HONUA_HELM_DIR" in m for m in avail.missing)
    assert any("HONUA_ECS_IMAGE" in m for m in avail.missing)


_CRED_ENV = ("HONUA_AWS_ROLE_ARN", "AWS_ROLE_ARN", "AWS_ACCESS_KEY_ID", "AWS_PROFILE",
             "AWS_WEB_IDENTITY_TOKEN_FILE")


def test_run_cloud_self_skips_only_optional_path_without_cloud_creds(monkeypatch):
    # No cloud/OIDC creds may SELF-SKIP an optional bootstrap run, but the same missing evidence is
    # a hard failure under require_real so the AWS matrix cannot be green without exercising AWS.
    for var in set(_AWS_ENV) | set(_CRED_ENV):
        monkeypatch.delenv(var, raising=False)
    for target in ("aws-serverless", "aws-ecs", "aws-eks"):
        for redis in (True, False):
            r = run_cloud.run(target, require_real=False, reference_endpoint=None, redis_enabled=redis)
            assert r["status"] == "skipped" and r["why"] == "cloud-creds-unset", (target, redis)
            assert r["redis"] == ("redis-on" if redis else "redis-off")
            r2 = run_cloud.run(target, require_real=True, reference_endpoint=None, redis_enabled=redis)
            assert r2["status"] == "fail", (target, redis)
            assert "required cloud certification evidence missing" in r2["why"], (target, redis)


def test_run_cloud_blocked_when_creds_present_but_infra_missing(monkeypatch):
    # Creds present but image/IaC missing => BLOCKED (half-wired, surfaced), require_real => FAIL.
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    r = run_cloud.run("aws-serverless", require_real=False, reference_endpoint=None, redis_enabled=False)
    assert r["status"] == "blocked", r
    r2 = run_cloud.run("aws-serverless", require_real=True, reference_endpoint=None, redis_enabled=False)
    assert r2["status"] == "fail", r2


def test_run_cloud_unknown_target_fails():
    assert run_cloud.run("aws-nonexistent", require_real=False, reference_endpoint=None)["status"] == "fail"


def test_cloud_endpoint_readiness_retries_transient_gateway_404():
    responses = iter([
        cc.HttpResponse(404, '{"message":"Not Found"}', {"server": "AmazonAPIGateway"}),
        cc.HttpResponse(503, "starting"),
        cc.HttpResponse(200, "ready"),
    ])
    sleeps = []
    ready, evidence = run_cloud._wait_for_endpoint(
        "https://example.execute-api.us-east-1.amazonaws.com/",
        lambda _url: next(responses),
        attempts=3,
        delay_seconds=0.25,
        sleep=sleeps.append,
    )
    assert ready is True
    assert evidence == {
        "url": "https://example.execute-api.us-east-1.amazonaws.com/healthz/ready",
        "status": 200,
        "attempts": 3,
    }
    assert sleeps == [0.25, 0.25]


def test_cloud_endpoint_readiness_preserves_final_failure_evidence():
    response = cc.HttpResponse(404, '{"message":"Not Found"}', {"server": "AmazonAPIGateway"})
    ready, evidence = run_cloud._wait_for_endpoint(
        "https://example.execute-api.us-east-1.amazonaws.com",
        lambda _url: response,
        attempts=2,
        delay_seconds=0,
        sleep=lambda _seconds: None,
    )
    assert ready is False
    assert evidence["status"] == 404
    assert evidence["attempts"] == 2
    assert evidence["body_head"] == '{"message":"Not Found"}'
    assert evidence["headers"]["server"] == "AmazonAPIGateway"


# ---- EKS: the chart + LoadBalancer cell ------------------------------------------------------------
_EKS_IMAGE = "ghcr.io/honua-io/honua-server:nightly-aot-6b6d3b8@sha256:" + "a" * 64


def _eks_env(monkeypatch, *, image: str = _EKS_IMAGE, cidr: str = "192.0.2.10/32"):
    monkeypatch.setenv("HONUA_ECS_IMAGE", image)
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", cidr)
    return REGISTRY["aws-eks"](run_id="run1234567890")


def _helm_sets(command):
    """Parse the `--set`/`--set-string` pairs out of a helm command line."""
    values = {}
    for flag, pair in zip(command, command[1:]):
        if flag in ("--set", "--set-string"):
            key, _, value = pair.partition("=")
            values[key] = value
    return values


def test_eks_requires_the_runner_cidr(monkeypatch):
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    avail = REGISTRY["aws-eks"]().availability()
    assert not avail.ok
    assert any("HONUA_AWS_RUNNER_CIDR" in m for m in avail.missing)


def test_eks_publishes_the_api_server_to_the_runner_only(monkeypatch):
    values = _tf_vars(_eks_env(monkeypatch)._tf_vars(True))
    assert values["cluster_endpoint_public_access"] == "true"
    assert json.loads(values["cluster_endpoint_public_access_cidrs"]) == ["192.0.2.10/32"]
    # kubectl/helm run as the role that created the cluster; without the access entry it has no
    # Kubernetes identity at all and the whole cell is unusable.
    assert values["enable_cluster_creator_admin_permissions"] == "true"
    assert values["name_prefix"].startswith("honuaeksr")


def _fake_eks_iac_root(monkeypatch, stack, *, declares_secret_encryption: bool):
    """A throwaway honua-iac tree at HONUA_IAC_DIR whose aws-eks root may or may not declare the
    secret-encryption variable — the two sides of the pin bump the harness has to survive."""
    base = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    root = base / "infrastructure" / "terraform" / "examples" / "aws-eks"
    root.mkdir(parents=True)
    body = 'variable "region" {\n  type = string\n}\n'
    if declares_secret_encryption:
        body += 'variable "cluster_secret_encryption_enabled" {\n  type    = bool\n  default = true\n}\n'
    (root / "variables.tf").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HONUA_IAC_DIR", str(base))
    # The standalone runner's monkeypatch shim mutates os.environ for real, so the pointer must not
    # outlive the directory it points at.
    stack.callback(lambda: monkeypatch.delenv("HONUA_IAC_DIR", raising=False))
    return base


def test_eks_mints_no_per_cell_kms_key_when_the_root_supports_it(monkeypatch):
    # honua-release#127: the cluster's secret-encryption CMK cannot be deleted by `terraform destroy`
    # — only SCHEDULED for deletion on AWS's 7-day minimum window — so each ephemeral cell left a key
    # billing for a week after the cell was gone (two per full matrix dispatch, forever). Nothing in
    # the parity suite asserts secret-at-rest encryption, so the cells are not certifying it and the
    # key is pure cost: the cell must switch it off.
    with contextlib.ExitStack() as stack:
        _fake_eks_iac_root(monkeypatch, stack, declares_secret_encryption=True)
        values = _tf_vars(_eks_env(monkeypatch)._tf_vars(True))
    assert values["cluster_secret_encryption_enabled"] == "false"


def test_eks_omits_the_kms_var_when_the_pinned_iac_root_does_not_declare_it(monkeypatch):
    # Ordering guard. honua-iac is pinned BY SHA (platform-manifest.yaml components.honua-iac.sha), so
    # this repo can be ahead of the tree it applies — and terraform HARD-ERRORS on `-var` for an
    # undeclared root variable ("Value for undeclared variable"), which would break every EKS cell in
    # the window between the two merges. The flag must therefore appear only once the checked-out root
    # actually declares it, and stay absent (not "true", not present) before that.
    with contextlib.ExitStack() as stack:
        _fake_eks_iac_root(monkeypatch, stack, declares_secret_encryption=False)
        values = _tf_vars(_eks_env(monkeypatch)._tf_vars(True))
    assert "cluster_secret_encryption_enabled" not in values

    # No honua-iac tree at all (the BLOCKED path) must not synthesise the var either.
    monkeypatch.delenv("HONUA_IAC_DIR", raising=False)
    assert "cluster_secret_encryption_enabled" not in _tf_vars(_eks_env(monkeypatch)._tf_vars(True))


def test_eks_teardown_passes_the_same_kms_var_it_applied(monkeypatch):
    # `terraform destroy` re-evaluates the root, so it must be handed the identical var set — a
    # destroy that omitted the flag would re-plan a key the apply never made.
    with contextlib.ExitStack() as stack:
        _fake_eks_iac_root(monkeypatch, stack, declares_secret_encryption=True)
        target = _eks_env(monkeypatch)
        target._prefix = "honuaeksrrun123"
        assert target._tf_vars(True) == target._tf_vars(True)
        assert "-var=cluster_secret_encryption_enabled=false" in target._tf_vars(False)


def test_eks_rejects_a_broad_api_server_cidr(monkeypatch):
    target = _eks_env(monkeypatch, cidr="0.0.0.0/0")
    with __import__("pytest").raises(ProvisionError, match="IPv4 /32"):
        target._tf_vars(False)


def test_eks_helm_pins_the_exact_manifest_image_by_digest(monkeypatch):
    target = _eks_env(monkeypatch)
    values = _helm_sets(target._helm_command(False, Path("/chart")))
    assert values["image.repository"] == "ghcr.io/honua-io/honua-server"
    assert values["image.digest"] == "sha256:" + "a" * 64
    assert values["image.tag"] == ""          # digest-pinned: the chart renders repository@digest
    # A tag-only reference stays a tag-only reference; a bare repository is not a usable pin.
    assert target._image_values("ghcr.io/x/y:tag") == ("ghcr.io/x/y", "tag", "")
    with __import__("pytest").raises(ProvisionError, match="tag or digest"):
        target._image_values("ghcr.io/x/y")


def test_eks_exposes_the_chart_service_through_a_load_balancer(monkeypatch):
    values = _helm_sets(_eks_env(monkeypatch)._helm_command(False, Path("/chart")))
    # The cell's endpoint is a real AWS load balancer in front of the chart's own Service — that is
    # what the canonical checks and canary probes are pointed at.
    assert values["service.type"] == "LoadBalancer"
    # Credentials live in an externally managed Secret, never in the release values.
    assert values["secret.create"] == "false"
    assert values["secret.name"] == "honua-runtime"
    # The chart's PostgreSQL subchart is development-only and carries no PostGIS.
    assert values["postgresql.enabled"] == "false"
    # The chart's pre-install hook makes Redis mandatory for every non-development environment, which
    # the redis-off dimension exists to disprove, and pre-install-probes its own not-yet-created Redis
    # Service in the redis-on cell. It gates nothing this tier certifies.
    assert values["preflight.enabled"] == "false"
    # Same runtime env the aws-ecs cell's honua-iac root passes, so the two cells differ in deploy
    # shape and nothing else. Host validation rejects a load balancer's generated DNS name with 400,
    # which would fail every canonical check for a reason unrelated to the candidate.
    assert values["config.env.HostValidation__Enabled"] == "false"
    assert values["config.env.HONUA_SERVE_ADMIN_UI"] == "true"
    assert values["config.env.HONUA_ADMIN_UI"] == "true"


def test_eks_threads_the_redis_dimension_through_the_chart(monkeypatch):
    target = _eks_env(monkeypatch)
    on = _helm_sets(target._helm_command(True, Path("/chart")))
    off = _helm_sets(target._helm_command(False, Path("/chart")))
    # redis-on must exercise the CHART's Redis path, not a bypass around it.
    assert on["redis.enabled"] == "true"
    assert on["redis.auth.enabled"] == "true"
    assert on["redis.auth.password"] == target._redis_password
    assert on["redis.master.persistence.enabled"] == "false"   # no CSI driver: a PVC never binds
    assert off["redis.enabled"] == "false"
    assert "redis.auth.password" not in off


def test_eks_runtime_secret_carries_redis_only_when_the_cell_enables_it(monkeypatch):
    target = _eks_env(monkeypatch)
    applied = []
    monkeypatch.setattr(target, "_apply", lambda manifest: applied.append(manifest))

    target._install_runtime_secret(True)
    on = applied[-1]["stringData"]
    assert on["ConnectionStrings__redis"].startswith("honua-redis-master:6379,password=")
    assert target._db_password in on["ConnectionStrings__DefaultConnection"]
    # The chart's preflight enforces these; a cell that cannot install is not a cert.
    assert len(on["HONUA_ADMIN_PASSWORD"]) >= 16
    assert len(on["Security__ConnectionEncryption__MasterKey"]) >= 32

    target._install_runtime_secret(False)
    assert "ConnectionStrings__redis" not in applied[-1]["stringData"]


def test_eks_teardown_deletes_load_balancers_before_terraform_destroys_the_vpc(monkeypatch):
    target = _eks_env(monkeypatch)
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    order = []

    def _run(command, **kwargs):
        order.append(command[:3])
        return subprocess.CompletedProcess(command, 0, "", "")

    def _kubectl(*args, **kwargs):
        order.append(["kubectl", *args[:2]])
        if args[:2] == ("get", "services"):
            body = {"items": [{"metadata": {"name": "honua", "namespace": "honua-cert"},
                               "spec": {"type": "LoadBalancer"}},
                              {"metadata": {"name": "postgis", "namespace": "honua-cert"},
                               "spec": {"type": "ClusterIP"}}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(target, "_run", _run)
    monkeypatch.setattr(target, "_kubectl", _kubectl)
    monkeypatch.setattr(target, "_tf", lambda root, *a, **k: order.append(["terraform", a[0]])
                        or subprocess.CompletedProcess(a, 0, "", ""))

    target.teardown(redis_enabled=True)

    flat = [" ".join(entry) for entry in order]
    delete = flat.index("kubectl delete service")
    destroy = flat.index("terraform destroy")
    # A surviving ELB holds the subnets and strands the whole VPC (honua-iac#142).
    assert delete < destroy
    assert "kubectl delete namespace" in flat
    # ...and only the LoadBalancer Service is chased; ClusterIP services die with the namespace.
    assert flat.count("kubectl delete service") == 1


def test_eks_teardown_sweeps_the_leaked_node_enis_and_retries_the_destroy(monkeypatch):
    # OBSERVED on run 32219953698: both cells stranded their VPC. The VPC CNI's secondary ENIs
    # survive the node group's deletion detached-but-alive, and a detached ENI holds its subnet and
    # security group, so terraform fails with
    #   DependencyViolation: resource sg-... has a dependent object
    #   DependencyViolation: The subnet 'subnet-...' has dependencies and cannot be deleted.
    # and the whole VPC keeps its quota slot forever (honua-iac#142).
    target = _eks_env(monkeypatch)
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(target, "_kubectl", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    aws = []

    def _run(command, **kwargs):
        aws.append(command)
        if command[:3] == ["aws", "ec2", "describe-vpcs"]:
            return subprocess.CompletedProcess(command, 0, "vpc-0eks\n", "")
        if command[:3] == ["aws", "ec2", "describe-network-interfaces"]:
            leaked = "eni-0leaked\tavailable\n" if not any(
                c[:3] == ["aws", "ec2", "delete-network-interface"] for c in aws) else ""
            return subprocess.CompletedProcess(command, 0, leaked, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    destroys = []

    def _tf(root, *args, **kwargs):
        if args[0] == "destroy":
            destroys.append(args)
            code = 1 if len(destroys) == 1 else 0
            return subprocess.CompletedProcess(args, code, "", "DependencyViolation: subnet ...")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(target, "_run", _run)
    monkeypatch.setattr(target, "_tf", _tf)
    target.teardown(redis_enabled=False)

    assert len(destroys) == 2, "the destroy must be retried once the leaked ENIs are gone"
    deleted = [c for c in aws if c[:3] == ["aws", "ec2", "delete-network-interface"]]
    assert [c[-1] for c in deleted] == ["eni-0leaked"]
    # The sweep must happen between the two attempts, never before the first.
    assert aws.index(deleted[0]) > 0


def test_eks_teardown_fails_closed_when_the_vpc_cannot_be_destroyed(monkeypatch):
    target = _eks_env(monkeypatch)
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(target, "_run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 1, "", "no cluster"))
    monkeypatch.setattr(target, "_tf", lambda root, *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "DependencyViolation"))
    with __import__("pytest").raises(ProvisionError, match="teardown failed"):
        target.teardown(redis_enabled=False)


def test_eks_never_leaks_a_generated_credential_into_a_failure(monkeypatch):
    target = _eks_env(monkeypatch)
    leaked = f"connection refused for Password={target._db_password}"
    assert target._db_password not in target._redact(leaked)
    assert "***" in target._redact(leaked)


def test_terraform_target_teardown_fails_closed(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    target = ecs(run_id="r1")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(target, "_tf", lambda root, *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "DependencyViolation: ALB in use"))
    with __import__("pytest").raises(ProvisionError, match="teardown failed"):
        target.teardown(redis_enabled=False)


# ---- teardown always runs, and a strand is a red cell ----------------------------------------------
class _StubTarget:
    name = "stub"
    admin_api_key = "stub-admin-key"

    def __init__(self, *, provision_error=None, teardown_error=None):
        self._provision_error = provision_error
        self._teardown_error = teardown_error
        self.torn_down = 0

    def availability(self):
        from targets.base import Availability
        return Availability(True, "stub ready")

    def provision(self, redis_enabled: bool = False) -> str:
        raise ProvisionError(self._provision_error or "boom")

    def teardown(self, redis_enabled: bool | None = None) -> None:
        self.torn_down += 1
        if self._teardown_error:
            raise ProvisionError(self._teardown_error)


def _run_with_stub(monkeypatch, stub):
    # Restored explicitly rather than left to monkeypatch: this module is also runnable standalone
    # (`python e2e/test_cloud.py`, the Makefile's no-pytest fallback), where nothing undoes a patch.
    registry = run_cloud.REGISTRY
    try:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setattr(run_cloud, "REGISTRY", {"stub": lambda **kwargs: stub})
        return run_cloud.run("stub", require_real=False, reference_endpoint=None, redis_enabled=True)
    finally:
        run_cloud.REGISTRY = registry
        os.environ.pop("AWS_ACCESS_KEY_ID", None)


def test_run_cloud_tears_down_after_a_failed_provision(monkeypatch):
    # honua-iac#142: a cell that failed mid-provision has real, billing AWS resources behind it.
    stub = _StubTarget(provision_error="terraform apply died")
    report = _run_with_stub(monkeypatch, stub)
    assert stub.torn_down == 1
    assert report["status"] == "fail" and "terraform apply died" in report["why"]


def test_run_cloud_reddens_a_cell_that_stranded_its_infrastructure(monkeypatch):
    stub = _StubTarget(provision_error="apply died", teardown_error="destroy died")
    report = _run_with_stub(monkeypatch, stub)
    assert report["status"] == "fail"
    assert "apply died" in report["why"] and "teardown failed: destroy died" in report["why"]


def test_reaper_retries_only_state_lock_contention_then_fails_closed():
    import reap_cloud

    class _Locked:
        def __init__(self, failures, message):
            self.failures = failures
            self.message = message
            self.calls = 0

        def teardown(self, redis_enabled=None):
            self.calls += 1
            if self.calls <= self.failures:
                raise ProvisionError(self.message)

    locked = _Locked(2, "Error acquiring the state lock: ConditionalCheckFailedException")
    reap_cloud.reap(locked, redis_enabled=True, sleep=lambda _s: None)
    assert locked.calls == 3

    broken = _Locked(1, "DependencyViolation: subnet still in use")
    try:
        reap_cloud.reap(broken, redis_enabled=False, sleep=lambda _s: None)
    except ProvisionError:
        pass
    else:  # pragma: no cover - the reaper must never swallow a real strand
        raise AssertionError("a non-lock teardown failure must fail closed")
    assert broken.calls == 1


# ---- honua-release#128: the ECS cell's ALB must admit the runner, and an unreachable endpoint is red
def test_ecs_opens_the_alb_to_the_runner_only(monkeypatch):
    # The aws-ecs module defaults its ALB security group to VPC-only HTTP ingress when neither
    # allow_http_ingress_cidrs nor a certificate is supplied, so the GitHub runner's requests were
    # dropped at the security group and every probe timed out (honua-release#128). The cell must open
    # the ALB to the ephemeral runner's own /32 — and to nothing wider.
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")

    ecs_vars = _tf_vars(ecs(run_id="r1")._vars(False))
    assert json.loads(ecs_vars["allow_http_ingress_cidrs"]) == ["192.0.2.10/32"]

    # Serverless has no ALB and its root does not declare the var — passing it would be a tf error.
    assert "allow_http_ingress_cidrs" not in _tf_vars(serverless(run_id="r1")._vars(False))


def test_ecs_rejects_a_broad_alb_ingress(monkeypatch):
    # A plain-HTTP ALB on 0.0.0.0/0 is exactly what the module's own http_ingress_requires_https check
    # exists to discourage; the harness must never be the thing that opens it.
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "0.0.0.0/0")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    with __import__("pytest").raises(ProvisionError, match="single IPv4 /32"):
        ecs(run_id="r1")._vars(False)


def test_ecs_requires_the_runner_cidr(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.delenv("HONUA_AWS_RUNNER_CIDR", raising=False)
    avail = ecs(run_id="r1").availability()
    assert not avail.ok and any("HONUA_AWS_RUNNER_CIDR" in m for m in avail.missing)


class _ServingStub:
    """A target that provisions an endpoint; what that endpoint *serves* is the patched fetch's job."""
    name = "stub"
    admin_api_key = "stub-admin-key"

    def __init__(self, endpoint: str = "http://stub.example.invalid"):
        self.endpoint = endpoint
        self.torn_down = 0

    def availability(self):
        from targets.base import Availability
        return Availability(True, "stub ready")

    def provision(self, redis_enabled: bool = False) -> str:
        return self.endpoint

    def teardown(self, redis_enabled: bool | None = None) -> None:
        self.torn_down += 1


@contextlib.contextmanager
def _isolated_journey():
    # These tests isolate HTTP probe verdicts. The imported journey and cost paths have separate
    # integration tests below, so no registry, Docker or AWS access belongs in this helper.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        path.write_text("{}")
        with mock.patch.object(run_cloud.cloud_journey, "attempt", return_value={"receipt": str(path)}), \
             mock.patch.object(run_cloud.cloud_journey, "validate_attempt", return_value=True), \
             mock.patch.object(run_cloud.cloud_journey, "check_cost", return_value={"status": "pass"}):
            yield


def _run_serving(monkeypatch, *, ready_status, checks, canary, require_real=False, extended=None):
    """Drive run_cloud.run() against a stub that provisions, with canned probe verdicts."""
    stub = _ServingStub()
    registry = run_cloud.REGISTRY
    attempts, delay = run_cloud._READY_ATTEMPTS, run_cloud._READY_DELAY_SECONDS
    try:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setattr(run_cloud, "REGISTRY", {"stub": lambda **kwargs: stub})
        monkeypatch.setattr(run_cloud, "_READY_ATTEMPTS", 1)
        monkeypatch.setattr(run_cloud, "_READY_DELAY_SECONDS", 0)
        monkeypatch.setattr(run_cloud, "make_fetch",
                            lambda **kwargs: (lambda _url: cc.HttpResponse(ready_status, "")))
        monkeypatch.setattr(run_cloud, "run_canonical", lambda *a, **k: checks)
        monkeypatch.setattr(run_cloud.canary_probes, "run_canary", lambda *a, **k: canary)
        monkeypatch.setattr(run_cloud, "run_extended", lambda *a, **k: extended if extended is not None else
                            [cc.CheckResult(name, "pass", "driver passed") for name in cloud_driver.DRIVERS])
        with _isolated_journey():
            report = run_cloud.run("stub", require_real, reference_endpoint=None, redis_enabled=True)
    finally:
        run_cloud.REGISTRY = registry
        run_cloud._READY_ATTEMPTS, run_cloud._READY_DELAY_SECONDS = attempts, delay
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
    return stub, report


def test_run_cloud_fails_a_cell_whose_endpoint_never_served(monkeypatch):
    # THE honua-release#128 regression. The aws-ecs cells reported a passing verdict in every run they
    # ever had while their ALB dropped every request: readiness never returned 200, all six canonical
    # checks and every reachability probe reported `blocked: endpoint unreachable`, and the cell's own
    # summary line read "canonical set passed". A cell that provisioned an endpoint which then never
    # answered is a FAILED cell.
    checks = [cc.unreachable("health", "endpoint unreachable (transport error: timed out)"),
              cc.CheckResult("service-catalog", "blocked", "endpoint unreachable")]
    canary = [cc.unreachable("security-headers"), cc.unreachable("metrics-gated")]
    stub, report = _run_serving(monkeypatch, ready_status=0, checks=checks, canary=canary)

    assert report["status"] == "fail"
    assert report["readiness"]["ready"] is False
    assert "security-headers" in report["why"] and "health" in report["why"]
    assert "stub.example.invalid" in report["why"]
    assert stub.torn_down == 1  # a failed cell still must not strand its infrastructure


def test_run_cloud_fails_an_unreachable_cell_even_when_readiness_squeaked_through(monkeypatch):
    # Readiness is one 200 on one route; the probes are the broader evidence. If they cannot reach the
    # endpoint the cell is still not serving, whatever the readiness poll happened to catch.
    _stub, report = _run_serving(monkeypatch, ready_status=200,
                                 checks=[cc.CheckResult("health", "pass", "ok")],
                                 canary=[cc.unreachable("security-headers")])
    assert report["status"] == "fail" and "security-headers" in report["why"]


def test_run_cloud_still_passes_when_the_only_blocks_are_missing_inputs(monkeypatch):
    # The other half of honua-release#128: do NOT redden cells that are legitimately skipping. A probe
    # with no admin key and a probe with no seeded service id are missing an INPUT we chose not to
    # supply — they say nothing about the candidate and must stay blocked, not fail.
    checks = [cc.CheckResult("health", "pass", "ok")]
    canary = [cc.CheckResult("metrics-gated", "blocked", "no admin API key configured"),
              cc.CheckResult("render-query-smoke", "blocked", "no demo service id configured to probe"),
              cc.CheckResult("security-headers", "pass", "all baseline security headers present")]
    _stub, report = _run_serving(monkeypatch, ready_status=200, checks=checks, canary=canary)
    assert report["status"] == "pass", report["why"]
    assert all(row["status"] == "pass" for row in report["scenarioCoverage"])


def test_require_real_promotes_genuine_extended_precondition_blocks(monkeypatch):
    extended = [cc.CheckResult(name, "blocked" if name == "top-demo" else "pass",
                              "pinned site CSP excludes AWS origin" if name == "top-demo" else "ok")
                for name in cloud_driver.DRIVERS]
    _stub, report = _run_serving(monkeypatch, ready_status=200,
        checks=[cc.CheckResult("health", "pass")], canary=[], require_real=True, extended=extended)
    assert report["status"] == "fail"
    assert "top-demo" in report["why"]
    assert report["scenarioCoverage"][-1]["status"] == "blocked"


def test_extended_regression_always_fails_bootstrap(monkeypatch):
    _stub, report = _run_serving(monkeypatch, ready_status=200,
        checks=[cc.CheckResult("health", "pass")], canary=[],
        extended=[cc.CheckResult("gp-execute", "fail", "job failed")])
    assert report["status"] == "fail" and "gp-execute" in report["why"]


def test_run_cloud_never_claims_a_blocked_canonical_set_passed(monkeypatch):
    # The sentence that hid #128: a cell whose canonical set was entirely blocked still said
    # "canonical set passed" in the job log.
    checks = [cc.CheckResult("geoprocessing", "blocked", "no GPServer catalogued")]
    _stub, report = _run_serving(monkeypatch, ready_status=200, checks=checks, canary=[])
    assert report["status"] == "blocked"
    assert "passed" not in report["why"] and "geoprocessing" in report["why"]


def test_licensing_var_is_only_passed_to_roots_that_declare_it(monkeypatch):
    # The first live aws-ecs cell failed on `-var=additional_env=...`, a module input the example
    # roots set internally and never declare. Only a root that declares licensing_mode receives it,
    # and a comment that merely mentions the variable is not a declaration.
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / "infrastructure" / "terraform" / "examples" / "aws"
        root.mkdir(parents=True)
        variables = root / "variables.tf"
        variables.write_text('variable "region" {}\n# variable "licensing_mode" arrives later\n', encoding="utf-8")
        monkeypatch.setenv("HONUA_IAC_DIR", base)
        monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
        monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
        monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
        monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
        target = ecs(run_id="r1")
        assert target._licensing_vars() == []
        assert not any(a.startswith("-var=additional_env") for a in target._vars(False))
        variables.write_text('variable "region" {}\nvariable "licensing_mode" {\n  default = "Disabled"\n}\n', encoding="utf-8")
        assert target._licensing_vars() == ["-var=licensing_mode=Disabled"]
        assert "-var=licensing_mode=Disabled" in target._vars(False)



def test_ecs_cell_disables_rds_deletion_protection_and_enables_postgis_when_declared(monkeypatch):
    # honua-iac v0.2.0 defaults rds_deletion_protection=true (destroy strands the RDS instance) and
    # enable_postgis=false (the server's PostGIS preflight exits, ALB 503) - e2e-cloud-aws run
    # 36560629698. The ephemeral cell overrides both, but only on a root that declares them.
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / "infrastructure" / "terraform" / "examples" / "aws"
        root.mkdir(parents=True)
        variables = root / "variables.tf"
        variables.write_text('variable "region" {}\n# variable "rds_deletion_protection" later\n',
                             encoding="utf-8")
        monkeypatch.setenv("HONUA_IAC_DIR", base)
        monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
        monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
        monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
        monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
        target = ecs(run_id="r1")
        tf = _tf_vars(target._vars(False))
        assert "rds_deletion_protection" not in tf and "enable_postgis" not in tf
        variables.write_text('variable "region" {}\nvariable "rds_deletion_protection" {}\n'
                             'variable "enable_postgis" {}\n', encoding="utf-8")
        tf = _tf_vars(target._vars(False))
        assert tf["rds_deletion_protection"] == "false"
        assert tf["enable_postgis"] == "true"
        # The PostGIS bootstrap is local-exec psql from the runner: the cell must also open RDS to it.
        assert tf["db_publicly_accessible"] == "true"


# ---- promise journey cloud bindings: schema, candidate, retry history and cost --------------------
def _ecs_receipt(cell="aws-ecs/redis-off"):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    receipt = driver.build_receipt(manifest=cj.manifest(),
        journey=driver.load(cj.HERE / "journey.v1.json"), roster={"status": "pass"},
        evidence_uri="urn:test:cloud#honua-run=offline-run/2", mode="live",
        target={"id": cell, "kind": "aws-ecs"},
        target_path=None, target_base_url="https://cell.invalid",
        workspace=driver.pins.ClientWorkspace(status="blocked", root=None, reason="fixture"),
        stage_results=None, notices=[])
    receipt["status"] = "pass"
    receipt["clientWorkspace"].update(status="pass", resolved=[{
        "name": "honua-sdk-js", "package": "@honua/sdk-js", "version": "0.0.0",
        "ecosystem": "npm", "integrityVerified": True, "tarballSha256": "f" * 64, "bin": {}}])
    for stage in receipt["stages"]:
        stage.update(status="pass", blockedBy=[], checks=[{
            "id": "fixture-probe", "kind": "http", "invocation": "GET /fixture",
            "status": "pass", "detail": "offline fixture"}])
        for key in ("operationId", "policyDecisionId", "actuatorId", "verificationId", "approvalId"):
            stage[key] = "fixture-" + key
        stage["evidence"].update(source="live-aws-ecs", freshness="verified-current",
            completeness="complete", observedAt=receipt["generatedAt"])
    driver.validate_receipt(receipt, cj.HERE / "receipt.schema.json")
    return receipt


def _artifact(directory, receipt, *, number=1):
    cj = run_cloud.cloud_journey
    path = directory / f"receipt-{number}.json"
    path.write_text(json.dumps(receipt))
    record = {"number": number, "cell": receipt["target"]["id"], "runId": "offline-run",
        "runAttempt": "2", "candidateDigest": cj.candidate_digest(), "receipt": path.name,
        "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "failureAttribution": None if receipt["status"] == "pass" else "infrastructure"}
    return record


def _final_cost(amount="1", status="pass"):
    return {"status": status, "scope": "run", "amountUsd": amount, "ceilingUsd": "20",
            "runId": "offline-run", "runAttempt": "2", "measuredAt": run_cloud.cloud_journey.now()}


def _aggregate_fixture(reports, directory, full_scope=False, final_cost=None):
    if final_cost is None and full_scope:
        final_cost = _final_cost()
    return run_cloud.cloud_journey.aggregate(reports, directory, require_real=True,
        full_scope=full_scope, run_id="offline-run", run_attempt="2", final_cost=final_cost)


def _cell_report(directory, receipt):
    return {"cell": receipt["target"]["id"], "status": "pass", "artifactDirectory": str(directory),
        "journeyAttempts": [_artifact(directory, receipt)],
        "cost": {"status": "pass", "scope": "run", "amountUsd": "1", "ceilingUsd": "20",
            "runId": "offline-run", "runAttempt": "2", "measuredAt": run_cloud.cloud_journey.now(),
            "candidateDigest": run_cloud.cloud_journey.candidate_digest()}}


def test_cloud_missing_and_skipped_cell_receipts_fail():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        assert _aggregate_fixture([], root, full_scope=True)["status"] == "fail"
        receipt = _ecs_receipt()
        report = _cell_report(root, receipt)
        (root / report["journeyAttempts"][0]["receipt"]).unlink()
        assert _aggregate_fixture([report], root)["status"] == "fail"
        report = _cell_report(root, receipt)
        report["status"] = "skipped"
        assert _aggregate_fixture([report], root)["status"] == "fail"
        report["journeyAttempts"] = []
        assert _aggregate_fixture([report], root)["status"] == "fail"


def test_cloud_stale_and_wrong_candidate_receipts_fail():
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        receipt = _ecs_receipt()
        receipt["generatedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert "stale" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]
        receipt = _ecs_receipt()
        receipt["server"]["sourceSha"] = "0" * 40
        assert "wrong candidate" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]
        receipt = _ecs_receipt()
        report = _cell_report(root, receipt)
        report["journeyAttempts"][0]["candidateDigest"] = "0" * 64
        assert "wrong candidate" in _aggregate_fixture([report], root)["why"]
        report = _cell_report(root, receipt)
        report["journeyAttempts"][0]["runAttempt"] = "1"
        assert _aggregate_fixture([report], root)["status"] == "fail"
        receipt["stages"][0]["evidence"]["observedAt"] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        assert "stale stage" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]


def test_preview_cell_failure_is_informational_and_cannot_fill_a_ga_cell():
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        report = _cell_report(root, _ecs_receipt())
        previews = [{"cell": f"{target}/redis-off", "status": "fail"} for target in cj.PREVIEW_TARGETS]
        result = _aggregate_fixture([report, *previews], root)
        assert result["status"] == "blocked"  # focused dispatch remains diagnostic
        assert [r["evidenceTier"] for r in result["cells"]] == ["GA", "Preview", "Preview"]
        result = _aggregate_fixture([*previews], root, full_scope=True)
        assert result["status"] == "fail" and "missing required cells" in result["why"]


def test_cloud_every_attempt_is_validated_and_two_attempts_can_pass():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        success = _ecs_receipt()
        failed = _ecs_receipt()
        failed["status"] = "fail"
        failed["stages"][0]["status"] = "fail"
        failed["failure"] = {"number": 1, "stage": failed["stages"][0]["stage"],
            "command": "fixture", "check": "fixture", "detail": "infrastructure fixture"}
        report = _cell_report(root, success)
        report["journeyAttempts"] = [_artifact(root, failed), _artifact(root, success, number=2)]
        assert _aggregate_fixture([report], root)["status"] == "blocked"
        report["journeyAttempts"][0]["failureAttribution"] = None
        assert _aggregate_fixture([report], root)["status"] == "fail"
        report["journeyAttempts"][0]["failureAttribution"] = "infrastructure"
        (root / "receipt-1.json").unlink()
        assert _aggregate_fixture([report], root)["status"] == "fail"
        report["journeyAttempts"] = [_artifact(root, success), _artifact(root, success, number=2)]
        assert "after a passing" in _aggregate_fixture([report], root)["why"]


def test_attempt_strips_cloud_credentials_and_binds_the_run(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    monkeypatch.setenv("GITHUB_RUN_ID", "cloudscrub4242")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "7")
    monkeypatch.delenv("HONUA_RUN_URL", raising=False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-session")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "oidc-token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://oidc.example/token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_example")
    monkeypatch.setenv("GH_TOKEN", "gh_example")
    monkeypatch.setenv("HONUA_AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/release")
    seen = {}

    def live(*_args, **_kwargs):
        seen["env"] = dict(os.environ)
        raise RuntimeError("stop after capturing the candidate environment")

    monkeypatch.setattr(driver, "run_live", live)
    record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.example", "admin-secret")
    try:
        env = seen["env"]
        for key in env:
            assert not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_REQUEST_", "HONUA_AWS_"))
            assert key not in {"GITHUB_TOKEN", "GH_TOKEN"}
        assert "aws-secret" not in env.values()
        assert env["HONUA_CLOUD_JOURNEY_ADMIN"] == "admin-secret"
        assert "PATH" in env
        assert os.environ["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
        assert "HONUA_CLOUD_JOURNEY_ADMIN" not in os.environ
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        assert record["runId"] == "cloudscrub4242" and record["runAttempt"] == "7"
        assert {stage["evidence"]["uri"] for stage in receipt["stages"]} == {
            "urn:honua:cloud:aws-ecs/redis-off#honua-run=cloudscrub4242/7"
        }
        assert cj.validate_attempt(record, receipt, "aws-ecs/redis-off",
                                   run_id="cloudscrub4242", run_attempt="7") is False
        receipt["stages"][0]["evidence"]["uri"] = "urn:honua:cloud:aws-ecs/redis-off#honua-run=999/7"
        import pytest
        with pytest.raises(ValueError, match="wrong run"):
            cj.validate_attempt(record, receipt, "aws-ecs/redis-off",
                                run_id="cloudscrub4242", run_attempt="7")
    finally:
        import shutil
        shutil.rmtree(cj.EVIDENCE / "cloudscrub4242", ignore_errors=True)


def test_imported_journey_runs_inside_provision_teardown_and_records_each_attempt(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, adapter = cj.drivers()
    assert driver.__file__ == str(cj.HERE / "run.py")
    assert adapter.__file__ == str(cj.HERE / "live_driver.py")
    events = []
    stub = _ServingStub()
    def extended(endpoint, **kwargs):
        events.append("extended")
        assert endpoint == stub.endpoint and kwargs["target"] is stub
        return [cc.CheckResult(name, "pass") for name in cloud_driver.DRIVERS]
    monkeypatch.setattr(run_cloud, "run_extended", extended)
    monkeypatch.setattr(stub, "provision", lambda **kwargs: (events.append("provision") or stub.endpoint))
    monkeypatch.setattr(stub, "teardown", lambda **kwargs: events.append("teardown"))
    monkeypatch.setattr(run_cloud, "REGISTRY", {"aws-ecs": lambda **kwargs: stub})
    monkeypatch.setattr(run_cloud, "make_fetch", lambda **kwargs: lambda url: cc.HttpResponse(200, ""))
    monkeypatch.setattr(run_cloud, "run_canonical", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud.canary_probes, "run_canary", lambda *a, **k: [])
    def live(target, manifest, journey, workdir, endpoint, keep_stack):
        events.append("journey")
        assert endpoint == stub.endpoint and keep_stack is True
        assert os.environ["HONUA_CLOUD_JOURNEY_ADMIN"] == stub.admin_api_key
        assert manifest == cj.manifest()
        workdir.mkdir(parents=True)
        (workdir / "created-by-journey").write_text("cleanup fixture")
        raise RuntimeError("simulated journey failure")
    monkeypatch.setattr(driver, "run_live", live)
    def cost(*args, **kwargs):
        events.append("cost")
        return {"status": "pass"}
    monkeypatch.setattr(cj, "check_cost", cost)
    report = run_cloud.run("aws-ecs", True, None)
    assert events == ["provision", "extended", "journey", "journey", "cost", "teardown"]
    assert report["status"] == "fail" and len(report["journeyAttempts"]) == 2
    for record in report["journeyAttempts"]:
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        driver.validate_receipt(receipt, cj.HERE / "receipt.schema.json")
        assert record["failureAttribution"] == "infrastructure"
        assert stub.admin_api_key not in json.dumps(receipt)
    assert not list(cj.cell_dir("aws-ecs/redis-off").glob("work-*"))


def test_run_cost_ceiling_is_read_before_teardown_even_when_exceeded(monkeypatch):
    monkeypatch.setattr(run_cloud, "run_extended", lambda *a, **k: [])
    cj = run_cloud.cloud_journey
    events = []
    stub = _StubTarget(provision_error="apply failed")
    monkeypatch.setattr(stub, "teardown", lambda **kwargs: events.append("teardown"))
    monkeypatch.setattr(run_cloud, "REGISTRY", {"aws-ecs": lambda **kwargs: stub})
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "run-cost.json"
        def cost(report_path, ceiling, *, started_at):
            events.append("cost")
            path.write_text(json.dumps({"runId": os.environ.get("GITHUB_RUN_ID", "local"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "currency": "USD",
                "scope": "run", "measuredAt": cj.now(), "amount": "20.01"}))
            return original_cost(report_path, ceiling, started_at=started_at)
        original_cost = cj.check_cost
        monkeypatch.setattr(cj, "check_cost", cost)
        report = run_cloud.run("aws-ecs", True, None, cost_report=path, cost_ceiling_usd="20")
        assert report["status"] == "fail" and report["cost"]["status"] == "fail"
        assert "exceeds ceiling" in report["why"] and events == ["cost", "teardown"]
        events.clear()
        monkeypatch.setattr(cj, "check_cost", original_cost)
        path.unlink()
        report = run_cloud.run("aws-ecs", True, None, cost_report=path)
        assert report["status"] == "fail" and events == ["teardown"]


def test_reaper_covers_journey_workspace_and_attempts_destroy_after_cleanup_error(monkeypatch):
    import reap_cloud
    cj = run_cloud.cloud_journey
    cell = "aws-ecs/redis-off"
    workdir = cj.cell_dir(cell) / "work-1"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "client-artifact").write_text("fixture")
    cj.cleanup(cell)
    assert not workdir.exists()
    events = []
    monkeypatch.setattr(sys, "argv", ["reap_cloud.py", "--target", "aws-ecs", "--redis", "off"])
    monkeypatch.setattr(reap_cloud, "REGISTRY", {"aws-ecs": lambda **kwargs: object()})
    monkeypatch.setattr(reap_cloud, "reap", lambda *a, **k: events.append("destroy"))
    with mock.patch.object(cj, "cleanup", side_effect=OSError("cleanup fixture")):
        try:
            reap_cloud.main()
        except OSError:
            pass
        else:
            raise AssertionError("cleanup must fail closed")
    assert events == ["destroy"]


def test_cloud_workflow_requires_only_four_ga_cells_and_runs_preview():
    import yaml
    cj = run_cloud.cloud_journey
    workflow = yaml.safe_load((E2E_DIR.parent / ".github/workflows/e2e-cloud-aws.yml").read_text())
    job = workflow["jobs"]["parity"]
    assert len(cj.GA_CELLS) == 4 and all("eks" not in c and "mixed" not in c for c in cj.GA_CELLS)
    assert "aws-eks" in job["strategy"]["matrix"]["target"]
    assert "aws-mixed" in job["strategy"]["matrix"]["target"]
    assert job["continue-on-error"] == "${{ matrix.target == 'aws-eks' || matrix.target == 'aws-mixed' }}"
    assert job["strategy"]["fail-fast"] is False
    assembly = next(step["run"] for step in workflow["jobs"]["cloud-report"]["steps"] if step.get("id") == "assemble")
    assert "python e2e/cloud_journey.py --reports reports" in assembly
    assert "PARITY_RESULT" not in assembly and "IAC_LIVE_RESULT" not in assembly


def test_cloud_full_scope_preview_failure_cannot_redden_a_passing_ga_run():
    cj = run_cloud.cloud_journey
    # Isolate matrix verdict policy from the owned driver's currently ECS-only schema. Actual
    # schema/candidate/staleness rejection is exercised above using the unmocked ECS validator.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reports = []
        for cell in cj.GA_CELLS:
            subdir = root / cell.replace("/", "-")
            subdir.mkdir()
            reports.append(_cell_report(subdir, _ecs_receipt(cell)))
        previews = [{"cell": f"{target}/redis-{redis}", "status": "fail"}
                    for target in cj.PREVIEW_TARGETS for redis in ("off", "on")]
        with mock.patch.object(cj, "validate_attempt", return_value=True):
            result = _aggregate_fixture([*reports, *previews], root, full_scope=True)
            assert result["status"] == "pass" and result["certifying"] is True
            previews[0]["cost"] = {**reports[0]["cost"], "status": "fail", "amountUsd": "20.01"}
            over_budget = _aggregate_fixture([*reports, *previews], root, full_scope=True)
            assert over_budget["status"] == "fail" and "run cost ceiling exceeded" in over_budget["why"]
            del previews[0]["cost"]
            reports[0]["status"] = "fail"
            assert _aggregate_fixture([*reports, *previews], root, full_scope=True)["status"] == "fail"


def test_cloud_full_scope_requires_a_final_run_cost_reading_after_every_cloud_job():
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reports = []
        for cell in cj.GA_CELLS:
            subdir = root / cell.replace("/", "-")
            subdir.mkdir()
            reports.append(_cell_report(subdir, _ecs_receipt(cell)))
        with mock.patch.object(cj, "validate_attempt", return_value=True):
            # Every per-cell reading is under the ceiling; the later whole-run reading is not.
            over = cj.aggregate(reports, root, require_real=True, full_scope=True,
                                run_id="offline-run", run_attempt="2",
                                final_cost=_final_cost("20.01", "fail"))
            assert over["status"] == "fail" and "final run cost" in over["why"]
            missing = cj.aggregate(reports, root, require_real=True, full_scope=True,
                                   run_id="offline-run", run_attempt="2")
            assert missing["status"] == "fail" and "final run cost evidence missing" in missing["why"]
            assert _aggregate_fixture(reports, root, full_scope=True)["status"] == "pass"


def test_cloud_report_cli_reads_the_final_cost_after_aggregation_started(monkeypatch):
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cost = root / "run-cost.json"
        output = root / "out.json"
        started = datetime.now(timezone.utc)
        cost.write_text(json.dumps({"runId": os.environ.get("GITHUB_RUN_ID", "local"),
            "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "currency": "USD",
            "scope": "run", "amount": "3",
            "measuredAt": (started - timedelta(seconds=5)).isoformat()}))
        args = ["cloud_journey.py", "--reports", str(root / "none"), "--output", str(output),
                "--final-cost", str(cost), "--final-cost-after", started.isoformat()]
        monkeypatch.setattr(sys, "argv", args)
        cj.main()
        stale = json.loads(output.read_text())
        assert stale["status"] == "fail" and stale["finalCost"]["status"] == "fail"
        cost.write_text(cost.read_text().replace(
            (started - timedelta(seconds=5)).isoformat(), cj.now()))
        monkeypatch.setattr(sys, "argv", [*args[:-1], (started - timedelta(seconds=1)).isoformat()])
        cj.main()
        assert json.loads(output.read_text())["finalCost"]["amountUsd"] == "3"


def test_cloud_report_validates_receipts_against_the_candidate_overlay():
    import yaml
    workflow = yaml.safe_load((E2E_DIR.parent / ".github/workflows/e2e-cloud-aws.yml").read_text())
    for name in ("parity", "cloud-report"):
        steps = workflow["jobs"][name]["steps"]
        uses = [step.get("uses") for step in steps]
        overlay = uses.index("./.github/actions/candidate-input")
        assert steps[overlay]["with"]["candidate_ref"] == "${{ inputs.candidate_ref }}"
        if name == "cloud-report":
            assemble = next(i for i, step in enumerate(steps) if step.get("id") == "assemble")
            assert overlay < assemble and "--final-cost-after" in steps[assemble]["run"]


def test_self_test_receipts_never_enter_the_uploaded_evidence_tree():
    cj = run_cloud.cloud_journey
    production = E2E_DIR / "cloud-evidence"
    assert not cj.cell_dir("aws-ecs/redis-off").is_relative_to(production)
    assert cj.cell_dir("aws-ecs/redis-off").is_relative_to(E2E_DIR)  # receipts stay e2e-relative


def _ecs_aws(tasks, *, task_status="RUNNING"):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[2] == "list-tasks":
            body = {"taskArns": [task["taskArn"] for task in tasks]}
        else:
            body = {"tasks": [{**task, "lastStatus": task_status} for task in tasks]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
    return run, calls


class _EcsOutputs:
    _workdir = Path("/iac")

    def _tf(self, root, *args):
        return subprocess.CompletedProcess(args, 0, {"ecs_cluster_name": "c1",
            "ecs_service_name": "s1"}[args[-1]], "")


def test_ecs_running_image_is_read_back_from_every_running_task():
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    server = pinned["components"]["honua-server"]
    good = {"taskArn": "t1", "containers": [
        {"image": f"{server['image']}@{server['digest']}", "imageDigest": server["digest"],
         "lastStatus": "RUNNING"},
        {"image": "public.ecr.aws/aws-observability/aws-otel-collector:v1", "imageDigest": "sha256:" + "0" * 64,
         "lastStatus": "RUNNING"}]}
    run, calls = _ecs_aws([good])
    assert cj.observed_ecs_image(_EcsOutputs(), pinned, run=run) == f"{server['image']}@{server['digest']}"
    assert calls[0][:3] == ["aws", "ecs", "list-tasks"] and "s1" in calls[0] and "c1" in calls[1]
    stale = {"taskArn": "t2", "containers": [{**good["containers"][0], "imageDigest": "sha256:" + "1" * 64}]}
    for tasks, status in (([good, stale], "RUNNING"), ([], "RUNNING"), ([good], "PENDING"),
                          ([{"taskArn": "t3", "containers": good["containers"][1:]}], "RUNNING")):
        run, _ = _ecs_aws(tasks, task_status=status)
        assert cj.observed_ecs_image(_EcsOutputs(), pinned, run=run) is None


def test_external_ecs_image_reaches_the_owned_candidate_image_check(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    seen = []
    original = driver.observe

    def observe(target, base_url, workspace, bindir, image_ref, expected_revision):
        seen.append(image_ref)
        raise RuntimeError("stop after observation")
    monkeypatch.setattr(driver, "observe", observe)
    monkeypatch.setattr(driver.pins, "resolve_client_workspace", lambda *a, **k:
        driver.pins.ClientWorkspace(status="blocked", root=None, reason="fixture"))
    for image in ("ghcr.io/honua-io/honua-server@sha256:" + "a" * 64, None):
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.invalid", "key", image)
        assert record["failureAttribution"] == "infrastructure"
    assert seen == ["ghcr.io/honua-io/honua-server@sha256:" + "a" * 64, None]
    assert driver.observe is observe and original is not observe
    cj.cleanup("aws-ecs/redis-off")


def test_cloud_invalid_schema_wrong_cell_and_tampered_receipt_fail():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        receipt = _ecs_receipt()
        receipt["stages"].pop()
        assert _aggregate_fixture([_cell_report(root, receipt)], root)["status"] == "fail"
        report = _cell_report(root, _ecs_receipt())
        report["cell"] = "aws-ecs/redis-on"
        assert "wrong candidate" in _aggregate_fixture([report], root)["why"]
        report = _cell_report(root, _ecs_receipt())
        (root / "receipt-1.json").write_text("{}")
        assert "digest mismatch" in _aggregate_fixture([report], root)["why"]
        receipt = _ecs_receipt()
        receipt["roster"]["status"] = "blocked"
        assert "roster evidence" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]
        receipt = _ecs_receipt()
        receipt["stages"][3]["evidence"]["uri"] = "urn:test:cloud#honua-run=other-run/2"
        assert "wrong run" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]


def test_cost_meter_rejects_stale_wrong_run_and_nonfinite_amount():
    cj = run_cloud.cloud_journey
    import pytest
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cost.json"
        started = datetime.now(timezone.utc) - timedelta(minutes=1)
        base = {"runId": os.environ.get("GITHUB_RUN_ID", "local"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "currency": "USD",
                "scope": "run", "measuredAt": cj.now(), "amount": "20"}
        path.write_text(json.dumps(base))
        assert cj.check_cost(path, "20", started_at=started)["status"] == "pass"
        for change in ({"runId": "another-run"}, {"scope": "cell"}, {"currency": "EUR"},
                       {"amount": "NaN"}, {"amount": "-1"},
                       {"measuredAt": (started - timedelta(seconds=1)).isoformat()}):
            path.write_text(json.dumps({**base, **change}))
            with pytest.raises(ValueError):
                cj.check_cost(path, "20", started_at=started)
        path.write_text(json.dumps(base))
        for ceiling in ("0", "-1", "NaN"):
            with pytest.raises(ValueError):
                cj.check_cost(path, ceiling, started_at=started)


def test_cost_and_teardown_still_run_when_receipt_persistence_fails(monkeypatch):
    cj = run_cloud.cloud_journey
    events = []
    stub = _StubTarget(provision_error="apply failed")
    monkeypatch.setattr(stub, "teardown", lambda **kwargs: events.append("teardown"))
    monkeypatch.setattr(run_cloud, "REGISTRY", {"aws-ecs": lambda **kwargs: stub})
    def meter(*args, **kwargs):
        events.append("cost")
        return {"status": "pass"}
    monkeypatch.setattr(cj, "check_cost", meter)
    with mock.patch.object(cj, "attempt", side_effect=OSError("disk full")):
        report = run_cloud.run("aws-ecs", True, None)
    assert report["status"] == "fail" and events == ["cost", "teardown"]
    assert "receipt evidence unavailable" in report["why"]



def _driver_contract_fixture(monkeypatch, directory, *, blocked=False, missing=False, crash=False, ready=True):
    """Exercise the real shell report assembler with controlled subprocess verdicts."""
    def seed(endpoint, key, target, out):
        assert endpoint == 'https://cell.example.invalid' and key == target.admin_api_key
        (out / 'seed-manifest.json').write_text('{}')
    monkeypatch.setattr(cloud_driver, 'seed', seed)
    real_run = subprocess.run
    invoked = []
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'cloud-secret-must-not-reach-drivers')
    monkeypatch.setenv('GH_TOKEN', 'dispatch-token-must-not-reach-drivers')
    def process(command, **kwargs):
        if command[1] == '-c':
            return real_run(command, **kwargs)
        env = kwargs['env']
        assert not any(k.startswith('AWS_') or k in ('GH_TOKEN', 'GITHUB_TOKEN') for k in env)
        assert env['E2E_BASE'] == 'https://cell.example.invalid'
        assert env['E2E_API_KEY'] == 'test-app-key'
        driver = Path(command[1]).parent.name
        invoked.append(driver)
        rows = next(expected for name, (d, expected) in cloud_driver.DRIVERS.items() if d == driver)
        with (Path(directory) / 'scenarios.jsonl').open('a') as output:
            for scenario in rows:
                if missing and scenario == 'S2-mcp-tool-catalog':
                    continue
                json.dump({'scenario': scenario, 'status': 'blocked' if blocked and driver == 'demos' else 'pass',
                    'why': 'CSP excludes AWS origin' if blocked and driver == 'demos' else 'assertions passed',
                    'evidence': {'credentialEcho': env['E2E_API_KEY']}}, output)
                output.write('\n')
        return subprocess.CompletedProcess(command, 1 if crash and driver == 'gp' else 0)
    monkeypatch.setattr(cloud_driver.subprocess, 'run', process)
    target = _ServingStub()
    target.admin_api_key = 'test-app-key'
    results = cloud_driver.run_extended('https://cell.example.invalid', target=target,
                                        out=directory, ready=ready, require_real=True)
    assert invoked == ['mcp', 'studio', 'gp', 'demos']
    report = json.loads((Path(directory) / 'gate-report.json').read_text())
    assert 'test-app-key' not in json.dumps(report)
    return results, report


def test_cloud_driver_rows_and_strict_local_report_are_preserved(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        results, report = _driver_contract_fixture(monkeypatch, directory)
        assert all(result.status == 'pass' for result in results)
        assert report['status'] == 'pass' and report['require_real'] is True
        assert len(report['scenarios']) == 10


def test_cloud_driver_missing_row_and_crash_cannot_disappear(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        results, report = _driver_contract_fixture(monkeypatch, directory, missing=True, crash=True)
        assert results[0].status == results[2].status == 'fail'
        assert report['status'] == 'fail'
        assert any(row['scenario'] == 'S2-mcp-tool-catalog' and row['status'] == 'fail'
                   for row in report['scenarios'])


def test_cloud_driver_genuine_block_remains_visible_but_strict_report_fails(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        results, report = _driver_contract_fixture(monkeypatch, directory, blocked=True)
        assert results[-1].status == 'blocked'
        assert report['status'] == 'fail' and report['summary']['blocked'] == 6


def test_cloud_driver_report_preserves_failed_cell_readiness(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        _results, report = _driver_contract_fixture(monkeypatch, directory, ready=False)
        assert report['server']['booted'] is False
        assert report['boot']['failed'] is True and report['status'] == 'fail'


def test_cloud_seed_uses_cell_secret_without_passphrase_in_process_args(monkeypatch):
    target = ecs(run_id='seed-test')
    target._workdir = Path('/tmp/cell')
    connection = 'Host=cell-db;Port=5432;Database=honua;Username=honua;Password=private-password;SSL Mode=Require'
    state = {'values': {'root_module': {'child_modules': [{'resources': [{
        'type': 'aws_secretsmanager_secret_version', 'name': 'db_connection',
        'values': {'secret_string': connection}}]}]}}}
    monkeypatch.setattr(target, '_tf', lambda *args: subprocess.CompletedProcess(args, 0, json.dumps(state)))
    def psql(command, **kwargs):
        assert command == ['psql', '-v', 'ON_ERROR_STOP=1']
        assert kwargs['env']['PGPASSWORD'] == 'private-password'
        assert kwargs['env']['PGSSLMODE'] == 'require'
        assert kwargs['input'] == 'SELECT 1'
        assert not any(k.startswith('AWS_') for k in kwargs['env'])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', psql)
    result = target.seed_database('SELECT 1')
    assert result['host'] == 'cell-db' and result['password'] == 'private-password'

if __name__ == "__main__":
    import traceback

    class _MP:
        def __init__(self):
            self.restores = []

        def delenv(self, k, raising=True):
            previous = os.environ.get(k)
            self.restores.append(lambda: os.environ.pop(k, None) if previous is None
                                 else os.environ.__setitem__(k, previous))
            os.environ.pop(k, None)

        def setenv(self, k, v):
            previous = os.environ.get(k)
            self.restores.append(lambda: os.environ.pop(k, None) if previous is None
                                 else os.environ.__setitem__(k, previous))
            os.environ[k] = v

        def setattr(self, obj, name, value):
            previous = getattr(obj, name)
            self.restores.append(lambda: setattr(obj, name, previous))
            setattr(obj, name, value)

        def undo(self):
            for restore in reversed(self.restores):
                restore()

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            patches = _MP()
            try:
                fn(patches) if "monkeypatch" in fn.__code__.co_varnames else fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            finally:
                patches.undo()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
