"""Values a block needs from its reader, and the per-document variables files that supply them.

A block "needs" an environment variable when it reads one it does not set itself and no earlier
block of the same session set, and it needs a placeholder when it contains reader-replaceable text
such as `<your-api-key>`. Every needed value must come from the document's variables file
(vars/<doc-id>.json), and every entry there cites where the document tells the reader to supply it.
A block whose need has no entry is `needs-input`: the document gives a reader no way to run it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from blocks import Block

SHELL_BUILTIN_ENV = {
    "HOME", "PATH", "PWD", "OLDPWD", "USER", "SHELL", "TMPDIR", "RANDOM", "HOSTNAME", "LANG", "TERM",
    "UID", "EUID", "PPID", "SECONDS", "LINENO", "IFS", "BASH_SOURCE", "OSTYPE", "CI",
}
SHELL_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(:?[-=?+][^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))")
SHELL_ASSIGN = re.compile(r"(?:^|[\s;&|(])(?:export\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=", re.M)
SHELL_LOOP = re.compile(r"\b(?:for|read(?:\s+-\w+)*)\s+([A-Za-z_][A-Za-z0-9_]*)")
PY_REQUIRED = re.compile(r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]")
PY_OPTIONAL = re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*(,)?")
JS_ENV = re.compile(r"process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\])(\s*(?:\?\?|\|\|))?")
CS_ENV = re.compile(r"Environment\.GetEnvironmentVariable\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)(\s*\?\?)?")
PLACEHOLDER = re.compile(
    r"<(?:your|YOUR)[-_ ][^<>\n]{1,40}>|<[A-Z][A-Z0-9_]{2,}>|\bYOUR_[A-Z0-9_]{3,}\b|"
    r"(?:https?://)?\byour-[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?|\{\{\s*[A-Za-z_][\w.]*\s*\}\}")


def _strip_single_quoted_heredocs(code: str) -> str:
    """Text of a quoted heredoc (<<'EOF') is literal, so `$X` inside it is not a shell reference."""
    out, lines, i = [], code.splitlines(), 0
    while i < len(lines):
        out.append(lines[i])
        m = re.search(r"<<-?\s*(['\"])(\w+)\1", lines[i])
        if m:
            i += 1
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
        i += 1
    return "\n".join(out)


# A block may write NAME=value lines to a dotenv-style file that a later shell block sources
# (`. ./publish.env`). The names it writes are recorded as file-scoped markers in the defined set;
# they define NAME only for a later shell block that actually sources that file.
ENV_FILE_LITERAL = re.compile(r"""["']((?:\./)?[\w./-]*\.env)["']""")
ENV_FILE_NAME = re.compile(r"""(?:["']|\\n)([A-Za-z_][A-Za-z0-9_]*)=""")
SHELL_SOURCE = re.compile(r"""(?:^|[\s;&|(])(?:\.|source)\s+["']?([^\s;&|"')]+)""", re.M)
_FILE_MARK = "\0env-file:"


def _env_file_key(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _written_env_files(block: Block) -> set[str]:
    """File-scoped markers for NAME= lines a non-shell block writes to a quoted *.env path."""
    if block.language == "shell" or not re.search(r"\bopen\(|write_text\(|writeFileSync\(|WriteAllText\(", block.code):
        return set()
    names = set(ENV_FILE_NAME.findall(block.code))
    return {f"{_FILE_MARK}{_env_file_key(path)}\0{name}" for path in ENV_FILE_LITERAL.findall(block.code) for name in names}


def _sourced_names(block: Block, defined: set[str]) -> set[str]:
    if block.language != "shell":
        return set()
    out: set[str] = set()
    for path in SHELL_SOURCE.findall(block.code):
        prefix = f"{_FILE_MARK}{_env_file_key(path)}\0"
        out |= {entry[len(prefix):] for entry in defined if entry.startswith(prefix)}
    return out


def assigned_names(block: Block, defined: set[str] | None = None) -> set[str]:
    if block.language != "shell":
        return _written_env_files(block)
    return (set(SHELL_ASSIGN.findall(block.code)) | set(SHELL_LOOP.findall(block.code))
            | _sourced_names(block, defined or set()))


def needs(block: Block, defined: set[str]) -> dict[str, list[str]]:
    """{"env": [...required names...], "optionalEnv": [...], "placeholders": [...]} for one block."""
    required: set[str] = set()
    optional: set[str] = set()
    code = block.code
    if block.language == "shell":
        own = assigned_names(block, defined)
        for braced, modifier, bare in SHELL_REF.findall(_strip_single_quoted_heredocs(code)):
            name = braced or bare
            if not name or name in SHELL_BUILTIN_ENV or name in own or name in defined or name.isdigit():
                continue
            if modifier and modifier.lstrip(":")[:1] in {"-", "="}:
                optional.add(name)
            elif name.isupper():
                required.add(name)
    elif block.language == "python":
        required |= {n for n in PY_REQUIRED.findall(code) if n not in defined}
        for name, has_default in PY_OPTIONAL.findall(code):
            (optional if has_default else required).add(name)
    elif block.language in {"javascript", "typescript"}:
        for dotted, indexed, fallback in JS_ENV.findall(code):
            (optional if fallback else required).add(dotted or indexed)
    elif block.language == "csharp":
        for name, fallback in CS_ENV.findall(code):
            (optional if fallback else required).add(name)
    placeholders = sorted(set(PLACEHOLDER.findall(code)))
    return {"env": sorted(required - defined), "optionalEnv": sorted(optional - required - defined),
            "placeholders": placeholders}


def doc_id(repo: str, path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{repo.split('/')[-1]}-{path.rsplit('.', 1)[0]}").strip("-").lower()
    return slug


def load_vars(vars_dir: Path, document_id: str) -> dict[str, Any]:
    path = vars_dir / f"{document_id}.json"
    if not path.is_file():
        return {"env": {}, "substitute": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    for section in ("env", "substitute"):
        for key, entry in (data.get(section) or {}).items():
            if not isinstance(entry, dict) or "value" not in entry or not str(entry.get("documentedAt", "")).strip():
                raise ValueError(f"{path}: {section}.{key} needs a value and the documentedAt citation")
    data.setdefault("env", {})
    data.setdefault("substitute", {})
    return data


def render(template: str, context: dict[str, str]) -> str:
    """Expand {candidate.baseUrl}-style references; an unknown reference stays visible."""
    return re.sub(r"\{([a-zA-Z]+\.[A-Za-z0-9_]+)\}", lambda m: context.get(m.group(1), m.group(0)), template)
