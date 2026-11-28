"""Synthetic inputs for candidate check classification and attempt selection.

Names and workflow paths exercise policy; identities are test-only. These fixtures
contain no execution or qualification proof.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "certification"))
import check_build_test as bt  # noqa: E402
from build_test_github import enrich_action_workflow_ids, enrich_with_workflow_runs  # noqa: E402
from build_test_policy import BuildTestPolicy, ComponentPolicy  # noqa: E402

POLICY = BuildTestPolicy.load()
SERVER = POLICY.component("honua-server")
SDK_JS = POLICY.component("honua-sdk-js")

FIXTURES = ROOT / "tools" / "fixtures" / "candidate-classification"
THREE_ENGINE = "Three-engine smoke and source-packed parity"
FEATURE_SHARD = "Server Tests (FeatureServer Tiles and Replica)"
PREBUILD = (
    "Prebuild repeated project / geoservices",
    "Prebuild repeated project / server",
)
JS_ADVISORY = (
    "Publish artifacts PR + gate strict validation",
    "Shadow parity",
    "release-please-refresh",
)


def _fixture(name: str) -> tuple[dict, dict]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    payload = {"check_runs": copy.deepcopy(data["check_runs"])}
    return data, enrich_with_workflow_runs(payload, copy.deepcopy(data.get("workflow_runs") or []))


def _classify(component: str, payload: dict) -> tuple[str, str]:
    return bt.classify(payload, POLICY.component(component))


def _red_names(why: str) -> str:
    """The core-red name list, before any exclusion note."""
    head, _, _ = why.partition(";")
    return head


def test_dotnet_certify_pin_lag_is_advisory_on_the_published_source():
    data, payload = _fixture("sdk-dotnet.json")
    assert data["sha"] == "254f111547f6a9effc2c0a9da49e17905ff127d5"
    certify = [run for run in data["check_runs"] if run["name"] == "certify"]
    assert [run["id"] for run in certify] == [1593]
    assert certify[0]["conclusion"] == "failure"

    # Pin-follow certification is advisory; the 38 build/test lanes stay core.
    status, why = _classify("honua-sdk-dotnet", payload)
    assert status == "pass", why
    assert why.startswith("all 38 core check-run(s) green")
    assert "advisory" in why and "certify" in why
    assert "certify" in POLICY.component("honua-sdk-dotnet").advisory_names


def test_js_three_engine_firefox_red_is_advisory_and_the_rest_stays_core():
    data, payload = _fixture("sdk-js.json")
    assert data["sha"] == "6772a96710a2b6111bfd5fdece97e37be75636f2"
    status, why = _classify("honua-sdk-js", payload)
    # The sample assertion is advisory; the 46 SDK build/test lanes stay core.
    assert status == "pass", why
    assert why.startswith("all 46 core check-run(s) green")
    for name in JS_ADVISORY:
        assert name in why
    assert THREE_ENGINE in why
    assert "Build site" not in why
    assert "release-please-ci" not in why


def test_js_passes_when_the_three_engine_attempt_is_green():
    _, payload = _fixture("sdk-js.json")
    flipped = copy.deepcopy(payload)
    matched = [
        run for run in flipped["check_runs"]
        if run.get("name") == THREE_ENGINE
    ]
    assert len(matched) == 1 and matched[0]["id"] == 2043
    matched[0]["conclusion"] = "success"
    status, why = _classify("honua-sdk-js", flipped)
    assert status == "pass", why
    assert THREE_ENGINE not in why
    for name in JS_ADVISORY:
        assert name in why


def test_build_site_and_release_please_ci_are_superseded_not_excluded():
    data, payload = _fixture("sdk-js.json")
    latest = {run["name"]: run for run in bt._latest_named_runs(payload["check_runs"])}
    assert latest["Build site"]["id"] == 2040
    assert latest["Build site"]["conclusion"] == "success"
    assert latest["release-please-ci"]["id"] == 2045
    assert latest["release-please-ci"]["conclusion"] == "skipped"
    raw = data["check_runs"]
    assert any(run["id"] == 1989 and run["conclusion"] == "failure" for run in raw)
    assert any(run["id"] == 2029 and run["conclusion"] == "failure" for run in raw)
    assert any(run["id"] == 2033 and run["conclusion"] == "failure" for run in raw)
    advice = SDK_JS.advisory_names
    assert "Build site" not in advice
    assert "release-please-ci" not in advice


def test_server_prebuild_observer_is_advisory_only_with_its_workflow_path():
    data, payload = _fixture("server-observer.json")
    assert data["sha"] == "a176748b58ed9fb3b2480c71506be6ab9be50f1f"
    status, why = _classify("honua-server", payload)
    assert status == "pass", why
    # Governance and observer jobs are excluded; 115 build/test lanes remain core.
    assert why.startswith("all 115 core check-run(s) green")
    for name in PREBUILD:
        assert name in why

    report = bt.BuildTestGate(lambda *_: payload, POLICY).evaluate(
        {"components": {"honua-server": {"sha": data["sha"]}}}, "strict")
    row = report["components"][0]
    # Check metadata alone cannot establish actual jobs or the pinned shard inventory.
    assert row["decided"] == "fail"
    assert "missing actual Actions job and pinned shard selection evidence" in row["why"]


def test_prebuild_checks_stay_core_when_the_workflow_path_was_not_recorded():
    _, payload = _fixture("server-observer.json")
    stripped = copy.deepcopy(payload)
    for run in stripped["check_runs"]:
        run.pop("_workflow_path", None)
    status, why = _classify("honua-server", stripped)
    assert status == "fail"
    assert _red_names(why) == (
        "2/123 core check-run(s) red (['failure']: "
        "['Prebuild repeated project / geoservices', 'Prebuild repeated project / server'])"
    )


def test_a_prebuild_named_job_on_ci_yml_is_not_covered_by_the_observer_exclusion():
    _, payload = _fixture("server-observer.json")
    retargeted = copy.deepcopy(payload)
    moved = 0
    for run in retargeted["check_runs"]:
        if str(run.get("name", "")) in PREBUILD:
            run["_workflow_path"] = ".github/workflows/ci.yml"
            moved += 1
    assert moved >= 2
    status, why = _classify("honua-server", retargeted)
    assert status == "fail", why
    for name in PREBUILD:
        assert name in _red_names(why)


PARITY_FIXTURE = ROOT / "tools" / "fixtures" / "candidate-classification" / "server-parity.json"
PARITY_RED = ("Independent prebuild baseline / ai", "Opportunistic prebuild candidate / ai")
PARITY_WORKFLOW = ".github/workflows/server-test-prebuild-parity.yml"


def _parity_payload() -> tuple[dict, dict]:
    data = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    payload = enrich_action_workflow_ids({"check_runs": copy.deepcopy(data["check_runs"])},
                                             {"workflow_runs": data["workflow_runs"]})
    return data, payload


def test_server_prebuild_parity_observer_is_advisory_on_the_trunk_sha_it_is_filed_on():
    # Observer workflows evaluate other source heads and are advisory by workflow path.
    data, payload = _parity_payload()
    assert data["sha"] == "6f197b2c8b243497ec708421bc70d27a4e562277"
    red = [run for run in payload["check_runs"] if run["name"] in PARITY_RED and run["conclusion"] == "failure"]
    assert red and {run["_workflow_path"] for run in red} == {PARITY_WORKFLOW}
    status, why = _classify("honua-server", payload)
    assert status == "pass", why
    for name in PARITY_RED:
        assert name in why
    assert PARITY_WORKFLOW in SERVER.advisory_workflows


def test_server_prebuild_parity_checks_stay_core_without_a_workflow_path_or_on_ci_yml():
    _, payload = _parity_payload()
    for path in (None, ".github/workflows/ci.yml"):
        moved = copy.deepcopy(payload)
        for run in moved["check_runs"]:
            if run["name"] in PARITY_RED:
                if path is None:
                    run.pop("_workflow_path", None)
                else:
                    run["_workflow_path"] = path
        status, why = _classify("honua-server", moved)
        assert status == "fail", why
        for name in PARITY_RED:
            assert name in _red_names(why)


def test_feature_server_shard_uses_the_latest_attempt_and_stays_core():
    shard = json.loads((FIXTURES / "server-shard-attempts.json").read_text(encoding="utf-8"))
    assert [run["conclusion"] for run in shard["check_runs"]] == ["failure", "success"]
    assert shard["check_runs"][0]["check_suite"]["id"] != shard["check_runs"][1]["check_suite"]["id"]
    build = {
        "id": 1,
        "name": "Build & Format Check",
        "status": "completed",
        "conclusion": "success",
        "app": {"slug": "github-actions"},
        "details_url": "https://github.com/honua-io/honua-server/actions/runs/1/job/1",
        "check_suite": {"id": 9},
    }

    both = {"check_runs": [build, *copy.deepcopy(shard["check_runs"])]}
    enriched = enrich_action_workflow_ids(both, {"workflow_runs": [shard["workflow_run"]]})
    assert {run["_workflow_id"] for run in enriched["check_runs"] if run["name"] == FEATURE_SHARD} == {"1000"}
    status, why = bt.classify(enriched)
    assert status == "pass", why
    assert FEATURE_SHARD not in why

    failure_only = {
        "check_runs": [build, copy.deepcopy(shard["check_runs"][0])],
    }
    enriched_failure = enrich_action_workflow_ids(
        failure_only, {"workflow_runs": [shard["workflow_run"]]}
    )
    status, why = bt.classify(enriched_failure)
    assert status == "fail"
    assert FEATURE_SHARD in _red_names(why)

    # Distinct check suites stay independent when enrichment did not record a workflow id,
    # so the older failure cannot disappear behind the later success.
    unenriched = {"check_runs": [dict(build), *copy.deepcopy(shard["check_runs"])]}
    status, why = bt.classify(unenriched)
    assert status == "fail"
    assert FEATURE_SHARD in _red_names(why)


def test_claude_review_budget_miss_is_governance_and_not_a_build():
    payload = {"check_runs": [
        {"id": 1, "name": "Build & Format Check", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "Post exact-head Claude review evidence",
         "status": "completed", "conclusion": "failure"},
    ]}
    governance = SERVER.governance_names
    assert "Post exact-head Claude review evidence" in governance
    status, why = bt.classify(payload, ComponentPolicy(governance_names=governance))
    assert status == "pass", why
    assert "Post exact-head Claude review evidence" in why
    assert bt.classify(payload)[0] == "fail"


def test_cancelled_pr_identity_job_is_governance_and_not_a_core_flake():
    payload = {"check_runs": [
        {"id": 1, "name": "Build & Format Check", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "Resolve pull request identity",
         "status": "completed", "conclusion": "cancelled"},
    ]}
    # Red: unlisted, the superseded run is counted as a cancelled core lane.
    assert bt.classify(payload)[0] != "pass"
    # Green: listed, it is excluded and the real build lane decides.
    governance = SERVER.governance_names
    assert "Resolve pull request identity" in governance
    status, why = bt.classify(payload, ComponentPolicy(governance_names=governance))
    assert status == "pass", why


def test_only_advisory_checks_are_blocked_never_pass():
    payload = {"check_runs": [
        {"name": "Shadow parity", "status": "completed", "conclusion": "failure"},
    ]}
    advisory_only = ComponentPolicy(advisory_names=SDK_JS.advisory_names,
                                    advisory_workflows=SDK_JS.advisory_workflows)
    status, why = bt.classify(payload, advisory_only)
    assert status == "blocked"
    assert "no core build/test signal" in why


def test_resolver_evaluates_with_the_whole_loaded_policy():
    source = (ROOT / "tools" / "resolve_trunk_candidate.py").read_text(encoding="utf-8")
    assert "BuildTestPolicy.load()" in source
    assert ".github/workflows/server-test-prebuild-observe.yml" in SERVER.advisory_workflows
    assert ".github/workflows/ci.yml" not in SERVER.advisory_workflows


def _runs_named(*pairs):
    return {"check_runs": [{"name": name, "status": "completed", "conclusion": conclusion}
                           for name, conclusion in pairs]}


def test_dotnet_pin_follow_and_certify_pin_lag_are_advisory_but_build_stays_core():
    core = [("Build", "success"), ("Unit Tests", "success")]
    status, why = _classify("honua-sdk-dotnet", _runs_named(*core, ("follow", "failure")))
    assert status == "pass", why
    assert "advisory" in why and "follow" in why
    # Ruling 2026-10-09 (pin-lag rule): certify on the published source refuses by construction
    # (the pin file at that commit names the previous package), so it is advisory too.
    status, why = _classify("honua-sdk-dotnet", _runs_named(*core, ("follow", "failure"),
                                                           ("certify", "failure")))
    assert status == "pass", why
    assert "certify" in why
    # The package's own build lanes stay core.
    status, why = _classify("honua-sdk-dotnet", _runs_named(("Build", "failure"), ("Unit Tests", "success"),
                                                           ("certify", "failure")))
    assert status == "fail"
    assert _red_names(why) == "1/2 core check-run(s) red (['failure']: ['Build'])"


def test_js_live_evidence_observer_is_advisory_but_browser_impact_stays_core():
    live_health = "Required live evidence health"
    browser_impact = "Compare browser impact routing"
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), (live_health, "failure")))
    assert status == "pass", why
    assert "advisory" in why and live_health in why
    assert live_health in SDK_JS.advisory_names
    # The pinned tree's own routing check stays core. A red there still refuses.
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), (live_health, "failure"), (browser_impact, "failure")))
    assert status == "fail"
    assert _red_names(why) == (
        "1/2 core check-run(s) red (['failure']: ['Compare browser impact routing'])"
    )


def test_helm_chart_publication_is_advisory_but_chart_lint_and_smoke_stay_core():
    status, why = _classify("honua-helm", _runs_named(
        ("lint-chart", "success"), ("install-upgrade-rollback-smoke", "success"),
        ("Publish chart by digest", "failure")))
    assert status == "pass", why
    for core in ("lint-chart", "install-upgrade-rollback-smoke"):
        status, why = _classify("honua-helm", _runs_named(
            ("lint-chart", "success"), ("install-upgrade-rollback-smoke", "success"),
            ("Publish chart by digest", "failure"), (core, "failure")))
        assert status == "fail" and core in _red_names(why), why


def test_server_base_image_mirror_is_advisory_but_build_and_image_jobs_stay_core():
    mirror = "Mirror .NET base images (MCR -> GHCR)"
    core = (
        ("Build & Format Check", "success"),
        ("Server Tests (FeatureServer Tiles and Replica)", "success"),
        ("Build & Push JIT (amd64)", "skipped"),
    )
    # 8cb074f7: imaged on 2026-10-23, then the nightly re-dispatch failed only on the
    # public.ecr.aws 429 while mirroring the Lambda adapter base image.
    status, why = _classify("honua-server", _runs_named(*core, (mirror, "failure")))
    assert status == "pass", why
    assert "advisory" in why and mirror in why
    assert mirror in SERVER.advisory_names
    # The source's own build, test shards and image build jobs still refuse.
    for name, _ in core:
        runs = [(n, "failure" if n == name else c) for n, c in core]
        status, why = _classify("honua-server", _runs_named(*runs, (mirror, "failure")))
        assert status == "fail" and name in _red_names(why), why


def test_js_overture_live_lane_is_env_gated_and_three_engine_is_advisory_not_env_gated():
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), ("Bounded AWS semantic workflow", "failure")))
    assert status == "pass", why
    # The sample matrix is advisory; the unit and browser build lanes remain core.
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), ("Bounded AWS semantic workflow", "failure"),
        (THREE_ENGINE, "failure")))
    assert status == "pass", why
    assert "advisory" in why and THREE_ENGINE in why
    assert THREE_ENGINE not in SDK_JS.env_gated_names
    # A red unit lane still refuses.
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "failure"), (THREE_ENGINE, "failure")))
    assert status == "fail"
    assert _red_names(why) == "1/1 core check-run(s) red (['failure']: ['Unit tests'])"


def test_the_new_exclusions_alone_are_blocked_never_pass():
    for component, name in (("honua-sdk-dotnet", "follow"), ("honua-helm", "Publish chart by digest"),
                            ("honua-sdk-js", "Bounded AWS semantic workflow")):
        status, why = _classify(component, _runs_named((name, "failure")))
        assert status == "blocked", (component, why)


def test_impact_routing_ledger_stays_excluded_until_its_promotion_window():
    """The exclusion stays, with both fail-closed gates, links, and a review date."""
    text = (ROOT / "certification" / "governance-checks.yaml").read_text(encoding="utf-8")
    name = "Audit trusted impact-routing observations"
    assert name in SERVER.governance_names
    start = text.index(f'name: "{name}"')
    why = " ".join(text[start:text.index('name: "Post exact-head Claude review evidence"', start)].split())
    assert "receipt integrity violations" in why
    assert "measured receipt loss over the 5% budget" in why
    assert "Cohort drift, superseded heads" in why
    assert "in-flight builds, and tombstoned evidence" in why
    assert "https://github.com/honua-io/honua-server/actions/runs/39651147494" in why
    assert "https://github.com/honua-io/honua-server/actions/runs/39471979464" in why
    assert "https://github.com/honua-io/honua-server/actions/runs/39311450010" in why
    assert "consecutive_green_days 2 of 7" in why
    assert "promotion_gate_ready" in why
    assert "2026-10-27" in why
    assert "no longer holds" in why
    assert "32038145537" in why
    assert "honua-server#3521" in why


def test_server_gp_canary_is_env_gated_but_its_harness_checks_stay_core():
    status, why = _classify("honua-server", _runs_named(
        ("harness-checks", "success"), ("geometry-buffer", "failure")))
    assert status == "pass", why
    assert "env-gated" in why and "geometry-buffer" in why
    status, why = _classify("honua-server", _runs_named(
        ("harness-checks", "failure"), ("geometry-buffer", "failure")))
    assert status == "fail"
    assert _red_names(why) == "1/1 core check-run(s) red (['failure']: ['harness-checks'])"


def test_grpc_certify_harness_ruling_is_advisory_but_protocol_lanes_stay_core():
    core = [("Conformance Fixtures", "success"), ("Validate Protocol Schemas", "success")]
    # Coordinator ruling 2026-10-22 (#376): certify at tag v1.0.3 loads the previous tag's fixtures
    # (runs 36619225117, 39745208615); grpc#148 run 39705695180 certified the 1.0.3 clients 42/42.
    status, why = _classify("geospatial-grpc", _runs_named(*core, ("certify", "failure")))
    assert status == "pass", why
    assert "advisory" in why and "certify" in why
    status, why = _classify("geospatial-grpc", _runs_named(
        ("Conformance Fixtures", "failure"), ("Validate Protocol Schemas", "success"), ("certify", "failure")))
    assert status == "fail"
    assert _red_names(why) == "1/2 core check-run(s) red (['failure']: ['Conformance Fixtures'])"
    status, why = _classify("geospatial-grpc", _runs_named(("certify", "failure")))
    assert status == "blocked", why


def test_failed_capacity_soak_is_governance_and_does_not_evict_the_candidate():
    payload = {"check_runs": [
        {"id": 1, "name": "Build & Format Check", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "Candidate capacity soak", "status": "completed", "conclusion": "failure"},
    ]}
    # Red: unlisted, a failed soak under an older lock counts as a red core lane.
    assert bt.classify(payload)[0] == "fail"
    governance = SERVER.governance_names
    assert "Candidate capacity soak" in governance
    status, why = bt.classify(payload, ComponentPolicy(governance_names=governance))
    assert status == "pass", why


def test_mcp_certify_and_server_nightly_client_compat_are_advisory_while_core_stays_decisive():
    status, why = _classify("geospatial-mcp", _runs_named(
        ("Validate Schemas", "success"), ("certify", "failure")))
    assert status == "pass" and "certify" in why
    assert _classify("geospatial-mcp", _runs_named(
        ("Validate Schemas", "failure"), ("certify", "failure")))[0] == "fail"

    status, why = _classify("honua-server", _runs_named(
        ("Build & Format Check", "success"),
        ("Baseline diff and gap report", "failure"),
        ("PyQGIS OGC + WFS Compatibility", "failure")))
    assert status == "pass" and "Baseline diff and gap report" in why and "PyQGIS OGC + WFS Compatibility" in why
    assert _classify("honua-server", _runs_named(
        ("Build & Format Check", "failure"),
        ("Baseline diff and gap report", "failure")))[0] == "fail"
