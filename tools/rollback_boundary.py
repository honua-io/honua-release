"""A reader-incompatible refusal is a rollback boundary when every script is documented.

The server's N-1 check requires the same two facts: the script carries a
compatibility-review annotation, and its stem is named in the rollback note.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BOUNDARY = "reader-incompatible-declaration"
READER_PHRASE = "declared reader-incompatible, not expand"
NOTE_PATH = "docs/guides/deploy/upgrade-and-rollback.md"
MIGRATION_DIR = "src/Honua.Server/Migrations"
_SCRIPT = re.compile(r"((?:\d{3,}_)[^.\s\"']+)\.sql")
_REVIEW = re.compile(r"^[ \t]*--[ \t]*honua:compatibility-review\b", re.IGNORECASE | re.MULTILINE)


def script_stems(refusal: str) -> tuple[str, ...]:
    seen: list[str] = []
    for match in _SCRIPT.finditer(refusal):
        stem = match.group(1)
        if stem not in seen:
            seen.append(stem)
    return tuple(seen)


def boundary_for(refusal: str, note: str, script_text: dict[str, str]) -> str | None:
    stems = script_stems(refusal)
    if not stems or READER_PHRASE not in refusal:
        return None
    for stem in stems:
        if stem not in note or _REVIEW.search(script_text.get(stem, "")) is None:
            return None
    return BOUNDARY


def _remote_text(revision: str, path: str) -> str:
    result = subprocess.run(
        ["gh", "api", "--fail", "-H", "Accept: application/vnd.github.raw",
         f"repos/honua-io/honua-server/contents/{path}?ref={revision}"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def load_remote(revision: str, stems: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    note = _remote_text(revision, NOTE_PATH)
    scripts = {stem: _remote_text(revision, f"{MIGRATION_DIR}/{stem}.sql") for stem in stems}
    return note, scripts


def _local_scripts(directory: Path, stems: tuple[str, ...]) -> dict[str, str]:
    loaded = {}
    for stem in stems:
        path = directory / f"{stem}.sql"
        loaded[stem] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return loaded


def decide(refusal: str, note: str, script_text: dict[str, str]) -> str:
    return boundary_for(refusal, note, script_text) or "no"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade a schema-reader refusal.")
    parser.add_argument("--refusal-file", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--revision")
    source.add_argument("--note-file", type=Path)
    parser.add_argument("--scripts-dir", type=Path)
    args = parser.parse_args(argv)
    if args.note_file is not None and args.scripts_dir is None:
        parser.error("--note-file requires --scripts-dir")
    refusal = args.refusal_file.read_text(encoding="utf-8")
    if args.revision is not None:
        note, scripts = load_remote(args.revision, script_stems(refusal))
    else:
        note = args.note_file.read_text(encoding="utf-8")
        scripts = _local_scripts(args.scripts_dir, script_stems(refusal))
    print(decide(refusal, note, scripts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
