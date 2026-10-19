"""Explicit, source-bound execution contexts for mixed-language documents.

No command inspection selects a runtime. Every executable block in an opted-in
document must have exactly one reviewed index/hash binding.
"""
from __future__ import annotations

import re

from blocks import Block

RUNTIMES = {"node", "python", "dotnet"}
EXECUTABLE = {"run", "file", "compile", "teardown"}
LANGUAGE_RUNTIME = {"python": "python", "javascript": "node", "typescript": "node", "csharp": "dotnet"}


class ContextError(ValueError):
    pass


def compile_plan(document: dict, blocks: list[Block]) -> dict[int, dict[str, str]]:
    if "executionContexts" not in document:
        return {}
    declarations = document["executionContexts"]
    if (not isinstance(declarations, list) or not declarations
            or any(document.get(key) for key in ("session", "checkout", "sourceBuild", "docker"))):
        raise ContextError("executionContexts requires a standalone document without checkout/source-build/Docker orchestration")
    plan, names = {}, set()
    for declaration in declarations:
        if (not isinstance(declaration, dict)
                or set(declaration) != {"name", "runtime", "documentedAt", "blocks"}):
            raise ContextError("each execution context needs exactly name/runtime/documentedAt/blocks")
        name, runtime, citation, bindings = (declaration[key] for key in ("name", "runtime", "documentedAt", "blocks"))
        if (not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name) is None
                or name in names or not isinstance(runtime, str) or runtime not in RUNTIMES
                or not isinstance(citation, str) or not citation.strip()
                or not isinstance(bindings, list) or not bindings):
            raise ContextError("execution context has an invalid/duplicate name, runtime, citation or block list")
        names.add(name)
        indices = []
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"index", "sha256"}:
                raise ContextError("context blocks require exactly index/sha256")
            index, digest = binding["index"], binding["sha256"]
            if (type(index) is not int or not 0 <= index < len(blocks) or index in plan
                    or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                raise ContextError("context block index/hash is invalid or duplicated")
            block = blocks[index]
            required = LANGUAGE_RUNTIME.get(block.language)
            if (block.index != index or block.sha256 != digest or block.intent not in EXECUTABLE
                    or required is not None and required != runtime
                    or required is None and block.language not in {"shell", "http"} and block.intent != "file"):
                raise ContextError("execution context does not match the actual executable block/language")
            plan[index] = {"name": name, "runtime": runtime}
            indices.append(index)
        if indices != sorted(indices):
            raise ContextError("execution context blocks must preserve document order")
    if set(plan) != {block.index for block in blocks if block.intent in EXECUTABLE}:
        raise ContextError("every executable block needs exactly one explicit context")
    return plan
