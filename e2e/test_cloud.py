"""Tests for the cross-cloud parity tier.

The cloud gate must (a) compare targets correctly, (b) classify each canonical check correctly, and
(c) report BLOCKED — never a fake green — when the AWS infra isn't wired. All proven here with no
cloud, no terraform, no live server (injected fetchers + an unset environment).

Run: python -m pytest e2e/test_cloud.py    (or: python e2e/test_cloud.py)
"""
from __future__ import annotations

import contextlib
import io
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

import pytest

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

# The provision job runs this self-test with the live run's GITHUB_RUN_ID before the live cell, and
# the cell uploads e2e/cloud-evidence/**/receipt-*.json. Fixture receipts must never land in that tree.
SELFTEST_EVIDENCE = E2E_DIR / ".cloud-evidence-selftest" / str(os.getpid())
run_cloud.cloud_journey.EVIDENCE = SELFTEST_EVIDENCE
atexit.register(shutil.rmtree, SELFTEST_EVIDENCE.parent, True)

_AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_ROLE_ARN", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "HONUA_LAMBDA_IMAGE_URI", "HONUA_ECS_IMAGE", "HONUA_IAC_DIR", "HONUA_HELM_DIR",
            "HONUA_AWS_DB_INGRESS_CIDR", "HONUA_LAMBDA_ARCHITECTURE", "HONUA_ECS_ARCHITECTURE",
            "HONUA_AWS_RUNNER_CIDR", "HONUA_GP_BATCH_IMAGE", "HONUA_MIGRATE_IMAGE",
            "HONUA_ENABLE_BEDROCK_AI")

TEST_ADMIN_PASSWORD = "Test-Cell-Aa1!" + "x" * 24


@pytest.fixture(autouse=True)
def _cell_admin_password(monkeypatch):
    # The provision phase mints a per-cell password; the Terraform targets refuse to run without one.
    monkeypatch.setenv("HONUA_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)


# e2e-cloud-aws-cell.yml sets these at workflow level, so they reach its "Self-test the cloud parity
# logic" step too. A live cell's values must never steer a unit test: with HONUA_ENABLE_BEDROCK_AI=true
# (the aws-ecs/redis-off cell) ten provision tests refused before the cell ever provisioned. Every
# test starts without them and sets exactly what it exercises.
_CELL_WORKFLOW_ENV = ("HONUA_LAMBDA_IMAGE_URI", "HONUA_LAMBDA_ARCHITECTURE", "HONUA_ECS_IMAGE",
                      "HONUA_ECS_ARCHITECTURE", "HONUA_GP_BATCH_IMAGE", "HONUA_MIGRATE_IMAGE",
                      "HONUA_ENABLE_BEDROCK_AI", "HONUA_CLOUD_COST_CEILING_USD", "HONUA_RUN_URL",
                      "CELL_DIR", "CELL_ARTIFACT", "HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN",
                      "HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN",
                      "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", "HONUA_AWS_CELL_DNS_ZONE_ID",
                      "HONUA_AWS_CELL_DNS_PARENT")


@pytest.fixture(autouse=True)
def _hermetic_cell_env(monkeypatch):
    for var in _CELL_WORKFLOW_ENV:
        monkeypatch.delenv(var, raising=False)


def test_self_test_clears_every_cell_workflow_env_var():
    import yaml
    workflow = E2E_DIR.parent / ".github/workflows/e2e-cloud-aws-cell.yml"
    cell = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    leaked = {name for name in cell["env"] if name.startswith(("HONUA_", "CELL_"))} - set(_CELL_WORKFLOW_ENV)
    assert not leaked, f"cell workflow env reaches the self-test unreset: {sorted(leaked)}"


def test_bedrock_flag_from_the_cell_env_does_not_reach_a_test():
    assert "HONUA_ENABLE_BEDROCK_AI" not in os.environ

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


def test_admin_password_is_never_derived_from_the_public_run_id(monkeypatch):
    monkeypatch.delenv("HONUA_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    for factory in (serverless, ecs):
        target = factory(run_id="r1")
        with pytest.raises(ProvisionError, match="HONUA_ADMIN_PASSWORD is unset"):
            target.admin_api_key
        with pytest.raises(ProvisionError, match="HONUA_ADMIN_PASSWORD is unset"):
            target._vars(False)
    # Teardown on a fresh runner has no password: destroy gets a random throwaway, not one from r1.
    first = _tf_vars(serverless(run_id="r1")._vars(False, destroy=True))["honua_admin_password"]
    second = _tf_vars(serverless(run_id="r1")._vars(False, destroy=True))["honua_admin_password"]
    assert first != second and "r1" not in first
    for password in (first, TEST_ADMIN_PASSWORD):
        assert len(password) >= 32
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(not c.isalnum() for c in password)


def test_ephemeral_admin_password_meets_iac_contract(monkeypatch):
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    password = _tf_vars(serverless(run_id="r1")._vars(False))["honua_admin_password"]
    assert password == TEST_ADMIN_PASSWORD
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


def _patch_seam(monkeypatch, drivers, seed=lambda *a, **k: None):
    # The seed runs in the credentialed provision job; the drivers in the credential-free journey job.
    monkeypatch.setattr(run_cloud, "seed_cell", seed)
    monkeypatch.setattr(run_cloud, "run_drivers", drivers)


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
        _patch_seam(monkeypatch, lambda *a, **k: extended if extended is not None else
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
        for key in ("operationId", "operationInstanceId", "correlationId", "auditId", "proposalId",
                    "jobId", "resourceUri", "jobStatus", "jobCreatedAt"):
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
        receipt["server"]["sourceSha"] = "b" * 40
        assert "wrong candidate" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]
        receipt["server"]["sourceSha"] = cj.UNOBSERVED_SHA
        assert "not observed" in _aggregate_fixture([_cell_report(root, receipt)], root)["why"]
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
        assert [r["evidenceTier"] for r in result["cells"]] == ["GA", *(["Preview"] * len(cj.PREVIEW_TARGETS))]
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
    sha, image = _manifest_server()
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: {"revision": sha, "source": "commit-sha"})
    record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.example", "admin-secret", image)
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
    def seed(endpoint, **kwargs):
        events.append("seed")
        assert endpoint == stub.endpoint and kwargs["target"] is stub
    def extended(endpoint, **kwargs):
        events.append("extended")
        assert endpoint == stub.endpoint and kwargs["admin_key"] == stub.admin_api_key
        return [cc.CheckResult(name, "pass") for name in cloud_driver.DRIVERS]
    _patch_seam(monkeypatch, extended, seed)
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
    # The cell reports the exact candidate, so each failed attempt is an attributed failure.
    sha, image = _manifest_server()
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: {"revision": sha, "source": "commit-sha"})
    monkeypatch.setattr(cj, "observed_ecs_image", lambda *a, **k: image)
    def cost(*args, **kwargs):
        events.append("cost")
        return {"status": "pass"}
    monkeypatch.setattr(cj, "check_cost", cost)
    report = run_cloud.run("aws-ecs", True, None)
    assert events == ["provision", "seed", "extended", "journey", "journey", "cost", "teardown"]
    assert report["status"] == "fail" and len(report["journeyAttempts"]) == 2
    for record in report["journeyAttempts"]:
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        driver.validate_receipt(receipt, cj.HERE / "receipt.schema.json")
        assert record["failureAttribution"] == "infrastructure"
        assert stub.admin_api_key not in json.dumps(receipt)
    assert not list(cj.cell_dir("aws-ecs/redis-off").glob("work-*"))


def test_run_cost_ceiling_is_read_before_teardown_even_when_exceeded(monkeypatch):
    _patch_seam(monkeypatch, lambda *a, **k: [])
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


def _cloud_workflows():
    import yaml
    workflows = E2E_DIR.parent / ".github/workflows"
    return (yaml.safe_load((workflows / "e2e-cloud-aws.yml").read_text()),
            yaml.safe_load((workflows / "e2e-cloud-aws-cell.yml").read_text()))


def test_cloud_workflow_requires_only_four_ga_cells_and_runs_preview():
    cj = run_cloud.cloud_journey
    workflow, cell = _cloud_workflows()
    job = workflow["jobs"]["parity"]
    assert len(cj.GA_CELLS) == 4 and all("eks" not in c and "mixed" not in c for c in cj.GA_CELLS)
    assert "aws-eks" in job["strategy"]["matrix"]["target"]
    # rc.3: examples/aws-mixed does not exist; the cell is out of the matrix until honua-iac#209.
    assert "aws-mixed" not in job["strategy"]["matrix"]["target"]
    assert "aws-mixed" not in workflow[True]["workflow_dispatch"]["inputs"]["target"]["options"]
    assert job["with"]["preview"] == "${{ matrix.target == 'aws-eks' }}"
    assert job["strategy"]["fail-fast"] is False and job["strategy"]["max-parallel"] == 2
    # Preview tolerance lives on the called cell's jobs; a reusable-workflow call cannot carry it.
    assert all(cell_job["continue-on-error"] == "${{ inputs.preview }}" for cell_job in cell["jobs"].values())
    assembly = next(step["run"] for step in workflow["jobs"]["cloud-report"]["steps"] if step.get("id") == "assemble")
    assert "python e2e/cloud_journey.py --reports reports" in assembly
    assert "PARITY_RESULT" not in assembly and "IAC_LIVE_RESULT" not in assembly


def test_cloud_driver_clients_install_without_scripts_root_or_cloud_credentials():
    _, cell = _cloud_workflows()
    journey = cell["jobs"]["journey"]
    steps = journey["steps"]
    names = [step.get("name") for step in steps]
    script = steps[names.index("Install pinned extended driver clients")]["run"]
    # npm code never runs lifecycle scripts or as root; root installs only the reviewed distro
    # package list. The job itself holds no OIDC permission and no AWS credential (#381).
    assert journey["permissions"] == {"contents": "read"}
    assert not any("configure-aws-credentials" in str(step.get("uses", "")) for step in steps)
    assert "--with-deps" not in script and "install-deps" not in script
    assert all("sudo" not in line for line in script.splitlines() if "npm" in line or "node" in line)
    assert "npm install" in script and "--ignore-scripts" in script
    # The admit job opens the cell to this runner's address and seals the application key to this
    # runner's own public key; both are published before any pip/npm code runs.
    publish = names.index("Upload this runner's address and public key")
    assert publish < names.index("Install journey runner dependencies")
    assert publish < names.index("Install pinned extended driver clients")
    assert publish < names.index("Run the ${{ inputs.target }} / redis-${{ inputs.redis }} journey")


def test_demo_csp_block_cites_the_cell_hostname_issue_not_format_samples():
    driver = (E2E_DIR / "drivers/demos/run.sh").read_text()
    csp_block = next(line for line in driver.splitlines() if "CSP does not allow" in line)
    assert "honua-release#450" in csp_block and "#35" not in csp_block
    assert "--ignore-scripts" in driver


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


def test_cloud_report_names_a_missing_final_meter_reading_and_fails_it_only_when_strict(monkeypatch):
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "out.json"
        missing = root / "run-cost.json"
        monkeypatch.setattr(sys, "argv", ["cloud_journey.py", "--reports", str(root / "none"),
            "--output", str(output), "--final-cost", str(missing), "--final-cost-after", cj.now()])
        for strict, status in (("false", "blocked"), ("true", "fail")):
            monkeypatch.setenv("REQUIRE_REAL", strict)
            cj.main()
            report = json.loads(output.read_text())
            assert report["status"] == status and report["finalCost"]["status"] == "unavailable"
            assert str(missing) in report["why"]


def test_cloud_report_validates_receipts_against_the_candidate_overlay():
    workflow, cell = _cloud_workflows()
    jobs = [("cloud-report", workflow["jobs"]["cloud-report"])] + [
        (name, cell["jobs"][name]) for name in ("provision", "journey", "teardown")]
    for name, job in jobs:
        steps = job["steps"]
        uses = [step.get("uses") for step in steps]
        overlay = uses.index("./.github/actions/candidate-input")
        assert steps[overlay]["with"]["candidate_ref"] == "${{ inputs.candidate_ref }}"
        if name == "cloud-report":
            assemble = next(i for i, step in enumerate(steps) if step.get("id") == "assemble")
            assert overlay < assemble and "--final-cost-after" in steps[assemble]["run"]
    assert workflow["jobs"]["parity"]["with"]["candidate_ref"] == "${{ inputs.candidate_ref }}"


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



# ---- honua-release#381: the receipt's server is observed, and the cell runs as three jobs -----------
def _manifest_server():
    server = run_cloud.cloud_journey.manifest()["components"]["honua-server"]
    return server["sha"], f"{server['image']}@{server['digest']}"


def test_live_receipt_server_is_observed_not_copied_from_the_manifest(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    sha, image = _manifest_server()
    # The fixture receipt is bound to offline-run/2.
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    workspace = driver.pins.ClientWorkspace(status="pass", root=None, reason="")
    monkeypatch.setattr(driver, "run_live", lambda *a, **k: (workspace, [], [], None))
    fixture = json.dumps(_ecs_receipt())
    monkeypatch.setattr(driver, "build_receipt", lambda **kwargs: json.loads(fixture))
    identities = iter([{"revision": sha, "source": "commit-sha"},
                       {"revision": "b" * 40, "source": "commit-sha"}, None])
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: next(identities))
    try:
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.invalid", "key", image)
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        assert receipt["server"] == {"sourceSha": sha, "image": image}
        assert record["observedServer"] == {"revision": sha, "source": "commit-sha"}
        # The cell advertises another commit: the receipt says so, and cannot pass.
        record = cj.attempt("aws-ecs/redis-off", 2, "https://cell.invalid", "key", image)
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        assert receipt["server"]["sourceSha"] == "b" * 40
        import pytest
        with pytest.raises(ValueError, match="wrong candidate"):
            cj.validate_attempt(record, receipt, "aws-ecs/redis-off", run_id="offline-run", run_attempt="2")
        # Nothing advertised and no control-plane image: visibly unobserved, never the manifest.
        # A passing attempt is refused; a failed one is recorded as the cell's failure, since only
        # ECS reports a running image (serverless, EKS and mixed cells never observe one).
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.invalid", "key", None)
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        assert receipt["server"] == {"sourceSha": cj.UNOBSERVED_SHA, "image": "unobserved"}
        with pytest.raises(ValueError, match="not observed"):
            cj.validate_attempt(record, receipt, "aws-ecs/redis-off", run_id="offline-run", run_attempt="2")
        receipt["status"] = "fail"
        receipt["stages"][0]["status"] = "fail"
        receipt["failure"] = {"number": 1, "stage": receipt["stages"][0]["stage"],
            "command": "fixture", "check": "fixture", "detail": "infrastructure fixture"}
        failed = {**record, "failureAttribution": "infrastructure"}
        assert cj.validate_attempt(failed, receipt, "aws-ecs/redis-off",
                                   run_id="offline-run", run_attempt="2") is False
        receipt["server"] = {"sourceSha": sha, "image": "unobserved"}
        assert cj.validate_attempt(failed, receipt, "aws-ecs/redis-off",
                                   run_id="offline-run", run_attempt="2") is False
        # A server observed to be another candidate is refused on a failed attempt too.
        receipt["server"] = {"sourceSha": sha, "image": image.rsplit("@", 1)[0] + "@sha256:" + "9" * 64}
        with pytest.raises(ValueError, match="wrong candidate"):
            cj.validate_attempt(failed, receipt, "aws-ecs/redis-off", run_id="offline-run", run_attempt="2")
        # A driver that raised before any stage still reports what the reached cell advertised.
        monkeypatch.setattr(driver, "run_live", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        identities = iter([{"revision": "b" * 40, "source": "commit-sha"}])
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.invalid", "key", image)
        receipt = json.loads((E2E_DIR / record["receipt"]).read_text())
        assert record["failureAttribution"] == "infrastructure" and receipt["server"]["sourceSha"] == "b" * 40
    finally:
        shutil.rmtree(cj.EVIDENCE / "offline-run", ignore_errors=True)


def test_driver_exception_receipt_names_type_message_step_and_last_http(monkeypatch, capsys):
    """A raising imported driver leaves its exception, step and last HTTP exchange in the receipt
    and the job log, with credentials redacted (aws-ecs/redis-off run 38005014995 kept only the type)."""
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    discovery = sys.modules["discovery"]
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: None)
    secret = "hk_live_cell_application_key_value"

    def live(*args, **kwargs):
        discovery.mark_step("open candidate transport")
        discovery.record_http("POST", "https://cell.invalid/mcp", 401,
            json.dumps({"error": "denied", "apiKey": secret, "detail": "nope"}).encode(), "tools/list")
        raise discovery.DiscoveryError(f"credential-bearing HTTP must use loopback ({secret})")

    monkeypatch.setattr(driver, "run_live", live)
    try:
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.invalid", secret, None)
        raw = (E2E_DIR / record["receipt"]).read_text()
        receipt = json.loads(raw)
        notices = receipt["notices"]
        assert ("Imported journey driver raised DiscoveryError: credential-bearing HTTP must use loopback "
                "([redacted])") in notices
        assert "Imported journey driver failure step: open candidate transport" in notices
        http = [n for n in notices if n.startswith("Imported journey driver failure last HTTP:")]
        assert http == ['Imported journey driver failure last HTTP: POST /mcp (tools/list) -> 401; body: '
                        '{"error":"denied","apiKey":"[redacted]","detail":"nope"}']
        assert secret not in raw and record["failureAttribution"] == "infrastructure"
        # Still a failed attempt, never a documented block.
        assert cj.documented_blockers({**receipt, "status": "blocked", "stages": []}) == []
        log = capsys.readouterr().err
        assert "DiscoveryError: credential-bearing HTTP must use loopback" in log
        assert "step: open candidate transport" in log and "-> 401" in log and secret not in log
        # A raise before any instrumented request says so instead of inventing an exchange.
        def early(*args, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(driver, "run_live", early)
        record = cj.attempt("aws-ecs/redis-off", 2, "https://cell.invalid", secret, None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert "Imported journey driver raised RuntimeError: boom" in notices
        assert "Imported journey driver failure last HTTP: none recorded" in notices
    finally:
        shutil.rmtree(cj.EVIDENCE / "offline-run", ignore_errors=True)


def test_attempt_admits_plain_http_only_to_the_provisioned_cell_load_balancer(monkeypatch):
    """aws-ecs cells serve plain HTTP on their ALB; the imported driver refused it and raised
    DiscoveryError opening its transport. Only that cell host is admitted, only for the attempt."""
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    assert cj.http_cell_host("http://Cell-1.us-east-1.elb.amazonaws.com/") == "cell-1.us-east-1.elb.amazonaws.com"
    for other in ("https://cell-1.us-east-1.elb.amazonaws.com", "http://example.com", "http://.elb.amazonaws.com",
                  "http://cell.elb.amazonaws.com.evil.example", None):
        assert cj.http_cell_host(other) is None
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "4")
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: None)
    endpoint = "http://cell-1.us-east-1.elb.amazonaws.com"
    seen = []
    workspace = driver.pins.ClientWorkspace(status="blocked", root=None, reason="offline")

    def live(target, pinned, contract, workdir, base_url, keep):
        seen.append((driver.probes.credential_transport(base_url + "/mcp"),
                     driver.probes.credential_transport("http://other.example/mcp")))
        return workspace, None, [], None

    monkeypatch.setattr(driver, "run_live", live)
    try:
        record = cj.attempt("aws-ecs/redis-off", 1, endpoint, "key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert seen == [("http-cell-allowed", None)]
        assert "Candidate transport: http-cell-allowed (cell-1.us-east-1.elb.amazonaws.com)" in notices
        assert not any(n.startswith(cj.DRIVER_ERROR_NOTICE) for n in notices)
        assert driver.probes.credential_transport(endpoint + "/mcp") is None
        # A non-ELB HTTP endpoint is not admitted, so the driver refuses it as before.
        record = cj.attempt("aws-ecs/redis-off", 2, "http://203.0.113.9", "key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert seen[-1] == (None, None) and "Candidate transport: refused (203.0.113.9)" in notices
    finally:
        shutil.rmtree(cj.EVIDENCE / "offline-run", ignore_errors=True)


def test_server_identity_reads_the_anonymous_capability_manifest():
    cj = run_cloud.cloud_journey

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = []

    def opener(url, timeout):
        seen.append(url)
        return _Response(json.dumps({"server": {"deploymentRevision": "c" * 40,
                                                "deploymentRevisionSource": "commit-sha"}}).encode())
    assert cj.server_identity("https://cell.invalid/", opener=opener) == {
        "revision": "c" * 40, "source": "commit-sha"}
    assert seen == ["https://cell.invalid/api/v1/capabilities/manifest"]

    def broken(url, timeout):
        raise OSError("unreachable")
    assert cj.server_identity("https://cell.invalid", opener=broken) is None
    sha, image = _manifest_server()
    digest = image.rsplit("@", 1)[1]
    assert cj.observed_server({"revision": digest, "source": "image-digest"}, image) == {
        "sourceSha": cj.UNOBSERVED_SHA, "image": image}
    assert cj.observed_server({"revision": "sha256:" + "9" * 64, "source": "image-digest"}, image)[
        "image"] == "unobserved"


def _fake_secrets_manager(store):
    real = subprocess.run

    def run(argv, **kwargs):
        if argv[:2] != ["aws", "secretsmanager"]:
            return real(argv, **kwargs)
        name = argv[argv.index("--name" if "--name" in argv else "--secret-id") + 1]
        # Secret material arrives on stdin, never in argv.
        assert not any(value in " ".join(argv) for value in store.values())
        if argv[2] == "create-secret":
            assert name not in store and argv[argv.index("--secret-string") + 1] == "file:///dev/stdin"
            store[name] = kwargs["input"]
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[2] == "get-secret-value":
            return subprocess.CompletedProcess(argv, 0, store[name] + "\n", "")
        if argv[2] == "delete-secret":
            if name not in store:
                return subprocess.CompletedProcess(argv, 254, "", "ResourceNotFoundException")
            del store[name]
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(argv)
    return run


def test_sealed_state_round_trips_and_ships_only_ciphertext():
    import pytest
    store = {}
    run = _fake_secrets_manager(store)
    with tempfile.TemporaryDirectory() as directory:
        root, restored = Path(directory) / "root", Path(directory) / "restored"
        (root / ".terraform").mkdir(parents=True)
        (root / ".terraform" / "provider").write_text("re-fetched by init")
        state = json.dumps({"resources": [{"password": "db-secret-value"}]})
        (root / "terraform.tfstate").write_text(state)
        bundle = Path(directory) / "out" / "state.enc"
        digest = run_cloud.seal_state(root, bundle, "honua-cloud-cell-state/x", run=run)
        assert b"db-secret-value" not in bundle.read_bytes()
        assert json.loads(store["honua-cloud-cell-state/x"])["sha256"] == digest
        run_cloud.open_state(restored, bundle, "honua-cloud-cell-state/x", run=run)
        assert (restored / "terraform.tfstate").read_text() == state
        assert not (restored / ".terraform").exists()
        bundle.write_bytes(bundle.read_bytes() + b"tampered")
        with pytest.raises(ValueError, match="digest"):
            run_cloud.open_state(restored, bundle, "honua-cloud-cell-state/x", run=run)
        run_cloud.forget_secret("honua-cloud-cell-state/x", run=run)
        run_cloud.forget_secret("honua-cloud-cell-state/x", run=run)  # already gone: fine
        assert store == {}


def test_application_key_reaches_the_journey_only_sealed_to_its_own_key():
    store = {}
    run = _fake_secrets_manager(store)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(root / "private.pem")], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(root / "private.pem"), "-pubout",
                        "-out", str(root / "public.pem")], check=True, capture_output=True)
        run_cloud.store_secret("cell-app-key", "Honua-Gate-Aa1!application-key", "fixture", run=run)
        run_cloud.seal_key("cell-app-key", root / "public.pem", root / "key.sealed", run=run)
        assert b"application-key" not in (root / "key.sealed").read_bytes()
        assert run_cloud.open_key(root / "key.sealed", root / "private.pem") == "Honua-Gate-Aa1!application-key"
    # No job output carries it: outputs and step env are printed in the public job log.
    import yaml
    provision = yaml.safe_load((E2E_DIR.parent / ".github/workflows/e2e-cloud-aws-cell.yml").read_text())[
        "jobs"]["provision"]
    assert set(provision["outputs"]) == {"endpoint", "admission"}


class _PhaseStub(_ServingStub):
    admission = "alb"

    def __init__(self, root):
        super().__init__()
        self.root = root

    def _iac_root(self):
        return self.root


def _phase_env(monkeypatch, run_id="phase381"):
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return run_cloud.cloud_journey.cell_dir("aws-ecs/redis-off")


def test_teardown_job_fails_closed_when_provisioned_state_never_arrives(monkeypatch):
    cj = run_cloud.cloud_journey
    directory = _phase_env(monkeypatch)
    with tempfile.TemporaryDirectory() as scratch:
        stub = _PhaseStub(Path(scratch))
        monkeypatch.setattr(run_cloud, "REGISTRY", {**run_cloud.REGISTRY, "aws-ecs": lambda **kw: stub})
        monkeypatch.setattr(run_cloud, "REPORT_PATH", Path(scratch) / "report.json")
        monkeypatch.setattr(cj, "check_cost", lambda *a, **k: {"status": "pass"})
        monkeypatch.setenv("HONUA_CLOUD_PROVISION_MARKER", str(Path(scratch) / "marker"))
        forgotten = []
        monkeypatch.setattr(run_cloud, "forget_secret", lambda name: forgotten.append(name))
        monkeypatch.setattr(run_cloud.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
        try:
            state = {"cell": "aws-ecs/redis-off", "redis": "redis-off", "runId": "phase381",
                     "runAttempt": "1", "candidateDigest": cj.candidate_digest(),
                     "startedAt": cj.now(), "requireReal": True, "provisionAttempted": True,
                     "endpoint": None, "report": {"cell": "aws-ecs/redis-off", "status": "fail",
                                                  "why": "provision failed: apply died",
                                                  "journeyAttempts": []}}
            run_cloud._write_json(directory / run_cloud.HANDOFF_NAME, state)
            argv = ["--phase", "teardown", "--target", "aws-ecs", "--redis", "off"]
            # Provisioning started, but the sealed state was lost: never a silent destroy of nothing.
            assert run_cloud.main(argv) == 1
            report = json.loads(run_cloud.REPORT_PATH.read_text())
            assert stub.torn_down == 0 and "never reached teardown" in report["why"]
            # The application key goes regardless; the state key stays while the cell may still exist.
            assert forgotten == [run_cloud.app_key_secret_name("aws-ecs/redis-off")]
            forgotten.clear()
            # Restored state: destroy, then delete the state key.
            Path(scratch, "marker").write_text("")
            assert run_cloud.main(argv) == 1  # the provision failure still fails the cell
            assert stub.torn_down == 1 and forgotten == [run_cloud.app_key_secret_name("aws-ecs/redis-off"),
                                                         run_cloud.state_secret_name("aws-ecs/redis-off")]
            # A handoff from another run attempt cannot be adopted.
            run_cloud._write_json(directory / run_cloud.HANDOFF_NAME, {**state, "runAttempt": "2"})
            run_cloud.main(argv)
            assert "another run" in json.loads(run_cloud.REPORT_PATH.read_text())["why"]
        finally:
            shutil.rmtree(cj.EVIDENCE / "phase381", ignore_errors=True)


def _journey_upload(directory, receipts, *, run_id="offline-run", run_attempt="2", cell="aws-ecs/redis-off"):
    """A journey job's upload: receipt files plus the journey.json records that name them."""
    cj = run_cloud.cloud_journey
    cell_dir = cj.cell_dir(cell)
    records = []
    for number, receipt in enumerate(receipts, 1):
        data = json.dumps(receipt).encode()
        (directory / f"receipt-{number}.json").write_bytes(data)
        records.append({"number": number, "runId": run_id, "runAttempt": run_attempt,
            "cell": cell, "candidateDigest": cj.candidate_digest(),
            "receipt": str((cell_dir / f"receipt-{number}.json").relative_to(E2E_DIR)),
            "receiptSha256": hashlib.sha256(data).hexdigest(),
            "failureAttribution": None if receipt["status"] == "pass" else "infrastructure"})
    return {"cell": cell, "status": "pass", "journeyAttempts": records}


def test_teardown_reverifies_journey_receipts_and_adopts_no_claimed_verdict(monkeypatch):
    import pytest
    cj = run_cloud.cloud_journey
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    sha, image = _manifest_server()
    state = {"cell": "aws-ecs/redis-off", "observedServer": {"revision": sha, "source": "commit-sha"},
             "runningImage": image}
    failed = _ecs_receipt()
    failed["status"] = "fail"
    failed["stages"][0]["status"] = "fail"
    failed["failure"] = {"number": 1, "stage": failed["stages"][0]["stage"], "command": "fixture",
                         "check": "fixture", "detail": "infrastructure fixture"}
    try:
        with tempfile.TemporaryDirectory() as upload:
            upload = Path(upload)
            # The journey job claims a pass; its receipts say otherwise. Teardown decides.
            journey = _journey_upload(upload, [failed, failed])
            verified = run_cloud.verify_journey(state, journey, upload)
            assert verified["status"] == "fail" and "did not pass" in verified["why"]
            assert (cj.cell_dir("aws-ecs/redis-off") / "receipt-2.json").is_file()
            # A genuine pass is adopted.
            assert run_cloud.verify_journey(state, _journey_upload(upload, [_ecs_receipt()]), upload)[
                "status"] == "pass"
            # Edited bytes, a foreign path, and a server provision never saw are all refused.
            journey = _journey_upload(upload, [_ecs_receipt()])
            (upload / "receipt-1.json").write_text("{}")
            with pytest.raises(ValueError, match="digest"):
                run_cloud.verify_journey(state, journey, upload)
            journey = _journey_upload(upload, [_ecs_receipt()])
            journey["journeyAttempts"][0]["receipt"] = "cloud-evidence/other/receipt-1.json"
            with pytest.raises(ValueError, match="not this cell's"):
                run_cloud.verify_journey(state, journey, upload)
            with pytest.raises(ValueError, match="provision observed"):
                run_cloud.verify_journey({**state, "observedServer": {"revision": "b" * 40,
                                                                      "source": "commit-sha"}},
                                         _journey_upload(upload, [_ecs_receipt()]), upload)
            with pytest.raises(ValueError, match="1..n"):
                run_cloud.verify_journey(state, {"journeyAttempts": [{"number": 2}]}, upload)
            # No control-plane image (every non-ECS cell): the failed attempts are recorded, not rejected.
            unobserved = {**failed, "server": {"sourceSha": sha, "image": "unobserved"}}
            verified = run_cloud.verify_journey({**state, "runningImage": None},
                                                _journey_upload(upload, [unobserved, unobserved]), upload)
            assert verified["status"] == "fail" and "did not pass" in verified["why"]
            assert len(verified["journeyAttempts"]) == 2
            # An upload that claims a pass with no attempt against the provisioned endpoint fails.
            for claimed in ({"status": "pass"}, {}):
                empty = run_cloud.verify_journey({**state, "endpoint": "http://cell.invalid"},
                                                 {"cell": "aws-ecs/redis-off", "journeyAttempts": [],
                                                  "why": "forged", **claimed}, upload)
                assert empty["status"] == "fail" and "no attempt" in empty["why"]
            # A journey that failed before any attempt keeps its own reason.
            empty = run_cloud.verify_journey({**state, "endpoint": "http://cell.invalid"},
                                             {"journeyAttempts": [], "status": "fail",
                                              "why": "ingress admission failed"}, upload)
            assert empty["status"] == "fail" and empty["why"] == "ingress admission failed"
    finally:
        shutil.rmtree(cj.EVIDENCE / "offline-run", ignore_errors=True)


# ---- a journey BLOCKED by tracked limitations is blocked, not FAIL, outside require_real -----------
SERVERLESS_OFF = "aws-serverless/redis-off"


def _blocked_receipt(cell=SERVERLESS_OFF, notices=None):
    """The build-mode receipt attempt() writes when the cell kind is the tracked #377 gap: every
    stage blocked by the contract's named issues, the server identity observed on the cell."""
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    if notices is None:
        notices = ["Imported adapter protocol: terminal-journey-driver-v1",
                   cj.UNSUPPORTED_KIND_NOTICE + " and live evidence source; this contract receipt cannot "
                   "qualify the cell (honua-release#377)."]
    receipt = driver.build_receipt(manifest=cj.manifest(),
        journey=driver.load(cj.HERE / "journey.v1.json"), roster={"status": "pass"},
        evidence_uri="urn:test:cloud#honua-run=offline-run/2", mode="build",
        target={"id": cell, "kind": "none"}, target_path=None, target_base_url="https://cell.invalid",
        workspace=driver.pins.ClientWorkspace(status="blocked", root=None, reason="fixture"),
        stage_results=None, notices=notices)
    receipt["target"]["composeProject"] = None
    sha, _ = _manifest_server()
    receipt["server"] = {"sourceSha": sha, "image": "unobserved"}
    driver.validate_receipt(receipt, cj.HERE / "receipt.schema.json")
    assert receipt["status"] == "blocked"
    return receipt


def test_documented_blockers_name_tracked_limitations_and_refuse_everything_else():
    cj = run_cloud.cloud_journey
    receipt = _blocked_receipt()
    blockers = cj.documented_blockers(receipt)
    assert cj.UNSUPPORTED_KIND_ISSUE in blockers
    contract = {b for stage in receipt["stages"] for b in stage["blockedBy"]}
    assert contract and contract <= set(blockers)
    # A driver that raised is a failure of this run, not a documented limitation.
    raised = _blocked_receipt(notices=["Imported journey driver raised DiscoveryError"])
    assert cj.documented_blockers(raised) == []
    # A blocked stage that names no issue is not documented.
    unnamed = _blocked_receipt()
    unnamed["stages"][2]["blockedBy"] = []
    assert cj.documented_blockers(unnamed) == []
    failed = _blocked_receipt()
    failed["stages"][0]["status"] = "fail"
    assert cj.documented_blockers(failed) == []
    assert cj.documented_blockers(_ecs_receipt()) == []  # a pass is not blocked


def _serverless_state(require_real):
    sha, _ = _manifest_server()
    return {"cell": SERVERLESS_OFF, "redis": "redis-off", "requireReal": require_real,
            "observedServer": {"revision": sha, "source": "commit-sha"}, "runningImage": None,
            "endpoint": "https://cell.invalid"}


def test_teardown_reports_a_documented_journey_block_as_blocked_and_require_real_as_fail(monkeypatch):
    cj = run_cloud.cloud_journey
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    try:
        with tempfile.TemporaryDirectory() as upload:
            upload = Path(upload)
            blocked = _blocked_receipt()
            # The journey job's own claim (pass, or a forged blocker list) is never adopted.
            journey = {**_journey_upload(upload, [blocked], cell=SERVERLESS_OFF), "blockedBy": ["forged"]}
            verified = run_cloud.verify_journey(_serverless_state(False), journey, upload)
            assert verified["status"] == "blocked"
            assert cj.UNSUPPORTED_KIND_ISSUE in verified["blockedBy"] and "forged" not in verified["blockedBy"]
            assert "journey blocked by tracked limitations" in verified["why"]
            # require_real is the per-RC strict mode: the same receipts are a failure.
            strict = run_cloud.verify_journey(_serverless_state(True), journey, upload)
            assert strict["status"] == "fail" and strict["why"].startswith("require_real: journey blocked")
            # One attempt that is not a documented block keeps the journey a failure.
            raised = _blocked_receipt(notices=["Imported journey driver raised DiscoveryError"])
            mixed = run_cloud.verify_journey(_serverless_state(False),
                                             _journey_upload(upload, [raised, blocked], cell=SERVERLESS_OFF), upload)
            assert mixed["status"] == "fail" and "did not pass" in mixed["why"]
            # A journey job that reported its own failure keeps it.
            failed_job = {**_journey_upload(upload, [blocked], cell=SERVERLESS_OFF), "status": "fail",
                          "why": "journey evidence rejected: stale cell receipt"}
            kept = run_cloud.verify_journey(_serverless_state(False), failed_job, upload)
            assert kept["status"] == "fail" and kept["why"] == "journey evidence rejected: stale cell receipt"
    finally:
        shutil.rmtree(cj.EVIDENCE / "offline-run", ignore_errors=True)


def test_journey_job_stops_on_a_documented_block_and_reports_it(monkeypatch):
    cj = run_cloud.cloud_journey
    _phase_env(monkeypatch, run_id="blocked-j1")
    calls = []

    def attempt(cell, number, endpoint, admin_key, running_image=None):
        calls.append(number)
        path = cj.cell_dir(cell) / f"receipt-{number}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_blocked_receipt()))
        return {"number": number, "receipt": str(path.relative_to(E2E_DIR))}

    monkeypatch.setattr(cj, "attempt", attempt)
    monkeypatch.setattr(cj, "validate_attempt", lambda *a, **k: False)
    _patch_seam(monkeypatch, lambda *a, **k: [cc.CheckResult("top-demo", "blocked", "honua-release#450 CSP")])
    try:
        for require_real, status in ((False, "blocked"), (True, "fail")):
            calls.clear()
            state = {"cell": SERVERLESS_OFF, "redis": "redis-off", "endpoint": "https://cell.invalid",
                     "report": {}, "runId": "blocked-j1", "runAttempt": "1",
                     "candidateDigest": cj.candidate_digest(), "requireReal": require_real}
            journey = run_cloud.journey_phase(state, admin_key="k")
            # A documented limitation is deterministic: the second attempt is not spent on it.
            assert calls == [1] and journey["status"] == status
            assert cj.UNSUPPORTED_KIND_ISSUE in journey["blockedBy"]
    finally:
        shutil.rmtree(cj.EVIDENCE / "blocked-j1", ignore_errors=True)


def _blocked_cell_teardown(monkeypatch, *, require_real, cost_report, journey_status="blocked", started_at=None):
    cj = run_cloud.cloud_journey
    stub = _ServingStub()
    destroyed = []
    monkeypatch.setattr(stub, "teardown", lambda **kwargs: destroyed.append(True), raising=False)
    monkeypatch.setattr(cj, "cleanup", lambda cell: None)
    passing = [{"name": n, "status": "pass", "why": "ok"} for n in ("health", "capability-manifest")]
    state = {"cell": SERVERLESS_OFF, "redis": "redis-off", "requireReal": require_real,
             "provisionAttempted": True, "startedAt": started_at or cj.now(),
             "report": {"cell": SERVERLESS_OFF, "endpoint": "https://cell.invalid", "checks": passing,
                        "canaryProbes": [], "readiness": {"ready": True}, "journeyAttempts": []}}
    journey = {"status": journey_status, "blockedBy": [cj.UNSUPPORTED_KIND_ISSUE],
               "why": f"journey blocked by tracked limitations {[cj.UNSUPPORTED_KIND_ISSUE]}",
               "journeyAttempts": [{"number": 1, "receipt": "cloud-evidence/x/receipt-1.json"}],
               "scenarioCoverage": [
                   {"name": "mcp-handshake", "status": "pass", "why": "S1 ok"},
                   {"name": "top-demo", "status": "blocked", "why": "S9-demos-shim-security: honua-release#450 "
                    "top-demo: CSP does not allow this cell's backend origin"}]}
    report = run_cloud.teardown_phase(state, journey, stub, reference_endpoint=None, cost_report=cost_report)
    assert destroyed == [True] and state["destroyed"] is True
    return report


def test_teardown_blocked_cell_names_its_blockers_and_writes_cost_evidence(monkeypatch):
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        meter = Path(directory) / "run-cost.json"  # no harness meter reading exists
        report = _blocked_cell_teardown(monkeypatch, require_real=False, cost_report=meter)
        assert report["status"] == "blocked", report["why"]
        assert cj.UNSUPPORTED_KIND_ISSUE in report["why"] and "top-demo (honua-release#450)" in report["why"]
        assert report["journeyBlockedBy"] == [cj.UNSUPPORTED_KIND_ISSUE]
        # The cost entry is written, names what is missing, and never passes.
        assert report["cost"]["status"] == "unavailable" and str(meter) in report["cost"]["why"]
        assert "run cost meter unavailable" in report["why"] and "FileNotFoundError" not in report["why"]

        # Strict mode: the documented block and the missing meter are failures.
        strict = _blocked_cell_teardown(monkeypatch, require_real=True, cost_report=meter)
        assert strict["status"] == "fail"
        assert "require_real" in strict["why"] and "cost evidence unavailable: FileNotFoundError" in strict["why"]

        # A passing meter reading leaves only the tracked blockers; an over-ceiling one still fails.
        def write(amount):
            meter.write_text(json.dumps({"runId": os.environ.get("GITHUB_RUN_ID", "local"),
                "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "currency": "USD",
                "scope": "run", "measuredAt": cj.now(), "amount": amount}))
        started = cj.now()
        write("1.50")
        metered = _blocked_cell_teardown(monkeypatch, require_real=False, cost_report=meter, started_at=started)
        assert metered["status"] == "blocked" and metered["cost"]["status"] == "pass"
        assert "run cost meter unavailable" not in metered["why"]
        write("99")
        over = _blocked_cell_teardown(monkeypatch, require_real=False, cost_report=meter, started_at=started)
        assert over["status"] == "fail" and "exceeds ceiling" in over["why"]
        # A failed journey is never softened by the blocked path.
        meter.unlink()
        failed = _blocked_cell_teardown(monkeypatch, require_real=False, cost_report=meter, journey_status="fail")
        assert failed["status"] == "fail"


def test_cloud_report_keeps_a_documented_block_blocked_unless_require_real(monkeypatch):
    cj = run_cloud.cloud_journey
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        receipt = _blocked_receipt()
        report = {"cell": SERVERLESS_OFF, "status": "blocked", "artifactDirectory": str(directory),
                  "why": "journey blocked by tracked limitations",
                  "journeyAttempts": [_artifact(directory, receipt)],
                  "cost": {"status": "unavailable", "why": "run cost meter unavailable"}}
        lenient = cj.aggregate([report], directory, require_real=False, full_scope=False,
                               run_id="offline-run", run_attempt="2")
        assert lenient["status"] == "blocked" and SERVERLESS_OFF in lenient["why"]
        assert lenient["certifying"] is False
        strict = cj.aggregate([report], directory, require_real=True, full_scope=False,
                              run_id="offline-run", run_attempt="2")
        assert strict["status"] == "fail"
        # A real failure behind a blocked label is still a failure.
        raised = _blocked_receipt(notices=["Imported journey driver raised DiscoveryError"])
        hidden = {**report, "journeyAttempts": [_artifact(directory, raised)]}
        assert cj.aggregate([hidden], directory, require_real=False, full_scope=False,
                            run_id="offline-run", run_attempt="2")["status"] == "fail"
        over = {**report, "cost": {"status": "fail"}}
        assert cj.aggregate([over], directory, require_real=False, full_scope=False,
                            run_id="offline-run", run_attempt="2")["status"] == "fail"


def test_journey_job_refuses_a_foreign_handoff_and_an_unadmitted_endpoint(monkeypatch):
    cj = run_cloud.cloud_journey
    _phase_env(monkeypatch)
    monkeypatch.setattr(run_cloud, "_JOURNEY_READY_ATTEMPTS", 1)
    monkeypatch.setattr(run_cloud, "_READY_DELAY_SECONDS", 0)
    _patch_seam(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("drivers ran")))
    state = {"cell": "aws-ecs/redis-off", "redis": "redis-off", "runId": "phase381", "runAttempt": "1",
             "candidateDigest": cj.candidate_digest(), "endpoint": "http://cell.invalid", "ready": True,
             "report": {"cell": "aws-ecs/redis-off"}}
    try:
        foreign = run_cloud.journey_phase({**state, "candidateDigest": "0" * 64}, admin_key="k")
        assert foreign["status"] == "fail" and foreign["journeyAttempts"] == []
        assert run_cloud.journey_phase(state, admin_key="")["status"] == "fail"
        monkeypatch.setattr(run_cloud, "make_fetch", lambda **kw: lambda url: cc.HttpResponse(0, ""))
        unadmitted = run_cloud.journey_phase(state, admin_key="k")
        assert unadmitted["status"] == "fail" and "admission" in unadmitted["why"]
        assert unadmitted["readiness"]["ready"] is False
    finally:
        shutil.rmtree(cj.EVIDENCE / "phase381", ignore_errors=True)


def test_alb_admission_adds_only_the_journey_runner_to_the_serving_balancer(monkeypatch):
    import pytest
    calls = []
    balancers = {"LoadBalancers": [
        {"DNSName": "other.elb.amazonaws.com", "SecurityGroups": ["sg-other"]},
        {"DNSName": "Cell-ALB.us-east-1.elb.amazonaws.com", "SecurityGroups": ["sg-cell"]}]}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["elbv2", "describe-load-balancers"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(balancers), "")
        return subprocess.CompletedProcess(argv, 1 if len(calls) > 2 else 0, "",
                                           "InvalidPermission.Duplicate" if len(calls) > 2 else "")
    target = ecs(run_id="r1")
    assert target.admission == "alb" and serverless(run_id="r1").admission == "none"
    target.admit("http://cell-alb.us-east-1.elb.amazonaws.com", "198.51.100.7/32", run=run)
    group, permission = calls[1][calls[1].index("--group-id") + 1], json.loads(
        calls[1][calls[1].index("--ip-permissions") + 1])
    assert group == "sg-cell"
    assert permission[0]["FromPort"] == 80 and permission[0]["IpRanges"][0]["CidrIp"] == "198.51.100.7/32"
    target.admit("http://cell-alb.us-east-1.elb.amazonaws.com", "198.51.100.7/32", run=run)  # duplicate
    for broad in ("0.0.0.0/0", "198.51.100.0/24", "not-an-ip"):
        with pytest.raises(ProvisionError):
            target.admit("http://cell-alb.us-east-1.elb.amazonaws.com", broad, run=run)
    with pytest.raises(ProvisionError, match="no load balancer"):
        target.admit("http://missing.elb.amazonaws.com", "198.51.100.7/32", run=run)


def test_eks_admission_grants_this_runner_then_adds_the_journey_source_range(monkeypatch):
    from targets.aws_eks import AwsEksTarget
    _eks_env(monkeypatch, cidr="203.0.113.9/32")
    target = AwsEksTarget(run_id="r1")
    commands = []

    def run(command, *, input_text=None, env=None, check=True):
        commands.append(command)
        if "describe-cluster" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(["192.0.2.10/32"]), "")
        if "update-cluster-config" in command:
            return subprocess.CompletedProcess(command, 0, "upd-1\n", "")
        if "describe-update" in command:
            return subprocess.CompletedProcess(command, 0, "Successful\n", "")
        if command[:2] == ["kubectl", "get"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(
                {"spec": {"loadBalancerSourceRanges": ["192.0.2.10/32"]}}), "")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(target, "_run", run)
    target.admit("http://lb.invalid", "198.51.100.7/32", redis_enabled=False)
    update = next(c for c in commands if "update-cluster-config" in c)
    assert json.loads(update[update.index("--resources-vpc-config") + 1])["publicAccessCidrs"] == [
        "192.0.2.10/32", "203.0.113.9/32"]
    patch = next(c for c in commands if c[:2] == ["kubectl", "patch"])
    assert json.loads(patch[-1])["spec"]["loadBalancerSourceRanges"] == ["192.0.2.10/32", "198.51.100.7/32"]


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
        assert env['E2E_REDIS'] == 'on'
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


def test_run_passes_the_cell_redis_mode_to_the_extended_runner(monkeypatch):
    seen = {}
    stub = _ServingStub()
    def extended(endpoint, **kwargs):
        seen["redis_enabled"] = kwargs["redis_enabled"]
        return []
    _patch_seam(monkeypatch, extended)
    monkeypatch.setattr(stub, "provision", lambda **kwargs: stub.endpoint)
    monkeypatch.setattr(stub, "teardown", lambda **kwargs: None)
    monkeypatch.setattr(run_cloud, "REGISTRY", {"aws-ecs": lambda **kwargs: stub})
    monkeypatch.setattr(run_cloud, "make_fetch", lambda **kwargs: lambda url: cc.HttpResponse(200, ""))
    monkeypatch.setattr(run_cloud, "run_canonical", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud.canary_probes, "run_canary", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud.cloud_journey, "attempt", lambda *a, **k: {"receipt": "x"})
    monkeypatch.setattr(run_cloud.cloud_journey, "check_cost", lambda *a, **k: {"status": "pass"})
    monkeypatch.setattr(run_cloud.cloud_journey, "cleanup", lambda cell: None)
    run_cloud.run("aws-ecs", False, None, redis_enabled=False)
    assert seen["redis_enabled"] is False


def _run_gp_driver_against(status, body):
    """Run the real S5 driver in Redis-off mode against a one-shot HTTP stub."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            data = payload.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/problem+json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(200 if self.path == "/healthz/ready" else 404, "{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            assert self.path == "/ogc/processes/processes/geometry.area/execution"
            self._reply(status, body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as out:
            env = {**os.environ, "E2E_BASE": f"http://127.0.0.1:{server.server_address[1]}",
                   "E2E_API_KEY": "k", "E2E_OUT": out, "E2E_REDIS": "off"}
            subprocess.run(["bash", str(E2E_DIR / "drivers/gp/run.sh")], env=env, check=True,
                           capture_output=True, timeout=60)
            rows = [json.loads(line) for line in Path(out, "scenarios.jsonl").read_text().splitlines()]
    finally:
        server.shutdown()
    assert [row["scenario"] for row in rows] == ["S5-geoprocessing"]
    return rows[0]


def test_gp_driver_redis_off_passes_on_the_typed_capability_unavailable_refusal():
    row = _run_gp_driver_against(503, json.dumps({
        "type": "https://honua.io/problems/capability-unavailable", "status": 503,
        "code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis"}))
    assert row["status"] == "pass" and row["evidence"]["missingDependency"] == "redis"


def test_gp_driver_redis_off_fails_when_the_job_is_accepted_or_refusal_is_untyped():
    assert _run_gp_driver_against(201, json.dumps({"jobID": "j1", "status": "accepted"}))["status"] == "fail"
    assert _run_gp_driver_against(503, json.dumps({"title": "Service Unavailable"}))["status"] == "fail"


# ---- topology-aware S2 (MCP catalog) and S3 (Studio) drivers --------------------------------------
# The real drivers run against an in-process stub of the candidate. The stub serves a Redis-on
# server (the canonical 129-tool catalog) or a Redis-off one (minus the 23 durable-control-plane
# tools, or with them once honua-server S1 has landed), and reports its topology through the
# capability manifest / proposals endpoint the way the server does.
_EXPECTED_TOOLS = json.loads((E2E_DIR / "drivers/mcp/expected-tools.json").read_text())
_FULL_ROSTER = sorted(_EXPECTED_TOOLS["fullCatalog"]["tools"])
_DURABLE_ONLY = sorted(_EXPECTED_TOOLS["fullCatalog"]["requiresDurableControlPlane"]["tools"])
_REDIS_OFF_DETAIL = ("The operation proposal and approval control plane requires a Redis-backed durable "
                     "store. This server was started without a Redis connection, so proposals cannot be "
                     "listed, inspected, approved, or rejected.")


def test_durable_control_plane_roster_is_the_23_redis_gated_admin_tools():
    assert len(_FULL_ROSTER) == 129 and len(set(_FULL_ROSTER)) == 129
    assert len(_DURABLE_ONLY) == 23 and set(_DURABLE_ONLY) <= set(_FULL_ROSTER)
    assert all(n.startswith(("honua_admin_layer_", "honua_admin_services_", "honua_admin_connections_",
                                      "honua_admin_import_")) for n in _DURABLE_ONLY)
    assert not set(_DURABLE_ONLY) & set(_EXPECTED_TOOLS["criticalTools"])
    assert not set(_DURABLE_ONLY) & set(_EXPECTED_TOOLS["defaultView"]["tools"])


def _manifest(topology, signal, environment="Production"):
    caps = [{"id": "ogc.features", "available": True, "supported": True}]
    off = topology == "redis-off"
    if signal == "operations.proposals":
        caps.append({"id": "operations.proposals", "supported": True, "available": not off,
                     "reasonCode": "disabled-by-configuration" if off else None})
    caps.append({"id": "jobs.runner", "supported": True, "available": not off,
                 "reasonCode": "dependency-unavailable" if off else None})
    manifest = {"schemaVersion": "1", "capabilities": caps}
    if environment is not None:
        manifest["server"] = {"deploymentEnvironment": environment}
    return manifest


def _serve(handler_for):
    """Start a stub candidate. handler_for(method, path, body, authed) -> (status, json-able)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self, method):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = json.loads(body) if body else None
            status, reply = handler_for(method, self.path, payload, bool(self.headers.get("X-API-Key")))
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _common_routes(method, path, topology, signal, environment="Production"):
    if path == "/healthz/ready":
        return 200, {}
    if method == "GET" and path == "/api/v1/capabilities/manifest":
        return 200, _manifest(topology, signal, environment)
    if method == "GET" and path == "/api/v1/admin/proposals":
        if signal != "admin-proposals":
            return 404, {}
        if topology == "redis-off":
            return 503, {"type": "https://honua.io/problems/capability-unavailable", "status": 503,
                         "code": "dependency-unavailable", "missingDependency": "redis"}
        return 200, {"data": []}
    return None


def _mcp_handler(catalog, topology, signal):
    dv = _EXPECTED_TOOLS["defaultView"]
    page = _EXPECTED_TOOLS["fullCatalog"]["pageSize"]
    meta = {"view": dv["view"], "title": dv["title"], "revision": dv["revision"], "toolCount": dv["toolCount"],
            "fullCatalogView": dv["fullCatalogView"], "stages": dv["stages"]}

    def rpc_result(result):
        return 200, {"jsonrpc": "2.0", "id": 1, "result": result}

    def handle(method, path, body, authed):
        common = _common_routes(method, path, topology, signal)
        if common:
            return common
        assert method == "POST" and path == "/mcp", path
        m, params = body["method"], body.get("params") or {}
        if m == "initialize":
            return rpc_result({"protocolVersion": "2025-06-18", "serverInfo": {"name": "honua.operator.mcp"}})
        if m == "tools/list":
            if params.get("view") != "full":
                return rpc_result({"tools": [{"name": n} for n in dv["tools"]], "_meta": meta})
            if not authed:
                return 200, {"jsonrpc": "2.0", "id": 1,
                             "error": {"code": -32001, "message": "denied", "data": {"code": "permission_denied"}}}
            start = int(params.get("cursor") or 0)
            result = {"tools": [{"name": n} for n in catalog[start:start + page]]}
            if start + page < len(catalog):
                result["nextCursor"] = str(start + page)
            return rpc_result(result)
        assert m == "tools/call"
        name, args = params["name"], params.get("arguments") or {}
        if name == "honua_list_capabilities":
            if args.get("fullExport"):
                if not authed:
                    return rpc_result({"isError": True, "structuredContent": {"code": "permission_denied"}})
                return rpc_result({"structuredContent": {"toolCount": len(catalog), "resourceCount": 3,
                                                         "tools": [{"name": n} for n in catalog]}})
            return rpc_result({"structuredContent": {
                "toolCount": page, "resourceCount": 3, "totalToolCount": len(catalog), "totalResourceCount": 3,
                "nextToolCursor": "t12",
                "workflowViews": [{"name": dv["view"], "toolCount": dv["toolCount"], "revision": dv["revision"]}]}})
        return rpc_result({"content": []})
    return handle


def _run_driver(driver, handler, redis):
    server = _serve(handler)
    try:
        with tempfile.TemporaryDirectory() as out:
            env = {**os.environ, "E2E_BASE": f"http://127.0.0.1:{server.server_address[1]}",
                   "E2E_API_KEY": "k", "E2E_OUT": out}
            env.pop("E2E_REDIS", None)
            if redis is not None:
                env["E2E_REDIS"] = redis
            subprocess.run(["bash", str(E2E_DIR / f"drivers/{driver}/run.sh")], env=env, check=True,
                           capture_output=True, timeout=300)
            return [json.loads(line) for line in Path(out, "scenarios.jsonl").read_text().splitlines()]
    finally:
        server.shutdown()


def _s2(catalog, topology, redis, signal="jobs.runner"):
    rows = _run_driver("mcp", _mcp_handler(sorted(catalog), topology, signal), redis)
    assert [r["scenario"] for r in rows] == ["S1-mcp-handshake", "S2-mcp-tool-catalog"]
    return rows[1]


def test_s2_redis_on_full_129_catalog_passes():
    row = _s2(_FULL_ROSTER, "redis-on", None)
    assert row["status"] == "pass", row["why"]
    assert row["evidence"]["topology"]["topology"] == "redis-on"
    assert row["evidence"]["topology"]["durableControlPlaneTools"]["state"] == "required"


def test_s2_redis_off_106_catalog_passes_with_topology_recorded():
    catalog = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY]
    assert len(catalog) == 106
    for signal in ("jobs.runner", "operations.proposals", "admin-proposals"):
        row = _s2(catalog, "redis-off", "off", signal)
        assert row["status"] == "pass", (signal, row["why"])
        topo = row["evidence"]["topology"]
        assert topo["topology"] == "redis-off" and topo["declared"] == "off" and topo["mismatch"] is None
        assert topo["reasonCode"] and topo["signal"].startswith(("manifest:", "admin-proposals:"))
        assert topo["durableControlPlaneTools"] == {"state": "absent", "count": 23}
        assert "redis-off" in row["why"]


def test_s2_redis_off_after_server_s1_still_passes_with_the_23_advertised():
    row = _s2(_FULL_ROSTER, "redis-off", "off", "operations.proposals")
    assert row["status"] == "pass", row["why"]
    assert row["evidence"]["topology"]["durableControlPlaneTools"]["state"] == "advertised"


def test_s2_redis_off_missing_a_non_listed_name_fails():
    dropped = "honua_validate_package"
    assert dropped not in _DURABLE_ONLY
    catalog = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY and n != dropped]
    row = _s2(catalog, "redis-off", "off")
    assert row["status"] == "fail" and dropped in row["why"]


def test_s2_redis_off_extra_name_or_partial_durable_block_fails():
    catalog = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY] + ["honua_unrecorded_tool"]
    row = _s2(catalog, "redis-off", "off")
    assert row["status"] == "fail" and "honua_unrecorded_tool" in row["why"]
    partial = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY[:3]]
    row = _s2(partial, "redis-off", "off")
    assert row["status"] == "fail" and "partially advertised" in row["why"]


def test_s2_redis_on_cell_cannot_pass_on_a_106_catalog():
    catalog = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY]
    # A Redis-on cell whose server lost its control plane: the topology contradicts the cell.
    row = _s2(catalog, "redis-off", "on")
    assert row["status"] == "fail" and "declares Redis on" in row["why"]
    # A cell declaring Redis off gets no relaxation the server does not confirm.
    row = _s2(catalog, "redis-on", "off")
    assert row["status"] == "fail" and "full catalog missing tools" in json.dumps(row["evidence"]["failures"])


def _studio_handler(topology, signal, draft_reply, environment="Production", publish_reply=None):
    posts = []
    families = [{"family": f, "format": fmt, "currentSchemaVersion": "1.0",
                 "supportedOperations": ["publish-request.create"] if f != "analysis" else [],
                 "publishSupported": f != "analysis", "limitations": []}
                for f, fmt in (("query", "studio_query_package.v1"), ("analysis", "studio_analysis_package.v1"),
                               ("map", "honua_map_package.v1"))]

    def handle(method, path, body, authed):
        common = _common_routes(method, path, topology, signal, environment)
        if common:
            return common
        if method == "GET" and path == "/api/v1/admin/license":
            return 200, {"data": {"edition": "Enterprise", "mode": "disabled"}}
        if method == "GET" and path == "/api/v1/studio/package-families":
            return 200, {"data": {"families": families}}
        if method == "POST" and path == "/api/v1/admin/api-keys":
            return 201, {"data": {"key": "approver-key"}}
        if method != "POST":
            return 404, {}
        posts.append((path, body))
        if path == "/api/v1/studio/package-drafts":
            return draft_reply
        # The Development/Test lifecycle a volatile operation store serves (observed on the pinned
        # image, slice1-redis-off run 37892724773): composition direct-executes, the governed
        # publish-request is the step the missing proposal gateway answers.
        if path.endswith("/validate"):
            return 200, {"success": True, "data": {"status": "valid", "diagnostics": []}}
        if path.endswith("/preview-plan"):
            return 200, {"success": True, "data": {"draftId": "d", "synchronous": True}}
        if path.endswith("/content-versions"):
            return 201, {"success": True, "data": {"itemId": "item-1", "versionId": "ver-1", "versionNumber": 1}}
        if path.endswith("/publish-requests"):
            return publish_reply
        return 404, {}
    return handle, posts


def _s3(topology, redis, draft_reply, signal="jobs.runner", environment="Production", publish_reply=None):
    handler, posts = _studio_handler(topology, signal, draft_reply, environment, publish_reply)
    rows = _run_driver("studio", handler, redis)
    assert [r["scenario"] for r in rows] == ["S3-studio-authoring"]
    return rows[0], posts


_DRAFT_CREATED = (201, {"success": True, "data": {"draftId": "draft-1", "itemId": "item-1"}})
# What the pinned image answers today for the governed publish on a Redis-off Development host
# (AdminOperationApprovalBridge with no IOperationGateway) -- an untyped 500.
_UNTYPED_PUBLISH_500 = (500, {"type": "https://honua.io/problems/studio", "title": "Internal Server Error",
                              "status": 500,
                              "detail": "Approval is required, but the durable proposal gateway is unavailable."})


def test_s3_studio_redis_off_passes_on_the_typed_409_for_query_analysis_and_map():
    row, posts = _s3("redis-off", "off", (409, {"status": 409, "title": "Conflict", "detail": _REDIS_OFF_DETAIL}))
    assert row["status"] == "pass", row["why"]
    assert row["evidence"]["topology"]["topology"] == "redis-off"
    assert sorted(row["evidence"]["families"]) == ["analysis", "map", "query"]
    drafts = [body for path, body in posts if path == "/api/v1/studio/package-drafts"]
    assert [d["envelope"]["family"] for d in drafts] == ["query", "analysis", "map"] and len(posts) == 3
    map_body = drafts[2]["envelope"]["body"]
    assert drafts[2]["envelope"]["format"] == "honua_map_package.v1" == map_body["format"]
    assert map_body["mapPackageId"] == drafts[2]["packageKey"]
    assert "{layerId}" not in json.dumps(map_body)


def test_s3_studio_redis_off_fails_when_a_draft_is_composed_or_refused_untyped():
    row, _ = _s3("redis-off", "off", (201, {"data": {"draftId": "d1"}}))
    assert row["status"] == "fail"
    row, _ = _s3("redis-off", "off", (409, {"status": 409, "detail": "Draft key already exists."}))
    assert row["status"] == "fail"


def test_s3_studio_topology_mismatch_fails_before_any_lifecycle():
    row, posts = _s3("redis-off", "on", (409, {"detail": _REDIS_OFF_DETAIL}))
    assert row["status"] == "fail" and "declares Redis on" in row["why"] and posts == []


def test_s3_studio_redis_off_production_records_the_unavailable_operation_store():
    row, _ = _s3("redis-off", "off", (409, {"status": 409, "detail": _REDIS_OFF_DETAIL}))
    assert row["status"] == "pass", row["why"]
    topo = row["evidence"]["topology"]
    assert topo["deploymentEnvironment"] == "Production" and topo["operationStore"] == "unavailable"
    assert "query+analysis+map refused with the typed durable-store refusal at create-draft" in row["why"]
    assert "content-version boundary" not in row["why"]


def test_s3_studio_redis_off_development_composes_then_passes_on_a_typed_publish_refusal():
    for publish in ((409, {"status": 409, "title": "Conflict", "detail": _REDIS_OFF_DETAIL}),
                    (503, {"type": "https://honua.io/problems/capability-unavailable", "status": 503,
                           "code": "dependency-unavailable", "missingDependency": "redis"})):
        row, posts = _s3("redis-off", "off", _DRAFT_CREATED, environment="Development", publish_reply=publish)
        assert row["status"] == "pass", row["why"]
        assert row["evidence"]["topology"]["operationStore"] == "volatile"
        fam = row["evidence"]["families"]
        assert "publish-request refused" in fam["query"]["detail"] and "publish-request refused" in fam["map"]["detail"]
        # analysis stops at its declared content-version boundary; it is never sent a publish.
        assert "publish=not-advertised-by-family" in fam["analysis"]["detail"]
        published = [path for path, _ in posts if path.endswith("/publish-requests")]
        assert len(published) == 2
        # The summary claims the refusal only for the families that sent a publish-request, and
        # reports analysis at its own boundary.
        assert "query+map refused with the typed durable-store refusal at the governed publish-request" in row["why"]
        assert "analysis passed at its declared content-version boundary" in row["why"]
        assert "analysis refused" not in row["why"] and "query+analysis+map" not in row["why"]
        # No approver identity is minted: there is no proposal plane to approve against.
        assert not any(path == "/api/v1/admin/api-keys" for path, _ in posts)


def test_s3_studio_redis_off_development_untyped_publish_500_fails_and_names_the_server_gap():
    row, _ = _s3("redis-off", "off", _DRAFT_CREATED, environment="Development", publish_reply=_UNTYPED_PUBLISH_500)
    assert row["status"] == "fail"
    detail = row["evidence"]["families"]["query"]["detail"]
    assert "HTTP 500" in detail and "honua-server#5733" in detail
    assert "governed publish-request" in row["why"]


def test_s3_studio_redis_off_development_still_fails_when_composition_breaks_or_publish_is_accepted():
    row, _ = _s3("redis-off", "off", (500, {"detail": "boom"}), environment="Development",
                 publish_reply=(409, {"detail": _REDIS_OFF_DETAIL}))
    assert row["status"] == "fail"
    row, _ = _s3("redis-off", "off", _DRAFT_CREATED, environment="Development",
                 publish_reply=(201, {"success": True, "data": {"status": "accepted"}}))
    assert row["status"] == "fail"


def test_s3_studio_redis_off_without_a_reported_environment_cannot_pass():
    row, posts = _s3("redis-off", "off", (409, {"detail": _REDIS_OFF_DETAIL}), environment=None)
    assert row["status"] == "fail" and "deploymentEnvironment" in row["why"] and posts == []

# ---- rc.3 fix units C2 / C4 / C6: Lambda+Batch cell, pre-serving migration, Bedrock opt-in ---------
def _iac_root_with(monkeypatch, base, example, *variables):
    root = Path(base) / "infrastructure" / "terraform" / "examples" / example
    root.mkdir(parents=True, exist_ok=True)
    (root / "variables.tf").write_text(
        "".join(f'variable "{name}" {{}}\n' for name in ("region", *variables)), encoding="utf-8")
    monkeypatch.setenv("HONUA_IAC_DIR", str(base))
    return root


def _serverless_env(monkeypatch):
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "x86_64")


def test_mixed_cell_is_out_of_the_rc3_run_cloud_registry():
    assert "aws-mixed" not in run_cloud.REGISTRY
    assert {"aws-serverless", "aws-ecs", "aws-eks"} <= set(run_cloud.REGISTRY)


def test_serverless_spec_provisions_the_lambda_batch_ga_cell(monkeypatch):
    from targets.terraform_target import SERVERLESS_SPEC
    assert {"enable_gp_batch=true", "use_batch_service_linked_role=true",
            "image_repository_policy_mode=reuse"} <= set(SERVERLESS_SPEC.declared_ephemeral_vars)
    assert ("HONUA_GP_BATCH_IMAGE", "gp_batch_image") in SERVERLESS_SPEC.env_vars
    assert SERVERLESS_SPEC.migrate_image_env == "HONUA_MIGRATE_IMAGE"
    _serverless_env(monkeypatch)
    batch_image = "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64
    monkeypatch.setenv("HONUA_GP_BATCH_IMAGE", batch_image)
    with tempfile.TemporaryDirectory() as base:
        # An older iac pin that does not declare the Batch inputs gets none of them.
        _iac_root_with(monkeypatch, base, "aws-serverless")
        values = _tf_vars(serverless(run_id="r1")._vars(False))
        assert not {"enable_gp_batch", "use_batch_service_linked_role", "gp_batch_image"} & set(values)
        _iac_root_with(monkeypatch, base, "aws-serverless", "enable_gp_batch",
                       "use_batch_service_linked_role", "gp_batch_image")
        values = _tf_vars(serverless(run_id="r1")._vars(False))
        assert values["enable_gp_batch"] == "true"
        assert values["use_batch_service_linked_role"] == "true"
        assert values["gp_batch_image"] == batch_image
        assert json.loads(values["lambda_architectures"]) == ["x86_64"]


def test_serverless_is_blocked_when_the_root_takes_a_batch_image_and_none_is_pinned(monkeypatch):
    for var in _AWS_ENV:
        monkeypatch.delenv(var, raising=False)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "gp_batch_image")
        avail = serverless().availability()
        assert any("HONUA_GP_BATCH_IMAGE" in m for m in avail.missing)
        assert any("HONUA_MIGRATE_IMAGE" in m for m in avail.missing)
        monkeypatch.setenv("HONUA_GP_BATCH_IMAGE", "img@sha256:" + "c" * 64)
        monkeypatch.setenv("HONUA_MIGRATE_IMAGE", "img@sha256:" + "c" * 64)
        avail = serverless().availability()
        assert not any("HONUA_GP_BATCH_IMAGE" in m or "HONUA_MIGRATE_IMAGE" in m for m in avail.missing)
    # ECS never migrates from the runner (its task runs migrations) and takes no Batch image.
    assert not ecs().migrates_before_serving and serverless().migrates_before_serving


KEY_RING_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua/keyring-AbCdEf"
KEY_RING_KMS = "arn:aws:kms:us-east-1:123456789012:key/0000-1111"


def _ecs_env(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")


def test_ecs_spec_maps_the_operation_key_ring_secret_for_redis_on_cells(monkeypatch):
    from targets.terraform_target import ECS_SPEC
    assert ("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "operation_key_ring_certificate_secret_arn") in ECS_SPEC.redis_env_vars
    assert ("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN",
            "operation_key_ring_certificate_secret_kms_key_arn") in ECS_SPEC.redis_env_vars
    assert ECS_SPEC.redis_required_vars == ("operation_key_ring_certificate_secret_arn",)
    _ecs_env(monkeypatch)
    names = ("operation_key_ring_certificate_secret_arn", "operation_key_ring_certificate_secret_kms_key_arn")
    with tempfile.TemporaryDirectory() as base:
        # An older iac pin (v0.2.0) declares neither: nothing is passed and nothing refuses.
        _iac_root_with(monkeypatch, base, "aws")
        assert not set(names) & set(_tf_vars(ecs(run_id="r1")._vars(True)))
        _iac_root_with(monkeypatch, base, "aws", *names)
        # Redis-off never takes the key ring, even when the variables are set.
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", KEY_RING_KMS)
        assert not set(names) & set(_tf_vars(ecs(run_id="r1")._vars(False)))
        values = _tf_vars(ecs(run_id="r1")._vars(True))
        assert values["operation_key_ring_certificate_secret_arn"] == KEY_RING_ARN
        assert values["operation_key_ring_certificate_secret_kms_key_arn"] == KEY_RING_KMS
        # The KMS key is optional (AWS-managed aws/secretsmanager key).
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", "")
        values = _tf_vars(ecs(run_id="r1")._vars(True))
        assert values["operation_key_ring_certificate_secret_arn"] == KEY_RING_ARN
        assert "operation_key_ring_certificate_secret_kms_key_arn" not in values


def test_redis_on_ecs_refuses_without_the_key_ring_secret_but_destroy_still_plans(monkeypatch):
    _ecs_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", "operation_key_ring_certificate_secret_arn")
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "")
        with pytest.raises(ProvisionError, match="HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN"):
            ecs(run_id="r1")._vars(True)
        assert "operation_key_ring_certificate_secret_arn" not in _tf_vars(ecs(run_id="r1")._vars(True, destroy=True))
        assert "operation_key_ring_certificate_secret_arn" not in _tf_vars(ecs(run_id="r1")._vars(False))


def test_serverless_spec_maps_the_operation_key_ring_secret_for_redis_on_cells(monkeypatch):
    from targets.terraform_target import ECS_SPEC, SERVERLESS_SPEC
    # Same contract as ECS: the Redis-on Lambda server exits without the certificate too.
    assert SERVERLESS_SPEC.redis_env_vars == ECS_SPEC.redis_env_vars
    assert SERVERLESS_SPEC.redis_required_vars == ("operation_key_ring_certificate_secret_arn",)
    _serverless_env(monkeypatch)
    names = ("operation_key_ring_certificate_secret_arn", "operation_key_ring_certificate_secret_kms_key_arn")
    with tempfile.TemporaryDirectory() as base:
        # A pinned serverless root that declares neither (iac 5b09d0dd): nothing passed, nothing refused.
        _iac_root_with(monkeypatch, base, "aws-serverless")
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "")
        assert not set(names) & set(_tf_vars(serverless(run_id="r1")._vars(True)))
        _iac_root_with(monkeypatch, base, "aws-serverless", *names)
        # Redis-off never takes the key ring, even when the variables are set.
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", KEY_RING_KMS)
        assert not set(names) & set(_tf_vars(serverless(run_id="r1")._vars(False)))
        values = _tf_vars(serverless(run_id="r1")._vars(True))
        assert values["operation_key_ring_certificate_secret_arn"] == KEY_RING_ARN
        assert values["operation_key_ring_certificate_secret_kms_key_arn"] == KEY_RING_KMS
        # The KMS key is optional (AWS-managed aws/secretsmanager key).
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN", "")
        values = _tf_vars(serverless(run_id="r1")._vars(True))
        assert values["operation_key_ring_certificate_secret_arn"] == KEY_RING_ARN
        assert "operation_key_ring_certificate_secret_kms_key_arn" not in values


def test_redis_on_serverless_refuses_without_the_key_ring_secret_but_destroy_still_plans(monkeypatch):
    _serverless_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "operation_key_ring_certificate_secret_arn")
        monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "  ")
        with pytest.raises(ProvisionError, match="HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN"):
            serverless(run_id="r1")._vars(True)
        assert "operation_key_ring_certificate_secret_arn" not in _tf_vars(
            serverless(run_id="r1")._vars(True, destroy=True))
        assert "operation_key_ring_certificate_secret_arn" not in _tf_vars(serverless(run_id="r1")._vars(False))


def test_cell_provision_and_teardown_export_the_key_ring_repository_variables():
    _, cell = _cloud_workflows()
    steps = {step.get("name", ""): step for job in cell["jobs"].values() for step in job.get("steps", [])}
    provision = next(step for name, step in steps.items() if name.startswith("Provision "))
    teardown = next(step for name, step in steps.items() if name.startswith("Tear down "))
    for step in (provision, teardown):
        for var in ("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", "HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN",
                    "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN"):
            assert step["env"][var] == "${{ vars." + var + " }}"
    # Step-scoped, not workflow-level: the self-test never sees them.
    assert "HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN" not in cell["env"]
    assert "HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN" not in cell["env"]


AUDIT_KEY_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:honua/audit-chain-AbCdEf"
AUDIT_KEY_KMS = "arn:aws:kms:us-east-1:123456789012:key/2222-3333"


def test_serverless_cells_carry_the_audit_chain_key_on_both_redis_modes(monkeypatch):
    # honua-iac#230 keeps the Lambda environment under the 4 KB cap (run 38046060497 had measured
    # 4118 bytes with the key ring, GP Batch and the audit key), so the Redis-on withholding is gone.
    from targets.terraform_target import ECS_SPEC, SERVERLESS_SPEC
    assert SERVERLESS_SPEC.redis_on_withholds_optional_env_vars is False
    assert ECS_SPEC.redis_on_withholds_optional_env_vars is False
    _serverless_env(monkeypatch)
    names = ("audit_chain_key_secret_arn", "audit_chain_key_secret_kms_key_arn")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", *names)
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", AUDIT_KEY_ARN)
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", AUDIT_KEY_KMS)
        for redis in (False, True):
            if redis:
                monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
            values = _tf_vars(serverless(run_id="r1")._vars(redis))
            assert values["audit_chain_key_secret_arn"] == AUDIT_KEY_ARN
            assert values["audit_chain_key_secret_kms_key_arn"] == AUDIT_KEY_KMS
        assert not any("AUDIT_CHAIN" in m for m in serverless(run_id="r1").availability().missing)


@pytest.mark.parametrize("kind", ["ecs"])
def test_ecs_cells_map_the_audit_chain_key_when_declared_and_never_block(monkeypatch, kind):
    from targets.terraform_target import ECS_SPEC
    spec, example, factory, env = {
        "ecs": (ECS_SPEC, "aws", ecs, _ecs_env),
    }[kind]
    assert spec.optional_env_vars == (
        ("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", "audit_chain_key_secret_arn"),
        ("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", "audit_chain_key_secret_kms_key_arn"),
    )
    env(monkeypatch)
    names = ("audit_chain_key_secret_arn", "audit_chain_key_secret_kms_key_arn")
    with tempfile.TemporaryDirectory() as base:
        # A pinned root that declares neither gets nothing, even with the variables set.
        _iac_root_with(monkeypatch, base, example)
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", AUDIT_KEY_ARN)
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", AUDIT_KEY_KMS)
        assert not set(names) & set(_tf_vars(factory(run_id="r1")._vars(False)))
        _iac_root_with(monkeypatch, base, example, *names)
        # Declared: passed on Redis-off and Redis-on cells alike.
        for redis in (False, True):
            if redis:
                monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
            values = _tf_vars(factory(run_id="r1")._vars(redis))
            assert values["audit_chain_key_secret_arn"] == AUDIT_KEY_ARN
            assert values["audit_chain_key_secret_kms_key_arn"] == AUDIT_KEY_KMS
        # Recommended, not required: unset (or blank) never refuses and never blocks availability.
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN", " ")
        monkeypatch.setenv("HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN", "")
        values = _tf_vars(factory(run_id="r1")._vars(False))
        assert not set(names) & set(values)
        assert not any("AUDIT_CHAIN" in m for m in factory(run_id="r1").availability().missing)


def test_ecs_bedrock_is_opt_in_and_fails_closed_on_a_root_without_it(monkeypatch):
    from targets.terraform_target import ECS_SPEC
    assert ECS_SPEC.opt_in_env == "HONUA_ENABLE_BEDROCK_AI"
    assert set(ECS_SPEC.opt_in_vars) == {"enable_bedrock_ai=true", "bedrock_ai_region=us-east-1"}
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", "enable_bedrock_ai", "bedrock_ai_region")
        for flag in (None, "false"):
            if flag is None:
                monkeypatch.delenv("HONUA_ENABLE_BEDROCK_AI", raising=False)
            else:
                monkeypatch.setenv("HONUA_ENABLE_BEDROCK_AI", flag)
            assert "enable_bedrock_ai" not in _tf_vars(ecs(run_id="r1")._vars(False))
        monkeypatch.setenv("HONUA_ENABLE_BEDROCK_AI", "true")
        values = _tf_vars(ecs(run_id="r1")._vars(False))
        assert values["enable_bedrock_ai"] == "true" and values["bedrock_ai_region"] == "us-east-1"
        _iac_root_with(monkeypatch, base, "aws")
        with pytest.raises(ProvisionError, match="does not declare enable_bedrock_ai"):
            ecs(run_id="r1")._vars(False)
    # Serverless has no Bedrock opt-in.
    _serverless_env(monkeypatch)
    assert "enable_bedrock_ai" not in _tf_vars(serverless(run_id="r1")._vars(False))


def test_cloud_workflow_threads_the_batch_image_and_scopes_bedrock_to_the_genuine_model_cell():
    workflow, cell = _cloud_workflows()
    candidate = workflow["jobs"]["candidate"]
    assert candidate["outputs"]["gp_batch_image"] == "${{ steps.pins.outputs.gp_batch_image }}"
    pins = next(step["run"] for step in candidate["steps"] if step.get("id") == "pins")
    assert 'server.get("platformDigests")' in pins and '"amd64"' in pins
    assert 'pins["gp_batch_image"] = f"{repository}@{amd64}"' in pins
    parity = workflow["jobs"]["parity"]["with"]
    assert parity["gp_batch_image"] == "${{ needs.candidate.outputs.gp_batch_image }}"
    assert parity["enable_bedrock"] == ("${{ (inputs.genuine_model_bedrock || false) && "
                                        "matrix.target == 'aws-ecs' && matrix.redis == 'off' }}")
    for trigger in ("workflow_dispatch", "workflow_call"):
        assert workflow[True][trigger]["inputs"]["genuine_model_bedrock"]["default"] is False
    assert cell["env"]["HONUA_GP_BATCH_IMAGE"] == "${{ inputs.gp_batch_image }}"
    assert cell["env"]["HONUA_MIGRATE_IMAGE"] == "${{ inputs.gp_batch_image }}"
    assert cell["env"]["HONUA_ENABLE_BEDROCK_AI"] == "${{ inputs.enable_bedrock }}"
    assert cell[True]["workflow_call"]["inputs"]["enable_bedrock"]["default"] is False


def test_manifest_batch_image_is_the_ecs_amd64_child_by_digest():
    import re as _re
    server = run_cloud.cloud_journey.manifest()["components"]["honua-server"]
    image, amd64 = server["image"], server["platformDigests"]["amd64"]
    repository = image.rsplit(":", 1)[0] if "/" not in image.rsplit(":", 1)[-1] else image
    batch_image = f"{repository}@{amd64}"
    # honua-iac's gp_batch_image validation: registry/repository@sha256:<hex>, no tag.
    assert _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]+)?(/[A-Za-z0-9._-]+)+@sha256:[0-9a-f]{64}",
                         batch_image), batch_image


def _migration_target(monkeypatch, secrets_in_state):
    _serverless_env(monkeypatch)
    monkeypatch.setenv("HONUA_MIGRATE_IMAGE", "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64)
    target = serverless(run_id="run42")
    target._workdir = Path(".")
    state = {"values": {"root_module": {"resources": [], "child_modules": [{"resources": [
        {"type": "aws_secretsmanager_secret_version", "name": name, "values": {"secret_string": value}}
        for name, value in secrets_in_state.items()]}]}}}
    monkeypatch.setattr(target, "_tf", lambda root, *args, **kw: subprocess.CompletedProcess(
        args, 0, json.dumps(state), ""))
    return target


_CONNECTION = "Host=db.example;Port=5432;Database=honua;Username=honua;Password=S3cret-db-pw"


class _Docker:
    def __init__(self, *, start=0, running="true 0", logs=""):
        self.calls, self.envs = [], []
        self.start, self.running, self.logs = start, running, logs

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(kwargs.get("env"))
        verb = argv[1]
        if verb == "run":
            return subprocess.CompletedProcess(argv, self.start, "cid\n", "pull denied" if self.start else "")
        if verb == "inspect":
            return subprocess.CompletedProcess(argv, 0, self.running + "\n", "")
        if verb == "logs":
            return subprocess.CompletedProcess(argv, 0, self.logs, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_serverless_migration_runs_the_generic_image_until_ready_then_stops(monkeypatch):
    target = _migration_target(monkeypatch, {"connection_string": _CONNECTION, "master_key": "mk-123"})
    docker = _Docker()
    statuses = iter([0, 503, 200])
    result = target.migrate(redis_enabled=True, run=docker, probe=lambda url: next(statuses),
                            sleep=lambda _s: None)
    assert result["ready"] is True and result["attempts"] == 3 and result["path"] == "/healthz/ready"
    start = docker.calls[0]
    assert start[:2] == ["docker", "run"] and start[-1] == "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64
    assert "127.0.0.1:18080:8080" in start
    # Secrets travel in the docker process environment, named but never valued on argv.
    argv = " ".join(start)
    for secret in (_CONNECTION, "mk-123", TEST_ADMIN_PASSWORD):
        assert secret not in argv
    for name in ("HONUA_SKIP_MIGRATIONS", "ConnectionStrings__DefaultConnection",
                 "Security__ConnectionEncryption__MasterKey", "HONUA_ADMIN_PASSWORD", "Licensing__Mode"):
        assert name in start
    env = docker.envs[0]
    assert env["HONUA_SKIP_MIGRATIONS"] == "false"
    assert env["ConnectionStrings__DefaultConnection"] == _CONNECTION
    assert env["Security__ConnectionEncryption__MasterKey"] == "mk-123"
    assert env["Licensing__Mode"] == "Disabled"
    assert "AWS_ACCESS_KEY_ID" not in env
    # The container is always removed.
    assert docker.calls[-1][:3] == ["docker", "rm", "-f"]
    assert json.dumps(result).find(_CONNECTION) == -1


def test_serverless_migration_failure_is_a_redacted_reason_and_the_container_is_removed(monkeypatch):
    target = _migration_target(monkeypatch, {"connection_string": _CONNECTION, "master_key": "mk-123"})
    docker = _Docker(running="false 134",
                     logs=f"fatal: migration 0042 failed for {_CONNECTION}\nPassword=leak; api_key: abc\n")
    with pytest.raises(ProvisionError) as error:
        target.migrate(run=docker, probe=lambda url: 503, sleep=lambda _s: None)
    message = str(error.value)
    assert "exited 134 before Ready" in message and "migration 0042 failed" in message
    assert "S3cret-db-pw" not in message and "leak" not in message and "abc" not in message
    assert docker.calls[-1][:3] == ["docker", "rm", "-f"]

    never = _Docker()
    monkeypatch.setattr(type(target), "MIGRATE_ATTEMPTS", 3)
    with pytest.raises(ProvisionError, match=r"never reported Ready .*last status 503"):
        target.migrate(run=never, probe=lambda url: 503, sleep=lambda _s: None)
    assert never.calls[-1][:3] == ["docker", "rm", "-f"]

    refused = _Docker(start=1)
    with pytest.raises(ProvisionError, match="did not start: pull denied"):
        target.migrate(run=refused, probe=lambda url: 200, sleep=lambda _s: None)
    assert refused.calls[-1][:3] == ["docker", "rm", "-f"]

    monkeypatch.delenv("HONUA_MIGRATE_IMAGE")
    with pytest.raises(ProvisionError, match="HONUA_MIGRATE_IMAGE is unset"):
        target.migrate(run=_Docker(), probe=lambda url: 200, sleep=lambda _s: None)


class _MigratingStub(_ServingStub):
    migrates_before_serving = True

    def __init__(self, error=None):
        super().__init__()
        self.error, self.order = error, []

    def provision(self, redis_enabled: bool = False) -> str:
        self.order.append("provision")
        return self.endpoint

    def migrate(self, redis_enabled: bool = False) -> dict:
        self.order.append("migrate")
        if self.error:
            raise ProvisionError(self.error)
        return {"ready": True, "attempts": 1, "path": "/healthz/ready"}


def test_provision_phase_migrates_before_the_first_probe_and_fails_the_cell_on_error(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    probes = []
    stub = _MigratingStub()
    monkeypatch.setattr(run_cloud, "_READY_ATTEMPTS", 1)
    monkeypatch.setattr(run_cloud, "_READY_DELAY_SECONDS", 0)

    def fetch_factory(**kwargs):
        def fetch(url):
            stub.order.append("probe")
            probes.append(url)
            return cc.HttpResponse(200, "")
        return fetch

    monkeypatch.setattr(run_cloud, "make_fetch", fetch_factory)
    monkeypatch.setattr(run_cloud, "run_canonical", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud.canary_probes, "run_canary", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud, "seed_cell", lambda *a, **k: None)
    state = run_cloud.provision_phase(stub, "stub", require_real=True, redis_enabled=False)
    assert stub.order[:3] == ["provision", "migrate", "probe"]
    assert state["report"]["migration"]["ready"] is True and state["endpoint"] == stub.endpoint

    failing = _MigratingStub(error="migration container exited 1 before Ready: boom")
    state = run_cloud.provision_phase(failing, "stub", require_real=True, redis_enabled=False)
    report = state["report"]
    assert failing.order == ["provision", "migrate"]
    assert report["status"] == "fail" and report["why"].startswith("migration failed: ")
    assert "exited 1 before Ready" in report["why"]

    leaky = _MigratingStub(error="did not start: Host=db;Username=u;Password=S3cret; "
                                 "postgres://honua:S3cret@db:5432/honua")
    report = run_cloud.provision_phase(leaky, "stub", require_real=True, redis_enabled=False)["report"]
    assert report["why"].startswith("migration failed: ") and "S3cret" not in report["why"]
    # Nothing was probed, the endpoint is not handed to the journey, and teardown still destroys.
    assert state["endpoint"] is None and state["provisionAttempted"] is True


def test_ecs_readiness_diagnostics_capture_stop_reasons_and_the_server_log_tail(monkeypatch):
    target = ecs(run_id="r1")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    task_arn = "arn:aws:ecs:us-east-1:1:task/cluster/abc123"
    definition = "arn:aws:ecs:us-east-1:1:task-definition/honua:7"
    lines = [{"message": f"line {i}"} for i in range(350)] + [{"message": "Password=hunter2 boot failed"}]
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "terraform":
            return subprocess.CompletedProcess(argv, 0, {"ecs_cluster_name": "c1", "ecs_service_name": "s1"}[argv[-1]], "")
        verb = argv[2]
        payload = {
            "list-tasks": {"taskArns": [task_arn] if "STOPPED" in argv else []},
            "describe-tasks": {"tasks": [{"taskArn": task_arn, "lastStatus": "STOPPED",
                "desiredStatus": "STOPPED", "stopCode": "EssentialContainerExited",
                "stoppedReason": "Essential container in task exited", "taskDefinitionArn": definition,
                "createdAt": "2026-10-08T00:00:00Z",
                "containers": [{"name": "honua", "lastStatus": "STOPPED", "exitCode": 139,
                                "reason": "token=abc"}]}]},
            "describe-task-definition": {"taskDefinition": {"containerDefinitions": [{"name": "honua",
                "environment": [{"name": "ConnectionStrings__Redis", "value": "redis://u:hunter2@cache"},
                                {"name": "ASPNETCORE_ENVIRONMENT", "value": "Production"}],
                "secrets": [{"name": "HONUA_ADMIN_PASSWORD", "valueFrom": "arn:aws:secretsmanager:x"}],
                "logConfiguration": {"options": {"awslogs-group": "/ecs/honua", "awslogs-stream-prefix": "honua"}}}]}},
            "get-log-events": {"events": lines},
        }[verb]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    report = run_cloud.ecs_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    assert run_cloud.cloud_journey.PREVIEW_TARGETS == ("aws-eks",)
    assert report["cluster"] == "c1" and report["service"] == "s1"
    task = report["tasks"][0]
    assert task["stoppedReason"] == "Essential container in task exited"
    assert task["stopCode"] == "EssentialContainerExited" and task["containers"][0]["exitCode"] == 139
    assert task["containers"][0]["reason"] == "token=[redacted]"
    log = report["logs"][0]
    assert log["logStream"] == "honua/honua/abc123" and len(log["lines"]) == 300
    assert log["taskArn"] == task_arn and log["logGroup"] == "/ecs/honua"
    assert log["lines"][-1] == "Password=[redacted] boot failed"
    get_logs = next(c for c in calls if "get-log-events" in c)
    assert get_logs[get_logs.index("--limit") + 1] == "300"
    # Environment variable NAMES only: a missing or renamed setting is visible, no value is read.
    container = report["taskDefinitions"][0]["containers"][0]
    assert container["environmentNames"] == ["ASPNETCORE_ENVIRONMENT", "ConnectionStrings__Redis"]
    assert container["valueFromNames"] == ["HONUA_ADMIN_PASSWORD"]
    assert "hunter2" not in json.dumps(report) and "Production" not in json.dumps(report)


def test_ecs_diagnostics_tail_every_stopped_task_and_report_unreadable_streams(monkeypatch):
    target = ecs(run_id="r2")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    definition = "arn:aws:ecs:us-east-1:1:task-definition/honua:8"
    arns = [f"arn:aws:ecs:us-east-1:1:task/cluster/t{i}" for i in range(3)]
    states = {arns[0]: "STOPPED", arns[1]: "STOPPED", arns[2]: "RUNNING"}
    streams = []

    def run(argv, **kwargs):
        if argv[0] == "terraform":
            return subprocess.CompletedProcess(argv, 0, {"ecs_cluster_name": "c1", "ecs_service_name": "s1"}[argv[-1]], "")
        verb = argv[2]
        if verb == "get-log-events":
            stream = argv[argv.index("--log-stream-name") + 1]
            streams.append(stream)
            if stream.endswith("/t1"):
                return subprocess.CompletedProcess(argv, 254, "", "ResourceNotFoundException: Password=x stream gone")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"events": [{"message": stream}]}), "")
        payload = {
            "list-tasks": {"taskArns": [a for a in arns if (states[a] == "STOPPED") == ("STOPPED" in argv)]},
            "describe-tasks": {"tasks": [{"taskArn": a, "lastStatus": states[a], "taskDefinitionArn": definition,
                                          "createdAt": f"2026-10-09T0{i}:00:00Z", "containers": []}
                                         for i, a in enumerate(arns)]},
            "describe-task-definition": {"taskDefinition": {"containerDefinitions": [{"name": "honua",
                "logConfiguration": {"options": {"awslogs-group": "/ecs/honua", "awslogs-stream-prefix": "ecs"}}}]}},
        }[verb]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    report = run_cloud.ecs_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    # The newest task (running) plus every stopped task, each read once.
    assert sorted(streams) == ["ecs/honua/t0", "ecs/honua/t1", "ecs/honua/t2"]
    unreadable = next(log for log in report["logs"] if log["logStream"] == "ecs/honua/t1")
    assert "ResourceNotFoundException" in unreadable["error"] and "Password=[redacted]" in unreadable["error"]


def _lambda_cli(outputs, *, functions, groups, batch_groups=(), failing_streams=(), calls=None):
    """A fake terraform/aws CLI for lambda_readiness_diagnostics: `groups` maps a log group to
    {stream: [(timestamp, message), ...]}."""
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        if argv[0] == "terraform":
            name = argv[-1]
            return subprocess.CompletedProcess(argv, 0 if name in outputs else 1, outputs.get(name, ""), "")
        verb = argv[2]
        if verb == "list-functions":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"Functions": functions}), "")
        if verb == "describe-log-groups":
            prefix = argv[argv.index("--log-group-name-prefix") + 1]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"logGroups": [
                {"logGroupName": g} for g in batch_groups if g.startswith(prefix)]}), "")
        group = argv[argv.index("--log-group-name") + 1]
        if verb == "describe-log-streams":
            if group not in groups:
                return subprocess.CompletedProcess(argv, 254, "", "ResourceNotFoundException: token=abc gone")
            names = sorted(groups[group], key=lambda n: max(t for t, _ in groups[group][n]), reverse=True)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"logStreams": [
                {"logStreamName": n} for n in names[:int(argv[argv.index("--max-items") + 1])]]}), "")
        assert verb == "get-log-events"
        stream = argv[argv.index("--log-stream-name") + 1]
        if stream in failing_streams:
            return subprocess.CompletedProcess(argv, 254, "", "ThrottlingException: Password=x")
        limit = int(argv[argv.index("--limit") + 1])
        events = [{"timestamp": t, "message": m + "\n"} for t, m in groups[group][stream]][-limit:]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"events": events}), "")
    return run


def test_lambda_readiness_diagnostics_tail_every_cell_function_and_the_batch_log(monkeypatch):
    target = serverless(run_id="r1")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    stem = "honuaoffawsser1-dev"
    api, reconcile, backstop, tick = (f"{stem}-honua", f"{stem}-cp-reconcile", f"{stem}-cp-backstop",
                                      f"{stem}-cp-tick")
    outputs = {"lambda_function_name": api, "control_plane_reconcile_function_name": reconcile,
               "control_plane_backstop_function_name": backstop, "gp_batch_enabled": "true"}
    functions = [
        {"FunctionName": api, "State": "Active", "LastUpdateStatus": "Successful",
         "Environment": {"Variables": {"ConnectionStrings__Redis": "redis://u:hunter2@cache",
                                       "ASPNETCORE_ENVIRONMENT": "Production"}},
         "LoggingConfig": {"LogGroup": f"/aws/lambda/{api}"}},
        {"FunctionName": reconcile, "State": "Active"},
        {"FunctionName": tick, "State": "Failed", "StateReason": "token=abc init failed"},
        # Another cell's function sharing a prefix of the stem is not this cell's.
        {"FunctionName": f"{stem}x-honua", "State": "Active"},
        {"FunctionName": "unrelated-honua", "State": "Active"},
    ]
    # The API group spans three streams (one per execution environment); its last 300 lines interleave.
    api_streams = {f"s{n}": [(n + 3 * i, f"s{n} line {i}") for i in range(200)] for n in range(3)}
    api_streams["s2"].append((10_000, "operations.proposals: dependency-unavailable Password=hunter2"))
    groups = {f"/aws/lambda/{api}": api_streams,
              f"/aws/lambda/{reconcile}": {"r": [(1, "reconcile ok")]},
              f"/aws/lambda/{tick}": {"t": [(1, "tick AKIAABCDEFGHIJKLMNOP")]},
              f"/aws/batch/{stem}-gp": {"job/default/abc": [(5, "gp job running postgres://h:pw@db/x")]}}
    calls = []
    run = _lambda_cli(outputs, functions=functions, groups=groups, calls=calls,
                      batch_groups=(f"/aws/batch/{stem}-gp", "/aws/batch/other-gp"))

    report = run_cloud.lambda_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    assert report["nameStem"] == stem and report["gpBatchEnabled"] is True
    names = [f["functionName"] for f in report["functions"]]
    # The API function first; the backstop output names a function the listing no longer has.
    assert names == [api, backstop, reconcile, tick]
    by_name = {f["functionName"]: f for f in report["functions"]}
    assert by_name[backstop]["listed"] is False and by_name[api]["listed"] is True
    assert by_name[tick]["state"] == "Failed" and by_name[tick]["stateReason"] == "token=[redacted] init failed"
    assert by_name[api]["environmentNames"] == ["ASPNETCORE_ENVIRONMENT", "ConnectionStrings__Redis"]
    logs = {log["logGroup"]: log for log in report["logs"]}
    api_log = logs[f"/aws/lambda/{api}"]
    assert api_log["functionName"] == api and len(api_log["lines"]) == 300
    assert api_log["lines"][-1] == "operations.proposals: dependency-unavailable Password=[redacted]"
    # Merged by timestamp across streams, newest last, trailing newline stripped.
    assert api_log["lines"][-2] == "s2 line 199" and api_log["lines"][-3] == "s1 line 199"
    assert "error" in logs[f"/aws/lambda/{backstop}"] and "token=[redacted]" in logs[f"/aws/lambda/{backstop}"]["error"]
    assert logs[f"/aws/lambda/{tick}"]["lines"] == ["tick [redacted-aws-key-id]"]
    batch = logs[f"/aws/batch/{stem}-gp"]
    assert batch["batch"] is True and batch["lines"] == ["gp job running postgres://h:[redacted]@db/x"]
    assert "/aws/batch/other-gp" not in logs and f"/aws/lambda/{stem}x-honua" not in logs
    get_logs = next(c for c in calls if "get-log-events" in c)
    assert get_logs[get_logs.index("--limit") + 1] == "300"
    # Read-only: no AWS verb but list/describe/get, and terraform only reads outputs.
    for call in calls:
        if call[0] == "aws":
            assert call[2].split("-")[0] in ("list", "describe", "get"), call
        else:
            assert call[2:4] == ["output", "-raw"], call
    dumped = json.dumps(report)
    assert "hunter2" not in dumped and "Production" not in dumped and "AKIA" not in dumped


def test_lambda_diagnostics_skip_batch_when_disabled_and_report_unreadable_streams(monkeypatch):
    target = serverless(run_id="r2")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    api = "cell-dev-honua"
    calls = []
    run = _lambda_cli({"lambda_function_name": api, "gp_batch_enabled": "false"},
                      functions=[{"FunctionName": api}], calls=calls, failing_streams=("b",),
                      groups={f"/aws/lambda/{api}": {"a": [(1, "started")], "b": [(2, "lost")]}})
    report = run_cloud.lambda_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    assert report["gpBatchEnabled"] is False
    assert not any("describe-log-groups" in c for c in calls)
    log = report["logs"][0]
    assert log["lines"] == ["started"] and "Password=[redacted]" in log["errors"][0]
    # No API function output: the cell is not a Lambda cell this collector can read.
    with pytest.raises(ValueError, match="lambda_function_name"):
        run_cloud.lambda_readiness_diagnostics(
            target, run=_lambda_cli({}, functions=[], groups={}), redact=run_cloud._redact_log)


def test_diagnose_phase_writes_lambda_diagnostics_for_a_serverless_cell(monkeypatch, capsys):
    cj = run_cloud.cloud_journey
    _phase_env(monkeypatch, run_id="diag-l1")
    directory = cj.cell_dir("aws-serverless/redis-on")
    seen = []
    monkeypatch.setattr(run_cloud, "ecs_readiness_diagnostics",
                        lambda target, **kw: pytest.fail("a serverless cell has no ECS service"))
    monkeypatch.setattr(run_cloud, "lambda_readiness_diagnostics", lambda target, **kw: seen.append(target.name) or {
        "functions": [{"functionName": "c-honua", "state": "Active", "lastUpdateStatus": "Successful",
                       "stateReason": "", "environmentNames": ["ConnectionStrings__Redis"]}],
        "logs": [{"functionName": "c-honua", "logGroup": "/aws/lambda/c-honua",
                  "lines": ["operations.proposals: dependency-unavailable"]},
                 {"batch": True, "logGroup": "/aws/batch/c-gp", "error": "gone"}]})
    argv = ["--phase", "diagnose", "--target", "aws-serverless", "--redis", "on"]
    try:
        capsys.readouterr()
        assert run_cloud.main(argv) == 0 and seen == ["aws-serverless"]
        printed = capsys.readouterr().out
        assert "function c-honua: state=Active" in printed and "environment names ConnectionStrings__Redis" in printed
        assert "::group::/aws/lambda/c-honua (c-honua)" in printed
        assert "operations.proposals: dependency-unavailable" in printed
        assert "::group::/aws/batch/c-gp (batch)" in printed and "log unavailable: gone" in printed
        written = json.loads((directory / run_cloud.LAMBDA_DIAGNOSTICS_NAME).read_text())
        assert written["cell"] == "aws-serverless/redis-on"
        assert not (directory / run_cloud.DIAGNOSTICS_NAME).exists()
        monkeypatch.setattr(run_cloud, "lambda_readiness_diagnostics",
                            lambda target, **kw: (_ for _ in ()).throw(RuntimeError("Password=x denied")))
        assert run_cloud.main(argv) == 0
        assert "Password=[redacted]" in json.loads(
            (directory / run_cloud.LAMBDA_DIAGNOSTICS_NAME).read_text())["error"]
    finally:
        shutil.rmtree(cj.EVIDENCE / "diag-l1", ignore_errors=True)


@pytest.mark.parametrize("line, secret, kept", [
    ("redis://default:s3cr3t@cache:6379", "s3cr3t", "redis://default:[redacted]@cache:6379"),
    ("ConnectionStrings__Redis=cache:6379,password=s3cr3t", "s3cr3t", "ConnectionStrings__Redis=[redacted]"),
    ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP", "[redacted-aws-key-id]"),
])
def test_redact_log_strips_uri_userinfo_connection_strings_and_aws_key_ids(line, secret, kept):
    redacted = run_cloud._redact_log(line)
    assert secret not in redacted and kept in redacted


def test_redact_log_keeps_a_setting_name_in_an_error():
    assert run_cloud._redact_log("ConnectionStrings:Redis is required") == "ConnectionStrings:Redis is required"


def test_diagnose_phase_runs_for_every_cell_and_prints_the_log_tail(monkeypatch, capsys):
    cj = run_cloud.cloud_journey
    directory = _phase_env(monkeypatch, run_id="diag-c2")
    seen = []
    monkeypatch.setattr(run_cloud, "ecs_readiness_diagnostics",
                        lambda target, **kw: seen.append(target.name) or {"tasks": [], "logs": []})
    argv = ["--phase", "diagnose", "--target", "aws-ecs", "--redis", "off"]
    try:
        # A ready cell is diagnosed too: a task that served and later exited is only explainable
        # from its log, and the log group is destroyed with the cell.
        run_cloud._write_json(directory / run_cloud.HANDOFF_NAME, {"cell": "aws-ecs/redis-off", "ready": True})
        assert run_cloud.main(argv) == 0 and seen == ["aws-ecs"]
        run_cloud._write_json(directory / run_cloud.HANDOFF_NAME, {"cell": "aws-ecs/redis-off", "ready": False})
        monkeypatch.setattr(run_cloud, "ecs_readiness_diagnostics", lambda target, **kw: seen.append(target.name) or {
            "tasks": [{"taskArn": "t1", "lastStatus": "STOPPED", "stopCode": "EssentialContainerExited",
                       "stoppedReason": "Essential container in task exited",
                       "containers": [{"name": "honua", "exitCode": 134, "reason": ""}]}],
            "taskDefinitions": [{"taskDefinitionArn": "td:1", "containers": [
                {"name": "honua", "environmentNames": ["ConnectionStrings__Redis"], "valueFromNames": []}]}],
            "logs": [{"taskArn": "t1", "logStream": "ecs/honua/t1", "lines": ["Unhandled exception: boom"]}]})
        capsys.readouterr()
        assert run_cloud.main(argv) == 0 and seen == ["aws-ecs", "aws-ecs"]
        printed = capsys.readouterr().out
        assert "exitCode=134" in printed and "Unhandled exception: boom" in printed
        assert "environment names ConnectionStrings__Redis" in printed
        written = json.loads((directory / run_cloud.DIAGNOSTICS_NAME).read_text())
        assert written["cell"] == "aws-ecs/redis-off"
        # A diagnostics error is recorded, never raised: the step must not change the verdict.
        monkeypatch.setattr(run_cloud, "ecs_readiness_diagnostics",
                            lambda target, **kw: (_ for _ in ()).throw(RuntimeError("Password=x denied")))
        assert run_cloud.main(argv) == 0
        assert "Password=[redacted]" in json.loads((directory / run_cloud.DIAGNOSTICS_NAME).read_text())["error"]
    finally:
        shutil.rmtree(cj.EVIDENCE / "diag-c2", ignore_errors=True)


def test_cell_teardown_captures_cell_diagnostics_before_destroy_and_uploads_them():
    _, cell = _cloud_workflows()
    steps = cell["jobs"]["teardown"]["steps"]
    names = [step.get("name", "") for step in steps]
    diagnose = names.index("Capture cell readiness diagnostics")
    destroy = next(i for i, name in enumerate(names) if name.startswith("Tear down"))
    restore = names.index("Restore the Terraform state")
    assert restore < diagnose < destroy
    step = steps[diagnose]
    # Both GA cell kinds: a Lambda cell's log groups are destroyed with it just like an ECS cell's.
    assert step["continue-on-error"] is True
    assert "inputs.target == 'aws-ecs'" in step["if"] and "inputs.target == 'aws-serverless'" in step["if"]
    assert "--phase diagnose" in step["run"]
    # Each kind initialises its own Terraform root before reading outputs.
    assert "aws-ecs) ROOT=examples/aws ;;" in step["run"]
    assert "aws-serverless) ROOT=examples/aws-serverless ;;" in step["run"]
    upload = next(s for s in steps if s.get("name") == "Upload cloud gate-report (per cell)")
    assert "e2e/cloud-evidence/**/diagnostics-*.json" in upload["with"]["path"]


@pytest.mark.parametrize("line, secret, kept", [
    ("Password=hunter2;Host=db", "hunter2", "Password=[redacted]"),
    ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig", "Authorization: Bearer [redacted]"),
    ("authorization:bearer tok-123 retry", "tok-123", "authorization:bearer [redacted] retry"),
    ("connecting to postgres://honua:p%40ss@db.example:5432/honua", "p%40ss",
     "postgres://honua:[redacted]@db.example:5432/honua"),
    ("dsn=postgresql://admin:S3cret@10.0.0.5/gis", "S3cret", "postgresql://admin:[redacted]@10.0.0.5/gis"),
])
def test_redact_log_strips_key_values_bearer_tokens_and_postgres_uri_passwords(line, secret, kept):
    redacted = run_cloud._redact_log(line)
    assert secret not in redacted and kept in redacted



# ---- genuine-model canary on the nightly cell (fix unit J3) ------------------------------------------
LOCK = "sha256:" + "c" * 64


def _model_cell(monkeypatch, run_id, *, deterministic_status):
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    directory = run_cloud.cloud_journey.cell_dir("aws-ecs/redis-off")
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "receipt-1.json"
    receipt.write_text(json.dumps({"status": deterministic_status}))
    (directory / run_cloud.JOURNEY_NAME).write_text(json.dumps(
        {"journeyAttempts": [{"number": 1, "receipt": str(receipt.relative_to(run_cloud.E2E_DIR))}]}))
    return directory


def _canary_receipt(attempt, status, attribution):
    return {"mode": "genuine-model", "cell": "aws-ecs/redis-off", "attempt": attempt, "lockDigest": LOCK,
            "status": status, "failureAttribution": attribution,
            "completedAt": f"2026-10-08T12:0{attempt}:00.000000Z"}


def test_model_canary_is_refused_without_this_jobs_passing_deterministic_journey(monkeypatch):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990001", deterministic_status="fail")
    try:
        calls = []
        report = run_cloud.model_canary_phase(
            {"cell": "aws-ecs/redis-off", "endpoint": "http://cell.example"}, admin_key="k",
            lock_digest=LOCK, run=lambda *a, **k: calls.append(a))
        assert calls == []
        assert report["status"] == "fail" and "deterministic journey did not pass" in report["why"]
        assert json.loads((directory / "model-journey" / "gate-report-journey.json").read_text())["status"] == "fail"
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990001", ignore_errors=True)


def test_model_canary_binds_each_attempt_to_the_lock_and_reports_the_nightly_row(monkeypatch):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990002", deterministic_status="pass")
    try:
        calls = []

        def fake_run(argv, *, env, cwd, check):
            calls.append((argv, env))
            number = int(argv[argv.index("--attempt") + 1])
            outcome = ("fail", "infrastructure") if number == 1 else ("pass", None)
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(_canary_receipt(number, *outcome)))
            return subprocess.CompletedProcess(argv, 1 if number == 1 else 0)

        report = run_cloud.model_canary_phase(
            {"cell": "aws-ecs/redis-off", "endpoint": "https://cell.example/"}, admin_key="cell-key",
            lock_digest=LOCK, run=fake_run)
        assert len(calls) == 2
        argv, env = calls[0]
        assert argv[argv.index("--lock-digest") + 1] == LOCK
        assert argv[argv.index("--cell") + 1] == "aws-ecs/redis-off"
        assert argv[argv.index("--base-url") + 1] == "https://cell.example/api"
        # Owner ruling canary-http-cell-2026-10-08: only this cell's own host may use plain HTTP.
        assert argv[argv.index("--allow-http-cell") + 1] == "cell.example"
        assert argv[argv.index("--deterministic-receipt") + 1].endswith("receipt-1.json")
        assert env["TERMINAL_MODEL_API_KEY"] == "cell-key" and "cell-key" not in argv
        row = report["cells"][0]
        assert report["status"] == row["status"] == "pass"
        assert [(a["number"], a["status"], a["failureAttribution"], a["lockDigest"], a["driver"])
                for a in row["attempts"]] == [(1, "fail", "infrastructure", LOCK, "genuine-model"),
                                              (2, "pass", None, LOCK, "genuine-model")]
        # Canary receipts never take the receipt-*.json names the deterministic gate must account for.
        assert not list((directory / "model-journey").glob("receipt-*.json"))
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990002", ignore_errors=True)


def test_model_canary_without_a_lock_runs_once_unbound_and_records_an_honest_blocked_row(monkeypatch):
    """A manual dispatch has no nightly lock: the harness's own unbound refusal is the evidence."""
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990003", deterministic_status="pass")
    try:
        calls = []

        def fake_run(argv, *, env, cwd, check):
            calls.append(argv)
            receipt = {**_canary_receipt(1, "blocked", "infrastructure"), "lockDigest": None,
                       "notices": ["live execution requires --lock-digest: an unbound receipt counts for no lock"]}
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(receipt))
            return subprocess.CompletedProcess(argv, 1)

        report = run_cloud.model_canary_phase(
            {"cell": "aws-ecs/redis-off", "endpoint": "http://cell.example"}, admin_key="cell-key",
            lock_digest=None, run=fake_run)
        assert len(calls) == 1, "an unbound run is never retried"
        assert "--lock-digest" not in calls[0]
        assert calls[0][calls[0].index("--deterministic-receipt") + 1].endswith("receipt-1.json")
        assert report["status"] == report["cells"][0]["status"] == "blocked"
        assert report["lockDigest"] is None and "no platform lock to bind to" in report["why"]
        assert "manual dispatch" in report["why"] and "--lock-digest" in report["why"]
        written = json.loads((directory / "model-journey" / "gate-report-journey.json").read_text())
        assert written["status"] == "blocked"
        assert (directory / "model-journey" / "model-canary-1.json").is_file()
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990003", ignore_errors=True)


def test_model_canary_without_a_lock_never_turns_a_claimed_pass_or_a_failure_into_blocked(monkeypatch):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    _model_cell(monkeypatch, "990004", deterministic_status="pass")
    try:
        for status, attribution, reason in (("pass", None, "claims a pass"),
                                            ("fail", "infrastructure", "did not stop at the harness")):
            def fake_run(argv, *, env, cwd, check, status=status, attribution=attribution):
                receipt = {**_canary_receipt(1, status, attribution), "lockDigest": None}
                Path(argv[argv.index("--output") + 1]).write_text(json.dumps(receipt))
                return subprocess.CompletedProcess(argv, 0 if status == "pass" else 1)

            report = run_cloud.model_canary_phase(
                {"cell": "aws-ecs/redis-off", "endpoint": "http://cell.example"}, admin_key="k",
                lock_digest=None, run=fake_run)
            assert report["status"] == "fail" and reason in report["why"], status
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990004", ignore_errors=True)


def test_model_canary_phase_arguments_bind_the_lock_only_when_given(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(run_cloud, "_phase_model_canary", lambda args: seen.append(args.lock_digest) or 0)
    keys = ["--sealed-key", str(tmp_path / "app-key.sealed"), "--private-key", str(tmp_path / "private.pem")]
    base = ["--phase", "model-canary", "--target", "aws-ecs", "--redis", "off"]
    assert run_cloud.main(base + keys) == 0
    assert run_cloud.main(base + keys + ["--lock-digest", LOCK]) == 0
    assert seen == [None, LOCK]
    for bad in (base + ["--lock-digest", LOCK],                    # the cell's key material is required
                base + keys + ["--lock-digest", ""],               # an empty digest is a wiring error, not unbound
                base + keys + ["--lock-digest", "sha256:nothex"]):
        with pytest.raises(SystemExit):
            run_cloud.main(bad)
    assert seen == [None, LOCK]


@pytest.mark.parametrize("lock, status, code, notice", [
    (None, "blocked", 0, True), (None, "fail", 1, False), (LOCK, "blocked", 1, False), (LOCK, "pass", 0, False)])
def test_model_canary_step_is_green_only_on_a_pass_or_an_unbound_blocked_row(monkeypatch, capsys,
                                                                             lock, status, code, notice):
    monkeypatch.setattr(run_cloud, "open_key", lambda *_: "k")
    monkeypatch.setattr(run_cloud, "model_canary_phase",
                        lambda state, **kwargs: {"status": status, "why": "why", "lockDigest": kwargs["lock_digest"]})
    args = mock.Mock(target="aws-ecs", redis="off", sealed_key=Path("s"), private_key=Path("p"),
                     lock_digest=lock, max_attempts=2)
    assert run_cloud._phase_model_canary(args) == code
    assert ("::notice::genuine-model canary blocked" in capsys.readouterr().out) is notice


# ---- honua-release#450: per-run HTTPS hostname + demo CORS origin on the ECS cells -----------------
CELL_ZONE = "Z089181827C9GKIKHXUTT"
_DOMAIN_ROOT_VARS = ("domain_name", "route53_zone_id", "allow_https_ingress_cidrs",
                     "allow_http_ingress_cidrs", "cors_allowed_origins")


def _cell_dns(monkeypatch, zone=CELL_ZONE, parent="demo.honua.io"):
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", zone)
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_PARENT", parent)


def test_cell_dns_variables_never_leak_into_a_test():
    assert "HONUA_AWS_CELL_DNS_ZONE_ID" not in os.environ
    assert "HONUA_AWS_CELL_DNS_PARENT" not in os.environ


def test_ecs_cell_hostname_is_per_run_per_cell_and_under_the_parent(monkeypatch):
    import re as _re
    _ecs_env(monkeypatch)
    _cell_dns(monkeypatch)
    names = {(run, redis): ecs(run_id=run).cell_domain(redis)[0]
             for run in ("38038433205", "38038433206") for redis in (True, False)}
    assert names[("38038433205", False)] == "38038433205-aws-ecs-redis-off.cert.demo.honua.io"
    assert names[("38038433205", True)] == "38038433205-aws-ecs-redis-on.cert.demo.honua.io"
    # Two cells of one run, and the same cell of two runs, never share a name (run ids sharing a
    # prefix included: the label carries the whole run id, unlike the 18-character name_prefix).
    assert len(set(names.values())) == 4
    for fqdn in names.values():
        label = fqdn.split(".", 1)[0]
        assert fqdn.endswith(".cert.demo.honua.io") and len(fqdn) <= 64
        assert _re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        # The reviewed demo CSP admits https://(*.)honua.io only (honua-site csp-bootstrap.js).
        assert _re.fullmatch(r"https://(?:[a-z0-9-]+\.)*honua\.io", "https://" + fqdn)
    # Deterministic: the teardown job's fresh process recomputes the apply's name.
    assert ecs(run_id="38038433205").cell_domain(False) == (names[("38038433205", False)], CELL_ZONE)
    # A hostile or over-long run id still yields one valid, bounded, distinct label.
    long_a = ecs(run_id="Local_Run." + "9" * 80).cell_domain_label(False)
    long_b = ecs(run_id="Local_Run." + "9" * 79 + "8").cell_domain_label(False)
    assert long_a != long_b and _re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", long_a)
    assert len(f"{long_a}.cert.demo.honua.io") <= 64
    # Uppercase or trailing-dot parents normalise; the label sits one level under cert.<parent>.
    _cell_dns(monkeypatch, parent="Demo.Honua.IO.")
    assert ecs(run_id="r1").cell_domain(True)[0] == "r1-aws-ecs-redis-on.cert.demo.honua.io"


def test_ecs_cell_with_the_dns_variables_applies_https_domain_and_https_ingress(monkeypatch):
    _ecs_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS)
        for redis in (False, True):
            if redis:
                monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
            values = _tf_vars(ecs(run_id="38038433205")._vars(redis))
            mode = "on" if redis else "off"
            assert values["domain_name"] == f"38038433205-aws-ecs-redis-{mode}.cert.demo.honua.io"
            assert values["route53_zone_id"] == CELL_ZONE
            # HTTPS ingress for the runner's /32; plain-HTTP ingress is not requested.
            assert json.loads(values["allow_https_ingress_cidrs"]) == ["192.0.2.10/32"]
            assert "allow_http_ingress_cidrs" not in values
            assert json.loads(values["cors_allowed_origins"]) == ["http://127.0.0.1:18099"]
        # Destroy recomputes the same name, so teardown plans against what apply created.
        assert _tf_vars(ecs(run_id="38038433205")._vars(False, destroy=True))["domain_name"] == \
            "38038433205-aws-ecs-redis-off.cert.demo.honua.io"


def test_ecs_cell_without_the_dns_variables_keeps_plain_http_and_says_so(monkeypatch, capsys):
    _ecs_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS)
        target = ecs(run_id="r1")
        values = _tf_vars(target._vars(False))
        assert "domain_name" not in values and "route53_zone_id" not in values
        assert "allow_https_ingress_cidrs" not in values
        assert json.loads(values["allow_http_ingress_cidrs"]) == ["192.0.2.10/32"]
        # The CORS origin does not depend on the hostname.
        assert json.loads(values["cors_allowed_origins"]) == ["http://127.0.0.1:18099"]
        assert target.cell_domain(False) is None
        monkeypatch.setattr(target, "_tf", lambda root, *args, check=True: subprocess.CompletedProcess(
            args, 0, "http://cell.elb.amazonaws.com\n", ""))
        assert target.provision(redis_enabled=False) == "http://cell.elb.amazonaws.com"
        assert "plain-HTTP" in capsys.readouterr().out
        # Blank values are unset, not a malformed zone.
        monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", " ")
        monkeypatch.setenv("HONUA_AWS_CELL_DNS_PARENT", "")
        assert "domain_name" not in _tf_vars(ecs(run_id="r1")._vars(False))


def test_ecs_cell_logs_its_https_hostname_on_provision(monkeypatch, capsys):
    _ecs_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS)
        target = ecs(run_id="38038433205")
        monkeypatch.setattr(target, "_tf", lambda root, *args, check=True: subprocess.CompletedProcess(
            args, 0, "https://38038433205-aws-ecs-redis-off.cert.demo.honua.io\n", ""))
        assert target.provision(redis_enabled=False).startswith("https://")
        assert "HTTPS cell hostname 38038433205-aws-ecs-redis-off.cert.demo.honua.io" in capsys.readouterr().out


@pytest.mark.parametrize("zone,parent,match", [
    (CELL_ZONE, "", "HONUA_AWS_CELL_DNS_PARENT is unset"),
    ("", "demo.honua.io", "HONUA_AWS_CELL_DNS_ZONE_ID is unset"),
    ("not-a-zone", "demo.honua.io", "hosted zone id"),
    (CELL_ZONE, "demo honua io", "not a DNS name"),
    (CELL_ZONE, "a" * 40 + ".honua.io", "64-octet"),
])
def test_ecs_cell_dns_misconfiguration_refuses_provision_but_never_destroy(monkeypatch, zone, parent, match):
    _ecs_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", zone)
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_PARENT", parent)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS)
        with pytest.raises(ProvisionError, match=match):
            ecs(run_id="r1")._vars(False)
        assert "domain_name" not in _tf_vars(ecs(run_id="r1")._vars(False, destroy=True))


def test_ecs_cell_dns_on_a_root_without_the_domain_inputs_refuses(monkeypatch):
    _ecs_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", "allow_http_ingress_cidrs")
        with pytest.raises(ProvisionError, match="domain_name"):
            ecs(run_id="r1")._vars(False)
        values = _tf_vars(ecs(run_id="r1")._vars(False, destroy=True))
        # An older root without cors_allowed_origins gets no CORS var (Terraform rejects undeclared vars).
        assert "domain_name" not in values and "cors_allowed_origins" not in values


def test_serverless_cell_gets_no_custom_domain_or_cors_input(monkeypatch):
    # aws-serverless custom-domain wiring is out of scope; its spec must not inherit the ECS one.
    from targets.terraform_target import SERVERLESS_SPEC
    assert not SERVERLESS_SPEC.cell_domain and SERVERLESS_SPEC.cors_allowed_origins == ()
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", *_DOMAIN_ROOT_VARS)
        values = _tf_vars(serverless(run_id="r1")._vars(False))
        assert not {"domain_name", "route53_zone_id", "cors_allowed_origins"} & set(values)
        assert serverless(run_id="r1").cell_domain(False) is None


def test_admit_resolves_the_https_cell_hostname_to_its_alb_and_opens_443(monkeypatch):
    _cell_dns(monkeypatch)
    host = "38038433205-aws-ecs-redis-off.cert.demo.honua.io"
    calls = []
    balancers = {"LoadBalancers": [
        {"DNSName": "other.elb.amazonaws.com", "SecurityGroups": ["sg-other"]},
        {"DNSName": "cell-alb.us-east-1.elb.amazonaws.com", "SecurityGroups": ["sg-cell"]}]}
    alias = {"ResourceRecordSets": [{"Name": host + ".", "Type": "A", "AliasTarget": {
        "DNSName": "dualstack.Cell-ALB.us-east-1.elb.amazonaws.com.", "HostedZoneId": "Z35SXDOTRQ7X7K"}}]}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["elbv2", "describe-load-balancers"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(balancers), "")
        if argv[1:3] == ["route53", "list-resource-record-sets"]:
            assert argv[argv.index("--hosted-zone-id") + 1] == CELL_ZONE
            return subprocess.CompletedProcess(argv, 0, json.dumps(alias), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    ecs(run_id="38038433205").admit(f"https://{host}", "198.51.100.7/32", run=run)
    authorize = [c for c in calls if c[1:3] == ["ec2", "authorize-security-group-ingress"]]
    assert len(authorize) == 1 and authorize[0][authorize[0].index("--group-id") + 1] == "sg-cell"
    permission = json.loads(authorize[0][authorize[0].index("--ip-permissions") + 1])[0]
    assert permission["FromPort"] == permission["ToPort"] == 443
    assert permission["IpRanges"][0]["CidrIp"] == "198.51.100.7/32"
    # A name the zone does not alias to a balancer admits nobody.
    alias["ResourceRecordSets"] = [{"Name": "other.cert.demo.honua.io.", "Type": "A",
                                    "AliasTarget": {"DNSName": "cell-alb.us-east-1.elb.amazonaws.com."}}]
    with pytest.raises(ProvisionError, match="no load balancer"):
        ecs(run_id="38038433205").admit(f"https://{host}", "198.51.100.7/32", run=run)


def test_teardown_confirms_the_cells_dns_names_and_certificate_are_gone(monkeypatch, capsys):
    _ecs_env(monkeypatch)
    _cell_dns(monkeypatch)
    own = "38038433205-aws-ecs-redis-on.cert.demo.honua.io"
    zone = {"ResourceRecordSets": [
        {"Name": "demo.honua.io.", "Type": "A"},
        {"Name": "_acme.demo.honua.io.", "Type": "CNAME"},
        {"Name": "99-aws-ecs-redis-off.cert.demo.honua.io.", "Type": "A"}]}
    certs = {"CertificateSummaryList": [{"DomainName": "demo.honua.io", "CertificateArn": "arn:demo"}]}

    def run(argv, **kwargs):
        if argv[1:3] == ["route53", "list-resource-record-sets"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(zone), "")
        if argv[1:3] == ["acm", "list-certificates"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(certs), "")
        raise AssertionError(argv)

    target = ecs(run_id="38038433205")
    target._cell_dns_leftovers(True, run=run)  # nothing of this cell remains: passes
    out = capsys.readouterr().out
    warning = [line for line in out.splitlines() if line.startswith("::warning title=cell DNS names present")]
    assert len(warning) == 1 and warning[0].rsplit(": ", 1)[1].split(", ") == [
        "99-aws-ecs-redis-off.cert.demo.honua.io"]
    zone["ResourceRecordSets"].append({"Name": f"_0123abc.{own}.", "Type": "CNAME"})
    with pytest.raises(ProvisionError, match="1 Route53 record"):
        target._cell_dns_leftovers(True, run=run)
    zone["ResourceRecordSets"].pop()
    certs["CertificateSummaryList"].append({"DomainName": own, "CertificateArn": "arn:cell"})
    with pytest.raises(ProvisionError, match="1 ACM certificate"):
        target._cell_dns_leftovers(True, run=run)
    # A listing that cannot be read fails closed: cleanup that was not verified is not certified.
    with pytest.raises(ProvisionError, match="could not verify"):
        target._cell_dns_leftovers(True, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "denied"))

    # Unset variables: no AWS call at all.
    def refuse(*args, **kwargs):
        raise AssertionError("no AWS call without the cell DNS variables")
    monkeypatch.delenv("HONUA_AWS_CELL_DNS_ZONE_ID")
    monkeypatch.delenv("HONUA_AWS_CELL_DNS_PARENT")
    target._cell_dns_leftovers(True, run=refuse)


def test_provision_report_records_the_endpoint_transport(monkeypatch):
    stub = _ServingStub("https://38038433205-aws-ecs-redis-off.cert.demo.honua.io")
    monkeypatch.setattr(run_cloud, "_READY_ATTEMPTS", 1)
    monkeypatch.setattr(run_cloud, "_READY_DELAY_SECONDS", 0)
    monkeypatch.setattr(run_cloud, "make_fetch", lambda **kwargs: (lambda url: cc.HttpResponse(200, "")))
    monkeypatch.setattr(run_cloud, "run_canonical", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud.canary_probes, "run_canary", lambda *a, **k: [])
    monkeypatch.setattr(run_cloud, "seed_cell", lambda *a, **k: None)
    state = run_cloud.provision_phase(stub, "stub", require_real=False, redis_enabled=False)
    assert state["report"]["transport"] == {
        "scheme": "https", "host": "38038433205-aws-ecs-redis-off.cert.demo.honua.io"}


def test_cell_workflow_threads_the_cell_dns_repository_variables():
    _, cell = _cloud_workflows()
    steps = {step.get("name", ""): step for job in cell["jobs"].values() for step in job.get("steps", [])}
    provision = next(step for name, step in steps.items() if name.startswith("Provision "))
    teardown = next(step for name, step in steps.items() if name.startswith("Tear down "))
    admit = next(step for name, step in steps.items() if name.startswith("Admit the journey runner"))
    for step in (provision, teardown):
        for var in ("HONUA_AWS_CELL_DNS_ZONE_ID", "HONUA_AWS_CELL_DNS_PARENT"):
            assert step["env"][var] == "${{ vars." + var + " }}"
    assert admit["env"]["HONUA_AWS_CELL_DNS_ZONE_ID"] == "${{ vars.HONUA_AWS_CELL_DNS_ZONE_ID }}"
    # Step-scoped, never workflow-level, and the credential-free journey job never sees them.
    assert not {"HONUA_AWS_CELL_DNS_ZONE_ID", "HONUA_AWS_CELL_DNS_PARENT"} & set(cell["env"])
    assert "HONUA_AWS_CELL_DNS" not in json.dumps(cell["jobs"]["journey"])
