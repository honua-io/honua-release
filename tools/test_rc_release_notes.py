"""The rc notes name the unguarded 2026.1 rollback limit (honua-server#6484)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rc_release_notes as notes  # noqa: E402


def test_rc_notes_say_2026_1_is_not_a_safe_rollback_target():
    text = notes.render(
        {"components": {"honua-server": {"version": "pre-release", "sha": "a" * 40}}},
        "| gate | pass |",
        "2026.1-rc.4",
        None,
        None,
        [],
    )
    assert "not a safe rollback target across any migration" in text
    assert "the only safe way back to 2026.1 is restoring the backup taken before the upgrade" in text
    assert "schema 135 or later for additive migrations" in text
    assert "never across a contract-phase migration without a restore" in text
    assert "honua-release#776" in text
    assert "no release-named immutable tag" in text


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
    test_rc_notes_say_2026_1_is_not_a_safe_rollback_target()
    test_rc_notes_report_the_ai_gates_as_preview_evidence_outside_rc_admission()
    print("PASS test_rc_notes_say_2026_1_is_not_a_safe_rollback_target")


def test_rc_notes_place_the_committed_narrative_under_the_rc_title():
    narrative = (Path(__file__).resolve().parents[1] / "release-notes" / "2026.1.md").read_text(encoding="utf-8")
    text = notes.render({"components": {}}, "| gate | pass |", "2026.1-rc.3", None, None, [], narrative)
    assert text.startswith("# Honua 2026.1 rc.3\n")
    assert text.count("\n# ") == 0, "the narrative H1 must not repeat under the rc title"
    assert "**Status: Release Candidate.**" in text
    assert text.index("## Known limitations") < text.index("## Components")
