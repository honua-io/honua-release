#!/usr/bin/env python3
"""Fail when a workflow or composite action executes mutable third-party code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DOCKER_DIGEST = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
VERSION_COMMENT = re.compile(r"^v[0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)")
DOCKER_VERSION_COMMENT = re.compile(r"^(?:v)?[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)")
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<value>.+?)\s*$")


@dataclass(frozen=True)
class Violation:
    """One unsafe ``uses:`` declaration."""

    path: Path
    line: int
    message: str


def _workflow_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for base in (root / ".github" / "workflows", root / ".github" / "actions"):
        if not base.is_dir():
            continue
        for pattern in ("*.yml", "*.yaml"):
            files.update(base.rglob(pattern))
    return sorted(files)


def _split_value(raw: str) -> tuple[str, str]:
    value, separator, comment = raw.partition("#")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value, comment.strip() if separator else ""


def _problems(value: str, comment: str) -> list[str]:
    if value.startswith("./"):
        return []

    if value.startswith("docker://"):
        problems = []
        if not DOCKER_DIGEST.fullmatch(value):
            problems.append("Docker actions must use an immutable sha256 digest")
        if not DOCKER_VERSION_COMMENT.match(comment):
            problems.append("Docker action pins require a human-readable version comment")
        return problems

    problems = []
    if "@" not in value:
        problems.append("third-party actions must include a full 40-character commit SHA")
    else:
        action, ref = value.rsplit("@", 1)
        if not action or not FULL_SHA.fullmatch(ref):
            problems.append("third-party actions must use a full 40-character commit SHA")

    if not VERSION_COMMENT.match(comment):
        problems.append("third-party action pins require a human-readable version comment such as '# v4.3.1'")
    return problems


def validate_tree(root: Path) -> list[Violation]:
    """Return every mutable or undocumented external action reference below ``root``."""

    violations: list[Violation] = []
    for path in _workflow_files(root):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_LINE.match(line)
            if not match:
                continue
            value, comment = _split_value(match.group("value"))
            problems = _problems(value, comment)
            if problems:
                violations.append(Violation(path, line_number, "; ".join(problems)))
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = validate_tree(root)
    if violations:
        for violation in violations:
            relative = violation.path.relative_to(root)
            print(f"{relative}:{violation.line}: {violation.message}", file=sys.stderr)
        print(
            "Pin every external uses: declaration to a reviewed immutable SHA/digest and retain its version comment.",
            file=sys.stderr,
        )
        return 1

    print("All external GitHub Actions references are immutable and version-documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
