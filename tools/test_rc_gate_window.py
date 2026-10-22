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
