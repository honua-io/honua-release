#!/usr/bin/env python3
"""Report the scripting certification work scope without overriding release gates.

The two producer grids remain the outcome authority. This report reconciles their
complete cell identities with a versioned scope and retains excluded raw results.
Only the protocol certification gate can validate receipts and certify a lock.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path

from check_protocol_certification import (
    DIGEST_RE, SCRIPTING_DRIVER, _reject_json_constant, _unique_json_object,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "certification/sources/desktop-scripting-scope.v1.json"
DRIVERS = {"licensed-scripting": SCRIPTING_DRIVER, "qgis-scripting": "pyqgis"}
OUTCOMES = {"certified", "certified-prior", "failing", "blocked", "not-probed", "not-applicable"}


def read_json(path: Path) -> dict:
    return read_input(path)[0]


def read_input(path: Path) -> tuple[dict, str]:
    """Parse and hash the same captured bytes, including concurrently replaced grids."""
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object,
                       parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("Input root must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def identity_digest(cells: dict) -> str:
    payload = json.dumps(sorted(cells), separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def normalize(grid: dict, source: str) -> dict:
    driver = DRIVERS[source]
    cells = {}
    for row in grid["rows"]:
        if source == "licensed-scripting" and row.get("driver") != driver:
            continue
        for column, entry in row["cells"].items():
            cell = entry if source == "licensed-scripting" else entry[driver]
            if cell["value"] not in OUTCOMES:
                raise ValueError("Unknown original outcome")
            key = (row["row"], column)
            if key in cells:
                raise ValueError("Duplicate producer cell")
            cells[key] = cell
    if not cells:
        raise ValueError("Producer contains no scripting cells")
    return cells


def report(scope: dict, grids: dict, candidate_digest: str) -> dict:
    if scope.get("schema") != "honua.desktop-scripting-scope/v1":
        raise ValueError("Unknown scripting scope schema")
    if set(scope["sources"]) != set(DRIVERS) or set(grids) != set(DRIVERS):
        raise ValueError("Both governed scripting sources are required")
    # Individual client limitations require a separate, reviewed scope revision.
    # This initial scope admits only the already deferred preview surface.
    if scope.get("client_limitations") != []:
        raise ValueError("No client limitation has been approved in this scope version")
    if not isinstance(candidate_digest, str) or DIGEST_RE.fullmatch(candidate_digest) is None:
        raise ValueError("Exact candidate digest required")
    rows = []
    summaries = {}
    for source, expected in scope["sources"].items():
        grid = grids[source]
        all_cells = normalize(grid, source)
        cells = {key: cell for key, cell in all_cells.items() if cell["value"] != "not-applicable"}
        if len(cells) != expected["applicable_count"] or identity_digest(cells) != expected["applicable_identity_sha256"]:
            raise ValueError("Applicable denominator changed; a reviewed scope revision is required")
        if len(all_cells) != expected["total_count"] or identity_digest(all_cells) != expected["total_identity_sha256"]:
            raise ValueError("Producer cell inventory changed")
        deferred = {(entry["row"], entry["column"]): entry for entry in expected["deferred_preview_cells"]}
        if len(deferred) != len(expected["deferred_preview_cells"]) or not set(deferred) <= set(cells):
            raise ValueError("Deferred scope contains duplicate or unknown cells")
        for key, entry in deferred.items():
            if source != "qgis-scripting" or key[0] != "sensorthings" or entry["target_release"] != "2026.2":
                raise ValueError("Only the approved preview deferral belongs in this version")
        for (row, column), cell in sorted(cells.items()):
            entry = {"source": source, "row": row, "column": column,
                     "required": (row, column) not in deferred,
                     "original": copy.deepcopy(cell)}
            if not entry["required"]:
                entry["deferral"] = deferred[(row, column)]
            rows.append(entry)
        required = [entry for entry in rows if entry["source"] == source and entry["required"]]
        if len(required) != expected["required_count"]:
            raise ValueError("Required cell count differs from the versioned scope")
        summaries[source] = {"applicable": len(cells), "required": len(required), "deferred": len(deferred),
                             "original_outcomes": dict(sorted(Counter(c["value"] for c in cells.values()).items())),
                             "required_outcomes": dict(sorted(Counter(c["original"]["value"] for c in required).items())),
                             "candidate_matches": grid["candidate"]["digest"] == candidate_digest}
    outstanding = [entry for entry in rows if entry["required"] and
                   (entry["original"]["value"] != "certified" or not summaries[entry["source"]]["candidate_matches"])]
    return {"schema": "honua.desktop-scripting-progress/v1", "scope_revision": scope["revision"],
            "candidate_digest": candidate_digest, "summaries": summaries, "cells": rows,
            "required_count": sum(s["required"] for s in summaries.values()),
            "outstanding_count": len(outstanding),
            "progress": "outstanding" if outstanding else "all-required-cells-reported-current",
            "release_gate_evaluated": False,
            "qualification": "Requires the protocol gate to validate complete, fresh, lock-bound receipts"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, default=SCOPE)
    parser.add_argument("--licensed-grid", type=Path, required=True)
    parser.add_argument("--qgis-grid", type=Path, required=True)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inputs = {name: read_input(path) for name, path in (
        ("scope", args.scope), ("licensed-scripting", args.licensed_grid),
        ("qgis-scripting", args.qgis_grid))}
    result = report(inputs["scope"][0], {
        name: inputs[name][0] for name in DRIVERS
    }, args.candidate_digest)
    result["input_file_sha256"] = {name: value[1] for name, value in inputs.items()}
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "cells"}, indent=2))


if __name__ == "__main__":
    main()
