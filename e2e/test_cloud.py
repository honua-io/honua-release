"""Tests for the cross-cloud parity tier.

The cloud gate must (a) compare targets correctly, (b) classify each canonical check correctly, and
(c) report BLOCKED — never a fake green — when the AWS infra isn't wired. All proven here with no
cloud, no terraform, no live server (injected fetchers + an unset environment).

Run: python -m pytest e2e/test_cloud.py    (or: python e2e/test_cloud.py)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canonical_checks as cc  # noqa: E402
import parity as par  # noqa: E402
import run_cloud  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.base import ProvisionError  # noqa: E402
from targets.terraform_target import ecs, serverless  # noqa: E402

_AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_ROLE_ARN", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "HONUA_LAMBDA_IMAGE_URI", "HONUA_ECS_IMAGE", "HONUA_IAC_DIR", "HONUA_HELM_DIR",
            "HONUA_AWS_DB_INGRESS_CIDR", "HONUA_LAMBDA_ARCHITECTURE", "HONUA_ECS_ARCHITECTURE",
            "HONUA_AWS_RUNNER_CIDR")


# ---- canonical checks: result normalisation -------------------------------------------------------
def _fetcher(routes):
    """routes: list of (url_substr, HttpResponse). First match wins; default = unreachable."""
    def fetch(url):
        for sub, resp in routes:
            if sub in url:
                return resp
        return cc.HttpResponse(0, "no route")
    return fetch


def test_health_pass_fail_blocked():
    assert cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(200, "ok"))])).status == "pass"
    assert cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(503, "down"))])).status == "fail"
    assert cc.check_health("http://x", _fetcher([("/healthz", cc.HttpResponse(0, "conn refused"))])).status == "blocked"


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

    assert cc.check_health("http://x", unreachable_fallback).status == "blocked"


def test_geoservices_error_envelope_detection():
    env = cc.HttpResponse(200, '{"error":{"code":400,"message":"Invalid where"}}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", env)])).status == "pass"
    # A 200 that is NOT an error envelope (e.g. an empty featureset) means the convention isn't surfaced.
    ok = cc.HttpResponse(200, '{"features":[]}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", ok)])).status == "fail"
    # bool code must NOT be treated as an envelope (mirrors the SDK guards).
    boolcode = cc.HttpResponse(200, '{"error":{"code":true}}')
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([("/query", boolcode)])).status == "fail"
    assert cc.check_geoservices_error_surfacing("http://x", _fetcher([])).status == "blocked"


def test_service_catalog():
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(200, '{"services":[]}'))])).status == "pass"
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(200, "not json"))])).status == "fail"
    assert cc.check_service_catalog("http://x", _fetcher([("/rest/services", cc.HttpResponse(500, ""))])).status == "fail"


def test_admin_capabilities():
    ok = cc.HttpResponse(200, '{"contractVersions":{"admin":"v1"}}')
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", ok)])).status == "pass"
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", cc.HttpResponse(200, "no"))])).status == "fail"
    assert cc.check_admin_capabilities("http://x", _fetcher([("/api/v1/admin/capabilities", cc.HttpResponse(404, ""))])).status == "fail"
    assert cc.check_admin_capabilities("http://x", _fetcher([])).status == "blocked"


def test_geoprocessing_catalog():
    gp = cc.HttpResponse(200, '{"services":[{"name":"Buffer","type":"GPServer"}]}')
    assert cc.check_geoprocessing("http://x", _fetcher([("/rest/services", gp)])).status == "pass"
    # catalog reachable but no GP advertised => blocked (honest), never a fake pass.
    nogp = cc.HttpResponse(200, '{"services":[{"name":"roads","type":"FeatureServer"}]}')
    assert cc.check_geoprocessing("http://x", _fetcher([("/rest/services", nogp)])).status == "blocked"
    assert cc.check_geoprocessing("http://x", _fetcher([])).status == "blocked"


def test_capability_manifest_pass_unauthenticated():
    expected = {"expectedGa": ["a.one", "a.two"], "excluded": [{"id": "b.gated", "reason": "gated"}]}
    body = json.dumps({
        "schemaVersion": "honua.capability_manifest.v1",
        "capabilities": [
            {"id": "a.one", "supported": True, "available": True},
            {"id": "a.two", "supported": True, "available": False},
            {"id": "b.gated", "supported": True, "available": False},
        ],
    })
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected=expected)
    assert r.status == "pass"
    assert r.evidence["expectedGaCount"] == 2
    assert r.evidence["availableCountUnauthenticated"] == 1


def test_capability_manifest_fail_on_missing_or_unsupported_id():
    expected = {"expectedGa": ["a.one", "a.missing"], "excluded": []}
    body = json.dumps({
        "schemaVersion": "honua.capability_manifest.v1",
        "capabilities": [{"id": "a.one", "supported": False, "available": False}],
    })
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected=expected)
    assert r.status == "fail"
    assert "a.missing" in r.why


def test_capability_manifest_fail_on_wrong_schema_version():
    body = json.dumps({"schemaVersion": "wrong.v0", "capabilities": []})
    r = cc.check_capability_manifest("http://x", _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, body))]),
                                     expected={"expectedGa": [], "excluded": []})
    assert r.status == "fail" and "schemaVersion" in r.why


def test_capability_manifest_blocked_when_unreachable():
    assert cc.check_capability_manifest("http://x", _fetcher([])).status == "blocked"


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
    expected = {"expectedGa": ["a.one"], "excluded": []}
    unauth_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1",
                              "capabilities": [{"id": "a.one", "supported": True, "available": False}]})
    auth_ok_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1",
                               "capabilities": [{"id": "a.one", "supported": True, "available": True}]})
    fetch = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, unauth_body))])
    auth_fetch_ok = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, auth_ok_body))])
    r = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_ok)
    assert r.status == "pass" and r.evidence["authenticated"] is True

    auth_fetch_stale = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, unauth_body))])
    r2 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_stale)
    assert r2.status == "fail" and "available=true when authenticated" in r2.why

    # An expected-GA id entirely OMITTED from the authenticated manifest (not just present-but-
    # unavailable) must also fail, not silently drop out of the `unavailable` list.
    auth_omitted_body = json.dumps({"schemaVersion": "honua.capability_manifest.v1", "capabilities": []})
    auth_fetch_omitted = _fetcher([("/api/v1/capabilities/manifest", cc.HttpResponse(200, auth_omitted_body))])
    r3 = cc.check_capability_manifest("http://x", fetch, expected=expected, authenticated_fetch=auth_fetch_omitted)
    assert r3.status == "fail" and "a.one" in r3.why and "available=true when authenticated" in r3.why


def test_run_canonical_includes_capability_manifest():
    names = {r.name for r in cc.run_canonical("http://x", _fetcher([]))}
    assert "capability-manifest" in names


def test_extended_scenarios_blocked_pending_harness_image():
    # MCP/Studio/GP-execute/top-demo against a raw cloud endpoint are BLOCKED until honua-release#35.
    ext = cc.run_extended("http://x")
    names = {r.name for r in ext}
    assert names == {"mcp-handshake", "studio-authoring", "gp-execute", "top-demo"}
    assert all(r.status == "blocked" and "honua-release#35" in r.why for r in ext)


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
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "arm64")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    assert _tf_vars(ecs(run_id="r1")._vars(False)).get("alb_deletion_protection") == "false"
    assert "alb_deletion_protection" not in _tf_vars(serverless(run_id="r1")._vars(False))


def test_ecs_uses_the_proven_x86_64_aot_manifest(monkeypatch):
    monkeypatch.setenv("HONUA_ECS_IMAGE", "img")
    monkeypatch.setenv("HONUA_ECS_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")

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


def test_run_cloud_self_skips_without_cloud_creds(monkeypatch):
    # No cloud/OIDC creds => SELF-SKIP (status: skipped, why: cloud-creds-unset), even under
    # require_real — a no-cloud local cut must not be reddened by the cloud tier.
    for var in set(_AWS_ENV) | set(_CRED_ENV):
        monkeypatch.delenv(var, raising=False)
    for target in ("aws-serverless", "aws-ecs", "aws-eks"):
        for redis in (True, False):
            r = run_cloud.run(target, require_real=False, reference_endpoint=None, redis_enabled=redis)
            assert r["status"] == "skipped" and r["why"] == "cloud-creds-unset", (target, redis)
            assert r["redis"] == ("redis-on" if redis else "redis-off")
            r2 = run_cloud.run(target, require_real=True, reference_endpoint=None, redis_enabled=redis)
            assert r2["status"] == "skipped", (target, redis)  # creds unset => cannot enforce, still skip


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
    target = ecs(run_id="r1")
    monkeypatch.setattr(target, "_iac_root", lambda: Path("."))
    monkeypatch.setattr(target, "_tf", lambda root, *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "DependencyViolation: ALB in use"))
    with __import__("pytest").raises(ProvisionError, match="teardown failed"):
        target.teardown(redis_enabled=False)


# ---- teardown always runs, and a strand is a red cell ----------------------------------------------
class _StubTarget:
    name = "stub"

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
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setattr(run_cloud, "REGISTRY", {"stub": lambda **kwargs: stub})
    return run_cloud.run("stub", require_real=False, reference_endpoint=None, redis_enabled=True)


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


if __name__ == "__main__":
    import traceback

    class _MP:
        def delenv(self, k, raising=True):
            import os
            os.environ.pop(k, None)

        def setenv(self, k, v):
            import os
            os.environ[k] = v

        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(_MP()) if "monkeypatch" in fn.__code__.co_varnames else fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
