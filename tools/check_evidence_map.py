#!/usr/bin/env python3
"""Parse docs/2026.1-evidence-map.md into a machine-readable instance and validate it.

The evidence map's row data is a Markdown table. This tool turns each row into JSON and
validates the machine-checkable fields against schemas/2026.1-evidence-map.schema.json, so a
malformed id, an unknown disposition, an empty cell, or an unpinned proof link fails a gate
instead of sitting unnoticed in prose. --self-test proves the checker can fail before its
verdict is trusted (AGENTS.md: a gate that can't fail is worse than no gate).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROW = re.compile(r"^\|\s*\*\*(?P<id>[A-Z]+-\d{2})\*\*\s*/\s*(?P<sev>P[0-2])\s*\|")
CELL = re.compile(r"(?<!\\)\|")
DISPOSITIONS = ("keep", "strengthen", "consolidate", "move", "remove", "not yet reviewed")
MUTABLE_LINK = re.compile(r"honua-server/(?:blob|tree)/trunk/")
AUDITED_SHA = re.compile(r"honua-server/(?:blob|tree)/(?P<sha>[0-9a-f]{40})/")


def split_cells(line: str) -> list[str]:
    parts = CELL.split(line.rstrip("\n"))
    return [p.strip() for p in parts[1:-1]]


def parse(md: str) -> tuple[list[dict], list[str]]:
    """Return (rows, errors). Errors here are shape problems the schema cannot express."""
    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for n, line in enumerate(md.splitlines(), 1):
        m = ROW.match(line)
        if not m:
            continue
        cells = split_cells(line)
        rid = m.group("id")
        if len(cells) != 6:
            errors.append(f"line {n}: row {rid} has {len(cells)} cells, expected 6")
            continue
        if rid in seen:
            errors.append(f"line {n}: duplicate row id {rid}")
        seen.add(rid)
        _, contract, expected, proof, gaps, disposition = cells
        for name, value in (("Contract", contract), ("Expected behaviour", expected),
                            ("Proof", proof), ("Gaps", gaps), ("Disposition", disposition)):
            if not value or value in {"-", "—"}:
                errors.append(f"line {n}: row {rid} has an empty {name} cell")
        found = [d for d in DISPOSITIONS if f"**{d}" in disposition]
        if not found:
            errors.append(f"line {n}: row {rid} names no disposition")
        rows.append({
            "id": rid,
            "priority": m.group("sev").lower(),
            "disposition": sorted(set(found)),
            "_line": n,
        })
    if not rows:
        errors.append("no evidence rows found — the table shape changed")
    return rows, errors


def row_validator(schema: dict) -> Draft202012Validator:
    """Validate the fields the Markdown actually encodes, against the canonical definitions."""
    row = schema["$defs"]["row"]
    props = {k: row["properties"][k] for k in ("id", "priority", "disposition")}
    return Draft202012Validator({
        "$schema": schema["$schema"],
        "type": "object",
        "properties": props,
        "required": ["id", "priority", "disposition"],
    })


def check(md_path: Path, schema_path: Path) -> list[str]:
    md = md_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    rows, errors = parse(md)
    validator = row_validator(schema)
    for r in rows:
        for err in sorted(validator.iter_errors({k: v for k, v in r.items() if k != "_line"}),
                          key=lambda e: e.path):
            errors.append(f"line {r['_line']}: row {r['id']}: {err.message}")

    mutable = [f"line {n}" for n, line in enumerate(md.splitlines(), 1) if MUTABLE_LINK.search(line)]
    if mutable:
        errors.append("proof links must be pinned to the audited revision, not trunk: "
                      + ", ".join(mutable[:5]))
    shas = set(AUDITED_SHA.findall(md))
    if len(shas) > 1:
        errors.append(f"proof links point at {len(shas)} different revisions: {sorted(shas)}")
    if shas and not any(s[:10] in md for s in shas):
        errors.append("the audited revision is linked but never named in the header")
    return errors


def instance(md_path: Path) -> dict:
    rows, _ = parse(md_path.read_text(encoding="utf-8"))
    return {"schema": "honua.release.2026.1-evidence-map/v1", "packet": "part1",
            "rows": [{k: v for k, v in r.items() if k != "_line"} for r in rows]}


def self_test(md_path: Path, schema_path: Path) -> int:
    """A gate must be able to fail. Reject three deliberate defects before trusting a pass."""
    md = md_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = row_validator(schema)
    cases = {
        "bad id family": {"id": "ZZ-01", "priority": "p1", "disposition": ["keep"]},
        "unknown disposition": {"id": "A-01", "priority": "p1", "disposition": ["ignore"]},
        "empty disposition": {"id": "A-01", "priority": "p1", "disposition": []},
    }
    failures = [name for name, bad in cases.items() if validator.is_valid(bad)]
    rows, errs = parse(md.replace("| **A-01** / P0 |", "| **A-01** / P0 |  |", 1))
    if not errs:
        failures.append("a malformed row was accepted by the parser")
    if failures:
        print("SELF-TEST FAILED — the checker cannot detect: " + "; ".join(failures))
        return 1
    print("== evidence-map self-test — PASS (3 schema defects and 1 shape defect rejected)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", type=Path, default=Path("docs/2026.1-evidence-map.md"))
    ap.add_argument("--schema", type=Path, default=Path("schemas/2026.1-evidence-map.schema.json"))
    ap.add_argument("--emit", type=Path, help="write the parsed instance as JSON")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test(a.map, a.schema)
    errors = check(a.map, a.schema)
    if a.emit:
        a.emit.parent.mkdir(parents=True, exist_ok=True)
        a.emit.write_text(json.dumps(instance(a.map), indent=2) + "\n", encoding="utf-8")
    rows = instance(a.map)["rows"]
    if errors:
        for e in errors:
            print(f"::error::{e}")
        print(f"== evidence-map gate — FAIL ({len(errors)} violation(s), {len(rows)} rows parsed)")
        return 1
    print(f"== evidence-map gate — PASS ({len(rows)} rows validated against {a.schema})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
