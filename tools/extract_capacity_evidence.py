#!/usr/bin/env python3
"""Safely extract a bounded capacity-evidence ZIP into an empty directory."""
from __future__ import annotations

import argparse
import stat
import re
import zipfile
from pathlib import Path, PurePosixPath


MAX_MEMBERS = 64
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
RECEIPT_NAME = "capacity-soak-receipt.json"


def extract(bundle: Path, output: Path) -> list[Path]:
    """Extract regular root-level files and return their paths; raise on unsafe input."""
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("capacity evidence output directory must be empty")
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBERS:
            raise ValueError("capacity evidence archive member count is invalid")
        if sum(member.file_size for member in members) > MAX_TOTAL_BYTES:
            raise ValueError("capacity evidence archive is oversized")
        names: set[str] = set()
        extracted: list[Path] = []
        for member in members:
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", member.filename)
                or path.name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
                or path.name.endswith(".")
                or member.is_dir()
                or member.flag_bits & 0x1
                or path.is_absolute()
                or len(path.parts) != 1
                or path.name in {"", ".", ".."}
                or path.name.casefold() in names
                or member.file_size > MAX_FILE_BYTES
                or stat.S_ISLNK(mode)
            ):
                raise ValueError(f"unsafe capacity evidence archive member: {member.filename}")
            names.add(path.name.casefold())
            target = output / path.name
            with archive.open(member) as source, target.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
            extracted.append(target)
    if RECEIPT_NAME not in names:
        raise ValueError(f"capacity evidence archive is missing {RECEIPT_NAME}")
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        files = extract(args.bundle, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"capacity-evidence-extract: FAIL â€” {exc}")
        return 1
    print(f"capacity-evidence-extract: PASS â€” {len(files)} bounded regular files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
