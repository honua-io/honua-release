#!/usr/bin/env python3
"""Fail when a workflow or composite action executes mutable or misdocumented third-party code."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
from urllib.parse import quote


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DOCKER_DIGEST = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
VERSION_COMMENT = re.compile(r"^v[0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)")
DOCKER_VERSION_COMMENT = re.compile(r"^(?:v)?[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)")
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<value>.+?)\s*$")
SAME_ORG_REUSABLE_WORKFLOW = re.compile(
    r"^(?P<repository>honua-io/[^/]+)/\.github/workflows/[^@]+$", re.IGNORECASE
)


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


def _version_label(comment: str) -> str:
    """The leading version token of a pin comment (``v4.3.1 - why`` -> ``v4.3.1``)."""

    return comment.split()[0] if comment else ""


def _consistency_violations(pins: list[tuple[Path, int, str, str, str]]) -> list[Violation]:
    """Flag pins whose version comment disagrees with the rest of the repository.

    The per-line rules above only prove a comment is *shaped* like a version, so a pin bumped to a
    new SHA while its ``# v4.3.1`` comment was left behind still passed — and that comment is the
    only thing a human reviewer reads when approving a supply-chain pin. Two offline invariants
    close that hole without the network call this gate deliberately avoids:
      - one SHA of a given action must carry one version label everywhere it appears, and
      - one version label of a given action must resolve to one SHA everywhere it appears
    (a tag names exactly one commit, so any disagreement means a stale or fabricated comment).
    """

    by_ref: dict[tuple[str, str], Counter[str]] = {}
    by_version: dict[tuple[str, str], Counter[str]] = {}
    for _, _, action, ref, version in pins:
        by_ref.setdefault((action, ref), Counter())[version] += 1
        by_version.setdefault((action, version), Counter())[ref] += 1

    violations: list[Violation] = []
    for path, line, action, ref, version in pins:
        labels = by_ref[(action, ref)]
        refs = by_version[(action, version)]
        # Report only the lines that disagree with the repository's prevailing answer, so one stale
        # comment names one line instead of drowning the log in its own conforming siblings.
        if len(labels) > 1 and not _prevails(labels, version):
            agreed = ", ".join(f"# {label}" for label in _prevailing(labels))
            violations.append(
                Violation(
                    path,
                    line,
                    f"{action}@{ref} is documented as '# {version}' here but as '{agreed}' "
                    f"elsewhere — one pinned SHA must carry one version comment",
                )
            )
        elif len(refs) > 1 and not _prevails(refs, ref):
            agreed = ", ".join(_prevailing(refs))
            violations.append(
                Violation(
                    path,
                    line,
                    f"{action} '# {version}' is pinned to {ref} here but to {agreed} elsewhere "
                    f"— one version comment must resolve to one SHA",
                )
            )
    return violations


def _prevailing(counts: Counter[str]) -> list[str]:
    """Every value tied for most-used (a tie means no answer prevails, so all sides are reported)."""

    top = max(counts.values())
    return sorted(value for value, count in counts.items() if count == top)


def _prevails(counts: Counter[str], value: str) -> bool:
    prevailing = _prevailing(counts)
    return len(prevailing) == 1 and prevailing[0] == value


def _gh_api(endpoint: str) -> object:
    """Return one GitHub API response through the authenticated ``gh`` CLI."""

    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"gh api {endpoint}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"gh api {endpoint}: response was not valid JSON") from error


def _reachability_violations(
    pins: list[tuple[Path, int, str, str, str]], gh_api: Callable[[str], object]
) -> list[Violation]:
    """Verify same-organization reusable workflows are ancestors of their default branch."""

    repositories: dict[str, str] = {}
    comparisons: dict[tuple[str, str], str] = {}
    violations: list[Violation] = []
    for path, line, action, ref, _ in pins:
        match = SAME_ORG_REUSABLE_WORKFLOW.fullmatch(action)
        if not match:
            continue
        repository = match.group("repository")
        try:
            if repository not in repositories:
                metadata = gh_api(f"repos/{repository}")
                default_branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
                if not isinstance(default_branch, str) or not default_branch:
                    raise RuntimeError(f"repos/{repository}: response omitted default_branch")
                repositories[repository] = default_branch

            key = (repository, ref)
            if key not in comparisons:
                default_branch = repositories[repository]
                comparison = gh_api(
                    f"repos/{repository}/compare/{quote(ref, safe='')}..."
                    f"{quote(default_branch, safe='')}"
                )
                status = comparison.get("status") if isinstance(comparison, dict) else None
                if not isinstance(status, str):
                    raise RuntimeError(
                        f"repos/{repository}: compare response omitted status"
                    )
                comparisons[key] = status
        except RuntimeError as error:
            violations.append(
                Violation(path, line, f"could not verify default-branch reachability: {error}")
            )
            continue

        if comparisons[(repository, ref)] not in {"ahead", "identical"}:
            default_branch = repositories[repository]
            violations.append(
                Violation(
                    path,
                    line,
                    f"{action}@{ref} is not reachable from {repository}'s default branch "
                    f"'{default_branch}'. Merge the reusable-workflow change to the default "
                    "branch, then re-pin uses: to the resulting default-branch-reachable SHA.",
                )
            )
    return violations


def validate_tree(
    root: Path,
    *,
    check_reachability: bool = False,
    gh_api: Callable[[str], object] = _gh_api,
) -> list[Violation]:
    """Return every mutable, undocumented or misdocumented external action reference below ``root``."""

    violations: list[Violation] = []
    pins: list[tuple[Path, int, str, str, str]] = []
    for path in _workflow_files(root):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_LINE.match(line)
            if not match:
                continue
            value, comment = _split_value(match.group("value"))
            problems = _problems(value, comment)
            if problems:
                violations.append(Violation(path, line_number, "; ".join(problems)))
                continue
            if value.startswith("./") or "@" not in value:
                continue
            action, ref = value.rsplit("@", 1)
            pins.append((path, line_number, action, ref, _version_label(comment)))
    violations.extend(_consistency_violations(pins))
    if check_reachability:
        violations.extend(_reachability_violations(pins, gh_api))
    return sorted(violations, key=lambda item: (str(item.path), item.line))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Network access belongs in CI only. Local/offline validation retains every static pin check.
    violations = validate_tree(root, check_reachability=os.environ.get("GITHUB_ACTIONS") == "true")
    if violations:
        for violation in violations:
            relative = violation.path.relative_to(root)
            print(f"{relative}:{violation.line}: {violation.message}", file=sys.stderr)
        print(
            "Pin every external uses: declaration to a reviewed immutable SHA/digest and retain its version comment.",
            file=sys.stderr,
        )
        return 1

    print("All external GitHub Actions references are immutable and consistently version-documented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
