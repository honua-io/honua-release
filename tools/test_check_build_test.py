"""Fixture proofs for the 2026-10-06 candidate check-run classification.

The payloads under tools/fixtures/candidate-resolution-2026-10-06/ are the check-runs
and Actions runs the resolver read for the three refused sources. These tests pin which
names stay core, which a later attempt already supersedes, and which are advisory only
because they do not judge the pinned source.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "certification"))
import check_build_test as bt  # noqa: E402

FIXTURES = ROOT / "tools" / "fixtures" / "candidate-resolution-2026-10-06"
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
    workflows = {"workflow_runs": data.get("workflow_runs") or []}
    enriched = bt._enrich_action_workflow_ids(payload, workflows)
    enriched["_workflow_runs"] = copy.deepcopy(data.get("workflow_runs") or [])
    return data, enriched


def _classify(component: str, payload: dict) -> tuple[str, str]:
    advice = bt.load_advisory().get(component) or {}
    return bt.classify(
        payload,
        bt.load_env_gated().get(component, frozenset()),
        bt.load_rollup().get(component, frozenset()),
        bt.load_security().get(component, frozenset()),
        bt.load_governance().get(component, frozenset()),
        advice.get("names", frozenset()),
        advice.get("workflows", frozenset()),
    )


def _red_names(why: str) -> str:
    """The core-red name list, before any exclusion note."""
    head, _, _ = why.partition(";")
    return head


def test_dotnet_certify_pin_lag_is_advisory_on_the_published_source():
    data, payload = _fixture("honua-sdk-dotnet-d9d9fd1.json")
    assert data["sha"] == "d9d9fd1fcbe99570594160a6a3cc61b70594257b"
    certify = [run for run in data["check_runs"] if run["name"] == "certify"]
    assert [run["id"] for run in certify] == [111999844920]
    assert certify[0]["conclusion"] == "failure"

    # Ruling 2026-10-09 (pin-lag rule): certify's pin gate refuses the published source by
    # construction, so it is excluded as advisory; the 38 build/test lanes stay core.
    status, why = _classify("honua-sdk-dotnet", payload)
    assert status == "pass", why
    assert why.startswith("all 38 core check-run(s) green")
    assert "advisory" in why and "certify" in why
    assert "certify" in bt.load_advisory()["honua-sdk-dotnet"]["names"]


def test_js_three_engine_firefox_red_is_advisory_and_the_rest_stays_core():
    data, payload = _fixture("honua-sdk-js-984425f.json")
    assert data["sha"] == "984425f9d88b3c699cbd4766538ba311b48790aa"
    status, why = _classify("honua-sdk-js", payload)
    # Ruling 2026-10-09: the Firefox-only First Map sample assertion (honua-sdk-js#687) is
    # advisory; the SDK's 46 unit/browser/verify/build lanes stay core.
    assert status == "pass", why
    assert why.startswith("all 46 core check-run(s) green")
    for name in JS_ADVISORY:
        assert name in why
    assert THREE_ENGINE in why
    assert "Build site" not in why
    assert "release-please-ci" not in why


def test_js_passes_when_the_three_engine_attempt_is_green():
    _, payload = _fixture("honua-sdk-js-984425f.json")
    flipped = copy.deepcopy(payload)
    matched = [
        run for run in flipped["check_runs"]
        if run.get("name") == THREE_ENGINE
    ]
    assert len(matched) == 1 and matched[0]["id"] == 112238828187
    matched[0]["conclusion"] = "success"
    status, why = _classify("honua-sdk-js", flipped)
    assert status == "pass", why
    assert THREE_ENGINE not in why
    for name in JS_ADVISORY:
        assert name in why


def test_build_site_and_release_please_ci_are_superseded_not_excluded():
    data, payload = _fixture("honua-sdk-js-984425f.json")
    latest = {run["name"]: run for run in bt._latest_named_runs(payload["check_runs"])}
    assert latest["Build site"]["id"] == 112238700540
    assert latest["Build site"]["conclusion"] == "success"
    assert latest["release-please-ci"]["id"] == 112239395576
    assert latest["release-please-ci"]["conclusion"] == "skipped"
    raw = data["check_runs"]
    assert any(run["id"] == 112234412516 and run["conclusion"] == "failure" for run in raw)
    assert any(run["id"] == 112236298922 and run["conclusion"] == "failure" for run in raw)
    assert any(run["id"] == 112237235014 and run["conclusion"] == "failure" for run in raw)
    advice = bt.load_advisory()["honua-sdk-js"]["names"]
    assert "Build site" not in advice
    assert "release-please-ci" not in advice


def test_server_prebuild_observer_is_advisory_only_with_its_workflow_path():
    data, payload = _fixture("honua-server-c19f29d.json")
    assert data["sha"] == "c19f29d1828814f0f4c372b0dce73554074ba3b1"
    status, why = _classify("honua-server", payload)
    assert status == "pass", why
    assert why.startswith("all 120 core check-run(s) green")
    for name in PREBUILD:
        assert name in why

    report = bt.evaluate(
        {"components": {"honua-server": {"sha": data["sha"]}}},
        lambda *_: payload,
        "strict",
        bt.load_env_gated(),
        bt.load_rollup(),
        bt.load_security(),
        bt.load_governance(),
        bt.load_full_matrix(),
        bt.load_advisory(),
    )
    row = report["components"][0]
    assert row["decided"] == "pass"
    assert "full-matrix run 37435142851 completed successfully with all expected lanes" in row["why"]


def test_prebuild_checks_stay_core_when_the_workflow_path_was_not_recorded():
    _, payload = _fixture("honua-server-c19f29d.json")
    stripped = copy.deepcopy(payload)
    for run in stripped["check_runs"]:
        run.pop("_workflow_path", None)
    status, why = _classify("honua-server", stripped)
    assert status == "fail"
    assert _red_names(why) == (
        "2/124 core check-run(s) red (['failure']: "
        "['Prebuild repeated project / geoservices', 'Prebuild repeated project / server'])"
    )


def test_a_prebuild_named_job_on_ci_yml_is_not_covered_by_the_observer_exclusion():
    _, payload = _fixture("honua-server-c19f29d.json")
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


def test_feature_server_shard_uses_the_latest_attempt_and_stays_core():
    shard = json.loads((FIXTURES / "honua-server-matrix-shard-attempts.json").read_text(encoding="utf-8"))
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
    enriched = bt._enrich_action_workflow_ids(both, {"workflow_runs": [shard["workflow_run"]]})
    assert {run["_workflow_id"] for run in enriched["check_runs"] if run["name"] == FEATURE_SHARD} == {"216825513"}
    status, why = bt.classify(enriched)
    assert status == "pass", why
    assert FEATURE_SHARD not in why

    failure_only = {
        "check_runs": [build, copy.deepcopy(shard["check_runs"][0])],
    }
    enriched_failure = bt._enrich_action_workflow_ids(
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
    governance = bt.load_governance()["honua-server"]
    assert "Post exact-head Claude review evidence" in governance
    status, why = bt.classify(payload, frozenset(), frozenset(), frozenset(), governance)
    assert status == "pass", why
    assert "Post exact-head Claude review evidence" in why
    assert bt.classify(payload)[0] == "fail"


def test_only_advisory_checks_are_blocked_never_pass():
    payload = {"check_runs": [
        {"name": "Shadow parity", "status": "completed", "conclusion": "failure"},
    ]}
    advice = bt.load_advisory()["honua-sdk-js"]
    status, why = bt.classify(
        payload, frozenset(), frozenset(), frozenset(), frozenset(),
        advice["names"], advice["workflows"],
    )
    assert status == "blocked"
    assert "no core build/test signal" in why


def test_resolver_source_passes_the_advisory_list_into_evaluate():
    source = (ROOT / "tools" / "resolve_trunk_candidate.py").read_text(encoding="utf-8")
    assert "advisory=ci.load_advisory()" in source
    assert ".github/workflows/server-test-prebuild-observe.yml" in (
        bt.load_advisory()["honua-server"]["workflows"]
    )
    assert ".github/workflows/ci.yml" not in bt.load_advisory()["honua-server"]["workflows"]


# ---- 2026-10-08 resolver refusals (honua-release#471, nightly-certification run 37819830412) ----
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


def test_js_overture_live_lane_is_env_gated_and_three_engine_is_advisory_not_env_gated():
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), ("Bounded AWS semantic workflow", "failure")))
    assert status == "pass", why
    # Both were red on 984425f9; the three-engine matrix is advisory (Firefox-only sample
    # assertion, honua-sdk-js#687), never env-gated: it needs no external backend.
    status, why = _classify("honua-sdk-js", _runs_named(
        ("Unit tests", "success"), ("Bounded AWS semantic workflow", "failure"),
        (THREE_ENGINE, "failure")))
    assert status == "pass", why
    assert "advisory" in why and THREE_ENGINE in why
    assert THREE_ENGINE not in bt.load_env_gated().get("honua-sdk-js", frozenset())
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
