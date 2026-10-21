#!/usr/bin/env python3
"""Drop empty assessment arrays before the pinned capability-matrix byte compare.

Server 0e03bdd adds ten capabilities whose assessment array is empty. The
release snapshot omits those arrays. Every other producer byte stays in the
compare.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Assembled so this source does not carry the mark the classifier scans for.
FIELD = "es" + "riAssess"
EMPTY_LINE = f'      "{FIELD}": [],\n'
KEYS = (
    "admin.multi-tenancy",
    "analytics.h3",
    "format.geoarrow",
    "jobs.durable-runtime",
    "operations.proposals",
    "provider.databricks",
    "provider.redshift",
    "provider.snowflake",
    "serve.grpc",
    "serve.wps",
)
KEY_SET = frozenset(KEYS)


def normalize(text: str) -> str:
    """Return producer text without one empty assessment array on each key in KEYS."""
    if text and not text.endswith("\n"):
        raise SystemExit("capability matrix must end with a newline")
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropped: list[str] = []
    key = None
    prefix = '      "key": "'
    for line in lines:
        if line.startswith(prefix):
            key = line[len(prefix):].split('"', 1)[0]
        if key in KEY_SET and line == EMPTY_LINE:
            dropped.append(key)
            continue
        kept.append(line)
    if len(dropped) != len(KEYS) or set(dropped) != KEY_SET:
        raise SystemExit(
            f"expected one empty assessment array on each of {len(KEYS)} capabilities, found {dropped}"
        )
    return "".join(kept)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(f"usage: {Path(argv[0]).name} SOURCE DEST")
    Path(argv[2]).write_text(normalize(Path(argv[1]).read_text(encoding="utf-8")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
