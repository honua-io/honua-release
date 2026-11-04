"""A reader-incompatible refusal is a rollback boundary when every script is documented.

The server's N-1 check requires the same two facts: the script carries a
compatibility-review annotation, and its stem is named in the rollback note.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

BOUNDARY = "reader-incompatible-declaration"
READER_PHRASE = "declared reader-incompatible, not expand"
NOTE_PATH = "docs/guides/deploy/upgrade-and-rollback.md"
MIGRATION_DIR = "src/Honua.Server/Migrations"
_SCRIPT = re.compile(r"((?:\d{3,}_)[^.\s\"']+)\.sql")
_REASON = re.compile(r"((?:\d{3,}_)[^.\s\"']+)\.sql\s*\(([^)]*)\)")
_REVIEW = re.compile(r"^[ \t]*--[ \t]*honua:compatibility-review\b", re.IGNORECASE | re.MULTILINE)


def script_stems(refusal: str) -> tuple[str, ...]:
    seen: list[str] = []
    for match in _SCRIPT.finditer(refusal):
        stem = match.group(1)
        if stem not in seen:
            seen.append(stem)
    return tuple(seen)


def _reasons_by_stem(refusal: str) -> dict[str, str] | None:
    found: dict[str, str] = {}
    for match in _REASON.finditer(refusal):
        stem, reason = match.group(1), match.group(2)
        if stem in found and found[stem] != reason:
            return None
        found[stem] = reason
    return found


def declaration_reasons(refusal: str) -> dict[str, str] | None:
    stems = script_stems(refusal)
    parsed = _reasons_by_stem(refusal)
    if not stems or parsed is None:
        return None
    if any(parsed.get(stem) != READER_PHRASE for stem in stems):
        return None
    return {stem: READER_PHRASE for stem in stems}


def note_names_stem(note: str, stem: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(stem)}(?![A-Za-z0-9_])", note) is not None


def _reviewed(stem: str, note: str, script_text: dict[str, str]) -> bool:
    return note_names_stem(note, stem) and _REVIEW.search(script_text.get(stem, "")) is not None


def boundary_for(refusal: str, note: str, script_text: dict[str, str]) -> str | None:
    reasons = declaration_reasons(refusal)
    if reasons is None:
        return None
    if all(_reviewed(stem, note, script_text) for stem in reasons):
        return BOUNDARY
    return None


def _remote_text(revision: str, path: str) -> str:
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw",
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
