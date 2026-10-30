"""Reject operator-visible names and text that would reveal a graded exercise."""

from __future__ import annotations

from pathlib import Path

from constants import FORBIDDEN_WORDS


def hits(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in FORBIDDEN_WORDS if word in lowered]


def check_text(label: str, text: str) -> list[str]:
    found = hits(text)
    if not found:
        return []
    return [f"{label} contains {', '.join(found)}"]


def check_tree(root: Path) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        problems.extend(check_text(relative, relative))
        if path.is_file():
            problems.extend(check_text(relative, path.read_text(encoding="utf-8", errors="replace")))
    return problems


def check_env_names(names: list[str]) -> list[str]:
    problems = []
    for name in names:
        problems.extend(check_text(name, name))
    return problems


def assert_clean(*, tree: Path | None = None, texts: dict[str, str] | None = None, env_names: list[str] | None = None) -> None:
    problems = []
    if tree is not None:
        problems.extend(check_tree(tree))
    for label, text in (texts or {}).items():
        problems.extend(check_text(label, text))
    if env_names:
        problems.extend(check_env_names(env_names))
    if problems:
        raise RuntimeError("operator-visible material is not blinded: " + "; ".join(problems))
