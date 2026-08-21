#!/usr/bin/env python3
"""Validate the generated protocol certification requirements catalog."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parent


def main() -> None:
    catalog = json.loads((ROOT / "protocol-certification-requirements.v1.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "protocol-certification-requirements.v1.schema.json").read_text(encoding="utf-8"))
    revisions = json.loads((ROOT / "sources" / "source-revisions.v1.json").read_text(encoding="utf-8"))["sources"]
    jsonschema.validate(catalog, schema)
    if catalog["source_revisions"] != revisions:
        raise ValueError("Catalog source revisions differ from the pinned source manifest.")
    if catalog["complete"] is not True:
        raise ValueError("Protocol certification denominator is not declared complete.")
    keys = [
        (row["capability_key"], row["surface"], row["operation"], row["canonical_client"], row["client_lane"])
        for row in catalog["requirements"]
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("Protocol certification requirements contain duplicate cells.")
    required_surfaces = {"sdk-dotnet", "sdk-python", "sdk-js", "feature-server", "ogc", "cog", "hdf5-netcdf", "zarr"}
    missing = required_surfaces - {row["surface"] for row in catalog["requirements"]}
    if missing:
        raise ValueError(f"Protocol certification denominator is missing required surfaces: {sorted(missing)}")
    print(f"Validated {len(keys)} complete, unique protocol certification cells.")


if __name__ == "__main__":
    main()
