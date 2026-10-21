"""Install the runner's pinned TypeScript parser once, outside reader contexts."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "5.9.3"
RESOLVED = "https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz"
INTEGRITY = "sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw=="
TIMEOUT_SECONDS = 240


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bootstrap(tools: Path, node: Path, npm_cli: Path) -> dict:
    target = tools / "compiler"
    target.mkdir(parents=True, exist_ok=True)
    package = HERE / "compiler" / "package.json"
    lock = HERE / "compiler" / "package-lock.json"
    data = json.loads(lock.read_text())
    dependency = data["packages"]["node_modules/typescript"]
    if (data["packages"][""]["dependencies"] != {"typescript": VERSION}
            or dependency["version"] != VERSION or dependency["resolved"] != RESOLVED
            or dependency["integrity"] != INTEGRITY):
        raise RuntimeError("executable-docs TypeScript lock does not match its pinned package and SRI")
    shutil.copy2(package, target / "package.json")
    shutil.copy2(lock, target / "package-lock.json")
    (target / "npm-userconfig").write_text("")
    (target / "npm-globalconfig").write_text("")
    receipt_path = tools / "compiler-bootstrap.json"
    started = time.monotonic()
    receipt = {"schemaVersion": "executable-docs-compiler-bootstrap.v1", "qualification": False,
               "package": "typescript", "version": VERSION, "resolved": RESOLVED,
               "integrity": INTEGRITY, "lockSha256": _sha(lock), "status": "fail"}
    try:
        env = os.environ.copy()
        env.pop("NODE_OPTIONS", None)
        for key in list(env):
            if key.lower().startswith("npm_config_"):
                env.pop(key)
        env["npm_config_cache"] = str(tools / "npm-cache")
        result = subprocess.run([str(node), str(npm_cli), "ci", "--ignore-scripts", "--no-audit",
            "--no-fund", "--registry=https://registry.npmjs.org",
            f"--userconfig={target / 'npm-userconfig'}", f"--globalconfig={target / 'npm-globalconfig'}"], cwd=target, env=env, text=True, capture_output=True,
            timeout=TIMEOUT_SECONDS, check=False)
        if result.returncode:
            raise RuntimeError(f"pinned compiler npm ci failed ({result.returncode}): {result.stderr[-600:]}")
        source = target / "node_modules" / "typescript" / "lib" / "typescript.js"
        metadata = target / "node_modules" / "typescript" / "package.json"
        if json.loads(metadata.read_text())["version"] != VERSION:
            raise RuntimeError("installed TypeScript version differs from the lock")
        version = subprocess.run([str(node), "--version"], text=True, capture_output=True,
                                 timeout=10, check=True).stdout.strip()
        receipt.update(status="pass", nodeVersion=version, librarySha256=_sha(source),
                       libraryPath=str(source), durationSeconds=round(time.monotonic() - started, 3))
    except Exception as exc:
        receipt.update(detail=str(exc), durationSeconds=round(time.monotonic() - started, 3))
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
