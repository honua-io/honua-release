#!/usr/bin/env python3
"""Consume the exact #136 `clientArtifacts` pins and prove what commands they ship.

The terminal journey may only run from the published bytes named by
`platform-manifest.yaml`. Per honua-release#136 there is no checkout, workspace,
regenerated or floating-`npx` fallback: if the pinned bytes are unreachable, fail
their integrity pin, or do not ship a required executable, the affected stages are
`blocked` and the receipt says so. Nothing here can produce a `pass`.

Trust logic is not re-implemented: registry resolution and integrity verification
are imported from `tools/verify_client_artifacts.py`, the repository's existing
pin verifier, so this driver and the manifest gate agree by construction.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

# The two artifacts #123/#161 bind. Keeping this list aligned with
# tools/terminal_model_canary.py's exact-equality check is deliberate.
JOURNEY_ARTIFACTS = ("honua-sdk-js", "honua-mcp-server")


def _load_verifier():
    """Import the repository's published-bytes verifier without a package install."""
    spec = importlib.util.spec_from_file_location(
        "_honua_verify_client_artifacts", ROOT / "tools" / "verify_client_artifacts.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise PinError("tools/verify_client_artifacts.py could not be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PinError(RuntimeError):
    """The pinned client artifacts cannot be consumed as published bytes."""


# Terminal commands the journey needs, the manifest target that claims to supply
# each one, and the numbered stages that cannot run without it.
@dataclass(frozen=True)
class RequiredCommand:
    command: str
    manifest_target: str
    required_by: tuple[int, ...]
    subcommand_of: str | None = None


REQUIRED_COMMANDS: tuple[RequiredCommand, ...] = (
    # Stages 2, 3 and 8 need `honua admin`, which depends on `honua` transitively;
    # the subcommand row below reports them, so this row names only stage 1.
    RequiredCommand("honua", "honua-cli", (1,)),
    RequiredCommand("honua admin", "honua-admin", (2, 3, 8), subcommand_of="honua"),
    RequiredCommand("honua-mcp-proxy", "honua-mcp-proxy", (1, 4, 5, 6, 7)),
)


@dataclass
class ResolvedArtifact:
    name: str
    package: str
    version: str
    ecosystem: str
    registry_url: str | None
    integrity_verified: bool
    tarball_sha256: str
    bin: dict[str, str]
    root: Path | None = None
    tarball: Path | None = None

    def as_receipt(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "package": self.package,
            "version": self.version,
            "ecosystem": self.ecosystem,
            "registryUrl": self.registry_url,
            "integrityVerified": self.integrity_verified,
            "tarballSha256": self.tarball_sha256,
            "bin": dict(sorted(self.bin.items())),
        }


@dataclass
class ClientWorkspace:
    status: str
    root: Path | None
    reason: str | None
    resolved: list[ResolvedArtifact] = field(default_factory=list)
    command_surface: list[dict[str, Any]] = field(default_factory=list)
    install_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_receipt(cls, receipt: dict[str, Any], root: Path) -> ClientWorkspace:
        """Reconstruct setup's verified workspace metadata without registry access."""
        resolved = [
            ResolvedArtifact(
                name=row["name"],
                package=row["package"],
                version=row["version"],
                ecosystem=row["ecosystem"],
                registry_url=row.get("registryUrl"),
                integrity_verified=bool(row["integrityVerified"]),
                tarball_sha256=row["tarballSha256"],
                bin=dict(row.get("bin") or {}),
            )
            for row in receipt.get("resolved", [])
        ]
        return cls(
            status=receipt["status"],
            root=root,
            reason=receipt.get("reason"),
            resolved=resolved,
            command_surface=list(receipt.get("commandSurface") or []),
            install_notes=list(receipt.get("installNotes") or []),
        )

    def command(self, name: str) -> dict[str, Any] | None:
        for row in self.command_surface:
            if row["command"] == name:
                return row
        return None

    def missing_for_stage(self, number: int) -> list[str]:
        """Blockers this workspace imposes on one numbered stage."""
        blockers = []
        for row in self.command_surface:
            if row["status"] == "absent" and number in row["requiredBy"]:
                blockers.append(
                    f"pinned clientArtifacts ship no `{row['command']}` executable "
                    f"({row['detail']})"
                )
        return blockers

    def as_receipt(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": "published-registry-bytes",
            "root": str(self.root) if self.root else None,
            "reason": self.reason,
            "resolved": [row.as_receipt() for row in self.resolved],
            "commandSurface": self.command_surface,
            "installNotes": list(self.install_notes),
        }


def _npm_bin_map(package_json: dict[str, Any], package: str) -> dict[str, str]:
    bins = package_json.get("bin") or {}
    if isinstance(bins, str):
        return {package.rsplit("/", 1)[-1]: bins}
    return {str(k): str(v) for k, v in bins.items()}


def _fetch_npm(verifier, name: str, pin: dict[str, Any], dest: Path) -> ResolvedArtifact:
    package = str(pin["package"])
    version = str(pin["version"])
    expected = str(pin.get("integrity", ""))
    if not expected:
        raise PinError(f"{name}: npm pin carries no integrity value")

    encoded = package.replace("/", "%2F")
    metadata = verifier._request_json(f"https://registry.npmjs.org/{encoded}/{version}")
    dist = metadata.get("dist") or {}
    if dist.get("integrity") != expected:
        raise PinError(f"{name}: npm registry integrity does not match the manifest pin")
    tarball_url = str(dist.get("tarball", ""))
    if not tarball_url.startswith("https://registry.npmjs.org/"):
        raise PinError(f"{name}: npm registry returned an untrusted tarball URL")

    data = verifier._request(tarball_url)
    if verifier._sha512_sri(data) != expected:
        raise PinError(f"{name}: downloaded npm bytes do not match the manifest integrity pin")
    # Re-run the repository's own archive identity check.
    verifier._verify_npm_archive(data, package, version)

    root = dest / name
    if root.exists():
        raise PinError(f"{name}: refusing to reuse an existing workspace at {root}")
    root.mkdir(parents=True)
    # Keep the verified bytes on disk so any later install consumes exactly these,
    # never a re-resolved floating version.
    tarball = root / f"{name}.tgz"
    tarball.write_bytes(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(root, filter="data")
        entry = archive.extractfile("package/package.json")
        if entry is None:  # pragma: no cover - _verify_npm_archive already guards
            raise PinError(f"{name}: package.json missing from published bytes")
        package_json = json.load(entry)

    return ResolvedArtifact(
        name=name,
        package=package,
        version=version,
        ecosystem="npm",
        registry_url=tarball_url,
        integrity_verified=True,
        tarball_sha256=hashlib.sha256(data).hexdigest(),
        bin=_npm_bin_map(package_json, package),
        root=root / "package",
        tarball=tarball,
    )


def _npm_install(prefix: Path, tarballs: list[str], extra: list[str]) -> subprocess.CompletedProcess[str] | str:
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "package.json").write_text(
        json.dumps({"name": "honua-terminal-journey-workspace", "private": True}) + "\n"
    )
    try:
        return subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--loglevel", "error", *extra, *tarballs],
            cwd=prefix,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"npm install could not be executed: {exc}"


def install_executables(workspace: ClientWorkspace, prefix: Path) -> tuple[Path | None, str, list[str]]:
    """Install the already-verified tarballs so their executables are runnable.

    Only bytes that matched the manifest integrity pin are installed; npm is pointed
    at those local files, never at a version range, so no floating resolution can
    substitute a different client build.

    The default resolution is attempted first. If it fails only because npm's
    semver ranges exclude prerelease versions across differing patch tuples, the
    install is retried with peer-range resolution relaxed and the deviation is
    reported to the caller for the receipt. The installed bytes are identical
    either way; only npm's range solver is bypassed.
    """
    tarballs = [str(a.tarball) for a in workspace.resolved if a.tarball is not None]
    if not tarballs:
        return None, "no verified client tarballs are available to install", []

    notes: list[str] = []
    result = _npm_install(prefix, tarballs, [])
    if isinstance(result, str):
        return None, result, notes

    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        if "ERESOLVE" in output:
            peer = next(
                (line.strip() for line in output.splitlines() if "peer " in line),
                "peer dependency range conflict",
            )
            notes.append(
                "the pinned client pair does not co-install under npm's default peer "
                f"resolution ({peer}); npm's caret ranges exclude prereleases with a "
                "different patch tuple, so the exact manifest-verified bytes were "
                "installed with --legacy-peer-deps. Bytes are unchanged; only npm's "
                "range solver was bypassed."
            )
            retry = _npm_install(prefix, tarballs, ["--legacy-peer-deps"])
            if isinstance(retry, str):
                return None, retry, notes
            if retry.returncode != 0:
                detail = (retry.stderr or retry.stdout).strip().splitlines()
                return None, f"npm install of the verified pinned tarballs failed: {detail[-1] if detail else '(no output)'}", notes
        else:
            detail = output.splitlines()
            return None, f"npm install of the verified pinned tarballs failed: {detail[-1] if detail else '(no output)'}", notes

    bindir = prefix / "node_modules" / ".bin"
    if not bindir.is_dir():
        return None, "npm install produced no node_modules/.bin directory", notes
    return bindir, "installed from manifest-verified published bytes", notes


def _honua_has_admin(artifact: ResolvedArtifact, bin_path: str) -> tuple[bool, str]:
    """Ask the pinned `honua` binary whether it implements an `admin` command.

    Deterministic: one fixed `--help` invocation, parsed for a top-level `admin`
    verb. No model, no heuristics beyond the command table the binary prints.
    """
    assert artifact.root is not None
    entry = artifact.root / str(bin_path).lstrip("./")
    if not entry.is_file():
        return False, f"{artifact.package}@{artifact.version} bin entry {bin_path} is missing"
    try:
        completed = subprocess.run(
            ["node", str(entry), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not execute the pinned honua CLI: {exc}"
    text = f"{completed.stdout}\n{completed.stderr}"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("honua admin") or stripped.split()[:1] == ["admin"]:
            return True, "the pinned honua CLI advertises an `admin` command"
    return False, (
        f"{artifact.package}@{artifact.version} `honua --help` advertises no `admin` "
        "command; the journey's admin verbs have no pinned client surface"
    )


def _build_command_surface(resolved: list[ResolvedArtifact]) -> list[dict[str, Any]]:
    by_command: dict[str, tuple[ResolvedArtifact, str]] = {}
    for artifact in resolved:
        for command, target in artifact.bin.items():
            by_command.setdefault(command, (artifact, target))

    surface: list[dict[str, Any]] = []
    for required in REQUIRED_COMMANDS:
        if required.subcommand_of:
            parent = by_command.get(required.subcommand_of)
            if parent is None:
                surface.append(
                    {
                        "command": required.command,
                        "requiredBy": list(required.required_by),
                        "status": "absent",
                        "providedBy": None,
                        "detail": (
                            f"no pinned artifact ships a `{required.subcommand_of}` "
                            f"executable, so `{required.command}` cannot exist"
                        ),
                    }
                )
                continue
            artifact, bin_path = parent
            present, detail = _honua_has_admin(artifact, bin_path)
            surface.append(
                {
                    "command": required.command,
                    "requiredBy": list(required.required_by),
                    "status": "present" if present else "absent",
                    "providedBy": f"{artifact.package}@{artifact.version}" if present else None,
                    "detail": detail,
                }
            )
            continue

        found = by_command.get(required.command)
        if found is None:
            shipped = sorted({name for a in resolved for name in a.bin})
            surface.append(
                {
                    "command": required.command,
                    "requiredBy": list(required.required_by),
                    "status": "absent",
                    "providedBy": None,
                    "detail": (
                        f"manifest target `{required.manifest_target}` is declared, but the "
                        f"pinned bytes ship only {shipped or ['(no executables)']}"
                    ),
                }
            )
            continue
        artifact, bin_path = found
        surface.append(
            {
                "command": required.command,
                "requiredBy": list(required.required_by),
                "status": "present",
                "providedBy": f"{artifact.package}@{artifact.version}",
                "detail": f"shipped as {bin_path}",
            }
        )
    return surface


def resolve_client_workspace(manifest: dict[str, Any], dest: Path) -> ClientWorkspace:
    """Materialize the pinned clients from published registry bytes, or fail closed."""
    artifacts = manifest.get("clientArtifacts") or {}
    wanted = {name: artifacts[name] for name in JOURNEY_ARTIFACTS if name in artifacts}
    missing = [name for name in JOURNEY_ARTIFACTS if name not in artifacts]
    if missing:
        return ClientWorkspace(
            status="blocked",
            root=None,
            reason=f"platform-manifest.yaml has no clientArtifacts pin for {missing}",
        )

    resolved: list[ResolvedArtifact] = []
    try:
        verifier = _load_verifier()
        # Always materialize fresh. Stale bytes from a previous run must never be
        # able to stand in for the pins this run is certifying.
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        for name, pin in sorted(wanted.items()):
            if pin.get("publicationState") not in {"published", "promoted"}:
                raise PinError(
                    f"{name}: publicationState is {pin.get('publicationState')!r}; only "
                    "published/promoted bytes may satisfy a release receipt"
                )
            if pin.get("ecosystem") != "npm":
                raise PinError(f"{name}: unsupported ecosystem {pin.get('ecosystem')!r}")
            resolved.append(_fetch_npm(verifier, name, pin, dest))
    except (PinError, Exception) as exc:  # noqa: BLE001 - any failure must fail closed
        if not isinstance(exc, PinError) and not isinstance(exc, OSError):
            reason = f"pinned client artifacts could not be consumed: {exc}"
        else:
            reason = str(exc)
        return ClientWorkspace(status="blocked", root=dest, reason=reason, resolved=resolved)

    surface = _build_command_surface(resolved)
    return ClientWorkspace(
        status="pass",
        root=dest,
        reason=None,
        resolved=resolved,
        command_surface=surface,
    )


def receipt_pins(manifest: dict[str, Any]) -> dict[str, Any]:
    """The exact clientArtifacts block the canary compares by equality."""
    return {
        name: {key: pin.get(key) for key in ("package", "version", "integrity", "digest", "sourceSha")}
        for name, pin in manifest["clientArtifacts"].items()
        if name in JOURNEY_ARTIFACTS
    }
