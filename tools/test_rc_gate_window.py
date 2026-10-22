"""The rc gate window must be able to fail: never-run, stale and red-only gates block admission."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import rc_gate_window as w

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)


def _run(days_ago: float, conclusion: str = "success", sha: str = "abc123def456"):
    at = (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": 1, "html_url": "https://x/run/1", "created_at": at, "conclusion": conclusion,
            "status": "completed", "event": "schedule", "head_sha": sha}


def test_all_green_inside_window_is_admissible():
    records = {"a.yml": {"latest_success": _run(1), "latest": _run(1)},
               "b.yml": {"latest_success": _run(6.9), "latest": _run(0.5, "failure")}}
    result = w.evaluate(records, NOW, 7)
    assert result["admissible"] is True
    assert result["gates"]["b.yml"]["verdict"] == "green"
    assert "latest run failure" in result["gates"]["b.yml"]["reason"]


def test_never_run_gate_blocks():
    records = {"a.yml": {"latest_success": _run(1), "latest": _run(1)},
               "never.yml": {"latest_success": None, "latest": None}}
    result = w.evaluate(records, NOW, 7)
    assert result["admissible"] is False
    assert result["red"] == ["never.yml"]
    assert result["gates"]["never.yml"]["reason"] == "never green on trunk"


def test_red_only_gate_blocks():
    records = {"r.yml": {"latest_success": None, "latest": _run(0.1, "failure")}}
    result = w.evaluate(records, NOW, 7)
    assert result["red"] == ["r.yml"]


def test_stale_green_outside_window_blocks():
    records = {"s.yml": {"latest_success": _run(7.01), "latest": _run(7.01)}}
    result = w.evaluate(records, NOW, 7)
    assert result["red"] == ["s.yml"]
    assert "outside the 7-day window" in result["gates"]["s.yml"]["reason"]


def test_markdown_lists_every_gate_and_the_verdict():
    records = {"a.yml": {"latest_success": _run(1), "latest": _run(1)},
               "never.yml": {"latest_success": None, "latest": None}}
    md = w.render_markdown(w.evaluate(records, NOW, 7))
    assert "Admissible: **no**" in md and "red: never.yml" in md
    assert "| `a.yml` | ✅" in md and "| `never.yml` | ❌" in md


def test_gate_list_matches_release_train_uses():
    """Every reusable workflow release-train.yml calls is in TRAIN_GATES, and nothing extra."""
    train = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-train.yml"
    called = set(re.findall(r"uses:\s*\./\.github/workflows/([\w.-]+\.yml)", train.read_text()))
    assert called == set(w.TRAIN_GATES), (called ^ set(w.TRAIN_GATES))


def test_ga_deferred_gates_are_real_gates_and_excluded_from_rc_set():
    assert set(w.GA_DEFERRED) <= set(w.TRAIN_GATES + w.AI_GATES)
    assert not set(w.GA_DEFERRED) & set(w.RC_GATES)
    assert "gate-build-test.yml" in w.RC_GATES and "terminal-model-canary.yml" in w.RC_GATES


def test_default_rc_set_requires_twenty_gates_including_both_ai_gates():
    assert len(w.RC_GATES) == 20
    assert set(w.AI_GATES) == {"gate-agent-e2e.yml", "terminal-model-canary.yml"}
    assert {"gate-agent-e2e.yml", "terminal-model-canary.yml"} <= set(w.RC_GATES)
    assert set(w.GA_DEFERRED) == {"capacity-soak.yml", "rollback-certification.yml", "gate-journey.yml"}


def test_each_missing_ai_gate_prevents_rc_admission_with_all_other_gates_green():
    for missing in ("gate-agent-e2e.yml", "terminal-model-canary.yml"):
        for latest_success, latest in ((None, None), (None, _run(0.1, "failure")),
                                       (_run(7.01), _run(7.01))):
            records = {gate: {"latest_success": _run(1), "latest": _run(1)} for gate in w.RC_GATES}
            records[missing] = {"latest_success": latest_success, "latest": latest}
            result = w.evaluate(records, NOW, 7)
            assert result["admissible"] is False, (missing, latest)
            assert result["red"] == [missing]


def test_rc_release_uses_default_required_set_and_enforces_before_publish():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/rc-release.yml").read_text()
    invocation = re.search(r"python tools/rc_gate_window\.py.*?--enforce", workflow, re.DOTALL)
    assert invocation is not None
    assert "--without-ai-gates" not in invocation.group() and "--gate " not in invocation.group()
    assert "needs: gate-window" in workflow
    assert "needs.gate-window.outputs.admissible == 'true'" in workflow


def test_markdown_names_deferred_gates():
    result = w.evaluate({"a.yml": {"latest_success": _run(1), "latest": _run(1)}}, NOW, 7)
    result["deferred_to_ga"] = ["gate-journey.yml"]
    assert "Deferred to GA" in w.render_markdown(result) and "gate-journey.yml" in w.render_markdown(result)
