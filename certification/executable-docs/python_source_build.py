"""Plan exact Python source blocks; this module grants no installed-origin exception.

The SDK producer must prove each ordered pip invocation independently. A profile
with extra requirements cannot substitute for the command the document executes.
Actual source admission and installed-byte verification remain separate consumers
of a protected producer receipt; deriving this request is never admission.
"""
from __future__ import annotations

import hashlib
import re
import shlex

from blocks import extract

REPOSITORY = "honua-io/honua-sdk-python"
DOCUMENTS = {"README.md", "INSTALL.md"}
CLONE = "git clone https://github.com/honua-io/honua-sdk-python.git"
SOURCE_PACKAGE = re.compile(r"(?:\./)?packages/(honua-sdk|honua-admin)(?:\[([a-z,]+)\])?")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pip_invocation(command: str) -> dict | None:
    words = shlex.split(command, comments=True)
    if words[:2] != ["pip", "install"]:
        return None
    requested, editable = [], []
    pending_editable = False
    for word in words[2:]:
        if word == "-e":
            if pending_editable:
                raise ValueError("duplicate editable option in documented install")
            pending_editable = True
            continue
        if word.startswith("-") or any(token in word for token in ";&|$`<>"):
            raise ValueError("unreviewed option or shell expansion in documented install")
        match = SOURCE_PACKAGE.fullmatch(word)
        if ("packages/honua-" in word or word.startswith(("./", "../", "/", "packages/"))) and match is None:
            raise ValueError("source package path escapes the reviewed checkout")
        if pending_editable and match is None:
            raise ValueError("editable target is not a reviewed source package")
        if match is None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", word):
            raise ValueError("unreviewed package request in documented install")
        requested.append(word)
        editable.append(pending_editable)
        pending_editable = False
    if not requested or pending_editable:
        raise ValueError("incomplete documented pip invocation")
    return {"command": command, "commandSha256": digest(command.encode()),
            "requested": requested, "editable": editable}


def derive(document: dict, revision: str, text: str) -> dict | None:
    declaration = document.get("pythonSourceBuild")
    if declaration is None:
        return None
    if document.get("repo") != REPOSITORY or document.get("path") not in DOCUMENTS:
        raise ValueError("Python source planning is not configured for this document")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Python source planning requires an immutable source revision")
    blocks = [block for block in extract(text, "markdown")
              if CLONE in block.code or "packages/honua-" in block.code]
    expected = [{"index": block.index, "sha256": block.sha256} for block in blocks]
    if not blocks or declaration != {"blocks": expected}:
        raise ValueError("Python source declaration must bind every exact source block in document order")
    planned = []
    for block in blocks:
        if block.language != "shell" or block.intent != "run":
            raise ValueError("Python source blocks must remain executable shell")
        invocations = []
        for line in block.code.splitlines():
            invocation = pip_invocation(line)
            if invocation is not None:
                invocations.append({"ordinal": len(invocations), **invocation})
        if not invocations:
            raise ValueError("Python source block has no bound install invocation")
        planned.append({"index": block.index, "sha256": block.sha256,
                        "invocations": invocations})
    return {"schema": "honua.python-document-source-request/v1", "repository": REPOSITORY,
            "sourceSha": revision, "document": document["path"],
            "documentSha256": digest(text.encode()), "blocks": planned,
            "sourceAdmitted": False, "publishedWheelIdentity": False,
            "candidateQualified": False}


def require_exact_invocation_coverage(request: dict, receipts: list[dict]) -> None:
    """Refuse absent/reordered/superset recipes before any admission can be considered.

    This checks only exact command coverage. It does not authenticate the producer,
    validate its artifacts, execute commands or accept an installed package origin.
    """
    expected = [{"sourceSha": request["sourceSha"], "blockIndex": block["index"],
                 "blockSha256": block["sha256"], **invocation}
                for block in request["blocks"] for invocation in block["invocations"]]
    if receipts != expected:
        raise ValueError("Python source proof does not cover the exact ordered install invocations")
