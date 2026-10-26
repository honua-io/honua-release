"""The nightly's candidate DR drill must not share a pending slot with PR and trunk drills.

GitHub keeps one running and one pending run per concurrency group; a newer arrival cancels the
pending one even with cancel-in-progress: false. With a single static group, the nightly's
dispatched drill (candidate_ref) waiting behind any running drill was cancelled by the next pull
request or trunk-manifest drill (e.g. run 40066741676), so `dr` published no receipt_url and the
train was skipped.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/dr-drill-local-docker.yml"


def _group(event_name: str, ref: str, candidate_ref: str = "") -> str:
    concurrency = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["concurrency"]
    assert concurrency["cancel-in-progress"] is False

    def evaluate(match: re.Match) -> str:
        terms = [term.strip() for term in match.group(1).split("||")]
        values = {"inputs.candidate_ref": candidate_ref, "github.ref": ref, "github.event_name": event_name}
        for term in terms:
            if term not in values:
                raise AssertionError(f"unsupported concurrency expression term {term!r}")
            if values[term]:
                return values[term]
        return ""

    return re.sub(r"\$\{\{(.*?)\}\}", evaluate, concurrency["group"])


def test_nightly_candidate_drill_has_its_own_concurrency_group():
    candidate = _group("workflow_dispatch", "refs/heads/trunk", "a" * 40)
    others = {_group("pull_request", "refs/pull/979/merge"), _group("push", "refs/heads/trunk"),
              _group("schedule", "refs/heads/trunk"), _group("workflow_dispatch", "refs/heads/trunk")}
    assert candidate not in others
    assert candidate != _group("workflow_dispatch", "refs/heads/trunk", "b" * 40)


def test_trunk_producer_runs_stay_serialised_with_each_other():
    assert _group("push", "refs/heads/trunk") == _group("schedule", "refs/heads/trunk") \
        == _group("workflow_dispatch", "refs/heads/trunk")


def test_pull_request_drills_cannot_cancel_a_pending_trunk_drill():
    assert _group("pull_request", "refs/pull/1/merge") != _group("push", "refs/heads/trunk")
