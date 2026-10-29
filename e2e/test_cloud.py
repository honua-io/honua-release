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
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
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
                      "HONUA_ENABLE_BEDROCK_AI", "HONUA_BEDROCK_AI_MODEL", "HONUA_CLOUD_COST_CEILING_USD", "HONUA_RUN_URL",
                      "CELL_DIR", "CELL_ARTIFACT", "HONUA_DEPLOYMENT_PREPARED", "HONUA_CLOUD_TENANT_FIXTURE",
                      "HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN",
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


TEST_DIGEST = "sha256:" + "d" * 64
TEST_CHILD = "sha256:" + "c" * 64


def _expected_ga(ids, excluded=None):
    return {"expectedGa": ids, "excluded": excluded or []}


def _observation(sha=TEST_SERVER_SHA, digest=TEST_DIGEST):
    """A generated observation bound to the test candidate (tools/expected_ga_snapshot.py shape)."""
    return {"schemaVersion": "honua.expected-ga-observation.v1",
            "boundCandidate": {"serverSha": sha, "imageIndexDigest": digest, "bootedPlatform": "amd64",
                               "bootedChildDigest": TEST_CHILD},
            "deploymentRevision": sha, "deploymentRevisionSource": "commit-sha", "capabilities": []}


TEST_SERVER = {"sha": TEST_SERVER_SHA, "digest": TEST_DIGEST, "platformDigests": {"amd64": TEST_CHILD}}


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
                                     expected=expected, frozen_server=TEST_SERVER, observation=_observation())
    assert r.status == "pass"
    assert r.evidence["expectedGaCount"] == 2
    assert r.evidence["availableCountUnauthenticated"] == 1


def test_capability_manifest_revision_match_passes():
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    r = cc.check_capability_manifest(
        "http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
        expected=_expected_ga([]), frozen_server=TEST_SERVER, observation=_observation(), enforcement="strict")
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


def test_capability_manifest_observation_of_another_server_fails_closed_by_default():
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))])
    for observation, named in ((_observation(sha="b" * 40), "b" * 40),
                               (_observation(digest="sha256:" + "e" * 64), "e" * 64)):
        result = cc.check_capability_manifest(
            "http://x", fetch, expected=_expected_ga([]), observation=observation, frozen_server=TEST_SERVER)
        assert result.status == "fail"
        assert named in result.why and "expected-GA snapshot is not bound to the candidate" in result.why


def test_capability_manifest_unbound_or_missing_observation_fails_closed_in_all_modes(monkeypatch, tmp_path):
    body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))])
    unbound = _observation()
    del unbound["boundCandidate"]
    for enforcement in ("strict", "bootstrap"):
        result = cc.check_capability_manifest("http://x", fetch, expected=_expected_ga([]), observation=unbound,
                                              frozen_server=TEST_SERVER, enforcement=enforcement)
        assert result.status == "fail"
    # The default lookup with no generated observation file is a refusal, never a skip.
    monkeypatch.setattr(cc.ego, "OBSERVATION_PATH", tmp_path / "absent.json")
    result = cc.check_capability_manifest("http://x", fetch, expected=_expected_ga([]), frozen_server=TEST_SERVER)
    assert result.status == "fail" and "unbound" in result.why
    # A candidate whose digest is unknown cannot be bound either.
    result = cc.check_capability_manifest("http://x", fetch, expected=_expected_ga([]), observation=_observation(),
                                          frozen_server={"sha": TEST_SERVER_SHA})
    assert result.status == "fail"


def test_committed_observation_is_bound_to_the_committed_manifest_server():
    result = cc.check_capability_manifest(
        "http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, json.dumps(
            {"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})))]),
        expected=_expected_ga([]))
    assert result.status == "pass", result.why


def test_capability_manifest_fail_on_missing_or_unsupported_id():
    expected = _expected_ga(["a.one", "a.missing"])
    body = json.dumps({
        "schemaVersion": "honua.capability_manifest.v1",
        "capabilities": [{"id": "a.one", "supported": False, "available": False}],
    })
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected=expected, frozen_server=TEST_SERVER, observation=_observation())
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
                                     frozen_server=TEST_SERVER, observation=_observation())
    assert r.status == "pass" and r.evidence["authenticated"] is True

    auth_fetch_stale = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, unauth_body))])
    r2 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_stale,
                                      frozen_server=TEST_SERVER, observation=_observation())
    assert r2.status == "fail" and "available=true when authenticated" in r2.why

    # An expected-GA id entirely OMITTED from the authenticated manifest (not just present-but-
    # unavailable) must also fail, not silently drop out of the `unavailable` list.
    auth_omitted_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    auth_fetch_omitted = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, auth_omitted_body))])
    r3 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_omitted,
                                      frozen_server=TEST_SERVER, observation=_observation())
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
                                            frozen_server=TEST_SERVER, observation=_observation())

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
                                         frozen_server=TEST_SERVER, observation=_observation())
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
    # Teardown on a fresh runner has no password: destroy gets a random throwaway, not one derived
    # from the run id. The id is a long token so it cannot appear in a random password by chance
    # (a two-character "r1" did, on e2e-cloud-aws run 38091910280, and failed the self-test).
    leak_guard = "run-id-never-in-the-password-6f1c2a9e"
    first = _tf_vars(serverless(run_id=leak_guard)._vars(False, destroy=True))["honua_admin_password"]
    second = _tf_vars(serverless(run_id=leak_guard)._vars(False, destroy=True))["honua_admin_password"]
    assert first != second and leak_guard not in first and "6f1c2a9e" not in first
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
    import normal_cell_retirement
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

    monkeypatch.setattr(normal_cell_retirement, "retire", lambda _target, root, variables, mode: _record(root, *variables))
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
    # The post-destroy leftover check has its own tests (test_aws_eks.py).
    monkeypatch.setattr(target, "_verify_teardown", lambda *a, **k: None)

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
    monkeypatch.setattr(target, "_verify_teardown", lambda *a, **k: None)
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


def _run_serving(monkeypatch, *, ready_status, checks, canary, require_real=False, extended=None,
                 stub=None, canary_calls=None):
    """Drive run_cloud.run() against a stub that provisions, with canned probe verdicts."""
    stub = stub or _ServingStub()
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
        monkeypatch.setattr(run_cloud.canary_probes, "run_canary",
                            lambda *a, **k: (canary_calls.append((a, k)) if canary_calls is not None
                                             else None) or canary)
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
    # A new test producer invocation replaces this numbered artifact, including its sidecar.
    (directory / f"serving-refusal-{number}.json").unlink(missing_ok=True)
    path.write_text(json.dumps(receipt))
    record = {"number": number, "cell": receipt["target"]["id"], "runId": "offline-run",
        "runAttempt": "2", "candidateDigest": cj.candidate_digest(), "receipt": path.name,
        "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "failureAttribution": None if receipt["status"] == "pass" else "infrastructure"}
    if receipt["target"]["id"].endswith("/redis-off") and receipt["status"] == "pass":
        from test_deterministic_refusal_profile import attach_fixture_profile
        path.write_bytes(attach_fixture_profile(directory, receipt, record))
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
        previews = [{"cell": f"{target}/redis-off", "status": "fail", "provisionAttempted": False} for target in cj.PREVIEW_TARGETS]
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
    monkeypatch.setattr(cj, "observe_ecs_image", lambda *a, **k: (image, "1 RUNNING task(s) of s1"))
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


def test_cloud_workflow_requires_two_ecs_ga_cells_and_keeps_lambda_and_eks_preview():
    # October16 owner amendment explicitly supersedes the prior EKS GA scope; the founder re-tier of
    # 2026-10-25 moves Lambda (aws-serverless) to Preview and keeps AWS ECS as the only GA cloud target.
    cj = run_cloud.cloud_journey
    workflow, cell = _cloud_workflows()
    job = workflow["jobs"]["parity"]
    assert set(cj.GA_CELLS) == {f"aws-ecs/redis-{mode}" for mode in ("off", "on")}
    assert cj.PREVIEW_TARGETS == ("aws-serverless", "aws-eks")
    # Operator ruling 2026-10-22: target=all is the GA targets only; a Preview target runs only on
    # an explicit dispatch, and the report names it "not certified (preview)".
    matrix = job["strategy"]["matrix"]["target"]
    assert "(inputs.target == '' || inputs.target == 'all') && '[\"aws-ecs\"]'" in matrix
    assert '"aws-eks"' not in matrix and '"aws-serverless"' not in matrix
    assert "aws-serverless" in workflow[True]["workflow_dispatch"]["inputs"]["target"]["options"]
    assert "aws-eks" in workflow[True]["workflow_dispatch"]["inputs"]["target"]["options"]
    # rc.3: examples/aws-mixed does not exist; the cell is out of the matrix until honua-iac#209.
    assert "aws-mixed" not in job["strategy"]["matrix"]["target"]
    assert "aws-mixed" not in workflow[True]["workflow_dispatch"]["inputs"]["target"]["options"]
    assert job["with"]["preview"] == "${{ matrix.target == 'aws-eks' || matrix.target == 'aws-serverless' }}"
    assert job["strategy"]["fail-fast"] is False and job["strategy"]["max-parallel"] == 1
    # Preview tolerance lives on the called cell's jobs; a reusable-workflow call cannot carry it.
    assert all(cell_job["continue-on-error"] == "${{ inputs.preview }}"
               for name, cell_job in cell["jobs"].items() if name != "teardown")
    assert cell["jobs"]["teardown"]["continue-on-error"] is False
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
        # EKS failures stay informational; required ECS/Lambda failures and cost ceilings still refuse.
        previews = [{"cell": f"{target}/redis-{redis}", "status": "fail", "provisionAttempted": False}
                    for target in (cj.PREVIEW_TARGETS or ("stub",)) for redis in ("off", "on")]
        with mock.patch.object(cj, "qualify_attempt", return_value=True):
            result = _aggregate_fixture([*reports, *previews], root, full_scope=True)
            assert result["status"] == "pass" and result["certifying"] is True
            previews[0]["cost"] = {**reports[0]["cost"], "status": "fail", "amountUsd": "20.01"}
            over_budget = _aggregate_fixture([*reports, *previews], root, full_scope=True)
            assert over_budget["status"] == "fail" and "run cost ceiling exceeded" in over_budget["why"]
            del previews[0]["cost"]
            previews[0].update(provisionAttempted=True, teardown={"attempted": True, "completed": False})
            leaked = _aggregate_fixture([*reports, *previews], root, full_scope=True)
            assert leaked["status"] == "fail" and "infrastructure cleanup incomplete" in leaked["why"]
            previews[0]["teardown"]["completed"] = True
            assert _aggregate_fixture([*reports, *previews], root, full_scope=True)["status"] == "pass"
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
        with mock.patch.object(cj, "qualify_attempt", return_value=True):
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
        assert cj.observed_ecs_image(_EcsOutputs(), pinned, run=run, sleep=lambda _s: None) is None


def test_ecs_running_image_matches_the_tagless_reference_the_cell_deploys():
    # Run 38110106995: the cell deploys repository@digest (honua-iac refuses a tag), so matching the
    # manifest's tagged image found no server container and 1.1-candidate-image stayed blocked.
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    server = pinned["components"]["honua-server"]
    repository = server["image"].rsplit(":", 1)[0]
    candidate = f"{server['image']}@{server['digest']}"
    assert candidate == cj.candidate_image("aws-ecs/redis-on", pinned)
    amd64 = server["platformDigests"]["amd64"]
    for reported in (server["digest"], amd64):
        task = {"taskArn": "t1", "containers": [
            {"image": f"{repository}@{server['digest']}", "imageDigest": reported, "lastStatus": "RUNNING"}]}
        run, _ = _ecs_aws([task, {**task, "taskArn": "t2"}])
        assert cj.observed_ecs_image(_EcsOutputs(), pinned, run=run) == candidate, reported
    # The other architecture's child, a foreign digest, or another repository is not the candidate.
    for image, digest in ((f"{repository}@{server['digest']}", server["platformDigests"]["arm64"]),
                          (f"{repository}@{server['digest']}", "sha256:" + "2" * 64),
                          ("ghcr.io/honua-io/other@" + server["digest"], server["digest"])):
        run, _ = _ecs_aws([{"taskArn": "t1", "containers": [
            {"image": image, "imageDigest": digest, "lastStatus": "RUNNING"}]}])
        assert cj.observed_ecs_image(_EcsOutputs(), pinned, run=run) is None, (image, digest)


_SCALE_ROOT = ("desired_count", "max_capacity", "deployment_mode", "file_storage_provider",
               "file_storage_aws_s3_bucket_name", "file_storage_aws_s3_key_prefix")


def test_redis_on_ecs_cell_runs_two_multinode_tasks_with_the_operator_bucket(monkeypatch):
    # Run 38110106995: one task, so 6.replica-map saw a single Honua-Instance.
    from targets.terraform_target import REDIS_ON_TASKS
    assert REDIS_ON_TASKS == 2
    _ecs_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
    monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "honua-release-cell-files")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_SCALE_ROOT)
        target = ecs(run_id="38110106995")
        values = _tf_vars(target._vars(True))
        assert {k: values[k] for k in _SCALE_ROOT} == {
            "desired_count": "2", "max_capacity": "2", "deployment_mode": "MultiNode",
            "file_storage_provider": "AwsS3", "file_storage_aws_s3_bucket_name": "honua-release-cell-files",
            "file_storage_aws_s3_key_prefix": "release-cells/" + values["name_prefix"]}
        # Destroy plans with the same inputs.
        assert _tf_vars(target._vars(True, destroy=True))["desired_count"] == "2"
        # Redis-off stays the single-task serving topology.
        assert not set(_SCALE_ROOT) & set(_tf_vars(target._vars(False)))
        # A malformed bucket name refuses rather than reaching terraform.
        monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "Not_A_Bucket")
        with pytest.raises(ProvisionError, match="not an S3 bucket name"):
            target._vars(True)


def test_redis_on_ecs_cell_stays_single_task_without_the_bucket_or_on_an_older_root(monkeypatch):
    _ecs_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
    monkeypatch.delenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", raising=False)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_SCALE_ROOT)
        assert not set(_SCALE_ROOT) & set(_tf_vars(ecs(run_id="r1")._vars(True)))
        monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "honua-release-cell-files")
        _iac_root_with(monkeypatch, base, "aws", *_SCALE_ROOT[:-1])   # no key-prefix input
        assert not set(_SCALE_ROOT) & set(_tf_vars(ecs(run_id="r1")._vars(True)))
    # The serverless cell never scales this way (it gets only the shared file storage, below).
    _serverless_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", *_SCALE_ROOT)
        assert not {"desired_count", "max_capacity", "deployment_mode"} & set(
            _tf_vars(serverless(run_id="r1")._vars(True)))


_FILE_STORAGE_ROOT = ("file_storage_provider", "file_storage_aws_s3_bucket_name",
                      "file_storage_aws_s3_region", "file_storage_aws_s3_key_prefix")
_FILE_STORAGE_PASSED = ("file_storage_provider", "file_storage_aws_s3_bucket_name", "file_storage_aws_s3_key_prefix")


def test_redis_on_serverless_cell_shares_file_storage_with_the_batch_workers(monkeypatch):
    # honua-server#5916 (S33): a background import the Lambda accepts runs on Batch only with shared
    # S3 file storage; honua-iac#243 adds the inputs to examples/aws-serverless.
    from targets.terraform_target import SERVERLESS_SPEC, FILE_STORAGE_INPUTS
    assert SERVERLESS_SPEC.file_storage_inputs == FILE_STORAGE_INPUTS == _FILE_STORAGE_PASSED
    assert SERVERLESS_SPEC.file_storage_bucket_env == "HONUA_AWS_CELL_FILE_STORAGE_BUCKET"
    _serverless_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "release-cells-file-storage-585192672263")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", *_FILE_STORAGE_ROOT)
        target = serverless(run_id="38046060497")
        argv = target._vars(True)
        values = _tf_vars(argv)
        # honua-iac#243's passthrough test pins exactly this prefix for the serverless Redis-on cell.
        assert values["name_prefix"] == "honuarawsse380460"
        assert [a for a in argv if a.startswith("-var=file_storage_")] == [
            "-var=file_storage_provider=AwsS3",
            "-var=file_storage_aws_s3_bucket_name=release-cells-file-storage-585192672263",
            "-var=file_storage_aws_s3_key_prefix=release-cells/honuarawsse380460"]
        # The region is left to the module (it renders the root region); no scaling inputs.
        assert "file_storage_aws_s3_region" not in values
        assert not {"desired_count", "max_capacity", "deployment_mode"} & set(values)
        # Destroy plans with the same inputs.
        assert {k: v for k, v in _tf_vars(target._vars(True, destroy=True)).items()
                if k.startswith("file_storage_")} == {k: values[k] for k in _FILE_STORAGE_PASSED}
        # Redis-off is not given shared storage: without Redis the Lambda refuses with the typed 503.
        assert not set(_FILE_STORAGE_ROOT) & set(_tf_vars(target._vars(False)))
        # A malformed bucket refuses rather than reaching terraform.
        monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "Not_A_Bucket")
        with pytest.raises(ProvisionError, match="HONUA_AWS_CELL_FILE_STORAGE_BUCKET is not an S3 bucket name"):
            target._vars(True)
        # A declaring root with the bucket unset refuses the Redis-on cell, naming the variable;
        # Redis-off and destroy still plan.
        monkeypatch.delenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET")
        with pytest.raises(ProvisionError, match="HONUA_AWS_CELL_FILE_STORAGE_BUCKET is unset"):
            target._vars(True)
        assert not set(_FILE_STORAGE_ROOT) & set(_tf_vars(target._vars(True, destroy=True)))
        assert not set(_FILE_STORAGE_ROOT) & set(_tf_vars(target._vars(False)))


def test_serverless_file_storage_stays_off_on_an_older_root(monkeypatch):
    _serverless_env(monkeypatch)
    for env in ("release-cells-file-storage-585192672263", None):
        if env:
            monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", env)
        else:
            monkeypatch.delenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", raising=False)
        with tempfile.TemporaryDirectory() as base:
            # Today's pins declare none of them; a partial root (no key prefix) passes none either.
            for declared in ((), _FILE_STORAGE_ROOT[:2]):
                _iac_root_with(monkeypatch, base, "aws-serverless", *declared)
                assert not set(_FILE_STORAGE_ROOT) & set(_tf_vars(serverless(run_id="r1")._vars(True)))


def test_ecs_cell_file_storage_is_unchanged_by_the_serverless_passthrough(monkeypatch):
    from targets.terraform_target import ECS_SPEC
    assert ECS_SPEC.file_storage_inputs == () and ECS_SPEC.file_storage_bucket_env == ""
    _ecs_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
    monkeypatch.setenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET", "honua-release-cell-files")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_SCALE_ROOT, "file_storage_aws_s3_region")
        argv = ecs(run_id="38110106995")._vars(True)
        prefix = _tf_vars(argv)["name_prefix"]
        watched = set(_SCALE_ROOT) | {"file_storage_aws_s3_region"}
        assert [a for a in argv if a.startswith("-var=") and a[5:].split("=", 1)[0] in watched] == [
            "-var=desired_count=2", "-var=max_capacity=2", "-var=deployment_mode=MultiNode",
            "-var=file_storage_provider=AwsS3", "-var=file_storage_aws_s3_bucket_name=honua-release-cell-files",
            f"-var=file_storage_aws_s3_key_prefix=release-cells/{prefix}"]
        # Without the bucket the ECS cell still stays one task rather than refusing.
        monkeypatch.delenv("HONUA_AWS_CELL_FILE_STORAGE_BUCKET")
        assert not set(_SCALE_ROOT) & set(_tf_vars(ecs(run_id="38110106995")._vars(True)))


def test_s10_fixture_and_cell_file_storage_prefixes_agree(monkeypatch):
    # The cell's FileStorage prefix (`release-cells/<name prefix>`, the roles' object grant per
    # honua-iac#243) and the S10 staging key (`release-cells/<cell>/s10/...`) share the bucket's
    # `release-cells/` lifecycle root. The S10 object is read only through the harness-minted
    # presigned URL, never with the cell role, so it sits beside the cell's prefix, not inside it:
    # the server's FileStorage namespace stays server-owned and the S10 delete cannot touch it.
    import large_import
    from targets.terraform_target import CELL_FILE_STORAGE_ROOT, cell_file_storage_prefix
    _serverless_env(monkeypatch)
    _ecs_env(monkeypatch)
    for factory, target_name in ((serverless, "aws-serverless"), (ecs, "aws-ecs")):
        for redis in (True, False):
            mode = "on" if redis else "off"
            key = large_import.object_key(f"{target_name}/redis-{mode}", "38046060497", "1")
            name_prefix = _tf_vars(factory(run_id="38046060497")._vars(redis, destroy=True))["name_prefix"]
            cell_prefix = cell_file_storage_prefix(name_prefix)
            assert cell_prefix == f"release-cells/{name_prefix}"
            assert key.startswith(CELL_FILE_STORAGE_ROOT) and cell_prefix.startswith(CELL_FILE_STORAGE_ROOT)
            assert key.startswith(f"release-cells/{target_name}-redis-{mode}/s10/")
            assert not key.startswith(cell_prefix + "/"), (key, cell_prefix)


def _ecs_polls(polls):
    """An ECS API whose DescribeTasks answers `polls[i]` (a list of (arn, status, digest)) on the
    i-th read, repeating the last; ListTasks lists the same arns."""
    reads = {"n": 0}

    def run(argv, **kwargs):
        rows = polls[min(reads["n"], len(polls) - 1)]
        if argv[2] == "list-tasks":
            body = {"taskArns": [f"arn:aws:ecs:us-east-1:1:task/c1/{arn}" for arn, _, _ in rows]}
        else:
            reads["n"] += 1
            body = {"tasks": [{"taskArn": f"arn:aws:ecs:us-east-1:1:task/c1/{arn}", "lastStatus": status,
                               "containers": [{"name": "honua", "image": _TAGLESS, "imageDigest": digest,
                                               "lastStatus": "RUNNING"}]} for arn, status, digest in rows]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
    return run, reads


_SERVER = run_cloud.cloud_journey.manifest()["components"]["honua-server"]
_TAGLESS = _SERVER["image"].rsplit(":", 1)[0] + "@" + _SERVER["digest"]


def test_two_task_ecs_cell_passes_when_every_running_task_reports_the_candidate():
    # Run 38119871941: the Redis-on cell on two MultiNode tasks reported no running image.
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    candidate = cj.candidate_image("aws-ecs/redis-on", pinned)
    child = _SERVER["platformDigests"]["amd64"]
    sleeps = []
    # Both tasks running, one reporting the index digest and one the amd64 child.
    run, _ = _ecs_polls([[("aaa", "RUNNING", _SERVER["digest"]), ("bbb", "RUNNING", child)]])
    image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=sleeps.append)
    assert image == candidate and detail.startswith("2 RUNNING task(s) of s1") and sleeps == []
    # The second task is still starting: wait for it, then count both.
    run, reads = _ecs_polls([[("aaa", "RUNNING", _SERVER["digest"]), ("bbb", "PENDING", "")],
                             [("aaa", "RUNNING", _SERVER["digest"]), ("bbb", "RUNNING", _SERVER["digest"])]])
    image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=sleeps.append)
    assert image == candidate and reads["n"] == 2 and sleeps == [cj.ECS_IMAGE_POLL_SECONDS]
    # A task that never starts within the wait is not counted, and is named.
    sleeps.clear()
    run, _ = _ecs_polls([[("aaa", "RUNNING", _SERVER["digest"]), ("bbb", "PENDING", "")]])
    image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=sleeps.append)
    assert image == candidate and "not counted: bbb PENDING" in detail
    assert len(sleeps) == cj.ECS_IMAGE_POLLS - 1


def test_two_task_ecs_cell_names_the_task_and_digest_that_disagree():
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    foreign = "sha256:" + "3" * 64
    run, _ = _ecs_polls([[("aaa", "RUNNING", _SERVER["digest"]), ("bbb", "RUNNING", foreign)]])
    image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=lambda _s: None)
    assert image is None
    assert detail.startswith(f"task bbb container honua reports {foreign} (candidate {_SERVER['digest']}")
    assert "aaa" not in detail
    run, _ = _ecs_polls([[("aaa", "STOPPED", _SERVER["digest"])]])
    image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=lambda _s: None)
    assert image is None and detail == "service s1 has no RUNNING task (aaa STOPPED)"


def test_ecs_image_is_never_observed_from_a_partial_describe_tasks_answer():
    # Every listed task must be described: a DescribeTasks failure or a missing record is no pass.
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    good = {"taskArn": "arn:aws:ecs:us-east-1:1:task/c1/aaa", "lastStatus": "RUNNING", "containers": [
        {"name": "honua", "image": _TAGLESS, "imageDigest": _SERVER["digest"], "lastStatus": "RUNNING"}]}
    for described in ({"tasks": [good], "failures": [{"arn": "arn:aws:ecs:us-east-1:1:task/c1/bbb",
                                                      "reason": "MISSING"}]},
                      {"tasks": [good], "failures": []}):
        def run(argv, **kwargs):
            body = ({"taskArns": [good["taskArn"], "arn:aws:ecs:us-east-1:1:task/c1/bbb"]}
                    if argv[2] == "list-tasks" else described)
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        image, detail = cj.observe_ecs_image(_EcsOutputs(), pinned, run=run, sleep=lambda _s: None)
        assert image is None
        assert detail.startswith("DescribeTasks described 1 of 2 listed tasks of s1")
        assert ("bbb (MISSING)" in detail) == bool(described["failures"])


def test_ecs_image_detail_reaches_the_candidate_image_check(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    stages = sys.modules["stages"]
    seen = []
    monkeypatch.setattr(driver, "observe", lambda target, base_url, workspace, bindir, image_ref, revision:
                        stages.Observation(image_ref=image_ref))

    def live(target, pinned, contract, workdir, base_url, keep):
        seen.append(driver.observe(target, base_url, None, None, None, None))
        raise RuntimeError("stop after observation")
    monkeypatch.setattr(driver, "run_live", live)
    try:
        cj.attempt("aws-ecs/redis-on", 1, "https://cell.invalid", "key", None,
                   image_detail="task bbb container honua reports sha256:33")
    finally:
        cj.cleanup("aws-ecs/redis-on")
    (observation,) = seen
    assert observation.image_ref is None
    assert observation.image_detail == "task bbb container honua reports sha256:33"
    (row,) = [c for c in stages.stage_1(observation, lambda _n: []) if c.id == "1.1-candidate-image"]
    assert row.status == "blocked"
    assert row.detail == ("the pinned candidate stack was not brought up: "
                          "task bbb container honua reports sha256:33")


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


# ---- owner decision 7 of 2026-10-10: journey attempts are idempotent (delete-and-recreate) --------
_ATTEMPT1_CONNECTION = "3f2b8c1e-5d4a-4e7b-9c0d-1a2b3c4d5e6f"
_OTHER_CONNECTION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_ATTEMPT2_CONNECTION = "0f0e0d0c-0b0a-4908-8706-050403020100"
_JOURNEY_EXECUTION = {"datasource": {"name": "journey_source"}, "styleId": "journey-solid-red"}


class _CellHoldingAttempt1:
    """A fake cell admin API that still holds what attempt 1 created: its journey_source connection
    and its standalone style, beside an unrelated connection the reset must not touch. Creating a
    connection whose name exists answers 409, as the cell did on every attempt 2."""

    def __init__(self, *, in_use=False, list_status=200):
        self.connections = {_ATTEMPT1_CONNECTION: "journey_source", _OTHER_CONNECTION: "customer_db"}
        self.styles = {"journey-solid-red"}
        self.in_use, self.list_status, self.calls = in_use, list_status, []

    def __call__(self, method, url, admin_key):
        assert admin_key == "cell-admin-key"
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method, path))
        if method == "GET" and path == "/api/v1/admin/connections":
            if self.list_status != 200:
                return self.list_status, None
            return 200, {"success": True, "data": [{"connectionId": cid, "name": name}
                                                   for cid, name in self.connections.items()]}
        if method == "DELETE" and path.startswith("/api/v1/admin/connections/"):
            cid = path.rsplit("/", 1)[1]
            if cid not in self.connections:
                return 404, None
            if self.in_use:
                return 409, {"success": False, "message": "Connection is in use by services"}
            del self.connections[cid]
            return 200, {"success": True}
        if method == "DELETE" and path.startswith("/ogc/styles/"):
            style = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if style not in self.styles:
                return 404, None
            self.styles.remove(style)
            return 204, None
        raise AssertionError(f"unexpected call {method} {path}")

    def create_connection(self, name):
        if name in self.connections.values():
            return 409
        self.connections[_ATTEMPT2_CONNECTION] = name
        return 201


def test_reset_deletes_attempt_ones_connection_and_style_so_attempt_two_can_create_them():
    cj = run_cloud.cloud_journey
    cell = _CellHoldingAttempt1()
    assert cell.create_connection("journey_source") == 409   # the failure every attempt 2 hit
    notices = cj.reset_prior_attempt("https://cell.example/", "cell-admin-key", _JOURNEY_EXECUTION, 2,
                                     request=cell)
    assert cell.calls == [("GET", "/api/v1/admin/connections"),
                          ("DELETE", f"/api/v1/admin/connections/{_ATTEMPT1_CONNECTION}"),
                          ("DELETE", "/ogc/styles/journey-solid-red")]
    assert cell.connections == {_OTHER_CONNECTION: "customer_db"} and cell.styles == set()
    assert cell.create_connection("journey_source") == 201   # attempt 2 is a genuine second try
    (notice,) = notices
    assert notice.startswith(cj.RETRY_STRATEGY_NOTICE + "Before attempt 2 the harness removed attempt 1's")
    assert f"journey_source connection {_ATTEMPT1_CONNECTION} deleted" in notice
    assert "style journey-solid-red deleted" in notice
    assert "cell-admin-key" not in notice
    # Attempt 3 removes what attempt 2 created, and says when an object was already gone.
    again = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 3, request=cell)
    assert f"journey_source connection {_ATTEMPT2_CONNECTION} deleted" in again[0]
    assert "style journey-solid-red already absent" in again[0]
    assert cell.connections == {_OTHER_CONNECTION: "customer_db"}
    empty = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 2, request=cell)
    assert "no journey_source connection was left" in empty[0]


def test_a_retry_creates_its_own_connection_when_attempt_ones_is_still_in_use():
    cj = run_cloud.cloud_journey
    assert cj.attempt_datasource_name("journey_source", 1) == "journey_source"
    assert cj.attempt_datasource_name("journey_source", 2) == "journey_source-attempt2"
    cell = _CellHoldingAttempt1()
    cell.in_use = True   # attempt 1 published a service on it: the cell refuses the delete (409)
    (notice,) = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 2,
                                       request=cell)
    assert "refused 409" in notice
    # The retry no longer collides with the undeletable name.
    assert cell.create_connection(cj.attempt_datasource_name("journey_source", 2)) == 201
    # Attempt 3 resets attempt 2's per-attempt connection (and attempt 1's) by the authored base name.
    cell.in_use = False
    execution = {**_JOURNEY_EXECUTION, "datasource": {**_JOURNEY_EXECUTION["datasource"],
                                                      "name": cj.attempt_datasource_name("journey_source", 3)}}
    (again,) = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", execution, 3, request=cell)
    assert f"journey_source connection {_ATTEMPT1_CONNECTION} deleted" in again
    assert f"journey_source connection {_ATTEMPT2_CONNECTION} deleted" in again
    assert cell.connections == {_OTHER_CONNECTION: "customer_db"}


def test_a_retry_imports_into_its_own_table_so_publish_meets_no_layer_conflict():
    # e2e-cloud-aws run 40742242038: attempt 2 re-imported into attempt 1's table, whose published
    # layer survived (its connection is held: 409), so service.publish refused the layer conflict.
    cj = run_cloud.cloud_journey
    assert cj.attempt_table_name("journey_source", 1) == "journey_source"
    assert cj.attempt_table_name("journey_source", 2) == "journey_source_attempt2"
    assert re.fullmatch(r"[a-z_][a-z0-9_]*", cj.attempt_table_name("journey_source", 3))
    assert cj.attempt_table_name("", 2) == ""


def test_an_image_digest_identity_binds_the_candidate_sha_only_when_both_observations_agree():
    # The honua-helm chart injects HONUA_IMAGE_DIGEST, so an EKS cell advertises source=image-digest.
    cj = run_cloud.cloud_journey
    pinned = cj.manifest()
    server = pinned["components"]["honua-server"]
    sha, image = _manifest_server()
    digest = image.rsplit("@", 1)[1]
    assert cj.observed_server({"revision": digest, "source": "image-digest"}, image, pinned) == {
        "sourceSha": sha, "image": image}
    # Without the control plane's agreement, or on another digest, nothing is bound.
    assert cj.observed_server({"revision": digest, "source": "image-digest"}, None, pinned)[
        "sourceSha"] == cj.UNOBSERVED_SHA
    other = "sha256:" + "9" * 64
    assert other not in {server["digest"], *(server.get("platformDigests") or {}).values()}
    assert cj.observed_server({"revision": other, "source": "image-digest"}, image.split("@")[0] + "@" + other,
                              pinned)["sourceSha"] == cj.UNOBSERVED_SHA
    assert cj.observed_server({"revision": digest, "source": "image-digest"}, image) == {
        "sourceSha": cj.UNOBSERVED_SHA, "image": image}


def test_reset_is_a_no_op_for_the_first_attempt():
    cj = run_cloud.cloud_journey
    cell = _CellHoldingAttempt1()
    assert cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 1,
                                  request=cell) == []
    assert cell.calls == []


def test_reset_reports_a_refused_or_unlistable_cell_instead_of_claiming_a_clean_retry():
    cj = run_cloud.cloud_journey
    busy = _CellHoldingAttempt1(in_use=True)
    (notice,) = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 2,
                                       request=busy)
    assert f"journey_source connection {_ATTEMPT1_CONNECTION} refused 409" in notice
    assert _ATTEMPT1_CONNECTION in busy.connections
    unlisted = _CellHoldingAttempt1(list_status=503)
    (notice,) = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 2,
                                       request=unlisted)
    assert "listing connections answered 503, so no journey_source connection was deleted" in notice
    assert ("DELETE", f"/api/v1/admin/connections/{_ATTEMPT1_CONNECTION}") not in unlisted.calls
    # A malformed id from the cell is never interpolated into a DELETE path.
    odd = _CellHoldingAttempt1()
    odd.connections = {"../../api-keys": "journey_source"}
    (notice,) = cj.reset_prior_attempt("https://cell.example", "cell-admin-key", _JOURNEY_EXECUTION, 2,
                                       request=odd)
    assert "malformed id was not deleted" in notice
    assert [c for c in odd.calls if c[0] == "DELETE" and "connections" in c[1]] == []


def test_attempt_two_resets_attempt_ones_objects_and_names_the_strategy_in_its_receipt(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    monkeypatch.setenv("GITHUB_RUN_ID", "retry-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: None)
    cell = _CellHoldingAttempt1()
    order = []

    def admin(*args, **kwargs):
        order.append("reset")
        return cell(*args)

    monkeypatch.setattr(cj, "admin_request", admin)
    workspace = driver.pins.ClientWorkspace(status="blocked", root=None, reason="offline")

    def live(target, pinned, contract, workdir, base_url, keep):
        order.append("driver")
        return workspace, None, [], None

    monkeypatch.setattr(driver, "run_live", live)
    try:
        record = cj.attempt("aws-ecs/redis-off", 1, "https://cell.example", "cell-admin-key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert order == ["driver"] and not any(n.startswith(cj.RETRY_STRATEGY_NOTICE) for n in notices)
        order.clear()
        record = cj.attempt("aws-ecs/redis-off", 2, "https://cell.example", "cell-admin-key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert order == ["reset", "reset", "reset", "driver"]   # the reset completes before the retry
        (strategy,) = [n for n in notices if n.startswith(cj.RETRY_STRATEGY_NOTICE)]
        assert f"journey_source connection {_ATTEMPT1_CONNECTION} deleted" in strategy
        assert "cell-admin-key" not in json.dumps(notices)
        # An endpoint the credential rule refuses never receives the admin key.
        order.clear()
        record = cj.attempt("aws-ecs/redis-off", 2, "http://203.0.113.9", "cell-admin-key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert "reset" not in order
        assert any(n.startswith(cj.RETRY_STRATEGY_NOTICE + "Not applied before attempt 2") for n in notices)
    finally:
        shutil.rmtree(cj.EVIDENCE / "retry-run", ignore_errors=True)


# ---- run 38099175519: the setup discovery sidecar travels with the journey receipts --------------
def test_discovery_sidecar_is_retained_per_attempt_with_every_known_credential_scrubbed(tmp_path):
    cj = run_cloud.cloud_journey
    workdir = tmp_path / "work-2"
    workdir.mkdir()
    (workdir / "private-principals.json").write_text(json.dumps(
        {"keys": {"operator": "hk_operator_value", "proposer": "hk_proposer_value"}, "keyIds": {}}))
    sidecar = {"http": {"rawResponse": "{\"tools\":[]}", "echo": "cell-admin-key"},
               "proxy": {"toolsListResponse": {"result": {"note": "hk_proposer_value"}}},
               "error": "proxy request override changed its negotiated session: _meta.view before \"setup\""}
    (workdir / "setup-discovery.json").write_text(json.dumps(sidecar))
    retained = cj.retain_discovery_sidecar(tmp_path, workdir, 2, ("cell-admin-key", "db-password", None))
    assert retained == tmp_path / "setup-discovery-2.json"
    text = retained.read_text()
    for secret in ("cell-admin-key", "hk_proposer_value"):
        assert secret not in text
    kept = json.loads(text)
    assert kept["error"].startswith("proxy request override changed its negotiated session: _meta.view")
    assert kept["proxy"]["toolsListResponse"]["result"]["note"] == "[redacted]"
    # No sidecar (the driver stopped before discovery), or a symlink, retains nothing.
    assert cj.retain_discovery_sidecar(tmp_path, tmp_path / "work-9", 9, ()) is None
    linked = tmp_path / "work-3"
    linked.mkdir()
    try:
        (linked / "setup-discovery.json").symlink_to(workdir / "setup-discovery.json")
    except OSError:
        return  # symlinks unavailable on this host
    assert cj.retain_discovery_sidecar(tmp_path, linked, 3, ()) is None


def test_attempt_retains_the_sidecar_the_driver_wrote_and_names_it(monkeypatch):
    cj = run_cloud.cloud_journey
    driver, _ = cj.drivers()
    monkeypatch.setenv("GITHUB_RUN_ID", "sidecar-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(cj, "server_identity", lambda endpoint: None)
    workspace = driver.pins.ClientWorkspace(status="blocked", root=None, reason="offline")

    def live(target, pinned, contract, workdir, base_url, keep):
        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "setup-discovery.json").write_text(json.dumps({"status": "fail", "key": "cell-admin-key"}))
        raise RuntimeError("stage 1 stopped")

    monkeypatch.setattr(driver, "run_live", live)
    try:
        record = cj.attempt("aws-serverless/redis-on", 1, "https://cell.example", "cell-admin-key", None)
        notices = json.loads((E2E_DIR / record["receipt"]).read_text())["notices"]
        assert "Setup discovery sidecar retained as setup-discovery-1.json" in notices
        retained = cj.cell_dir("aws-serverless/redis-on") / "setup-discovery-1.json"
        assert json.loads(retained.read_text()) == {"status": "fail", "key": "[redacted]"}
    finally:
        shutil.rmtree(cj.EVIDENCE / "sidecar-run", ignore_errors=True)
        shutil.rmtree(cj.cell_dir("aws-serverless/redis-on"), ignore_errors=True)


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


@pytest.mark.parametrize("cleanup_raises", [False, True])
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("tenant_fixture", [False, True])
@pytest.mark.parametrize("retained_retirement", [False, True])
def test_teardown_job_fails_closed_when_provisioned_state_never_arrives(monkeypatch, tenant_fixture, preview, cleanup_raises, retained_retirement):
    monkeypatch.setenv("HONUA_CLOUD_TENANT_FIXTURE", "true" if tenant_fixture else "false")
    cj = run_cloud.cloud_journey
    directory = _phase_env(monkeypatch)
    if preview:
        monkeypatch.setattr(cj, "PREVIEW_TARGETS", ("aws-ecs",))
    with tempfile.TemporaryDirectory() as scratch:
        stub = _PhaseStub(Path(scratch))
        if retained_retirement:
            stub.retirement_receipt = {"originalStateKeyRetained": True, "costReconciled": False}
        monkeypatch.setattr(run_cloud, "REGISTRY", {**run_cloud.REGISTRY, "aws-ecs": lambda **kw: stub})
        monkeypatch.setattr(run_cloud, "REPORT_PATH", Path(scratch) / "report.json")
        monkeypatch.setattr(cj, "check_cost", lambda *a, **k: {"status": "pass"})
        if cleanup_raises:
            monkeypatch.setattr(cj, "cleanup", mock.Mock(side_effect=OSError("cleanup fixture")))
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
            expected_secrets = [run_cloud.app_key_secret_name("aws-ecs/redis-off")]
            if tenant_fixture:
                expected_secrets.append(run_cloud.tenant_secret_name("aws-ecs/redis-off"))
            assert forgotten == expected_secrets
            forgotten.clear()
            # Restored state: destroy, then delete the state key.
            Path(scratch, "marker").write_text("")
            assert run_cloud.main(argv) == (0 if preview and not cleanup_raises else 1)
            assert json.loads(run_cloud.REPORT_PATH.read_text())["status"] == "fail"  # qualification is retained
            if cleanup_raises:
                assert "journey-cleanup-failed" in json.loads(run_cloud.REPORT_PATH.read_text())["cleanupFailures"]
            assert stub.torn_down == 1 and forgotten == expected_secrets + (
                [] if retained_retirement else [run_cloud.state_secret_name("aws-ecs/redis-off")])
            if retained_retirement:
                assert json.loads(run_cloud.REPORT_PATH.read_text())["retirement"] == stub.retirement_receipt
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
    # Each call stands for a fresh artifact from a separate journey job.
    for pattern in ("receipt-*.json", "serving-refusal-*.json", "target-*.json"):
        for stale in directory.glob(pattern):
            stale.unlink()
    records = []
    for number, receipt in enumerate(receipts, 1):
        data = json.dumps(receipt).encode()
        (directory / f"receipt-{number}.json").write_bytes(data)
        records.append({"number": number, "runId": run_id, "runAttempt": run_attempt,
            "cell": cell, "candidateDigest": cj.candidate_digest(),
            "receipt": str((cell_dir / f"receipt-{number}.json").relative_to(E2E_DIR)),
            "receiptSha256": hashlib.sha256(data).hexdigest(),
            "failureAttribution": None if receipt["status"] == "pass" else "infrastructure"})
        if cell.endswith("/redis-off") and receipt["status"] == "pass":
            from test_deterministic_refusal_profile import attach_fixture_profile
            (directory / f"receipt-{number}.json").write_bytes(attach_fixture_profile(directory, receipt, records[-1]))
    return {"cell": cell, "status": "pass", "journeyAttempts": records}


def test_teardown_reverifies_journey_receipts_and_adopts_no_claimed_verdict(monkeypatch):
    import pytest
    cj = run_cloud.cloud_journey
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    sha, image = _manifest_server()
    state = {"cell": "aws-ecs/redis-off", "observedServer": {"revision": sha, "source": "commit-sha"},
             "runningImage": image, "servingFixtureSha256": hashlib.sha256(json.dumps({
                 "slice1": {"e2e_src_fs": {"service": "e2e", "layerId": 7}},
                 "layers": {"e2e_src_fs": 7}}).encode()).hexdigest()}
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
            # An independent empty upload claims a pass without running against the endpoint.
            _journey_upload(upload, [])
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
ECS_OFF = "aws-ecs/redis-off"


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


def test_documented_blockers_name_a_blocked_roster_and_refuse_an_unnamed_or_failed_one():
    cj = run_cloud.cloud_journey
    roster_issue = "https://github.com/honua-io/honua-server/issues/3363"
    receipt = _blocked_receipt()
    receipt["roster"] = {"status": "blocked", "reason": "exports unavailable", "blockedBy": roster_issue}
    assert roster_issue in cj.documented_blockers(receipt)
    receipt["roster"] = {"status": "blocked", "reason": "exports unavailable"}
    assert cj.documented_blockers(receipt) == []
    receipt["roster"] = {"status": "fail", "problems": ["x"]}
    assert cj.documented_blockers(receipt) == []


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
    monkeypatch.setattr(cj, "qualify_attempt", lambda *a, **k: False)
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
        # Aggregation verdicts apply to GA cells; AWS ECS is the only GA target (2026-10-25 re-tier).
        receipt = _blocked_receipt(cell=ECS_OFF)
        report = {"cell": ECS_OFF, "status": "blocked", "artifactDirectory": str(directory),
                  "why": "journey blocked by tracked limitations",
                  "journeyAttempts": [_artifact(directory, receipt)],
                  "cost": {"status": "unavailable", "why": "run cost meter unavailable"}}
        lenient = cj.aggregate([report], directory, require_real=False, full_scope=False,
                               run_id="offline-run", run_attempt="2")
        assert lenient["status"] == "blocked" and ECS_OFF in lenient["why"]
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


# ---- run 38128300695: a failed fixture publication names its call, status and answering task ------
def _seed_cell_against(handler_answers):
    """Run the real seed against a loopback admin API answering `handler_answers(method, path)`
    with (status, body bytes, instance)."""
    import http.server
    import threading

    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            calls.append((self.command, self.path, json.loads(self.rfile.read(length) or b"{}"),
                          self.headers.get("X-API-Key")))
            status, body, instance = handler_answers(self.command, self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if instance:
                self.send_header("Honua-Instance", instance)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_PUT = _answer

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class Cell:
        admin_api_key = "cell-admin-key-value"

        def seed_database(self, sql):
            return {"host": "db.internal.example", "port": 5432, "databaseName": "honua",
                    "username": "honua_admin", "password": "pg-password-value", "provider": "PostGIS",
                    "sslRequired": True, "sslMode": "Require"}
    try:
        with tempfile.TemporaryDirectory() as out:
            error = cloud_driver.seed_cell(f"http://127.0.0.1:{server.server_address[1]}", target=Cell(), out=out)
            wrote = (Path(out) / "seed-manifest.json").exists()
    finally:
        server.shutdown()
        server.server_close()
    return error, calls, wrote


def test_seed_failure_names_the_call_status_instance_and_a_masked_body():
    cid = "3f2b8c1e-5d4a-4e7b-9c0d-1a2b3c4d5e6f"

    def answers(method, path):
        if path == "/api/v1/admin/connections/":
            return 201, json.dumps({"data": {"connectionId": cid}}).encode(), "task-a"
        return 404, json.dumps({"success": False, "message": "Connection not found",
                                "detail": "pg-password-value at db.internal.example",
                                "password": "pg-password-value"}).encode(), "task-b"
    error, calls, wrote = _seed_cell_against(answers)
    assert not wrote
    assert error.startswith(f"HTTPError HTTP 404 on POST /api/v1/admin/connections/{cid}/layers "
                            "(answered by instance task-b): ")
    assert '"message":"Connection not found"' in error
    assert error.endswith("; 1 earlier publication call(s) were answered by task-a")
    for secret in ("pg-password-value", "db.internal.example", "honua_admin", "cell-admin-key-value"):
        assert secret not in error
    assert [c[:2] for c in calls] == [("POST", "/api/v1/admin/connections/"),
                                      ("POST", f"/api/v1/admin/connections/{cid}/layers")]


@pytest.mark.parametrize("acknowledgement", [
    {"storageMode": "managed", "capabilities": ["Query", "Create", "Update", "Delete"]},
    {},
    {"storageMode": "source", "capabilities": ["Query", "Create", "Update", "Delete"]},
    {"storageMode": "managed", "capabilities": ["Query", "Create", "Delete"]},
])
def test_cloud_editing_fixture_requires_actual_managed_publication(acknowledgement):
    published = 0

    def answers(method, path):
        nonlocal published
        if path == "/api/v1/admin/connections/":
            return 201, b'{"data":{"connectionId":"c1"}}', "task-a"
        if method == "POST":
            published += 1
            data = {"layerId": published}
            if published == 7:
                data.update(acknowledgement)
            return 201, json.dumps({"data": data}).encode(), "task-a"
        return 200, b'{}', "task-a"

    error, calls, wrote = _seed_cell_against(answers)
    accepted = acknowledgement.get("storageMode") == "managed" and "Update" in acknowledgement.get("capabilities", [])
    assert wrote is accepted
    assert (error is None) is accepted
    layers = [call[2] for call in calls if call[0] == "POST" and call[1].endswith("/layers")]
    editable = [layer for layer in layers if "storageMode" in layer]
    assert len(editable) == 1 and editable[0]["serviceName"] == "maui-inspections"
    assert editable[0]["storageMode"] == "managed"
    assert editable[0]["capabilities"] == ["Query", "Create", "Update", "Delete"]
    if not accepted:
        assert not any(call[0] == "PUT" and "/maui-inspections/" in call[1] for call in calls)


def test_seed_failure_on_a_non_json_body_is_bounded_and_unnamed_instances_are_said():
    def answers(method, path):
        if path == "/api/v1/admin/connections/":
            return 200, json.dumps({"data": {"connectionId": "c1"}}).encode(), None
        return 502, b"<html>Bad Gateway token=abc123 " + b"x" * 1000 + b"</html>", None
    error, _, _ = _seed_cell_against(answers)
    assert error.startswith("HTTPError HTTP 502 on POST /api/v1/admin/connections/c1/layers: <html>Bad Gateway")
    assert "abc123" not in error and "...(truncated)" in error
    assert error.endswith("; 1 earlier publication call(s) were answered by no named instance")


def test_seed_failure_never_keeps_part_of_a_secret_cut_by_the_excerpt_bound():
    # Review on #540: a secret straddling the 300-character cut must be replaced before the cut.
    from harness.cloud.seed import MAX_BODY_CHARS, body_excerpt
    secret = "pg-password-" + "s" * 40
    for raw in (json.dumps({"message": "x" * (MAX_BODY_CHARS - 30) + secret}).encode(),
                ("<html>" + "y" * (MAX_BODY_CHARS - 20) + secret + "</html>").encode()):
        excerpt = body_excerpt(raw, (secret, None, 5432))
        assert "pg-password-" not in excerpt and "s" * 10 not in excerpt
        assert len(excerpt) <= MAX_BODY_CHARS + len("...(truncated)")
    # A secret JSON escaping would alter is replaced in the parsed value, before serialization.
    quoted = 'pa"ss/wörd-value'
    excerpt = body_excerpt(json.dumps({"detail": f"auth failed: {quoted}"}).encode(), (quoted,))
    assert excerpt == '{"detail":"auth failed: [redacted]"}'


def test_seed_failure_that_is_not_http_keeps_only_the_error_type():
    class Cell:
        admin_api_key = "k"

        def seed_database(self, sql):
            raise RuntimeError("psql: password authentication failed for user honua_admin")
    with tempfile.TemporaryDirectory() as out:
        assert cloud_driver.seed_cell("http://127.0.0.1:9", target=Cell(), out=out) == "RuntimeError"


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


def test_cloud_seed_registers_the_rds_proxy_host_when_the_cell_has_one(monkeypatch):
    target = ecs(run_id='seed-proxy')
    target._workdir = Path('/tmp/cell')
    connection = 'Host=cell-db;Port=5432;Database=honua;Username=honua;Password=private-password;SSL Mode=Require'
    state = {'values': {'root_module': {'child_modules': [{'resources': [{
        'type': 'aws_secretsmanager_secret_version', 'name': 'db_connection',
        'values': {'secret_string': connection}}]}]}}}
    def tf(root, *args, check=True):
        if args[:2] == ('output', '-json'):
            return subprocess.CompletedProcess(args, 0, json.dumps('cell-proxy.proxy-abc.us-east-1.rds.amazonaws.com'))
        return subprocess.CompletedProcess(args, 0, json.dumps(state))
    monkeypatch.setattr(target, '_tf', tf)
    seen = {}
    def psql(command, **kwargs):
        seen['host'] = kwargs['env']['PGHOST']
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', psql)
    result = target.seed_database('SELECT 1')
    # psql runs from the runner over the direct endpoint; the server's data source uses the proxy.
    assert seen['host'] == 'cell-db'
    assert result['host'] == 'cell-proxy.proxy-abc.us-east-1.rds.amazonaws.com'


def test_cloud_seed_keeps_the_direct_host_without_a_proxy_output(monkeypatch):
    target = ecs(run_id='seed-noproxy')
    target._workdir = Path('/tmp/cell')
    connection = 'Host=cell-db;Port=5432;Database=honua;Username=honua;Password=private-password;SSL Mode=Require'
    state = {'values': {'root_module': {'child_modules': [{'resources': [{
        'type': 'aws_secretsmanager_secret_version', 'name': 'db_connection',
        'values': {'secret_string': connection}}]}]}}}
    def tf(root, *args, check=True):
        if args[:2] == ('output', '-json'):
            return subprocess.CompletedProcess(args, 0, 'null')
        return subprocess.CompletedProcess(args, 0, json.dumps(state))
    monkeypatch.setattr(target, '_tf', tf)
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    assert target.seed_database('SELECT 1')['host'] == 'cell-db'

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


# ---- S9-demos-geoprocessing on a Redis-off cell (run 38047168879, aws-ecs/redis-off) ----------------
# The demo page must show the same typed capability-unavailable refusal S5 asserts, not wait 120 s for a
# job that can never run. The verdict logic is unit-tested in node; the topology plumbing in run.sh is
# exercised for real against a stub candidate serving a fake capability manifest.
def test_demos_geoprocessing_topology_verdicts_hold_for_both_topologies():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    completed = subprocess.run([node, "--test", str(E2E_DIR / "drivers/demos/gp-topology.test.mjs")],
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# ---- S9 demo call log (run 38192851328, aws-serverless): a gateway 5xx without CORS headers is a call
# the sample made, not a shim failure; the editing/workbench verdicts name who answered.
def test_demos_call_log_attributes_gateway_answers_and_cors_blocked_calls():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    completed = subprocess.run([node, "--test", str(E2E_DIR / "drivers/demos/backend-calls.test.mjs")],
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _demos_topology_plumbing(declared, topology):
    """Run run.sh's own topology block against a stub candidate; return what drive.mjs would receive."""
    script = (E2E_DIR / "drivers/demos/run.sh").read_text(encoding="utf-8")
    block = script.split("# ── topology (Redis-on vs Redis-off)", 1)[1].split("\n", 1)[1].split("# ── drive", 1)[0]
    server = _serve(lambda method, path, body, authed: _common_routes(method, path, topology, "operations.proposals")
                    or (404, {}))
    try:
        with tempfile.TemporaryDirectory() as out:
            env = {**os.environ, "E2E_BASE": f"http://127.0.0.1:{server.server_address[1]}",
                   "E2E_API_KEY": "k", "E2E_OUT": out, "E2E_REDIS": declared}
            program = (f'source "{(E2E_DIR / "harness/lib/common.sh").as_posix()}"\n{block}\n'
                       'printf "%s\n%s\n" "$E2E_TOPOLOGY_JSON" "$E2E_JOBS_RUNNER_JSON"')
            completed = subprocess.run(["bash", "-c", program], env=env, capture_output=True, text=True,
                                       timeout=60, check=True)
    finally:
        server.shutdown()
    topo, jobs = completed.stdout.strip().splitlines()[-2:]
    return json.loads(topo), json.loads(jobs)


def test_demos_driver_resolves_redis_off_from_the_capability_manifest():
    topo, jobs = _demos_topology_plumbing("off", "redis-off")
    assert topo["topology"] == "redis-off" and topo["declared"] == "off" and topo["mismatch"] is None
    assert jobs == {"id": "jobs.runner", "supported": True, "available": False,
                    "reasonCode": "dependency-unavailable"}


def test_demos_driver_keeps_redis_on_live_and_flags_a_contradicted_topology():
    topo, jobs = _demos_topology_plumbing("on", "redis-on")
    assert topo["topology"] == "redis-on" and topo["mismatch"] is None and jobs["available"] is True
    topo, _ = _demos_topology_plumbing("off", "redis-on")
    assert topo["mismatch"] and "declares Redis off" in topo["mismatch"]


def test_demos_driver_hands_the_topology_to_the_page_driver_before_driving():
    script = (E2E_DIR / "drivers/demos/run.sh").read_text(encoding="utf-8")
    assert script.index("resolve_topology") < script.index('node "$HERE/drive.mjs"')
    drive = (E2E_DIR / "drivers/demos/drive.mjs").read_text(encoding="utf-8")
    assert 'parseEnvJson("E2E_TOPOLOGY_JSON")' in drive and "judgeRedisOffGeoprocessing" in drive
    # The page's own execution response is observed, never routed, faked or rewritten.
    assert "page.route(" not in drive.split("async function checkGeoprocessingRedisOff", 1)[1].split("/* ──", 1)[0]


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


def test_durable_control_plane_roster_is_the_24_redis_gated_admin_tools():
    # 4709145 re-pin: 132 tools (+honua_admin_import_upload_object, honua_diagnose_job, honua_job_evidence),
    # 24 durable-control-plane names (+honua_admin_import_upload_object).
    # RC re-pin #4 (53bb94a, unchanged at a4bde4b): 134 tools (+honua_get_job_results, honua_ingest_job_artifact);
    # durable set unchanged.
    assert len(_FULL_ROSTER) == 134 and len(set(_FULL_ROSTER)) == 134
    assert len(_DURABLE_ONLY) == 24 and set(_DURABLE_ONLY) <= set(_FULL_ROSTER)
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


def test_s2_redis_off_110_catalog_passes_with_topology_recorded():
    catalog = [n for n in _FULL_ROSTER if n not in _DURABLE_ONLY]
    assert len(catalog) == 110
    for signal in ("jobs.runner", "operations.proposals", "admin-proposals"):
        row = _s2(catalog, "redis-off", "off", signal)
        assert row["status"] == "pass", (signal, row["why"])
        topo = row["evidence"]["topology"]
        assert topo["topology"] == "redis-off" and topo["declared"] == "off" and topo["mismatch"] is None
        assert topo["reasonCode"] and topo["signal"].startswith(("manifest:", "admin-proposals:"))
        assert topo["durableControlPlaneTools"] == {"state": "absent", "count": 24}
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


def test_serverless_runs_the_event_driven_control_plane_when_the_root_declares_it(monkeypatch):
    # Run 38052304982: on Lambda the in-process control-plane timers only run inside an invocation,
    # so the stage 5 job stayed Queued. The module's TriggerMode=Event path is passed only on a root
    # that declares it; the event functions keep the API image and the module's tick cadences.
    from targets.terraform_target import SERVERLESS_SPEC
    assert "enable_control_plane_events=true" in SERVERLESS_SPEC.declared_ephemeral_vars
    _serverless_env(monkeypatch)
    for redis in (True, False):
        with tempfile.TemporaryDirectory() as base:
            # An older iac pin whose root predates the variable gets nothing.
            _iac_root_with(monkeypatch, base, "aws-serverless", "enable_gp_batch")
            values = _tf_vars(serverless(run_id="r1")._vars(redis))
            assert "enable_control_plane_events" not in values and values["enable_gp_batch"] == "true"
            _iac_root_with(monkeypatch, base, "aws-serverless", "enable_control_plane_events",
                           "control_plane_events_image", "control_plane_scheduled_tick_schedules")
            values = _tf_vars(serverless(run_id="r1")._vars(redis))
            assert values["enable_control_plane_events"] == "true"
            assert "control_plane_events_image" not in values
            assert "control_plane_scheduled_tick_schedules" not in values
    # The ECS cell keeps its long-running in-process loop.
    with tempfile.TemporaryDirectory() as base:
        _ecs_env(monkeypatch)
        _iac_root_with(monkeypatch, base, "aws", "enable_control_plane_events")
        assert "enable_control_plane_events" not in _tf_vars(ecs(run_id="r1")._vars(False))


# ---- Production typed-operation policy on the ECS and Lambda cells (run 38066103745) --------------
_EXPECTED_POLICY_LITERAL = (
    '[{operation_id="*",role="scoped-admin-key",decision="Allow",'
    'reason="Release cell: scoped admin keys run typed operations"},'
    '{operation_id="*",role="admin",decision="Allow",'
    'reason="Release cell: bootstrap and full admins run typed operations"}]')


def test_cell_operation_policy_is_two_explicit_allow_rules_for_admin_roles_only():
    # Run 38066103745 stage 3: service.publish ... status=Denied; policyOutcome=Deny; message=
    # "Operations require an explicit production policy rule." The cells run Production, whose
    # policy is fail-closed; the harness allows exactly the two admin roles and nothing else.
    from targets.terraform_target import CELL_OPERATION_POLICY_RULES, OPERATIONS_POLICY_RULES_VAR
    rules = [dict(rule) for rule in CELL_OPERATION_POLICY_RULES]
    assert [(r["operation_id"], r["role"], r["decision"]) for r in rules] == [
        ("*", "scoped-admin-key", "Allow"), ("*", "admin", "Allow")]
    for rule in rules:
        assert rule["role"].strip() and rule["decision"] == "Allow" and rule["reason"].strip()
        assert set(rule) <= {"operation_id", "role", "tier", "decision", "reason", "approval_lane"}
    # The proposer and viewer roles get no rule; the server's default Deny stays the fallback.
    assert not {"layer-write-key", "scoped-api-key"} & {r["role"].lower() for r in rules}
    assert OPERATIONS_POLICY_RULES_VAR == "operations_policy_rules=" + _EXPECTED_POLICY_LITERAL


def test_hcl_object_list_refuses_anything_but_plain_string_literals():
    from targets.terraform_target import hcl_object_list
    assert hcl_object_list([(("a", "x"), ("b", "y=z"))]) == '[{a="x",b="y=z"}]'
    for bad in ('say "hi"', "back\\slash", "${var.x}", "%{if x}", "two\nlines"):
        with pytest.raises(ValueError):
            hcl_object_list([(("reason", bad),)])


def test_cells_enable_studio_end_user_authorization_only_when_the_root_declares_it(monkeypatch):
    # Run 38091905679 (aws-ecs/redis-on): stage 6 got 403 on GET /api/v1/studio/package-families for
    # the proposer (role layer-write-key) because the cell left Studio:EndUserAuthorization disabled.
    from targets.terraform_target import ECS_SPEC, SERVERLESS_SPEC, STUDIO_END_USER_AUTHORIZATION_VAR
    assert STUDIO_END_USER_AUTHORIZATION_VAR == "studio_end_user_authorization=true"
    assert STUDIO_END_USER_AUTHORIZATION_VAR in SERVERLESS_SPEC.declared_ephemeral_vars
    assert STUDIO_END_USER_AUTHORIZATION_VAR in ECS_SPEC.declared_ephemeral_vars
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN",
                       "arn:aws:secretsmanager:us-east-1:111111111111:secret:keyring")
    cells = ((serverless, "aws-serverless", _serverless_env, "enable_gp_batch"),
             (ecs, "aws", _ecs_env, "enable_postgis"))
    for factory, example, env, sibling in cells:
        env(monkeypatch)
        for redis in (True, False):
            for destroy in (False, True):
                with tempfile.TemporaryDirectory() as base:
                    # An older iac pin whose root predates the input passes nothing.
                    _iac_root_with(monkeypatch, base, example, sibling)
                    values = _tf_vars(factory(run_id="r1")._vars(redis, destroy=destroy))
                    assert "studio_end_user_authorization" not in values and sibling in values
                    _iac_root_with(monkeypatch, base, example, sibling, "studio_end_user_authorization")
                    values = _tf_vars(factory(run_id="r1")._vars(redis, destroy=destroy))
                    assert values["studio_end_user_authorization"] == "true"


def test_cell_operation_policy_passes_on_both_specs_and_redis_modes_only_when_declared(monkeypatch):
    from targets.terraform_target import ECS_SPEC, OPERATIONS_POLICY_RULES_VAR, SERVERLESS_SPEC
    assert OPERATIONS_POLICY_RULES_VAR in SERVERLESS_SPEC.declared_ephemeral_vars
    assert OPERATIONS_POLICY_RULES_VAR in ECS_SPEC.declared_ephemeral_vars
    monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN",
                       "arn:aws:secretsmanager:us-east-1:111111111111:secret:keyring")
    cells = ((serverless, "aws-serverless", _serverless_env, "enable_gp_batch"),
             (ecs, "aws", _ecs_env, "enable_postgis"))
    for factory, example, env, sibling in cells:
        env(monkeypatch)
        for redis in (True, False):
            for destroy in (False, True):
                with tempfile.TemporaryDirectory() as base:
                    # An older iac pin whose root predates the variable passes nothing, so the
                    # receipt keeps recording the server's Deny instead of a terraform error.
                    _iac_root_with(monkeypatch, base, example, sibling)
                    argv = factory(run_id="r1")._vars(redis, destroy=destroy)
                    assert not any(a.startswith("-var=operations_policy_rules") for a in argv)
                    assert sibling in _tf_vars(argv)
                    _iac_root_with(monkeypatch, base, example, sibling, "operations_policy_rules")
                    argv = factory(run_id="r1")._vars(redis, destroy=destroy)
                    # One argv element carrying the whole literal, `=` and quotes intact (no shell).
                    matching = [a for a in argv if a.startswith("-var=operations_policy_rules=")]
                    assert matching == ["-var=operations_policy_rules=" + _EXPECTED_POLICY_LITERAL]
                    assert _tf_vars(argv)["operations_policy_rules"] == _EXPECTED_POLICY_LITERAL


def test_root_declares_matches_the_variable_name_of_a_value_with_equals_and_quotes(monkeypatch):
    # declared_ephemeral_vars split on the FIRST `=`; a value holding `=` and `"` still names its
    # variable correctly, and a root declaring only a prefix-sharing name does not match.
    from targets.terraform_target import OPERATIONS_POLICY_RULES_VAR
    assert OPERATIONS_POLICY_RULES_VAR.split("=", 1)[0] == "operations_policy_rules"
    _serverless_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "operations_policy")
        target = serverless(run_id="r1")
        assert not target._root_declares("operations_policy_rules")
        assert "operations_policy_rules" not in _tf_vars(target._vars(False))


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform CLI not installed")
def test_cell_operation_policy_literal_is_accepted_by_terraform_for_the_iac_type(tmp_path):
    # Local, provider-free plan: proves terraform parses the -var literal into the honua-iac
    # variable's list(object) type with the expected values. No state, no cloud.
    from targets.terraform_target import OPERATIONS_POLICY_RULES_VAR
    (tmp_path / "main.tf").write_text(
        'variable "operations_policy_rules" {\n'
        '  type = list(object({ operation_id = optional(string, "*"), role = optional(string),\n'
        '    tier = optional(string), decision = string, reason = optional(string),\n'
        '    approval_lane = optional(string) }))\n'
        '}\n'
        'output "rules" { value = var.operations_policy_rules }\n', encoding="utf-8")
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_DATA_DIR": str(tmp_path / ".terraform")}
    run = lambda *args: subprocess.run(["terraform", f"-chdir={tmp_path}", *args], text=True,
                                       capture_output=True, env=env, timeout=120)
    assert run("init", "-input=false", "-backend=false").returncode == 0
    plan = run("plan", "-input=false", "-no-color", "-refresh=false", "-out=plan.bin",
               f"-var={OPERATIONS_POLICY_RULES_VAR}")
    assert plan.returncode == 0, plan.stderr
    shown = run("show", "-json", "plan.bin")
    assert shown.returncode == 0, shown.stderr
    rules = json.loads(shown.stdout)["planned_values"]["outputs"]["rules"]["value"]
    assert [(r["operation_id"], r["role"], r["decision"], r["tier"]) for r in rules] == [
        ("*", "scoped-admin-key", "Allow", None), ("*", "admin", "Allow", None)]


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
    # Serverless uses the same explicit opt-in and fails closed on older roots.
    from targets.terraform_target import SERVERLESS_SPEC
    assert SERVERLESS_SPEC.opt_in_env == "HONUA_ENABLE_BEDROCK_AI"
    _serverless_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "enable_bedrock_ai", "bedrock_ai_region")
        monkeypatch.setenv("HONUA_ENABLE_BEDROCK_AI", "false")
        assert "enable_bedrock_ai" not in _tf_vars(serverless(run_id="r1")._vars(False))
        monkeypatch.setenv("HONUA_ENABLE_BEDROCK_AI", "true")
        values = _tf_vars(serverless(run_id="r1")._vars(False))
        assert values["enable_bedrock_ai"] == "true" and values["bedrock_ai_region"] == "us-east-1"
        _iac_root_with(monkeypatch, base, "aws-serverless")
        with pytest.raises(ProvisionError, match="does not declare enable_bedrock_ai"):
            serverless(run_id="r1")._vars(False)


def test_ecs_bedrock_model_is_passed_only_when_set_valid_and_declared(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_AWS_RUNNER_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_ENABLE_BEDROCK_AI", "true")
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", "enable_bedrock_ai", "bedrock_ai_region", "bedrock_ai_model")
        monkeypatch.delenv("HONUA_BEDROCK_AI_MODEL", raising=False)
        assert "bedrock_ai_model" not in _tf_vars(ecs(run_id="r1")._vars(False))
        for model in ("us.anthropic.claude-sonnet-4-6", "us.openai.gpt-6.1-terra"):
            monkeypatch.setenv("HONUA_BEDROCK_AI_MODEL", model)
            assert _tf_vars(ecs(run_id="r1")._vars(False))["bedrock_ai_model"] == model
        monkeypatch.setenv("HONUA_BEDROCK_AI_MODEL", "us.openai.*")
        with pytest.raises(ProvisionError, match="must be an exact"):
            ecs(run_id="r1")._vars(False)
        monkeypatch.setenv("HONUA_BEDROCK_AI_MODEL", "us.anthropic.claude-sonnet-4-6")
        _iac_root_with(monkeypatch, base, "aws", "enable_bedrock_ai", "bedrock_ai_region")
        with pytest.raises(ProvisionError, match="does not declare bedrock_ai_model"):
            ecs(run_id="r1")._vars(False)


def test_cloud_workflow_threads_the_bedrock_model_to_the_cell_and_its_canary():
    workflow, cell = _cloud_workflows()
    assert "bedrock_model" in (workflow.get("on") or workflow[True])["workflow_dispatch"]["inputs"]
    cell_call = next(job for job in workflow["jobs"].values() if "bedrock_model" in (job.get("with") or {}))
    assert cell_call["with"]["bedrock_model"] == "${{ inputs.bedrock_model || '' }}"
    assert cell["env"]["HONUA_BEDROCK_AI_MODEL"] == "${{ inputs.bedrock_model }}"
    canary = next(step for job in cell["jobs"].values() for step in job.get("steps", [])
                  if str(step.get("name", "")).startswith("Run the genuine-model canary"))
    assert canary["env"]["TERMINAL_MODEL_NAME"].startswith("${{ inputs.bedrock_model || ")


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
                                        "(matrix.target == 'aws-ecs' || matrix.target == 'aws-serverless') }}")
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


# ---- honua-server#6750: the Lambda cell stays closed until the pre-serving migration is Ready -------
def _closing_target(monkeypatch, base, *declared, migration_ready=True):
    """A serverless target on a fake pinned root that records every terraform and docker call in
    order; `show -json` answers with the cell's connection secret for the migration."""
    _serverless_env(monkeypatch)
    monkeypatch.setenv("HONUA_MIGRATE_IMAGE", "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64)
    _iac_root_with(monkeypatch, base, "aws-serverless", *declared)
    target = serverless(run_id="run42")
    calls = []
    state = {"values": {"root_module": {"resources": [], "child_modules": [{"resources": [
        {"type": "aws_secretsmanager_secret_version", "name": "connection_string",
         "values": {"secret_string": _CONNECTION}}]}]}}}

    def tf(root, *args, check=True):
        calls.append(("terraform", *args))
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, json.dumps(state), "")
        if args[0] == "output":
            return subprocess.CompletedProcess(args, 0, "https://abc.execute-api.us-east-1.amazonaws.com\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(target, "_tf", tf)
    docker = _Docker() if migration_ready else _Docker(running="false 1", logs="migration 0031 failed\n")

    def run(argv, **kwargs):
        calls.append(tuple(argv[:2]))
        return docker(argv, **kwargs)

    return target, calls, run


def _applies(calls):
    return [_tf_vars(call).get("serving_closed") for call in calls if call[:2] == ("terraform", "apply")]


def test_serverless_cell_applies_closed_migrates_then_opens(monkeypatch):
    with tempfile.TemporaryDirectory() as base:
        target, calls, run = _closing_target(monkeypatch, base, "serving_closed")
        endpoint = target.provision(redis_enabled=True)
        assert endpoint.startswith("https://")
        # One apply so far, closed; nothing opened before the migration.
        assert _applies(calls) == ["true"]
        # The open input set is what update/rollback and the sealed transition inputs re-apply.
        assert "serving_closed" not in _tf_vars(target._last_vars)
        result = target.migrate(redis_enabled=True, run=run, probe=lambda url: 200, sleep=lambda _s: None)
        assert result["ready"] is True and result["servingOpened"] is True
        assert _applies(calls) == ["true", "false"]
        order = [call[:2] for call in calls]
        closed_apply = order.index(("terraform", "apply"))
        docker_run, docker_rm = order.index(("docker", "run")), order.index(("docker", "rm"))
        open_apply = len(order) - 1 - order[::-1].index(("terraform", "apply"))
        # closed apply -> migration container -> Ready -> container removed -> opening apply.
        assert closed_apply < docker_run < docker_rm < open_apply == len(order) - 1
        # Both applies carry the same open inputs; only serving_closed differs.
        applies = [_tf_vars(call) for call in calls if call[:2] == ("terraform", "apply")]
        assert {k: v for k, v in applies[0].items() if k != "serving_closed"} == \
            {k: v for k, v in applies[1].items() if k != "serving_closed"}
        # Opening is idempotent: a second call applies nothing.
        assert target.open_serving() is False and _applies(calls) == ["true", "false"]


def test_serverless_cell_on_an_older_root_applies_once_open(monkeypatch):
    # The pinned honua-iac v0.4.1 root does not declare serving_closed: passing it would be a hard
    # terraform error, so the cell applies once, open, exactly as before (activates on the re-pin).
    with tempfile.TemporaryDirectory() as base:
        target, calls, run = _closing_target(monkeypatch, base)
        target.provision(redis_enabled=False)
        result = target.migrate(redis_enabled=False, run=run, probe=lambda url: 200, sleep=lambda _s: None)
        assert result["ready"] is True and result["servingOpened"] is False
        assert _applies(calls) == [None]
        assert not any("serving_closed" in arg for call in calls for arg in call)


def test_ecs_cell_never_applies_closed(monkeypatch):
    # No pre-serving migration on ECS, so nothing would ever open it: never closed, even if declared.
    with tempfile.TemporaryDirectory() as base:
        _ecs_env(monkeypatch)
        _iac_root_with(monkeypatch, base, "aws", "serving_closed")
        target = ecs(run_id="r1")
        calls = []
        monkeypatch.setattr(target, "_tf", lambda root, *args, check=True: (
            calls.append(args), subprocess.CompletedProcess(args, 0, "http://alb.example\n", ""))[1])
        target.provision(redis_enabled=False)
        assert [_tf_vars(c).get("serving_closed") for c in calls if c[0] == "apply"] == [None]


def test_serverless_migration_failure_leaves_the_cell_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as base:
        target, calls, run = _closing_target(monkeypatch, base, "serving_closed", migration_ready=False)
        target.provision(redis_enabled=True)
        with pytest.raises(ProvisionError, match="exited 1 before Ready"):
            target.migrate(redis_enabled=True, run=run, probe=lambda url: 503, sleep=lambda _s: None)
        # No opening apply: teardown destroys a cell nothing could invoke.
        assert _applies(calls) == ["true"] and target._serving_closed is True
        assert calls[-1][:2] == ("docker", "rm")


def test_serverless_failed_opening_apply_is_a_provision_error(monkeypatch):
    with tempfile.TemporaryDirectory() as base:
        target, calls, run = _closing_target(monkeypatch, base, "serving_closed")
        target.provision(redis_enabled=False)
        recorded = target._tf

        def failing(root, *args, check=True):
            if args[0] == "apply":
                raise subprocess.CalledProcessError(1, "terraform", stderr="PutFunctionConcurrency denied")
            return recorded(root, *args, check=check)

        monkeypatch.setattr(target, "_tf", failing)
        with pytest.raises(ProvisionError, match="apply opening the cell failed: PutFunctionConcurrency denied"):
            target.migrate(redis_enabled=False, run=run, probe=lambda url: 200, sleep=lambda _s: None)
        assert target._serving_closed is True


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
    assert run_cloud.cloud_journey.PREVIEW_TARGETS == ("aws-serverless", "aws-eks")
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


def _filter_matches(pattern, message):
    """The two filter-pattern shapes the collector sends: `?"a" ?"b"` phrases, or the access-log
    JSON status pattern."""
    if pattern.startswith("{"):
        try:
            status = str(json.loads(message).get("status", ""))
        except ValueError:
            return False
        return status[:1] in ("4", "5")
    phrases = re.findall(r'\?"([^"]+)"', pattern)
    assert phrases, pattern
    return any(phrase in message for phrase in phrases)


def _lambda_cli(outputs, *, functions, groups, batch_groups=(), failing_streams=(), calls=None,
                stream_order=None):
    """A fake terraform/aws CLI for lambda_readiness_diagnostics: `groups` maps a log group to
    {stream: [(timestamp, message), ...]}. `stream_order` overrides describe-log-streams' order for
    a group (CloudWatch's LastEventTime order is eventually consistent, so it can be stale)."""
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
            names = (stream_order or {}).get(group) or sorted(
                groups[group], key=lambda n: max(t for t, _ in groups[group][n]), reverse=True)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"logStreams": [
                {"logStreamName": n} for n in names[:int(argv[argv.index("--max-items") + 1])]]}), "")
        if verb == "filter-log-events":
            if group not in groups:
                return subprocess.CompletedProcess(argv, 254, "", "ResourceNotFoundException: token=abc gone")
            pattern = argv[argv.index("--filter-pattern") + 1]
            events = sorted(({"timestamp": t, "logStreamName": n, "message": m + "\n"}
                             for n, rows in groups[group].items() for t, m in rows
                             if _filter_matches(pattern, m)), key=lambda e: e["timestamp"])
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"events": events[:int(argv[argv.index("--max-items") + 1])]}), "")
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
            assert call[2].split("-")[0] in ("list", "describe", "get", "filter"), call
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


def test_lambda_diagnostics_read_every_stream_sweep_failure_signatures_and_the_access_log(monkeypatch):
    """Run 38133126673: the API tail ended at a cold start minutes before the journey's failure
    because describe-log-streams' LastEventTime order was stale, and the access log that tells an
    API Gateway refusal from a server response was never read."""
    target = serverless(run_id="r3")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    api, reconcile = "cell-it-honua", "cell-it-cp-reconcile"
    t0 = 1_791_000_000_000
    cold = [(t0 + i, f"cold start line {i}") for i in range(400)]
    cold.append((t0 + 500, "Npgsql.PostgresException (0x80004005): 53300: remaining connection slots "
                           "are reserved Password=hunter2"))
    serving = [(t0 + 180_000 + i, f"request line {i}") for i in range(50)]
    serving.append((t0 + 180_100, "warn: Honua.Ai.Protocols.Mcp.McpDataAccessSurface[8163] MCP tool "
                                  "invocation failed unexpectedly: ToolName=honua_get_style"))
    access = [(t0 + 60_000, json.dumps({"status": "200", "httpMethod": "GET", "path": "/odata/Layers"})),
              (t0 + 61_000, json.dumps({"status": "500", "httpMethod": "GET", "path": "/odata/Layers"})),
              (t0 + 61_500, json.dumps({"status": "500", "httpMethod": "GET", "path": "/odata/Layers"})),
              (t0 + 62_000, json.dumps({"status": "429", "httpMethod": "GET",
                                        "path": "/rest/services/maui-hillshade/ImageServer/tile/13/1/2"}))]
    groups = {f"/aws/lambda/{api}": {"2026/10/11/[1]cold": cold, "2026/10/11/[1]serving": serving},
              f"/aws/lambda/{reconcile}": {"r": [
                  (t0 + 9, "INIT_REPORT Init Duration: 768.74 ms Phase: invoke Status: error "
                           "Error Type: Runtime.ExitError"),
                  (t0 + 8, "Unhandled exception. System.InvalidOperationException: PostGIS preflight check "
                           "failed: Database compatibility check failed.")]},
              f"/aws/apigateway/{api}": {"access": access}}
    # LastEventTime has not caught up: the stream that served last is listed after the cold one.
    order = {f"/aws/lambda/{api}": ["2026/10/11/[1]cold", "2026/10/11/[1]serving"]}
    calls = []
    run = _lambda_cli({"lambda_function_name": api, "control_plane_reconcile_function_name": reconcile,
                       "gp_batch_enabled": "false"},
                      functions=[{"FunctionName": api}, {"FunctionName": reconcile}],
                      groups=groups, stream_order=order, calls=calls)
    report = run_cloud.lambda_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    logs = {log["logGroup"]: log for log in report["logs"]}
    api_log = logs[f"/aws/lambda/{api}"]
    # Every stream is read, so the tail ends with the serving environment's last line.
    assert api_log["lines"][-1].endswith("ToolName=honua_get_style")
    describe = next(c for c in calls if "describe-log-streams" in c)
    assert describe[describe.index("--max-items") + 1] == str(run_cloud._LOG_STREAMS)
    signatures = api_log["failureSignatures"]["signatures"]
    assert signatures["db-connection-slots"] == {
        "lines": 1, "streams": 1, "first": "2026-10-03T04:00:00.500Z", "last": "2026-10-03T04:00:00.500Z"}
    assert signatures["mcp-tool-unexpected"]["lines"] == 1
    assert "2026/10/11/[1]serving" in api_log["failureSignatures"]["samples"][-1]
    assert "hunter2" not in json.dumps(api_log)
    crash = logs[f"/aws/lambda/{reconcile}"]["failureSignatures"]["signatures"]
    assert set(crash) == {"runtime-exit", "unhandled-exception", "preflight-failed"}
    access_log = logs[f"/aws/apigateway/{api}"]
    assert access_log["accessLog"] is True and len(access_log["lines"]) == 4
    assert access_log["accessErrors"]["byStatus"] == {"429": 1, "500": 2}
    assert access_log["accessErrors"]["byRoute"]["500 GET /odata/Layers"] == 2
    assert len(access_log["accessErrors"]["samples"]) == 3


def test_lambda_diagnostics_record_unreadable_signature_and_access_sweeps(monkeypatch):
    target = serverless(run_id="r4")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("iac"))
    api = "cell-dev-honua"
    run = _lambda_cli({"lambda_function_name": api, "gp_batch_enabled": "false"},
                      functions=[{"FunctionName": api}], groups={f"/aws/lambda/{api}": {"a": [(1, "ok")]}})
    report = run_cloud.lambda_readiness_diagnostics(target, run=run, redact=run_cloud._redact_log)
    logs = {log["logGroup"]: log for log in report["logs"]}
    assert logs[f"/aws/lambda/{api}"]["failureSignatures"] == {"signatures": {}, "truncated": False, "samples": []}
    # The cell's access log group is missing: recorded, never fatal.
    access_log = logs[f"/aws/apigateway/{api}"]
    assert "token=[redacted]" in access_log["error"] and "token=[redacted]" in access_log["accessErrors"]["error"]


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


def _model_cell(monkeypatch, run_id, *, deterministic_status, cell="aws-ecs/redis-off", endpoint="http://cell.example"):
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(run_cloud.cloud_journey, "reset_prior_attempt", lambda *args, **kwargs: ["reset test fixture"])
    directory = run_cloud.cloud_journey.cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / "receipt-1.json"
    target = directory / "target-1.json"
    target.write_text(json.dumps({"id": cell, "replicaBaseUrl": endpoint}))
    receipt.write_text(json.dumps({"status": deterministic_status, "target": {
        "id": cell, "configSha256": hashlib.sha256(target.read_bytes()).hexdigest()}}))
    (directory / run_cloud.JOURNEY_NAME).write_text(json.dumps(
        {"journeyAttempts": [{"number": 1, "receipt": str(receipt.relative_to(run_cloud.E2E_DIR))}]}))
    return directory


def _canary_receipt(attempt, status, attribution, cell="aws-ecs/redis-off"):
    return {"mode": "genuine-model", "cell": cell, "attempt": attempt, "lockDigest": LOCK,
            "status": status, "failureAttribution": attribution,
            "profile": "redis-off-serving-refusal" if cell.endswith("/redis-off") else "full-workflow",
            "executionOutcome": "refused" if cell.endswith("/redis-off") else "completed",
            "functionalGpExecuted": not cell.endswith("/redis-off"),
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


@pytest.mark.parametrize("cell", sorted(run_cloud.MODEL_CANARY_CELLS))
def test_model_canary_binds_each_attempt_to_the_lock_and_reports_the_nightly_row(monkeypatch, cell):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990002", deterministic_status="pass", cell=cell, endpoint="https://cell.example")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-forward-cloud-credentials")
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward-github-credentials")
    try:
        calls = []

        def fake_run(argv, *, env, cwd, check):
            calls.append((argv, env))
            number = int(argv[argv.index("--attempt") + 1])
            outcome = ("fail", "infrastructure") if number == 1 else ("pass", None)
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(_canary_receipt(number, *outcome, cell=cell)))
            return subprocess.CompletedProcess(argv, 1 if number == 1 else 0)

        report = run_cloud.model_canary_phase(
            {"cell": cell, "endpoint": "https://cell.example/"}, admin_key="cell-key",
            lock_digest=LOCK, run=fake_run)
        assert len(calls) == 2
        argv, env = calls[0]
        assert argv[argv.index("--lock-digest") + 1] == LOCK
        assert argv[argv.index("--cell") + 1] == cell
        assert argv[argv.index("--profile") + 1] == ("redis-off-serving-refusal" if cell.endswith("/redis-off") else "full-workflow")
        assert argv[argv.index("--target") + 1] == str(directory / "target-1.json")
        assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
        assert env["HONUA_CLOUD_JOURNEY_ADMIN"] == "cell-key"
        assert argv[argv.index("--base-url") + 1] == "https://cell.example/api"
        # Owner ruling canary-http-cell-2026-10-08: only this cell's own host may use plain HTTP.
        assert argv[argv.index("--allow-http-cell") + 1] == "cell.example"
        assert argv[argv.index("--deterministic-receipt") + 1].endswith("receipt-1.json")
        assert env["TERMINAL_MODEL_API_KEY"] == "cell-key" and "cell-key" not in argv
        # Never a bearer token: an OIDC-enabled cell routes bearers to the JWT handler (#795).
        assert argv[argv.index("--api-key-header") + 1] == "x-api-key"
        row = report["cells"][0]
        assert report["status"] == row["status"] == "pass"
        assert row["cell"] == cell
        assert [r["modelAttempt"] for r in report["harnessSetup"]] == [1, 2]
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


# ---- owner decision 8 of 2026-10-10: the HTTPS ECS cell redirects plain HTTP ----------------------
def test_ecs_https_cell_requests_the_http_redirect_only_on_the_tls_path_of_a_declaring_root(monkeypatch):
    _ecs_env(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS, "alb_enable_http_redirect")
        # Plain-HTTP cell: no redirect to a port 443 that serves nothing.
        target = ecs(run_id="r1")
        assert "alb_enable_http_redirect" not in _tf_vars(target._vars(False))
        assert target.https_redirect_expectation(False)[0] is False
        assert "plain-HTTP cell" in target.https_redirect_expectation(False)[1]
        _cell_dns(monkeypatch)
        for redis in (False, True):
            if redis:
                monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
            for destroy in (False, True):
                values = _tf_vars(ecs(run_id="r1")._vars(redis, destroy=destroy))
                assert values["alb_enable_http_redirect"] == "true" and "domain_name" in values
            mode = "on" if redis else "off"
            assert ecs(run_id="r1").https_redirect_expectation(redis) == (
                True, f"HTTPS cell r1-aws-ecs-redis-{mode}.cert.demo.honua.io")
        # An iac pin whose root predates the variable passes nothing (the module default applies).
        _iac_root_with(monkeypatch, base, "aws", *_DOMAIN_ROOT_VARS)
        values = _tf_vars(ecs(run_id="r1")._vars(False))
        assert "alb_enable_http_redirect" not in values and "domain_name" in values


def test_serverless_cell_never_requests_or_expects_the_http_redirect(monkeypatch):
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "alb_enable_http_redirect", "domain_name")
        target = serverless(run_id="r1")
        assert "alb_enable_http_redirect" not in _tf_vars(target._vars(False))
        expected, reason = target.https_redirect_expectation(False)
        assert expected is False and "no load-balancer HTTPS listener" in reason


def test_ecs_dns_misconfiguration_expectation_is_blocked_with_the_refusal(monkeypatch):
    _ecs_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", CELL_ZONE)
    expected, reason = ecs(run_id="r1").https_redirect_expectation(False)
    assert expected is False and "HONUA_AWS_CELL_DNS_PARENT is unset" in reason


def test_run_cloud_hands_the_cell_redirect_expectation_and_a_non_following_fetch_to_the_canary(monkeypatch):
    class _HttpsStub(_ServingStub):
        def https_redirect_expectation(self, redis_enabled):
            return True, f"HTTPS cell redis={redis_enabled}"

    for stub, want in ((_HttpsStub("https://stub.example.invalid"), (True, "HTTPS cell redis=True")),
                       (_ServingStub(), (False, "stub does not provision an HTTP->HTTPS redirect listener"))):
        calls = []
        _run_serving(monkeypatch, ready_status=200, checks=[], canary=[], stub=stub, canary_calls=calls)
        (args, kwargs), = calls
        assert kwargs["https_redirect"] == want
        assert callable(kwargs["redirect_fetch"])


def test_serverless_cell_with_the_dns_variables_applies_its_https_domain_on_a_declaring_root(monkeypatch):
    # honua-iac#232: examples/aws-serverless takes domain_name + route53_zone_id (API Gateway regional
    # custom domain, DNS-validated ACM certificate, Route53 alias). Same per-run name as the ECS cells.
    from targets.terraform_target import SERVERLESS_SPEC
    assert SERVERLESS_SPEC.cell_domain and SERVERLESS_SPEC.cors_allowed_origins == ("http://127.0.0.1:18099",)
    assert not SERVERLESS_SPEC.alb_http_redirect and not SERVERLESS_SPEC.needs_runner_alb_access
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "domain_name", "route53_zone_id",
                       "cors_allowed_origins", "alb_enable_http_redirect")
        for redis in (False, True):
            if redis:
                monkeypatch.setenv("HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN", KEY_RING_ARN)
            mode = "on" if redis else "off"
            fqdn = f"38038433205-aws-serverless-redis-{mode}.cert.demo.honua.io"
            target = serverless(run_id="38038433205")
            assert target.cell_domain(redis) == (fqdn, CELL_ZONE)
            for destroy in (False, True):
                values = _tf_vars(target._vars(redis, destroy=destroy))
                assert values["domain_name"] == fqdn and values["route53_zone_id"] == CELL_ZONE
                assert json.loads(values["cors_allowed_origins"]) == ["http://127.0.0.1:18099"]
                # No ALB: no runner ingress input and never the redirect listener.
                assert not {"alb_enable_http_redirect", "allow_https_ingress_cidrs",
                            "allow_http_ingress_cidrs"} & set(values)
        expected, reason = serverless(run_id="r1").https_redirect_expectation(False)
        assert expected is False and "API Gateway endpoint serves HTTPS only" in reason


def test_serverless_cell_on_an_older_root_keeps_execute_api_instead_of_refusing(monkeypatch, capsys):
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        # A pin whose serverless root predates honua-iac#232 (one input alone is not enough either).
        for declared in ((), ("domain_name",), ("route53_zone_id", "cors_allowed_origins")):
            _iac_root_with(monkeypatch, base, "aws-serverless", *declared)
            target = serverless(run_id="r1")
            assert target.cell_domain(False) is None
            values = _tf_vars(target._vars(False))
            assert not {"domain_name", "route53_zone_id"} & set(values)
        execute_api = "https://abc123.execute-api.us-east-1.amazonaws.com"
        monkeypatch.setattr(target, "_tf", lambda root, *args, check=True: subprocess.CompletedProcess(
            args, 0, execute_api + "\n", ""))
        assert target.provision(redis_enabled=False) == execute_api
        assert "keeps its default endpoint" in capsys.readouterr().out
        # Teardown makes no DNS or ACM call for a cell that never had a name.
        target._cell_dns_leftovers(False, run=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no AWS call for a cell without a domain")))


def test_serverless_cell_reads_its_https_endpoint_from_honua_url(monkeypatch, capsys):
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "domain_name", "route53_zone_id")
        target = serverless(run_id="38038433205")
        calls = []

        def tf(root, *args, check=True):
            calls.append(args)
            out = "https://38038433205-aws-serverless-redis-off.cert.demo.honua.io\n" if args[0] == "output" else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        monkeypatch.setattr(target, "_tf", tf)
        assert target.provision(redis_enabled=False) == \
            "https://38038433205-aws-serverless-redis-off.cert.demo.honua.io"
        assert ("output", "-raw", "honua_url") in calls
        words = capsys.readouterr().out.split()
        assert words[words.index("hostname") + 1] == "38038433205-aws-serverless-redis-off.cert.demo.honua.io"


def test_serverless_dns_misconfiguration_refuses_provision_but_never_destroy(monkeypatch):
    _serverless_env(monkeypatch)
    monkeypatch.setenv("HONUA_AWS_CELL_DNS_ZONE_ID", CELL_ZONE)
    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "domain_name", "route53_zone_id")
        with pytest.raises(ProvisionError, match="HONUA_AWS_CELL_DNS_PARENT is unset"):
            serverless(run_id="r1")._vars(False)
        assert "domain_name" not in _tf_vars(serverless(run_id="r1")._vars(False, destroy=True))


def test_serverless_teardown_confirms_its_dns_names_and_certificate_are_gone(monkeypatch, capsys):
    _serverless_env(monkeypatch)
    _cell_dns(monkeypatch)
    own = "38038433205-aws-serverless-redis-on.cert.demo.honua.io"
    zone = {"ResourceRecordSets": [
        {"Name": "demo.honua.io.", "Type": "A"},
        {"Name": "38038433205-aws-ecs-redis-on.cert.demo.honua.io.", "Type": "A"}]}
    certs = {"CertificateSummaryList": []}

    def run(argv, **kwargs):
        if argv[1:3] == ["route53", "list-resource-record-sets"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(zone), "")
        if argv[1:3] == ["acm", "list-certificates"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(certs), "")
        raise AssertionError(argv)

    with tempfile.TemporaryDirectory() as base:
        _iac_root_with(monkeypatch, base, "aws-serverless", "domain_name", "route53_zone_id")
        target = serverless(run_id="38038433205")
        target._cell_dns_leftovers(True, run=run)   # the concurrent ECS name is a warning only
        warning = [line for line in capsys.readouterr().out.splitlines()
                   if line.startswith("::warning title=cell DNS names present")]
        assert len(warning) == 1 and warning[0].rsplit(": ", 1)[1].split(", ") == [
            "38038433205-aws-ecs-redis-on.cert.demo.honua.io"]
        zone["ResourceRecordSets"].append({"Name": f"{own}.", "Type": "A"})
        with pytest.raises(ProvisionError, match="1 Route53 record"):
            target._cell_dns_leftovers(True, run=run)
        zone["ResourceRecordSets"].pop()
        certs["CertificateSummaryList"].append({"DomainName": own, "CertificateArn": "arn:cell"})
        with pytest.raises(ProvisionError, match="1 ACM certificate"):
            target._cell_dns_leftovers(True, run=run)
        with pytest.raises(ProvisionError, match="could not verify"):
            target._cell_dns_leftovers(True, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "x"))


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


@pytest.mark.parametrize("change,expected", [
    ("qualification-only", 0), ("never-provisioned", 0), ("destroy-failed", 1),
    ("lost-state", 1), ("unknown-obligation", 1), ("missing-report", 1),
    ("secret-cleanup", 1), ("staged-fixture", 1), ("cost-failed", 1),
])
def test_preview_workflow_cannot_tolerate_failed_cleanup(tmp_path, change, expected):
    workflow, cell = _cloud_workflows()
    teardown = cell["jobs"]["teardown"]
    assert teardown["continue-on-error"] is False
    steps = teardown["steps"]
    destroy = next(step for step in steps if (step.get("name") or "").startswith("Tear down"))
    assert "continue-on-error" not in destroy
    guard = next(step for step in steps if step.get("name") == "Refuse incomplete cleanup on every topology")
    assert guard["if"] == "always()" and steps.index(guard) > steps.index(destroy)
    # Execute the actual workflow guard against retained reports. Qualification failure alone
    # remains informational; lost state, failed destroy, leaked secrets and spend remain fatal.
    report = {"status": "fail", "provisionAttempted": True,
              "teardown": {"attempted": True, "completed": True}, "cost": {"status": "pass"}}
    if change == "never-provisioned":
        report.update(provisionAttempted=False, teardown={"attempted": False, "completed": False})
    elif change == "destroy-failed": report["teardown"]["completed"] = False
    elif change == "lost-state": report["teardown"].update(attempted=False, completed=False)
    elif change == "unknown-obligation": del report["provisionAttempted"]
    elif change == "secret-cleanup": report["cleanupFailures"] = ["owned-secret-delete-failed"]
    elif change == "staged-fixture": report["s10Cleanup"] = {"status": "failed"}
    elif change == "cost-failed": report["cost"]["status"] = "fail"
    directory = tmp_path / "e2e"
    directory.mkdir()
    if change != "missing-report":
        (directory / "gate-report-cloud.json").write_text(json.dumps(report))
    result = subprocess.run(["bash", "-c", guard["run"]], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(E2E_DIR)}, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("handoff", ["missing", "stale", "confirmed-no-provision"])
def test_preview_teardown_refuses_unconfirmed_missing_state(monkeypatch, tmp_path, handoff):
    cj = run_cloud.cloud_journey
    directory = _phase_env(monkeypatch, run_id="preview-missing-state")
    monkeypatch.setattr(cj, "PREVIEW_TARGETS", ("aws-ecs",))
    stub = _PhaseStub(tmp_path)
    monkeypatch.setattr(run_cloud, "REGISTRY", {**run_cloud.REGISTRY, "aws-ecs": lambda **kw: stub})
    monkeypatch.setattr(run_cloud, "REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr(run_cloud, "forget_secret", lambda *_: None)
    monkeypatch.setenv("HONUA_CLOUD_PROVISION_MARKER", str(tmp_path / "absent-marker"))
    if handoff != "missing":
        state = {"cell": "aws-ecs/redis-off", "redis": "redis-off", "runId": "preview-missing-state",
                 "runAttempt": "2" if handoff == "stale" else "1", "candidateDigest": cj.candidate_digest(),
                 "startedAt": cj.now(), "provisionAttempted": False,
                 "report": {"cell": "aws-ecs/redis-off", "status": "fail", "journeyAttempts": []}}
        run_cloud._write_json(directory / run_cloud.HANDOFF_NAME, state)
    try:
        result = run_cloud.main(["--phase", "teardown", "--target", "aws-ecs", "--redis", "off"])
        report = json.loads(run_cloud.REPORT_PATH.read_text())
        assert stub.torn_down == 0
        assert result == (0 if handoff == "confirmed-no-provision" else 1)
        assert report["status"] == "fail"  # informational qualification never becomes a pass
        if handoff != "confirmed-no-provision":
            assert "provisioning-obligation-unconfirmed" in report["cleanupFailures"]
            assert cj.cleanup_failure(report) is not None
    finally:
        shutil.rmtree(cj.EVIDENCE / "preview-missing-state", ignore_errors=True)


@pytest.mark.parametrize("mutation", ["missing-config", "digest", "endpoint", "cell", "history"])
def test_model_canary_refuses_mismatched_target_or_history_without_invoking_model(monkeypatch, mutation):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990008", deterministic_status="pass", cell="aws-serverless/redis-on")
    try:
        path = directory / "target-1.json"
        if mutation == "missing-config":
            path.unlink()
        elif mutation == "history":
            (directory / run_cloud.JOURNEY_NAME).write_text(json.dumps({"journeyAttempts": [None, "wrong"]}))
        else:
            config = json.loads(path.read_text())
            config[{"digest": "extra", "endpoint": "replicaBaseUrl", "cell": "id"}[mutation]] = "changed"
            path.write_text(json.dumps(config))
        calls = []
        report = run_cloud.model_canary_phase(
            {"cell": "aws-serverless/redis-on", "endpoint": "http://cell.example"},
            admin_key="private-app-key", lock_digest=LOCK, run=lambda *args, **kwargs: calls.append(args))
        assert not calls and report["status"] == "fail"
        assert report["cells"][0]["cell"] == "aws-serverless/redis-on"
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990008", ignore_errors=True)


def test_model_phase_rejects_handoff_for_another_cell(monkeypatch):
    seen = []
    monkeypatch.setattr(run_cloud, "_read_json", lambda *_: {"cell": "aws-ecs/redis-off", "endpoint": "https://wrong.example"})
    monkeypatch.setattr(run_cloud, "open_key", lambda *_: "application-key")
    monkeypatch.setattr(run_cloud, "model_canary_phase", lambda state, **kwargs: (
        seen.append(state) or {"status": "fail", "why": state["handoffError"]}))
    args = mock.Mock(target="aws-serverless", redis="on", sealed_key=Path("s"), private_key=Path("p"),
                     lock_digest=LOCK, max_attempts=2)
    assert run_cloud._phase_model_canary(args) == 1
    assert seen[0]["cell"] == "aws-serverless/redis-on" and "endpoint" not in seen[0]


def test_model_canary_never_reuses_old_output_when_current_process_writes_no_receipt(monkeypatch):
    sys.path.insert(0, str(run_cloud.E2E_DIR.parent / "tools"))
    directory = _model_cell(monkeypatch, "990009", deterministic_status="pass")
    try:
        out = directory / "model-journey"
        out.mkdir()
        stale = out / "model-canary-1.json"
        stale.write_text(json.dumps(_canary_receipt(1, "pass", None)))
        report = run_cloud.model_canary_phase({"cell": "aws-ecs/redis-off", "endpoint": "http://cell.example"},
            admin_key="private-app-key", lock_digest=LOCK,
            run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0))
        assert report["status"] == "fail" and not stale.exists()
        assert "before writing attempt" in report["why"]
    finally:
        shutil.rmtree(run_cloud.cloud_journey.EVIDENCE / "990009", ignore_errors=True)
@pytest.mark.parametrize('matches', [True, False])
def test_off_profile_native_after_observation_cannot_bypass_cost_or_teardown(monkeypatch, matches):
    cj = run_cloud.cloud_journey
    monkeypatch.setenv('GITHUB_RUN_ID', 'offline-run')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    cell = 'aws-ecs/redis-off'; directory = cj.cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    events = []; sha, image = _manifest_server()
    stub = _ServingStub()
    monkeypatch.setattr(stub, 'teardown', lambda **kwargs: events.append('destroy'), raising=False)
    monkeypatch.setattr(cj, 'cleanup', lambda cell: events.append('cleanup'))
    monkeypatch.setattr(cj, 'check_cost', lambda *a, **k: (events.append('cost') or {'status': 'pass'}))
    monkeypatch.setattr(cj, 'observe_ecs_image', lambda *a: (image if matches else 'wrong-image', 'native readback'))
    monkeypatch.setattr(cj, 'server_identity', lambda endpoint: {'revision': sha, 'source': 'commit-sha'})
    try:
        record = _artifact(directory, _ecs_receipt())
        record['receipt'] = str((directory / 'receipt-1.json').relative_to(E2E_DIR))
        state = {'cell': cell, 'target': 'aws-ecs', 'redis': 'redis-off', 'runId': 'offline-run', 'runAttempt': '2',
            'candidateDigest': cj.candidate_digest(), 'endpoint': 'https://cell.invalid', 'startedAt': cj.now(),
            'provisionAttempted': True, 'observedServer': {'revision': sha, 'source': 'commit-sha'}, 'runningImage': image,
            'servingFixtureSha256': record['profileObservation']['seedManifestSha256'],
            'report': {'cell': cell, 'endpoint': 'https://cell.invalid', 'readiness': {'ready': True},
                       'checks': [], 'canaryProbes': [], 'journeyAttempts': []}}
        report = run_cloud.teardown_phase(state, {'journeyAttempts': [record]}, stub,
            reference_endpoint=None, cost_report=directory / 'meter.json')
        assert state['destroyed'] is True
        assert events == ['cost', 'cleanup', 'destroy']
        assert record['profileObservation']['source'] == 'aws-control-plane'
        assert record['profileObservation']['after']['image'] == (image if matches else 'wrong-image')
        if matches:
            assert 'native observation rejected' not in report.get('why', '')
        else:
            assert report['status'] == 'fail' and 'native observation rejected' in report['why']
    finally:
        shutil.rmtree(cj.EVIDENCE / 'offline-run', ignore_errors=True)


def test_off_profile_teardown_admits_its_runner_and_reads_the_restored_root(monkeypatch, tmp_path):
    """A fresh teardown runner reads both native observations: the restored Terraform root for the
    image, and (on ECS) the cell endpoint behind an ALB that admits only listed /32s (run 39772292511)."""
    cj = run_cloud.cloud_journey
    monkeypatch.setenv('GITHUB_RUN_ID', 'offline-run')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    monkeypatch.setenv('HONUA_AWS_RUNNER_CIDR', '203.0.113.7/32')
    cell = 'aws-ecs/redis-off'; directory = cj.cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    sha, image = _manifest_server()
    stub = _ServingStub()
    stub._workdir = None
    admitted, seen_roots = [], []
    monkeypatch.setattr(stub, 'teardown', lambda **kwargs: None, raising=False)
    monkeypatch.setattr(stub, '_iac_root', lambda: tmp_path, raising=False)
    monkeypatch.setattr(stub, 'admit', lambda endpoint, cidr, **kw: admitted.append((endpoint, cidr)), raising=False)
    monkeypatch.setattr(cj, 'cleanup', lambda cell: None)
    monkeypatch.setattr(cj, 'check_cost', lambda *a, **k: {'status': 'pass'})
    def observe(target, pinned):
        seen_roots.append(target._workdir)
        return image, 'native readback'
    monkeypatch.setattr(cj, 'observe_ecs_image', observe)
    def identity(endpoint):
        # Only an admitted runner reads the identity.
        return {'revision': sha, 'source': 'commit-sha'} if admitted else None
    monkeypatch.setattr(cj, 'server_identity', identity)
    try:
        record = _artifact(directory, _ecs_receipt())
        record['receipt'] = str((directory / 'receipt-1.json').relative_to(E2E_DIR))
        state = {'cell': cell, 'target': 'aws-ecs', 'redis': 'redis-off', 'runId': 'offline-run', 'runAttempt': '2',
            'candidateDigest': cj.candidate_digest(), 'endpoint': 'https://cell.invalid', 'startedAt': cj.now(),
            'provisionAttempted': True, 'observedServer': {'revision': sha, 'source': 'commit-sha'}, 'runningImage': image,
            'servingFixtureSha256': record['profileObservation']['seedManifestSha256'],
            'report': {'cell': cell, 'endpoint': 'https://cell.invalid', 'readiness': {'ready': True},
                       'checks': [], 'canaryProbes': [], 'journeyAttempts': []}}
        report = run_cloud.teardown_phase(state, {'journeyAttempts': [record]}, stub,
            reference_endpoint=None, cost_report=directory / 'meter.json')
        assert admitted == [('https://cell.invalid', '203.0.113.7/32')]
        assert seen_roots == [tmp_path]
        assert record['profileObservation']['after'] == record['profileObservation']['before']
        assert 'native observation rejected' not in report.get('why', '')
    finally:
        shutil.rmtree(cj.EVIDENCE / 'offline-run', ignore_errors=True)


def test_off_profile_transient_failure_keeps_raw_block_and_spends_only_existing_retry(monkeypatch):
    cj = run_cloud.cloud_journey
    monkeypatch.setenv('GITHUB_RUN_ID', 'offline-run')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    calls = []; cell = 'aws-ecs/redis-off'
    from test_deterministic_refusal_profile import attach_fixture_profile
    def attempt(cell, number, endpoint, admin_key, running_image=None):
        calls.append(number)
        directory = cj.cell_dir(cell); directory.mkdir(parents=True, exist_ok=True)
        raw = _ecs_receipt()
        if number == 1:
            raw['status'] = 'blocked'
            for stage in raw['stages'][2:]:
                stage.update(status='blocked', blockedBy=['honua-release#381'])
                for check in stage['checks']: check.update(status='blocked', blockedBy=['honua-release#381'])
                for key in ('jobId', 'operationId', 'operationInstanceId', 'resourceUri', 'jobStatus', 'jobCreatedAt'):
                    stage[key] = None
        record = _artifact(directory, raw, number=number)
        path = directory / f'receipt-{number}.json'
        path.write_bytes(attach_fixture_profile(directory, raw, record))
        record['receipt'] = str(path.relative_to(E2E_DIR))
        return record
    monkeypatch.setattr(cj, 'attempt', attempt)
    monkeypatch.setattr(cj, 'cleanup', lambda cell: None)
    monkeypatch.setattr(run_cloud.large_import, 'run_scenario', lambda *a, **k: None)
    monkeypatch.setattr(run_cloud, 'run_drivers', lambda *a, **k: [])
    state = {'cell': cell, 'redis': 'redis-off', 'endpoint': 'https://cell.invalid', 'report': {},
             'runId': 'offline-run', 'runAttempt': '2', 'candidateDigest': cj.candidate_digest()}
    try:
        journey = run_cloud.journey_phase(state, admin_key='private-app-key')
        assert calls == [1, 2] and journey.get('status') != 'fail'
        assert len(journey['journeyAttempts']) == 2
        first = json.loads((E2E_DIR / journey['journeyAttempts'][0]['receipt']).read_bytes())
        assert first['status'] == 'blocked' and first['stages'][4]['status'] == 'blocked'
        final = journey['journeyAttempts'][-1]
        proof = json.loads((cj.cell_dir(cell) / final['servingRefusal']['file']).read_bytes())
        assert proof['status'] == 'pass' and proof['functionalGpExecuted'] is False
    finally:
        shutil.rmtree(cj.EVIDENCE / 'offline-run', ignore_errors=True)


def test_raw_pass_with_failed_profile_is_attributed_by_actual_attempt_producer(monkeypatch):
    import deterministic_refusal_profile as profile
    cj = run_cloud.cloud_journey; driver, _ = cj.drivers()
    monkeypatch.setenv('GITHUB_RUN_ID', 'offline-run'); monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    raw = _ecs_receipt(); workspace = driver.pins.ClientWorkspace(status='blocked', root=None, reason='unit seam')
    monkeypatch.setattr(driver, 'run_live', lambda *a, **k: (workspace, [], [], None))
    monkeypatch.setattr(driver, 'build_receipt', lambda **kwargs: raw)
    sha, image = _manifest_server()
    monkeypatch.setattr(cj, 'server_identity', lambda endpoint: {'revision': sha, 'source': 'commit-sha'})
    monkeypatch.setattr(profile, 'produce', lambda *a, **k: {'status': 'fail', 'why': 'controlled profile failure'})
    try:
        record = cj.attempt('aws-ecs/redis-off', 1, 'https://cell.invalid', 'private-app-key', image)
        assert raw['status'] == 'pass' and record['failureAttribution'] == 'infrastructure'
        assert json.loads((cj.cell_dir(record['cell']) / record['servingRefusal']['file']).read_bytes())['status'] == 'fail'
    finally: shutil.rmtree(cj.EVIDENCE / 'offline-run', ignore_errors=True)


def test_controller_and_aggregate_refuse_unaccounted_profile_artifacts(monkeypatch, tmp_path):
    cj = run_cloud.cloud_journey
    monkeypatch.setenv('GITHUB_RUN_ID', 'offline-run'); monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    report = _cell_report(tmp_path, _ecs_receipt())
    (tmp_path / 'serving-refusal-unrecorded.json').write_text('{"status":"pass"}')
    assert 'sidecar omitted from attempt history' in _aggregate_fixture([report], tmp_path)['why']
    # Controller rejects it before copying any uploaded evidence into its trusted directory.
    sha, image = _manifest_server()
    with pytest.raises(ValueError, match='artifact omitted from attempt history'):
        run_cloud.verify_journey({'cell': 'aws-ecs/redis-off', 'observedServer': {
            'revision': sha, 'source': 'commit-sha'}, 'runningImage': image},
            {'journeyAttempts': report['journeyAttempts']}, tmp_path)


# ---- post-import settle: the S10 bulk import's DB tail must not overlap the latency-bound drivers ----
SEED = {"demo": {"maui-zoning": {"service": "maui-zoning", "layerId": 2}}}


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _probe_fetch(clock, timings_ms, status=200, seen=None):
    """Each call takes the next scripted latency (the last one repeats)."""
    queue = list(timings_ms)

    def fetch(url):
        if seen is not None:
            seen.append(url)
        clock.t += (queue.pop(0) if len(queue) > 1 else queue[0]) / 1000.0
        return cc.HttpResponse(status, "{}")
    return fetch


def _seed(tmp_path, value=SEED):
    path = tmp_path / "seed-manifest.json"
    path.write_text(json.dumps(value))
    return path


def test_settle_waits_out_the_recorded_import_tail_then_reads_fast(tmp_path):
    clock, seen = _Clock(), []
    # Run 40510504109: the demo minute served 13.6 s at the ALB; then the cell settles.
    fetch = _probe_fetch(clock, [13600, 9000, 8200, 300, 250, 240], seen=seen)
    result = run_cloud._await_settled_reads("https://cell.example", _seed(tmp_path), fetch=fetch,
                                            clock=clock, sleep=clock.sleep)
    assert result["settled"] is True and result["probes"] == 6 and result["slowestMs"] == 13600
    assert seen[0] == ("https://cell.example/rest/services/maui-zoning/FeatureServer/2/query"
                       "?f=json&where=1%3D1&outFields=*&returnGeometry=true&resultRecordCount=1")


def test_settle_is_bounded_and_recorded_never_a_verdict(tmp_path):
    clock = _Clock()
    result = run_cloud._await_settled_reads("https://cell.example", _seed(tmp_path),
                                            fetch=_probe_fetch(clock, [9000]), clock=clock, sleep=clock.sleep)
    assert result["settled"] is False and "drivers run" in result["why"]
    # Bounded by the budget plus at most one probe (make_fetch timeout 30 s).
    assert run_cloud.SETTLE_BUDGET_SECONDS - run_cloud.SETTLE_INTERVAL_SECONDS < result["waitedSeconds"] <= run_cloud.SETTLE_BUDGET_SECONDS + 30


def test_settle_counts_only_fast_successful_reads(tmp_path):
    clock = _Clock()
    result = run_cloud._await_settled_reads("https://cell.example", _seed(tmp_path),
                                            fetch=_probe_fetch(clock, [100], status=503), clock=clock, sleep=clock.sleep)
    assert result["settled"] is False


@pytest.mark.parametrize("seed", [None, {}, {"demo": {"maui-zoning": {"service": "../x", "layerId": 2}}},
                                  {"demo": {"maui-zoning": {"service": "maui-zoning", "layerId": "2"}}}])
def test_settle_without_a_demo_layer_does_not_probe(tmp_path, seed):
    path = tmp_path / "missing.json" if seed is None else _seed(tmp_path, seed)
    result = run_cloud._await_settled_reads("https://cell.example", path,
                                            fetch=lambda url: pytest.fail("probed"))
    assert result["settled"] is None


@pytest.mark.parametrize("s10_ran", [True, False])
def test_drivers_start_only_after_the_bulk_import_settles(monkeypatch, s10_ran):
    order = []
    cj = run_cloud.cloud_journey
    monkeypatch.setattr(run_cloud.large_import, "run_scenario", lambda *a, **k: (
        order.append("s10") or cc.CheckResult(run_cloud.large_import.ROW, "pass", "imported")) if s10_ran else None)
    monkeypatch.setattr(run_cloud, "_await_settled_reads",
                        lambda endpoint, seed: order.append("settle") or {"settled": True})
    monkeypatch.setattr(run_cloud, "run_drivers", lambda *a, **k: order.append("drivers") or [])
    monkeypatch.setattr(cj, "attempt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after drivers")))
    monkeypatch.setattr(cj, "cleanup", lambda cell: None)
    monkeypatch.setenv("GITHUB_RUN_ID", "offline-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    state = {"cell": "aws-ecs/redis-on", "redis": "redis-on", "endpoint": "https://cell.invalid", "report": {},
             "runId": "offline-run", "runAttempt": "1", "candidateDigest": cj.candidate_digest()}
    journey = run_cloud.journey_phase(state, admin_key="k")
    assert order == (["s10", "settle", "drivers"] if s10_ran else ["drivers"])
    assert ("postImportSettle" in journey) is s10_ran


def test_verify_journey_expects_the_manifest_bound_server_identity(monkeypatch, tmp_path):
    # An EKS cell advertises source=image-digest; the receipt binds the manifest sha via the pinned
    # manifest, so the handoff verifier must apply the same conversion or reject every such receipt.
    import cloud_journey
    seen = []
    monkeypatch.setattr(cloud_journey, "observed_server", lambda identity, image, pinned=None: seen.append(pinned) or {})
    with pytest.raises(Exception):
        run_cloud.verify_journey({"cell": "aws-eks/redis-off"}, {"journeyAttempts": "not-a-list"}, tmp_path)
    run_cloud_state = {"cell": "aws-eks/redis-off", "observedServer": {"source": "image-digest"}, "runningImage": "x"}
    try:
        run_cloud.verify_journey(run_cloud_state, {"journeyAttempts": []}, tmp_path)
    except Exception:
        pass
    assert seen and seen[-1] is not None and "components" in seen[-1]
