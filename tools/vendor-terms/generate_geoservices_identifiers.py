#!/usr/bin/env python3
"""Generate the spec-required vendor-term vocabulary (honua-release#425).

The only identifiers the vendor-term classifier accepts as ``spec`` are the ones this script extracts
from the GeoServices REST Specification Version 1.0 (white paper J-9948, September 2010, the document
submitted to the OGC), each tagged with the numbered section it appears in. Identifiers from later ArcGIS
REST services are not GSR 1.0 vocabulary; they are hand-reviewed in ``arcgis-rest-interop.v1.json`` (R36)
and classed ``spec-interop``. The PDF is pinned by sha256 so a regenerated vocabulary is reproducible; it
is not vendored here.

    curl -sSLo /tmp/gsr.pdf https://www.esri.com/~/media/files/pdfs/library/whitepapers/pdfs/geoservices-rest-spec.pdf
    python3 -m pip install pypdf
    python3 tools/vendor-terms/generate_geoservices_identifiers.py --spec-pdf /tmp/gsr.pdf

``--check`` regenerates in memory and fails when the committed vocabulary differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "geoservices-identifiers.v1.json"

SPEC_URL = "https://www.esri.com/~/media/files/pdfs/library/whitepapers/pdfs/geoservices-rest-spec.pdf"
SPEC_SHA256 = "de002ce4fc95c180c8f3e9adeebd073ebc8b45b94bd1841d133fc1273271ae12"
SCHEMA = "honua.vendor-terms.geoservices-identifiers.v1"

IDENTIFIER = re.compile(r"\besri[A-Za-z0-9_]+")
TOC_ENTRY = re.compile(r"^(?P<number>\d+\.\d+(?:\.\d+)*)\s+(?P<title>.+?)\s*\.{3,}\s*\d+\s*$")
HEADING = re.compile(r"^(?P<number>\d+\.\d+(?:\.\d+)*)\s+[A-Z]")
# The spec's own typo, kept so the vocabulary is a faithful extraction, flagged so nobody copies it.
ERRATA = {"esriGeometryMultipiont": "esriGeometryMultipoint"}
# Type names the spec refers to (placeholders and constant-table names), not wire values.
TYPE_NAME = re.compile(r"(?:Type|TypeConstants|Units)$")


def _section_key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split("."))


def _unwrap(lines: list[str]) -> list[str]:
    """Rejoin identifiers the PDF wrapped mid-token (``esriSpa`` / ``tialRelIntersects``)."""
    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        while (index + 1 < len(lines) and line and not line[-1].isspace()
               and re.search(r"esri[A-Za-z0-9_]*$", line) and re.match(r"[a-z]", lines[index + 1])):
            index += 1
            line += lines[index]
        joined.append(line)
        index += 1
    return joined


def _table_of_contents(lines: list[str]) -> tuple[dict[str, str], int]:
    """Section number -> title, and the index of the last table-of-contents line."""
    sections: dict[str, str] = {}
    last = 0
    pending = ""
    for index, raw in enumerate(lines):
        line = (pending + " " + raw.strip()).strip() if pending else raw.strip()
        match = TOC_ENTRY.match(line)
        if match:
            sections[match["number"]] = re.sub(r"\s+", " ", match["title"]).strip()
            last = index
            pending = ""
        elif re.match(r"^\d+\.\d+(?:\.\d+)*\s", raw.strip()) and "...." not in raw:
            pending = raw.strip()  # entry wrapped onto the next line
        else:
            pending = ""
    return sections, last


def extract(text_pages: list[str]) -> tuple[list[dict], dict[str, str]]:
    lines = _unwrap("\n".join(text_pages).split("\n"))
    sections, toc_end = _table_of_contents(lines)
    current = ""
    found: dict[str, list[str]] = {}
    for line in lines[toc_end + 1:]:
        heading = HEADING.match(line.strip())
        if heading and heading["number"] in sections and (
                not current or _section_key(heading["number"]) > _section_key(current)):
            current = heading["number"]
        if not current:
            continue  # front matter (the trademark notice) is not spec vocabulary
        for identifier in IDENTIFIER.findall(line):
            seen = found.setdefault(identifier, [])
            if current not in seen:
                seen.append(current)
    entries = []
    for identifier in sorted(found):
        numbers = found[identifier]
        entry = {
            "identifier": identifier,
            "kind": "type-name" if TYPE_NAME.search(identifier) else "enum-value",
            "source": "gsr-1.0",
            "section": numbers[0],
            "sectionTitle": sections[numbers[0]],
            "sections": numbers,
        }
        if identifier in ERRATA:
            entry["erratumFor"] = ERRATA[identifier]
        entries.append(entry)
    return entries, sections


def build(text_pages: list[str], pdf_sha256: str) -> dict:
    spec, _ = extract(text_pages)
    return {
        "schema": SCHEMA,
        "generatedBy": "tools/vendor-terms/generate_geoservices_identifiers.py",
        "sources": {
            "gsr-1.0": {
                "title": "GeoServices REST Specification Version 1.0",
                "edition": "Esri white paper J-9948, September 2010 (submitted to the OGC)",
                "url": SPEC_URL,
                "sha256": pdf_sha256,
            },
        },
        "notes": [
            "The 1.0 specification defines no JSON key and no URL path segment that contains 'esri' or "
            "'arcgis'; jsonKeys and pathSegments are therefore empty. The '/arcgis/rest/services' URL "
            "root is a server-instance naming convention, not a specification requirement.",
        ],
        "jsonKeys": [],
        "pathSegments": [],
        "identifiers": spec,
    }


def _read_pdf(path: Path) -> tuple[list[str], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SPEC_SHA256:
        raise SystemExit(f"{path}: sha256 {digest} is not the pinned specification {SPEC_SHA256}")
    from pypdf import PdfReader  # generator-only dependency; the classifier never imports it

    return [page.extract_text() or "" for page in PdfReader(str(path)).pages], digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec-pdf", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="fail when the committed file differs")
    args = parser.parse_args(argv)
    pages, digest = _read_pdf(args.spec_pdf)
    rendered = json.dumps(build(pages, digest), indent=2) + "\n"
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"{OUTPUT} is stale; regenerate it", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
