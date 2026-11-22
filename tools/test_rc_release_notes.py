"""Candidate notes retain the release admission and forward-upgrade policies."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rc_release_notes as notes  # noqa: E402


def test_rc_notes_name_the_forward_ga_qualification_policy():
    from upgrade_release_notes import UPGRADE_POLICY

    text = notes.render({"components": {}}, "| gate | pass |", "2026.1-rc.2", None, None, [])
    assert UPGRADE_POLICY in text
    assert "## Forward upgrade and recovery" in text
    assert "published `2026.1.0-rc.2` quickstart" in text
    assert "This source declaration does not certify an upgrade" in text
    assert "Migrations are forward-only" in text
    assert "There is no rolling rollback or N-1 database-reader compatibility contract" in text
    assert "schema 135 or later" not in text
    assert "Roll back only to images" not in text


def test_rc_notes_require_a_candidate_bound_run():
    text = notes.render({"components": {}}, "| gate | pass |", "2026.1-rc.4", None, None, [])
    assert "against any build" not in text
    assert "honua-server digest" in text
    assert "platform lock" in text
    assert "does not pass" in text
    assert "2026-10-22 window" in text
    assert "pass (manifest-pin bound, no receipt)" in text


def test_rc_notes_report_the_ai_gates_as_preview_evidence_outside_rc_admission():
    gate_record = "| gate-agent-e2e | fail |\n| terminal-model-canary | pass |"
    text = notes.render({"components": {}}, gate_record, "2026.1-rc.4", None, None, [])
    assert "mandatory for this RC" not in text
    assert ("The AI gates, `gate-agent-e2e` and `terminal-model-canary`, are out of RC admission "
            "under the 2026-10-25 founder re-tier") in text
    assert "reported as Preview evidence" in text
    assert gate_record in text
    assert "selected immutable candidate" in text
    assert "honua-release#883" in text


if __name__ == "__main__":
    test_rc_notes_name_the_forward_ga_qualification_policy()
    test_rc_notes_report_the_ai_gates_as_preview_evidence_outside_rc_admission()
    print("PASS test_rc_notes_name_the_forward_ga_qualification_policy")


def test_rc_notes_place_the_committed_narrative_under_the_rc_title():
    narrative = (Path(__file__).resolve().parents[1] / "release-notes" / "2026.1.md").read_text(encoding="utf-8")
    text = notes.render({"components": {}}, "| gate | pass |", "2026.1-rc.3", None, None, [], narrative)
    assert text.startswith("# Honua 2026.1 rc.3\n")
    assert text.count("\n# ") == 0, "the narrative H1 must not repeat under the rc title"
    assert "**Status: Release Candidate.**" in text
    assert text.index("## Known limitations") < text.index("## Components")


def test_rc_notes_publish_no_lambda_or_helm_deployment_tier():
    text = notes.render({"components": {}}, "| gate | pass |", "2026.1-rc.2", None, None, [])
    assert "EKS / Helm deployment (Preview" not in text
    assert "this release candidate publishes neither the Lambda image nor the Helm chart" in text


def test_committed_narrative_matches_the_rc2_platform():
    narrative = (Path(__file__).resolve().parents[1] / "release-notes" / "2026.1.md").read_text(encoding="utf-8")
    assert "AWS Lambda (Preview)" not in narrative
    assert "Helm on EKS" not in narrative
    assert "Image export needs Redis" not in narrative
    assert "No deployment needs Redis." in narrative
    assert "**Qualified for rc.2:** AWS ECS on Fargate" in narrative
    assert "Set this only on a single server instance" not in narrative
    assert "enable it only on a single server instance" in narrative
    assert "PostGIS as its only database store" in narrative
    assert "run weekly" not in narrative
    assert "both report server version `2026.1.1`" not in narrative
    assert "is a required admission gate" not in narrative


def test_rc_notes_leave_out_of_lock_components_out_of_the_component_table():
    manifest = {"components": {
        "honua-helm": {
            "lifecycleStatus": "Preview",
            "version": "pre-release",
            "sha": "b" * 40,
            "outOfLock": "helm-preview-out-of-lock-2026-10-26",
        },
        "honua-sdk-js": {"version": "0.4.0", "sha": "c" * 40},
    }}
    text = notes.render(manifest, "| gate | pass |", "2026.1-rc.2", None, None, [])
    assert "| honua-sdk-js | 0.4.0 |" in text
    assert "| honua-helm |" not in text


def test_rc_notes_keep_published_or_non_preview_component_even_with_out_of_lock_key():
    manifest = {"components": {
        "honua-helm": {
            "lifecycleStatus": "Preview",
            "version": "pre-release",
            "sha": "b" * 40,
            "digest": "sha256:" + "d" * 64,
            "outOfLock": "helm-preview-out-of-lock-2026-10-26",
        },
        "honua-versioned": {
            "lifecycleStatus": "Preview",
            "version": "0.1.0",
            "sha": "e" * 40,
            "outOfLock": "stale-ruling",
        },
        "honua-ga": {
            "lifecycleStatus": "GA",
            "version": "pre-release",
            "sha": "f" * 40,
            "outOfLock": "stale-ruling",
        },
    }}
    text = notes.render(manifest, "| gate | pass |", "2026.1-rc.2", None, None, [])
    assert "| honua-helm |" in text
    assert "| honua-versioned | 0.1.0 |" in text
    assert "| honua-ga |" in text
