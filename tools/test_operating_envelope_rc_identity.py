"""The customer operating envelope names the 2026.1 RC pins, not the September draft."""

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


def test_envelope_names_the_release_candidate_pins():
    text = ENVELOPE.read_text(encoding="utf-8")
    assert "ghcr.io/honua-io/honua-server:2026.1-rc" in text
    assert "`@honua/sdk-js` `0.2.10`" in text
    assert "`honua-sdk` `0.1.19`" in text
    assert "`Honua.Sdk` `1.12.0`" in text
    assert "Honua Studio" in text
    preview = text.split("## 2026.1 tiers", 1)[1].split("## Multi-tenancy", 1)[0]
    assert "GP catalog" in preview and "Preview" in preview
    assert "single-tenant only" in text
    for retired in RETIRED:
        assert retired not in text, retired
