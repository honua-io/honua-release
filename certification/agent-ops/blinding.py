"""Reject operator-visible names and text that would reveal a graded exercise."""

from __future__ import annotations

from pathlib import Path

from constants import FORBIDDEN_WORDS


def hits(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in FORBIDDEN_WORDS if word in lowered]


def strip_tokens(text: str, allow: tuple[str, ...] | list[str] | None) -> str:
    cleaned = text
    for token in sorted(allow or (), key=len, reverse=True):
        cleaned = cleaned.replace(token, " ")
    return cleaned


def check_text(label: str, text: str, allow: tuple[str, ...] | list[str] | None = None) -> list[str]:
    found = hits(strip_tokens(text, allow))
    if not found:
        return []
    return [f"{label} contains {', '.join(found)}"]


def check_tree(root: Path, allow: tuple[str, ...] | list[str] | None = None) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        problems.extend(check_text(relative, relative, allow))
        if path.is_file():
            problems.extend(check_text(relative, path.read_text(encoding="utf-8", errors="replace"), allow))
    return problems


def check_env_names(names: list[str]) -> list[str]:
    problems = []
    for name in names:
        problems.extend(check_text(name, name))
    return problems


def assert_clean(
    *,
    tree: Path | None = None,
    texts: dict[str, str] | None = None,
    env_names: list[str] | None = None,
    names: list[str] | None = None,
    allow: tuple[str, ...] | list[str] | None = None,
) -> None:
    problems = []
    if tree is not None:
        problems.extend(check_tree(tree, allow))
    for label, text in (texts or {}).items():
        problems.extend(check_text(label, text, allow))
    if env_names:
        problems.extend(check_env_names(env_names))
    for name in names or []:
        problems.extend(check_text(name, name, allow))
    if problems:
        raise RuntimeError("operator-visible material is not blinded: " + "; ".join(problems))
