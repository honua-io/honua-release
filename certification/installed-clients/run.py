#!/usr/bin/env python3
"""Certify clean installs of the exact customer client bytes pinned by the release."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = Path(__file__).with_name("matrix.json")
FLOATING = re.compile(r"(?:^|[-.])(latest|next|local|snapshot)(?:$|[-.])|[*^~<>]", re.I)


class CertificationError(RuntimeError):
    pass


def load_inputs(manifest_path: Path, matrix_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = yaml.safe_load(manifest_path.read_text())
    matrix = json.loads(matrix_path.read_text())
    return manifest, matrix


def validate_release_inputs(manifest: dict[str, Any], matrix: dict[str, Any]) -> None:
    server = manifest.get("components", {}).get("honua-server", {})
    image, digest = str(server.get("image", "")), str(server.get("digest", ""))
    if not image or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise CertificationError("release mode requires a server image and immutable sha256 digest")
    if os.environ.get("HONUA_SERVER_IMAGE"):
        raise CertificationError("release mode rejects HONUA_SERVER_IMAGE overrides")
    artifacts = manifest.get("clientArtifacts", {})
    if not artifacts:
        raise CertificationError("release mode requires clientArtifacts pin truth")
    seen: set[str] = set()
    for cell in matrix.get("cells", []):
        cell_id = cell.get("id", "")
        if not cell_id or cell_id in seen:
            raise CertificationError(f"matrix has missing/duplicate cell id: {cell_id!r}")
        seen.add(cell_id)
        artifact = artifacts.get(cell.get("artifact"))
        if not artifact:
            raise CertificationError(f"{cell_id}: artifact pin is missing")
        version = str(artifact.get("version", ""))
        if not version or FLOATING.search(version) or artifact.get("source") == "local":
            raise CertificationError(f"{cell_id}: release mode rejects local/floating version {version!r}")
        if artifact.get("publicationState") not in {"published", "promoted"}:
            raise CertificationError(f"{cell_id}: artifact is not published/promoted")
        if not artifact.get("integrity") and not artifact.get("digest"):
            raise CertificationError(f"{cell_id}: artifact lacks immutable byte integrity")
    if not seen:
        raise CertificationError("matrix has no cells")


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def install_npm(pin: dict[str, Any], work: Path) -> tuple[bool, str]:
    if not shutil.which("npm"):
        return False, "npm is unavailable"
    work.mkdir()
    (work / "package.json").write_text('{"private":true,"type":"module"}\n')
    spec = f"{pin['package']}@{pin['version']}"
    proc = _run(["npm", "install", "--ignore-scripts", "--package-lock-only=false", "--save-exact", spec], cwd=work)
    if proc.returncode:
        return False, proc.stderr[-2000:]
    lock = json.loads((work / "package-lock.json").read_text())
    entry = lock.get("packages", {}).get(f"node_modules/{pin['package']}", {})
    if entry.get("version") != pin["version"] or entry.get("integrity") != pin["integrity"]:
        return False, "npm lock does not match the exact version/integrity pin"
    if os.environ.get("HONUA_SERVER_URL"):
        probe = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/probe.mjs"
        local_probe = work / "probe.mjs"
        shutil.copy2(probe, local_probe)
        proc = _run(["node", str(local_probe)], cwd=work, env=os.environ.copy())
        if proc.returncode:
            return False, (proc.stdout + proc.stderr)[-2000:]
    return True, "exact npm package installed and lock integrity matched"


def install_pypi(pin: dict[str, Any], work: Path) -> tuple[bool, str]:
    work.mkdir()
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{pin['package']}/{pin['version']}/json", timeout=30
        ) as response:
            metadata = json.load(response)
        candidates = [item for item in metadata["urls"] if item["filename"] == pin.get("filename")]
        if len(candidates) != 1:
            return False, "PyPI release metadata did not contain exactly the pinned wheel"
        wheel = work / pin["filename"]
        with urllib.request.urlopen(candidates[0]["url"], timeout=60) as response:
            wheel.write_bytes(response.read())
    except Exception as exc:
        return False, f"PyPI download failed: {exc}"
    actual = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != pin["digest"]:
        return False, f"wheel digest mismatch: {actual}"
    target = work / "site-packages"
    target.mkdir()
    try:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(target)
    except zipfile.BadZipFile:
        return False, "pinned PyPI bytes are not a valid wheel"
    if not (target / "honua_sdk" / "__init__.py").is_file():
        return False, "installed wheel does not expose honua_sdk"
    if os.environ.get("HONUA_SERVER_URL"):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(target)
        probe = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/probe.py"
        proc = _run([sys.executable, str(probe)], cwd=work, env=env)
        if proc.returncode:
            return False, (proc.stdout + proc.stderr)[-2000:]
    return True, "exact PyPI wheel installed in an isolated target and sha256 matched"


def make_receipt(manifest: dict[str, Any], matrix: dict[str, Any], results: list[dict[str, Any]], evidence_uri: str) -> dict[str, Any]:
    server = manifest["components"]["honua-server"]
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "release": manifest["platformRelease"],
        "server": {"sourceSha": server["sha"], "image": f"{server['image'].split('@')[0]}@{server['digest']}"},
        "fixtureRevision": matrix["fixtureRevision"],
        "configRevision": matrix["configRevision"],
        "authPolicyRevision": matrix["authPolicyRevision"],
        "evidenceUri": evidence_uri,
        "status": "pass" if results and all(r["status"] == "pass" for r in results) else "fail",
        "results": results,
    }


def execute(manifest: dict[str, Any], matrix: dict[str, Any], evidence_uri: str) -> dict[str, Any]:
    pins = manifest["clientArtifacts"]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="honua-installed-client-") as tmp:
        base = Path(tmp)
        for cell in matrix["cells"]:
            pin = pins[cell["artifact"]]
            status, detail = "fail", "matrix cell was not executed"
            if cell["status"] == "blocked":
                detail = f"blocked by {cell['blockedBy']}"
            elif cell["driver"] == "npm":
                ok, detail = install_npm(pin, base / cell["id"])
                status = "pass" if ok else "fail"
            elif cell["driver"] == "pypi":
                work = base / cell["id"]
                ok, detail = install_pypi(pin, work)
                status = "pass" if ok else "fail"
            results.append({
                "cell": cell["id"], "operationId": cell["scenario"], "target": cell["driver"],
                "package": pin["package"], "version": pin["version"],
                "integrity": pin.get("integrity") or pin.get("digest"), "sourceSha": pin["sourceSha"],
                "status": status, "detail": detail,
            })
    return make_receipt(manifest, matrix, results, evidence_uri)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/installed-client-certification.json")
    parser.add_argument("--evidence-uri", required=True, help="durable CI artifact/run URI")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--live", action="store_true", help="boot and seed the single real server/PostgreSQL target")
    args = parser.parse_args()
    try:
        manifest, matrix = load_inputs(args.manifest, args.matrix)
        validate_release_inputs(manifest, matrix)
        if args.validate_only:
            return 0
        if args.live:
            os.environ["HONUA_SERVER_URL"] = os.environ.get("HONUA_SERVER_URL", "http://localhost:8080")
            boot = subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "up"], cwd=ROOT)
            if boot.returncode:
                raise CertificationError("the immutable server candidate did not become ready")
            try:
                seed = subprocess.run(["bash", str(ROOT / "e2e/harness/seed/seed.sh")], cwd=ROOT)
                if seed.returncode:
                    raise CertificationError("the immutable fixture could not be seeded")
                receipt = execute(manifest, matrix, args.evidence_uri)
            finally:
                subprocess.run(["bash", str(ROOT / "e2e/harness/boot.sh"), "down"], cwd=ROOT)
        else:
            receipt = execute(manifest, matrix, args.evidence_uri)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["status"] == "pass" else 1
    except CertificationError as exc:
        print(f"certification input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
