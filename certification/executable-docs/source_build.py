"""Exact source-build inputs, separate from installed candidate package identities.

Only opted-in SDK documents and their source checkout blocks use this boundary.
Every admission is derived from the same immutable Git source as the document,
retains lock bytes and package paths, and is rechecked before executing the block.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex

from blocks import extract
from registry_guard import NPM_HONUA

SDK_REPO = "honua-io/honua-sdk-js"
CLONE = "git clone --depth 1 https://github.com/honua-io/honua-sdk-js.git"
DOCUMENTS = {"README.md", "docs/quickstart.md", "mcp/README.md"}


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_name(path: str) -> str:
    return path.rsplit("node_modules/", 1)[-1]


def lock_record(path: str, raw: bytes, read_source) -> dict:
    lock = json.loads(raw)
    if lock.get("lockfileVersion") not in {2, 3} or not isinstance(lock.get("packages"), dict):
        raise ValueError(f"{path}: source build requires a packages-based npm lock")
    base = PurePosixPath(path).parent
    packages = []
    for relative, entry in sorted(lock["packages"].items()):
        if not relative or "node_modules/" not in relative:
            continue
        name = package_name(relative)
        if not NPM_HONUA.match(name):
            continue
        if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise ValueError("source dependency path escapes the checkout")
        row = {"path": str(base / relative), "name": name}
        if entry.get("link"):
            # npm's peer file:.. entry links MCP to this very SDK checkout.
            target = entry.get("resolved")
            if path != "mcp/package-lock.json" or target != ".." or name != "@honua/sdk-js":
                raise ValueError("unreviewed source package link")
            package = read_source("package.json")
            meta = json.loads(package)
            if meta.get("name") != name:
                raise ValueError("linked source package name mismatch")
            row.update(version=meta["version"], link=".", sourcePackageSha256=digest(package))
        else:
            version, integrity, resolved = entry.get("version", ""), entry.get("integrity", ""), entry.get("resolved", "")
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
                raise ValueError("source build dependencies require exact versions")
            if not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", integrity):
                raise ValueError("source build dependencies require exact sha512 integrity")
            expected = f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
            if resolved != expected:
                raise ValueError("source dependency must name its exact public npm tarball")
            row.update(version=version, integrity=integrity, resolved=resolved)
        if entry.get("optional"):
            row["optional"] = True
        packages.append(row)
    return {"path": path, "sha256": digest(raw), "packages": packages}


def derive(document: dict, revision: str, text: str, read_source) -> dict | None:
    if not document.get("sourceBuild"):
        return None
    if document.get("repo") != SDK_REPO or document.get("path") not in DOCUMENTS:
        raise ValueError("source-build admission is not configured for this document")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("source build requires an immutable source revision")
    admitted = []
    follows_clone = False
    for block in extract(text, "markdown"):
        clone = block.code.splitlines()[0] == CLONE if block.code.splitlines() else False
        fixture = follows_clone and block.code.startswith("npm run demo:quickstart:mock ")
        checkout = block.marker and "checkout" in block.marker
        if clone or fixture or checkout:
            if block.language != "shell" or block.intent != "run":
                raise ValueError("source-build blocks must be executable shell")
            admitted.append({"sha256": block.sha256, "index": block.index, "clone": clone})
        follows_clone = clone
    if not admitted:
        raise ValueError("opted-in document has no source build blocks")
    locks = [lock_record(path, read_source(path), read_source)
             for path in ["package-lock.json", "mcp/package-lock.json"]]
    return {"schemaVersion": 1, "repo": document["repo"], "document": document["path"],
            "sourceSha": revision, "documentSha256": digest(text.encode()),
            "blocks": admitted, "locks": locks}


def verification_script(binding: dict, root: Path) -> str:
    quoted = shlex.quote(str(root))
    lines = [f'test "$(git -C {quoted} rev-parse HEAD)" = {shlex.quote(binding["sourceSha"])}',
             f"git -C {quoted} diff --quiet HEAD --"]
    for lock in binding["locks"]:
        lines.append(f'test "$(sha256sum {shlex.quote(str(root / lock["path"]))} | cut -d " " -f 1)" = {lock["sha256"]}')
        for row in lock["packages"]:
            if row.get("link"):
                lines.append(f'test "$(sha256sum {quoted}/package.json | cut -d " " -f 1)" = {row["sourcePackageSha256"]}')
    return "\n".join(lines)


def bind_code(binding: dict, block, code: str, root: Path) -> str:
    row = next((row for row in binding["blocks"] if row["sha256"] == block.sha256 and row["index"] == block.index), None)
    if row is None:
        raise ValueError("block is outside the source-build admission")
    verify = verification_script(binding, root)
    if row["clone"]:
        if not code.startswith(CLONE + "\n"):
            raise ValueError("source clone command changed after admission")
        quoted = shlex.quote(str(root))
        revision = binding["sourceSha"]
        pin = f"git -C {quoted} fetch -q --depth 1 origin {revision}\ngit -C {quoted} checkout -q --detach {revision}\n{verify}"
        return code.replace(CLONE, CLONE + "\n" + pin, 1)
    return verify + "\n" + code


def audit_source(binding: dict, root: Path, installed: list[dict]) -> list[str]:
    expected = {row["path"]: row for lock in binding["locks"] for row in lock["packages"]}
    errors = []
    observed = {row["path"] for row in installed}
    for lock in binding["locks"]:
        hidden = root / Path(lock["path"]).parent / "node_modules/.package-lock.json"
        if hidden.exists():
            for wanted in lock["packages"]:
                if not wanted.get("optional") and str(root / wanted["path"]) not in observed:
                    errors.append(f"{wanted['path']}: source-lock package missing from installed inventory")
    for row in installed:
        path = Path(row["path"])
        if not path.is_relative_to(root):
            continue
        relative = str(path.relative_to(root))
        wanted = expected.get(relative)
        if wanted is None or any(row.get(key) != wanted.get(key) for key in ["name", "version"]):
            errors.append(f"{relative}: installed package differs from immutable source lock")
        elif not wanted.get("link") and row.get("integrity") != wanted["integrity"]:
            errors.append(f"{relative}: installed integrity differs from immutable source lock")
        elif wanted.get("link") and (not path.is_symlink() or path.resolve() != root.resolve()):
            errors.append(f"{relative}: source link target differs from reviewed checkout")
    return errors
