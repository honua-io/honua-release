"""The operating envelope delegates candidate identity to the release lock."""

from pathlib import Path

ENVELOPE = Path(__file__).resolve().parents[1] / "docs" / "2026.1-operating-envelope.md"

# Retired September draft identities. They must not remain as current pins.
RETIRED = (
    "0.1.9-beta.0",
    "`1.6.0`",
    "`0.1.11`",
    "candidate is not yet cut",
    "Whole BuiltInProcessCatalog GA",
)


def test_envelope_uses_release_identity_without_a_copied_pin_table():
    text = ENVELOPE.read_text(encoding="utf-8")
    assert "[release notes](../release-notes/2026.1.md)" in text
    assert "signed release lock" in text
    assert "[release lock draft](platform-lock.v1.draft.yaml)" in text
    assert "[incomplete and unqualified](SDK-SERVER-COMPATIBILITY.md)" in text
    assert "Resolve component versions from the candidate lock" in text
    assert "a published package" in text
    assert "alone does not establish a qualified pairing" in text
    assert "Honua Studio" in text
    preview = text.split("## 2026.1 tiers", 1)[1].split("## Multi-tenancy", 1)[0]
    assert "GP catalog" in preview and "Preview" in preview
    assert "single-tenant only" in text
    for retired in RETIRED:
        assert retired not in text, retired
