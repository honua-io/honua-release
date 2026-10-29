"""The documented 2026.1 Compose quickstarts must select the qualified amd64 image."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("docs/INSTALL-2026.1.md", "docs/CUSTOMER-INSTALL.md")


def _first_compose(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(.*?)```", text, re.S)
    assert match, path
    document = yaml.safe_load(match.group(1))
    assert isinstance(document, dict)
    return document


def test_install_quickstarts_pin_the_server_to_linux_amd64():
    for relative in GUIDES:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        compose = _first_compose(path)
        assert compose["services"]["honua"]["platform"] == "linux/amd64"
        assert "not a supported 2026.1 topology" in text
