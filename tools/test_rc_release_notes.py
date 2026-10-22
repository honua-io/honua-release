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


if __name__ == "__main__":
    test_rc_notes_say_2026_1_is_not_a_safe_rollback_target()
    print("PASS test_rc_notes_say_2026_1_is_not_a_safe_rollback_target")
