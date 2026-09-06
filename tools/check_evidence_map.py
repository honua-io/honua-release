#!/usr/bin/env python3
"""Parse docs/2026.1-evidence-map.md into machine-readable instances and validate them.

The evidence map's row data is a Markdown table. This tool turns each row into JSON and
validates the machine-checkable fields against schemas/2026.1-evidence-map.schema.json, so a
malformed id, an unknown disposition, an empty cell, or an unpinned proof link fails a gate
instead of sitting unnoticed in prose. --self-test proves the checker can fail before its
verdict is trusted (AGENTS.md: a gate that can't fail is worse than no gate).

One document holds two packets. A row's packet is read from the top-level heading that
precedes it, and --emit writes one instance per packet, so downstream evidence cannot
publish part-2 rows under a part-1 identity.
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
# The document is one file holding two packets. A row belongs to the packet whose
# top-level heading most recently preceded it, so the emitted instances carry the
# packet identity the schema distinguishes instead of a hard-coded guess.
PACKET_HEADING = re.compile(r"^# .*\bpart (?P<n>[12])\b", re.IGNORECASE)
PACKETS = ("part1", "part2")


def split_cells(line: str) -> list[str]:
    parts = CELL.split(line.rstrip("\n"))
    return [p.strip() for p in parts[1:-1]]


def parse(md: str) -> tuple[list[dict], list[str]]:
    """Return (rows, errors). Errors here are shape problems the schema cannot express."""
    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    packet = "part1"
    headings: set[str] = set()
    for n, line in enumerate(md.splitlines(), 1):
        heading = PACKET_HEADING.match(line)
        if heading:
            packet = f"part{heading.group('n')}"
            headings.add(packet)
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
            "_packet": packet,
            "_line": n,
        })
    if not rows:
        errors.append("no evidence rows found — the table shape changed")
    for declared in sorted(headings):
        if not any(r["_packet"] == declared for r in rows):
            errors.append(f"packet {declared} has a heading but owns no rows — the packet boundary moved")
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
        for err in sorted(validator.iter_errors({k: v for k, v in r.items() if not k.startswith("_")}),
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


def instances(md_path: Path) -> dict[str, dict]:
    """One schema-shaped instance per packet present, keyed by packet.

    The document holds both packets, so emitting every row under a single hard-coded
    packet would publish the part-2 rows under the wrong scope. Packets with no rows
    are omitted rather than emitted empty.
    """
    rows, _ = parse(md_path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for packet in PACKETS:
        owned = [{k: v for k, v in r.items() if not k.startswith("_")}
                 for r in rows if r["_packet"] == packet]
        if owned:
            out[packet] = {"schema": "honua.release.2026.1-evidence-map/v1",
                           "packet": packet, "rows": owned}
    return out


def emit(target: Path, by_packet: dict[str, dict]) -> list[Path]:
    """Write one file per packet next to `target`, named `<stem>.<packet><suffix>`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for packet, payload in by_packet.items():
        path = target.with_name(f"{target.stem}.{packet}{target.suffix}")
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


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

    # The packet split must come from the document, not from a constant. Dropping the
    # part-2 heading has to move rows into part1; keeping the heading but removing its
    # rows has to be reported rather than silently emitting an empty packet.
    packet_schema = {**schema["properties"]["packet"], "$schema": schema["$schema"]}
    if Draft202012Validator(packet_schema).is_valid("part1+part2"):
        failures.append("the schema accepted an invented packet identity")
    real, _ = parse(md)
    if len({r["_packet"] for r in real}) < 2:
        failures.append("the parser assigned every row to one packet")
    flattened, _ = parse(re.sub(r"^# .*\bpart 2\b.*$", "## flattened", md,
                                count=1, flags=re.IGNORECASE | re.MULTILINE))
    if any(r["_packet"] != "part1" for r in flattened):
        failures.append("removing the part-2 heading left rows in part2")
    if sum(1 for r in real if r["_packet"] == "part1") == len(flattened):
        failures.append("the part-2 heading did not change the packet split")
    if failures:
        print("SELF-TEST FAILED — the checker cannot detect: " + "; ".join(failures))
        return 1
    print("== evidence-map self-test — PASS "
          "(3 schema defects, 1 shape defect, 1 invented packet and 2 packet-split defects rejected)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", type=Path, default=Path("docs/2026.1-evidence-map.md"))
    ap.add_argument("--schema", type=Path, default=Path("schemas/2026.1-evidence-map.schema.json"))
    ap.add_argument("--emit", type=Path,
                    help="write one JSON instance per packet, named <stem>.<packet><suffix>")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test(a.map, a.schema)
    errors = check(a.map, a.schema)
    by_packet = instances(a.map)
    if a.emit:
        for path in emit(a.emit, by_packet):
            print(f"wrote {path}")
    counts = ", ".join(f"{p}={len(i['rows'])}" for p, i in by_packet.items())
    total = sum(len(i["rows"]) for i in by_packet.values())
    if errors:
        for e in errors:
            print(f"::error::{e}")
        print(f"== evidence-map gate — FAIL ({len(errors)} violation(s), {total} rows parsed; {counts})")
        return 1
    print(f"== evidence-map gate — PASS ({total} rows validated against {a.schema}; {counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
