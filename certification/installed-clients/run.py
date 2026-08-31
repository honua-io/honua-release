#!/usr/bin/env python3
"""Certify clean installs of the exact customer client bytes pinned by the release."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
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


def server_image_ref(manifest: dict[str, Any]) -> str:
    server = manifest["components"]["honua-server"]
    return f"{server['image'].split('@')[0]}@{server['digest']}"


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _npm_archive_matches(pin: dict[str, Any], work: Path) -> tuple[bool, str]:
    packed = _run(
        ["npm", "pack", f"{pin['package']}@{pin['version']}", "--json", "--ignore-scripts"],
        cwd=work,
    )
    if packed.returncode:
        return False, packed.stderr[-2000:]
    try:
        filename = json.loads(packed.stdout)[0]["filename"]
        archive = work / filename
        actual = "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    except (IndexError, KeyError, json.JSONDecodeError, OSError) as exc:
        return False, f"could not inspect npm archive bytes: {exc}"
    if actual != pin["integrity"]:
        return False, f"npm archive integrity mismatch: {actual}"
    return True, str(archive)


def install_npm(
    pin: dict[str, Any],
    work: Path,
    *,
    verify_bins: bool = False,
    companion_pin: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if not shutil.which("npm"):
        return False, "npm is unavailable"
    work.mkdir()
    (work / "package.json").write_text('{"private":true,"type":"module"}\n')
    matched, archive_or_detail = _npm_archive_matches(pin, work)
    if not matched:
        return False, archive_or_detail
    archives = [archive_or_detail]
    if companion_pin:
        companion_matched, companion_archive = _npm_archive_matches(companion_pin, work)
        if not companion_matched:
            return False, companion_archive
        archives.append(companion_archive)
    proc = _run(
        ["npm", "install", "--ignore-scripts", "--legacy-peer-deps", "--save-exact", *archives],
        cwd=work,
    )
    if proc.returncode:
        return False, proc.stderr[-2000:]
    lock = json.loads((work / "package-lock.json").read_text())
    entry = lock.get("packages", {}).get(f"node_modules/{pin['package']}", {})
    if entry.get("version") != pin["version"] or entry.get("integrity") != pin["integrity"]:
        return False, "npm lock does not match the exact version/integrity pin"
    if verify_bins:
        package_root = work / "node_modules" / Path(*pin["package"].split("/"))
        package_json = json.loads((package_root / "package.json").read_text())
        bins = package_json.get("bin") or {}
        if isinstance(bins, str):
            bins = {pin["package"].split("/")[-1]: bins}
        if not bins or any(not (package_root / target).is_file() for target in bins.values()):
            return False, "installed MCP package has no complete executable surface"
        for command in bins:
            probe = _run([str(work / "node_modules" / ".bin" / command), "--help"], cwd=work)
            if probe.returncode:
                return False, f"installed executable {command} --help failed: {(probe.stdout + probe.stderr)[-1000:]}"
        if os.environ.get("HONUA_SERVER_URL"):
            proxy = work / "node_modules" / ".bin" / "honua-mcp-proxy"
            if not proxy.exists():
                return False, "installed MCP package does not expose honua-mcp-proxy"
            probes_path = ROOT / "certification" / "terminal-journey" / "probes.py"
            spec = importlib.util.spec_from_file_location("terminal_journey_probes", probes_path)
            if spec is None or spec.loader is None:
                return False, "could not load the shared MCP probe"
            probes = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = probes
            spec.loader.exec_module(probes)
            names, error, note = probes.enumerate_tools(proxy, os.environ["HONUA_SERVER_URL"])
            if error or not names:
                detail = error or "tools/list returned an empty catalog"
                if note:
                    detail += f"; {note}"
                return False, f"installed MCP proxy tools/list failed: {detail}"
    if os.environ.get("HONUA_SERVER_URL"):
        probe = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/probe.mjs"
        local_probe = work / "probe.mjs"
        shutil.copy2(probe, local_probe)
        proc = _run(["node", str(local_probe)], cwd=work, env=os.environ.copy())
        if proc.returncode:
            return False, (proc.stdout + proc.stderr)[-2000:]
    suffix = ", package executables verified" if verify_bins else ""
    if verify_bins and os.environ.get("HONUA_SERVER_URL"):
        suffix += ", live tools/list returned a non-empty catalog"
    return True, f"exact npm archive sha512 and installed lock integrity matched{suffix}"


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
    pip = _run([sys.executable, "-m", "pip", "--version"], cwd=work)
    if pip.returncode == 0:
        installed = _run([sys.executable, "-m", "pip", "install", "--target", str(target), str(wheel)], cwd=work)
        if installed.returncode:
            return False, installed.stderr[-2000:]
    else:
        # Minimal hosts can still perform the exact-byte install preflight. Live certification
        # requires pip so the wheel's declared runtime dependencies are installed as a consumer sees them.
        try:
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(target)
        except zipfile.BadZipFile:
            return False, "pinned PyPI bytes are not a valid wheel"
    if not (target / "honua_sdk" / "__init__.py").is_file():
        return False, "installed wheel does not expose honua_sdk"
    if os.environ.get("HONUA_SERVER_URL"):
        if pip.returncode:
            return False, "live PyPI certification requires pip for declared dependencies"
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
            elif cell["driver"] == "npm-mcp":
                ok, detail = install_npm(
                    pin,
                    base / cell["id"],
                    verify_bins=True,
                    companion_pin=pins["honua-sdk-js"],
                )
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
            # boot.sh normally reads the repository-root manifest. Bind it to the already
            # validated candidate when the caller selected a different manifest.
            os.environ["HONUA_SERVER_IMAGE"] = server_image_ref(manifest)
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
