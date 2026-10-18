#!/usr/bin/env python3
"""Export authored source data for the source owner's real publication workflow.

This writes data only. It starts no service and emits no certification verdict.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from import_fidelity_journeys import FIXTURES, fixture_features, fixture_revision


def export(destination: Path):
    if destination.exists():
        raise ValueError("export destination must not already exist")
    destination.mkdir(parents=True)
    definitions = json.loads(FIXTURES.read_text())
    for role, definition in definitions["roles"].items():
        names = set(fixture_features(role)[0]["attributes"])
        document = {"fields": [field for field in definitions["fieldDefinitions"] if field["name"] in names], "role": role, "fixtureRevision": fixture_revision(), **definition,
                    "features": fixture_features(role)}
        (destination / (role + ".json")).write_text(json.dumps(document, indent=2) + "\n")
    attachment_directory = destination / "attachment-bytes"
    attachment_directory.mkdir()
    for attachment in definitions["roles"]["attachments"]["attachments"]:
        (attachment_directory / attachment["name"]).write_bytes(attachment["contentUtf8"].encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    export(arguments.output_dir)
    print("exported authored fixture data; real source publication and qualification remain required")


if __name__ == "__main__":
    main()
