"""Tests for the cross-cloud parity tier.

The cloud gate must (a) compare targets correctly, (b) classify each canonical check correctly, and
(c) report BLOCKED — never a fake green — when the AWS infra isn't wired. All proven here with no
cloud, no terraform, no live server (injected fetchers + an unset environment).

Run: python -m pytest e2e/test_cloud.py    (or: python e2e/test_cloud.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import canonical_checks as cc  # noqa: E402
import parity as par  # noqa: E402
import run_cloud  # noqa: E402
from targets import REGISTRY  # noqa: E402
from targets.terraform_target import ecs, serverless  # noqa: E402

_AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_ROLE_ARN", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "HONUA_LAMBDA_IMAGE_URI", "HONUA_ECS_IMAGE", "HONUA_IAC_DIR", "HONUA_HELM_DIR")


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
    assert _tf_vars(ecs(run_id="r1")._vars(False)).get("alb_deletion_protection") == "false"
    assert "alb_deletion_protection" not in _tf_vars(serverless(run_id="r1")._vars(False))


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


if __name__ == "__main__":
    import traceback

    class _MP:
        def delenv(self, k, raising=True):
            import os
            os.environ.pop(k, None)

        def setenv(self, k, v):
            import os
            os.environ[k] = v

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
