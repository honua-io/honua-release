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
import secrets
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
HERE = Path(__file__).resolve().parent
DEFAULT_MATRIX = Path(__file__).with_name("matrix.json")
FLOATING = re.compile(r"(?:^|[-.])(latest|next|local|snapshot)(?:$|[-.])|[*^~<>]", re.I)
DRIVERS = {"npm", "npm-mcp", "npm-mcp-setup-view", "pypi", "pypi-admin", "nuget", "nuget-import-fidelity"}
# Regression drivers: a published SDK, command-line client or the MCP proxy drives the candidate
# through a scenario contract (scenarios/*.json) with fixture-computed oracles (regression.py,
# oracles.py).
SUITE_DRIVERS = {"pypi-sdk", "npm-sdk", "nuget-sdk", "npm-cli", "pypi-cli", "npm-mcp-workflow"}
# Receipt step rows carry these fields only: never a response body, credential or server message.
SUITE_STEP_FIELDS = {"step", "api", "status", "oracle", "blockedBy"}
DRIVERS |= SUITE_DRIVERS
COMPOSE_BASE = ROOT / "e2e/harness/compose.candidate.yml"
COMPOSE_OVERLAY = HERE / "compose.sdk-regression.yml"
DEFAULT_ADMIN_KEY = "honua-console-dev-key"
FIXTURE_DB_PASSWORD = "honua"
# How each declared package executable is exercised. `--help` only proves anything for a CLI that
# implements it; the MCP server bins are stdio servers that refuse to start without configuration.
EXECUTABLE_CONTRACTS = {"help", "mcp-stdio", "mcp-proxy"}
BLOCKER = re.compile(r"https://github\.com/honua-io/[A-Za-z0-9_.-]+/issues/[0-9]+")
SETUP_BLOCKER = "https://github.com/honua-io/honua-sdk-js/issues/1875"
IMPORT_BLOCKER = "https://github.com/honua-io/honua-release/issues/418"
NUGET_ORG = "https://api.nuget.org/v3/index.json"
DOTNET_PROBE = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/dotnet/Probe.cs"
_IMPORT_FIDELITY = None
_PROBES = None
_REGRESSION = None


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
        validate_cell(cell)
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
    missing = set(artifacts) - {cell["artifact"] for cell in matrix["cells"]}
    if missing:
        raise CertificationError(f"matrix omits required client artifacts: {sorted(missing)}")
    suite = [cell for cell in matrix["cells"] if cell["driver"] in SUITE_DRIVERS]
    if suite:
        regression = _regression()
        try:
            scenarios = regression.load_scenarios()
            for cell in suite:
                regression.validate_suite_cell(cell, scenarios, BLOCKER)
        except (regression.RegressionError, OSError, json.JSONDecodeError, re.error) as exc:
            raise CertificationError(f"SDK regression contract is invalid: {exc}") from exc
        # A regression run covers every client: dropping a driver's cells is a red, not a smaller gate.
        absent = sorted(set(regression.DRIVER_CLIENTS) - {cell["driver"] for cell in suite})
        if absent:
            raise CertificationError(f"matrix omits regression drivers: {absent}")
        covered = {(cell["driver"], cell["scenario"]) for cell in suite}
        for driver in sorted({cell["driver"] for cell in suite}):
            family, artifact = regression.DRIVER_CLIENTS[driver]
            expected = {sid for sid in scenarios if regression.scenario_family(sid) == family}
            missing_scenarios = sorted(expected - {sid for drv, sid in covered if drv == driver})
            if missing_scenarios:
                raise CertificationError(
                    f"matrix omits {family.upper()} regression scenarios for {artifact} ({driver}): {missing_scenarios}")


def validate_cell(cell: dict[str, Any]) -> None:
    """The matrix is the only source of expected outcomes, so every expectation must be explicit."""
    cell_id = cell["id"]
    if cell.get("driver") not in DRIVERS:
        raise CertificationError(f"{cell_id}: unknown driver {cell.get('driver')!r}")
    status = cell.get("status")
    if status == "active":
        if "blockedBy" in cell:
            raise CertificationError(f"{cell_id}: an active cell cannot carry blockedBy")
    elif status == "blocked":
        blockers = cell.get("blockedBy") if cell["driver"] in SUITE_DRIVERS else [cell.get("blockedBy", "")]
        if not isinstance(blockers, list) or not blockers or not all(BLOCKER.fullmatch(str(url)) for url in blockers):
            raise CertificationError(f"{cell_id}: a blocked cell must name its blocking issue URL(s)")
    else:
        raise CertificationError(f"{cell_id}: status must be active or blocked, not {status!r}")
    if cell["driver"] == "npm-mcp":
        executables = cell.get("executables")
        if (
            not isinstance(executables, dict)
            or not executables
            or any(contract not in EXECUTABLE_CONTRACTS for contract in executables.values())
        ):
            raise CertificationError(f"{cell_id}: npm-mcp cells must map every executable to {sorted(EXECUTABLE_CONTRACTS)}")
    if cell["driver"] == "npm-mcp-setup-view":
        expect = cell.get("expect")
        if (
            not isinstance(expect, dict)
            or not isinstance(expect.get("workflowView"), str)
            or not expect["workflowView"]
            or type(expect.get("toolCount")) is not int
            or expect["toolCount"] < 1
        ):
            raise CertificationError(f"{cell_id}: setup-view cells must expect a workflowView and a positive toolCount")


def boot_candidate(manifest: dict[str, Any]) -> list[str]:
    """Boot the exact candidate image@digest the way e2e-local-docker does (local Docker,
    licensing disabled, no AWS), plus the SDK regression overlay. Returns the compose command."""
    port = os.environ.get("E2E_SERVER_PORT", "8080")
    server = os.environ.setdefault("HONUA_SERVER_URL", f"http://localhost:{port}")
    os.environ.setdefault("E2E_BASE", server)
    os.environ.setdefault("HONUA_PUBLIC_BASE_URL", server)
    # Bind compose to the already validated candidate, whichever manifest the caller selected.
    os.environ["HONUA_SERVER_IMAGE"] = server_image_ref(manifest)
    # A per-run issuer key for the operator-bearer scenarios; it never outlives the stack.
    os.environ.setdefault("SDKREG_SIGNING_KEY", secrets.token_urlsafe(48))
    compose = ["docker", "compose", "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_OVERLAY)]
    os.environ["SDKREG_COMPOSE"] = " ".join(compose)
    if subprocess.run([*compose, "pull", "server"], cwd=ROOT).returncode:
        raise CertificationError(f"could not pull {os.environ['HONUA_SERVER_IMAGE']}")
    if subprocess.run([*compose, "up", "-d", "--wait", "--wait-timeout", "300"], cwd=ROOT).returncode:
        subprocess.run([*compose, "logs", "--tail", "120", "server"], cwd=ROOT)
        subprocess.run([*compose, "down", "-v"], cwd=ROOT, capture_output=True)
        raise CertificationError("the immutable server candidate did not become ready")
    licensing = subprocess.run(
        [sys.executable, str(ROOT / "e2e/licensing.py"), "--base-url", server,
         "--output", str(ROOT / "e2e/out/licensing.json")],
        cwd=ROOT, env={**os.environ, "HONUA_ADMIN_PASSWORD": os.environ.get("E2E_API_KEY", DEFAULT_ADMIN_KEY)})
    if licensing.returncode:
        subprocess.run([*compose, "down", "-v"], cwd=ROOT, capture_output=True)
        raise CertificationError("the candidate must report licensing mode: disabled")
    return compose


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
    executables: dict[str, str] | None = None,
    companion_pin: dict[str, Any] | None = None,
    sdk_probe: bool = True,
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
    server = os.environ.get("HONUA_SERVER_URL")
    if executables is not None:
        package_root = work / "node_modules" / Path(*pin["package"].split("/"))
        package_json = json.loads((package_root / "package.json").read_text())
        bins = package_json.get("bin") or {}
        if isinstance(bins, str):
            bins = {pin["package"].split("/")[-1]: bins}
        if not bins or any(not (package_root / target).is_file() for target in bins.values()):
            return False, "installed MCP package has no complete executable surface"
        if set(bins) != set(executables):
            return False, (
                f"installed executables {sorted(bins)} differ from the matrix execution contract "
                f"{sorted(executables)}"
            )
        for command, contract in sorted(executables.items()):
            shim = work / "node_modules" / ".bin" / command
            if contract == "help":
                probe = _run([str(shim), "--help"], cwd=work)
                if probe.returncode:
                    return False, f"installed executable {command} --help failed: {(probe.stdout + probe.stderr)[-1000:]}"
            elif server:
                ok, detail = mcp_tools_list(shim, contract, server)
                if not ok:
                    return False, f"installed executable {command}: {detail}"
    if server and sdk_probe:
        probe = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/probe.mjs"
        local_probe = work / "probe.mjs"
        shutil.copy2(probe, local_probe)
        proc = _run(["node", str(local_probe)], cwd=work, env=os.environ.copy())
        if proc.returncode:
            return False, (proc.stdout + proc.stderr)[-2000:]
    suffix = ""
    if executables is not None:
        suffix = ", package executables verified"
        if server:
            suffix += ", every MCP executable answered a live tools/list"
    return True, f"exact npm archive sha512 and installed lock integrity matched{suffix}"


def _terminal_probes():
    global _PROBES
    if _PROBES is None:
        probes_path = ROOT / "certification" / "terminal-journey" / "probes.py"
        spec = importlib.util.spec_from_file_location("terminal_journey_probes", probes_path)
        if spec is None or spec.loader is None:
            raise CertificationError("could not load the shared MCP probe")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _PROBES = module
    return _PROBES


class ExpectedBlocker(CertificationError):
    pass


def validate_initialize(payload: dict[str, Any]) -> None:
    result = payload.get("result")
    identity = result.get("serverInfo") if isinstance(result, dict) else None
    if (
        "error" in payload
        or not isinstance(result, dict)
        or result.get("protocolVersion") != "2025-06-18"
        or not isinstance(identity, dict)
        or any(not isinstance(identity.get(field), str) or not identity[field] for field in ("name", "version"))
    ):
        raise _terminal_probes().McpError("initialize omitted a supported protocol or valid server identity")


def valid_tool_names(tools: list[Any]) -> bool:
    return bool(tools) and all(
        isinstance(tool, dict) and isinstance(tool.get("name"), str) and bool(tool["name"])
        for tool in tools
    ) and len({tool["name"] for tool in tools}) == len(tools)


def mcp_tools_list(shim: Path, contract: str, server: str) -> tuple[bool, str]:
    """initialize + tools/list through the installed npm shim, exactly as a customer launches it."""
    probes = _terminal_probes()
    if contract == "mcp-proxy":
        remote, env = f"{server}/mcp", {}
    else:
        remote, env = "", {"HONUA_BASE_URL": server}
    try:
        with probes.McpProxySession([str(shim)], remote, env=env) as session:
            initialized = session.initialize()
            validate_initialize(initialized)
            tools = session.list_tools()
    except (probes.McpError, OSError) as exc:
        return False, f"live tools/list failed: {exc}"
    if not valid_tool_names(tools):
        return False, "live tools/list returned an empty or malformed catalog"
    return True, f"{len(tools)} tools"


def probe_setup_view(proxy: Path, remote_url: str, expect: dict[str, Any]) -> tuple[bool, str]:
    """initialize with the workflow-view selector, then a selector-free tools/list (sdk-js#1875)."""
    probes = _terminal_probes()
    view, count = expect["workflowView"], expect["toolCount"]
    try:
        with probes.McpProxySession([str(proxy)], remote_url) as session:
            initialized = session.initialize(workflow_view=view)
            validate_initialize(initialized)
            payload = session.request("tools/list")
    except (probes.McpError, OSError) as exc:
        return False, f"installed proxy setup-view exchange failed: {exc}"
    if "error" in payload:
        return False, f"selector-free tools/list returned {payload['error']}"
    result = payload.get("result")
    if not isinstance(result, dict):
        return False, "selector-free tools/list returned a malformed result"
    tools = result.get("tools") if isinstance(result.get("tools"), list) else []
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    observed = f"view={meta.get('view')!r} revision={meta.get('revision')!r} tools={len(tools)}"
    if (
        view == "setup" and count == 25
        and result.get("nextCursor") is None
        and meta.get("view") == "default" and meta.get("revision") == "default.v1"
        and meta.get("toolCount") == 12
        and len(tools) == 12 and valid_tool_names(tools)
    ):
        raise ExpectedBlocker(f"selector-free tools/list dropped the setup selector: {observed}")
    if (
        result.get("nextCursor") is not None
        or meta.get("view") != view
        or meta.get("toolCount") != count
        or len(tools) != count
        or not valid_tool_names(tools)
    ):
        return False, (
            f"selector-free tools/list after initialize with workflow view {view!r} returned {observed}; "
            f"the journey contract is the complete {view!r} view with {count} tools"
        )
    return True, f"installed proxy preserved the initialize-bound {view!r} view ({meta.get('revision')}) with {count} tools"


def _nuget_framework(pin: dict[str, Any]) -> str:
    targets = pin.get("targets") or []
    framework = targets[0] if targets else ""
    return framework if re.fullmatch(r"net[0-9]+\.[0-9]+", framework) else ""


def _restore_nuget(pin: dict[str, Any], work: Path, project: Path, build_args: list[str]) -> tuple[bool, str, dict[str, str]]:
    """Restore and build ``project`` against the pinned package from anonymous nuget.org only."""
    config = work / "NuGet.config"
    config.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<configuration><packageSources><clear />'
        f'<add key="nuget.org" value="{NUGET_ORG}" protocolVersion="3" />'
        "</packageSources></configuration>\n"
    )
    packages = work / "packages"
    env = {
        **os.environ,
        "NUGET_PACKAGES": str(packages),
        "NUGET_HTTP_CACHE_PATH": str(work / "http-cache"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
    }
    restore = _run(["dotnet", "restore", "--configfile", str(config), *build_args], cwd=project, env=env)
    if restore.returncode:
        return False, f"dotnet restore from nuget.org failed: {(restore.stdout + restore.stderr)[-2000:]}", env
    package_id, version = pin["package"].lower(), pin["version"].lower()
    restored = packages / package_id / version / f"{package_id}.{version}.nupkg"
    try:
        actual = "sha256:" + hashlib.sha256(restored.read_bytes()).hexdigest()
        source = json.loads((restored.parent / ".nupkg.metadata").read_text()).get("source")
        libraries = json.loads((project / "obj" / "project.assets.json").read_text()).get("libraries", {})
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not inspect the restored NuGet package: {exc}", env
    if actual != pin["digest"]:
        return False, f"restored NuGet package digest mismatch: {actual}", env
    if source != NUGET_ORG:
        return False, f"restored NuGet package came from {source!r}, not nuget.org", env
    if f"{pin['package']}/{pin['version']}" not in libraries:
        return False, "restored consumer does not resolve the exact pinned package version", env
    build = _run(["dotnet", "build", "--no-restore", "-c", "Release", "-o", str(work / "out"), *build_args], cwd=project, env=env)
    if build.returncode:
        return False, f"consumer build against the published package failed: {(build.stdout + build.stderr)[-2000:]}", env
    return True, "exact nuget.org package sha256 matched in an isolated restore", env


def install_nuget(pin: dict[str, Any], work: Path) -> tuple[bool, str]:
    """Restore the pinned package from nuget.org into an isolated consumer and check the bytes."""
    if pin.get("registry") != "nuget.org":
        return False, f"NuGet pin registry {pin.get('registry')!r} is not anonymous nuget.org"
    if not shutil.which("dotnet"):
        return False, "dotnet is unavailable"
    framework = _nuget_framework(pin)
    if not framework:
        return False, f"NuGet pin has no consumer target framework: {pin.get('targets')!r}"
    project = work / "consumer"
    project.mkdir(parents=True)
    (project / "Consumer.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n'
        f"    <TargetFramework>{framework}</TargetFramework>\n"
        "    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>\n  </PropertyGroup>\n"
        f'  <ItemGroup>\n    <PackageReference Include="{pin["package"]}" Version="[{pin["version"]}]" />\n'
        "  </ItemGroup>\n</Project>\n"
    )
    shutil.copy2(DOTNET_PROBE, project / "Probe.cs")
    ok, detail, env = _restore_nuget(pin, work, project, [])
    if not ok:
        return False, detail
    suffix = ""
    if os.environ.get("HONUA_SERVER_URL"):
        probe = _run(["dotnet", str(work / "out" / "Consumer.dll")], cwd=work, env=env)
        if probe.returncode:
            return False, (probe.stdout + probe.stderr)[-2000:]
        suffix = ", live GeoServices error probe passed"
    return True, f"{detail}{suffix}"


def _download_wheel(pin: dict[str, Any], work: Path) -> tuple[Path | None, str]:
    """Download exactly the pinned wheel from PyPI and check its sha256."""
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{pin['package']}/{pin['version']}/json", timeout=30
        ) as response:
            metadata = json.load(response)
        candidates = [item for item in metadata["urls"] if item["filename"] == pin.get("filename")]
        if len(candidates) != 1:
            return None, "PyPI release metadata did not contain exactly the pinned wheel"
        wheel = work / pin["filename"]
        with urllib.request.urlopen(candidates[0]["url"], timeout=60) as response:
            wheel.write_bytes(response.read())
    except Exception as exc:
        return None, f"PyPI download failed: {exc}"
    actual = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != pin["digest"]:
        return None, f"wheel digest mismatch: {actual}"
    return wheel, ""


def install_pypi(pin: dict[str, Any], work: Path) -> tuple[bool, str]:
    work.mkdir()
    wheel, failure = _download_wheel(pin, work)
    if wheel is None:
        return False, failure
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
    module = {"honua-sdk": "honua_sdk", "honua-admin": "honua_admin"}[pin["package"]]
    if not (target / module / "__init__.py").is_file():
        return False, f"installed wheel does not expose {module}"
    if module == "honua_admin":
        if pip.returncode:
            return False, "admin certification requires pip for declared dependencies"
        probe = _run([sys.executable, "-I", "-c",
                      "import sys; sys.path.insert(0, sys.argv[1]); "
                      "from honua_admin import HonuaAdminClient, AsyncHonuaAdminClient",
                      str(target)], cwd=work)
        if probe.returncode:
            return False, f"installed admin import failed: {probe.stderr[-2000:]}"
    elif os.environ.get("HONUA_SERVER_URL"):
        if pip.returncode:
            return False, "live PyPI certification requires pip for declared dependencies"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(target)
        probe = ROOT / "e2e/scenarios/geoservices_error_surfacing/probes/probe.py"
        proc = _run([sys.executable, str(probe)], cwd=work, env=env)
        if proc.returncode:
            return False, (proc.stdout + proc.stderr)[-2000:]
    return True, "exact PyPI wheel installed in an isolated target and sha256 matched"


def _import_fidelity():
    global _IMPORT_FIDELITY
    if _IMPORT_FIDELITY is None:
        path = Path(__file__).with_name("dotnet_import_fidelity.py")
        spec = importlib.util.spec_from_file_location("dotnet_import_fidelity", path)
        if spec is None or spec.loader is None:
            raise CertificationError("could not load the .NET import-fidelity gate")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _IMPORT_FIDELITY = module
    return _IMPORT_FIDELITY


def _regression():
    global _REGRESSION
    if _REGRESSION is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))  # regression.py imports its sibling oracles.py
        spec = importlib.util.spec_from_file_location("sdk_regression", HERE / "regression.py")
        if spec is None or spec.loader is None:
            raise CertificationError("could not load the SDK regression suite")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _REGRESSION = module
    return _REGRESSION


def _install_python_wheels(pins: dict[str, Any], work: Path) -> tuple[Path | None, str]:
    """Install the pinned honua-sdk and honua-admin wheels (sha256-checked) into an isolated target."""
    work.mkdir()
    wheels = []
    for key in ("honua-sdk-python-wheel", "honua-admin-python-wheel"):
        wheel, failure = _download_wheel(pins[key], work)
        if wheel is None:
            return None, f"{pins[key]['package']}: {failure}"
        wheels.append(wheel)
    target = work / "site-packages"
    installed = _run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(target), *map(str, wheels)], cwd=work)
    if installed.returncode:
        return None, f"pip install of the pinned wheels failed: {installed.stderr[-2000:]}"
    for key in ("honua-sdk-python-wheel", "honua-admin-python-wheel"):
        pin = pins[key]
        if not (target / f"{pin['package'].replace('-', '_')}-{pin['version']}.dist-info").is_dir():
            return None, f"installed {pin['package']} is not the pinned {pin['version']}"
    return target, "exact PyPI wheels (honua-sdk + honua-admin) sha256 matched and installed in isolation"


def install_suite_client(driver: str, pins: dict[str, Any], work: Path) -> tuple[bool, str, list[str], dict[str, str]]:
    """Install only the manifest-pinned published client bytes and return the driver command."""
    drivers = HERE / "drivers"
    if driver == "npm-sdk":
        ok, detail = install_npm(pins["honua-sdk-js"], work, sdk_probe=False)
        if not ok:
            return False, detail, [], {}
        work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(drivers / "js" / "driver.mjs", work / "driver.mjs")
        return True, detail, ["node", str(work / "driver.mjs")], dict(os.environ)
    if driver == "pypi-sdk":
        target, detail = _install_python_wheels(pins, work)
        if target is None:
            return False, detail, [], {}
        launcher = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
                    "runpy.run_path(sys.argv[2], run_name='__main__')")
        command = [sys.executable, "-I", "-c", launcher, str(target), str(drivers / "python" / "driver.py")]
        return True, detail, command, dict(os.environ)
    if driver == "pypi-cli":
        target, detail = _install_python_wheels(pins, work)
        if target is None:
            return False, detail, [], {}
        honua = target / "bin" / "honua"
        if not honua.is_file():
            return False, "the pinned wheels installed no honua console script", [], {}
        # The console script runs on this interpreter with the isolated target on its path.
        env = {**os.environ, "PYTHONPATH": str(target)}
        return True, f"{detail}; honua console script installed", [
            sys.executable, str(drivers / "cli" / "driver.py"), "--client", "pypi", "--honua", str(honua)], env
    if driver == "npm-cli":
        # The proposal step goes through the co-installed, byte-verified MCP proxy (journey stage 7).
        ok, detail = install_npm(pins["honua-sdk-js"], work, companion_pin=pins["honua-mcp-server"], sdk_probe=False)
        if not ok:
            return False, detail, [], {}
        shims = work / "node_modules" / ".bin"
        return True, detail, [sys.executable, str(drivers / "cli" / "driver.py"), "--client", "npm",
                              "--honua", str(shims / "honua"), "--proxy", str(shims / "honua-mcp-proxy")], dict(os.environ)
    if driver == "npm-mcp-workflow":
        ok, detail = install_npm(pins["honua-mcp-server"], work, companion_pin=pins["honua-sdk-js"], sdk_probe=False)
        if not ok:
            return False, detail, [], {}
        return True, detail, [sys.executable, str(drivers / "mcp" / "driver.py"),
                              "--proxy", str(work / "node_modules" / ".bin" / "honua-mcp-proxy")], dict(os.environ)
    if driver == "nuget-sdk":
        pin = pins["honua-sdk-dotnet"]
        if pin.get("registry") != "nuget.org":
            return False, f"NuGet pin registry {pin.get('registry')!r} is not anonymous nuget.org", [], {}
        if not shutil.which("dotnet"):
            return False, "dotnet is unavailable", [], {}
        framework = _nuget_framework(pin)
        if not framework:
            return False, f"NuGet pin has no consumer target framework: {pin.get('targets')!r}", [], {}
        project = work / "driver"
        project.mkdir(parents=True)
        for name in ("Driver.csproj", "Program.cs"):
            shutil.copy2(drivers / "dotnet" / name, project / name)
        ok, detail, env = _restore_nuget(pin, work, project, [
            f"-p:SdkRegressionTargetFramework={framework}", f"-p:SdkRegressionHonuaSdkVersion={pin['version']}"])
        if not ok:
            return False, detail, [], {}
        return True, detail, ["dotnet", str(work / "out" / "SdkRegressionDriver.dll")], env
    return False, f"no SDK regression driver {driver!r}", [], {}


def run_suite(manifest: dict[str, Any], matrix: dict[str, Any], base: Path) -> dict[str, tuple[str, str, list[dict[str, Any]]]]:
    """Run every regression cell (SDK, command-line and MCP): one install and one driver run per client."""
    regression = _regression()
    pins = manifest["clientArtifacts"]
    cells = [cell for cell in matrix["cells"] if cell["driver"] in SUITE_DRIVERS]
    if not cells:
        return {}
    scenarios, fixture = regression.load_scenarios(), regression.load_fixture()
    results: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}
    server = os.environ.get("HONUA_SERVER_URL")
    drivers = sorted({cell["driver"] for cell in cells})
    published, bearer, harness_failure, principals = None, "", "", None
    if server:
        try:
            family = lambda driver: regression.DRIVER_CLIENTS[driver][0]  # noqa: E731
            slugs = [regression.SUITE_DRIVERS[driver] for driver in drivers if family(driver) == "sdk"]
            workflow_slugs = [regression.SUITE_DRIVERS[driver] for driver in drivers if family(driver) == "cli"]
            regression.seed(fixture, slugs, workflow_slugs=workflow_slugs)
            admin_key = os.environ.get("E2E_API_KEY", DEFAULT_ADMIN_KEY)
            api = regression.AdminApi(server, admin_key)
            published = regression.publish_harness(api, fixture, slugs, FIXTURE_DB_PASSWORD)
            if workflow_slugs:
                principals = regression.mint_principals(api, secrets.token_hex(4))
            signing_key = os.environ.get("SDKREG_SIGNING_KEY")
            if not signing_key:
                raise regression.RegressionError("SDKREG_SIGNING_KEY is not set; the candidate cannot validate an operator bearer")
            bearer = regression.mint_bearer(signing_key)
        except regression.RegressionError as exc:
            harness_failure = f"fixture preparation failed: {exc}"
    for driver in drivers:
        group = [cell for cell in cells if cell["driver"] == driver]
        artifact = group[0]["artifact"]
        pin = pins[artifact]
        client = f"{pin['package']} {pin['version']}"
        work = base / f"suite-{driver}"
        ok, detail, command, env = install_suite_client(driver, pins, work)
        if not ok:
            for cell in group:
                results[cell["id"]] = ("fail", f"{client} install failed: {detail}", [])
            continue
        if not server or harness_failure or published is None:
            reason = harness_failure or "SDK regression scenarios need a live candidate (--live)"
            for cell in group:
                results[cell["id"]] = ("fail", f"{client}: {reason}", [])
            continue
        slug = regression.SUITE_DRIVERS[driver]
        workflow = principals if regression.DRIVER_CLIENTS[driver][0] == "cli" else None
        plan = regression.build_plan(fixture, published, slug, [cell["scenario"] for cell in group], scenarios, server,
                                     principals=workflow)
        plan_path = work / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2))
        code, stdout, stderr = regression.run_driver(command, work, regression.driver_env(
            env, api_key=os.environ.get("E2E_API_KEY", DEFAULT_ADMIN_KEY), bearer=bearer,
            db_password=FIXTURE_DB_PASSWORD, plan_path=plan_path, principals=workflow))
        regression.log(f"== {client} driver exited {code} ==\n{stderr[-6000:]}")
        observations = regression.parse_observations(stdout)
        for cell in group:
            status, cell_detail, rows = regression.evaluate_cell(
                cell, scenarios[cell["scenario"]], artifact, observations, fixture, plan, client)
            if code and status != "fail":
                status, cell_detail = "fail", f"{client} driver exited {code}; {cell_detail}"
            results[cell["id"]] = (status, cell_detail, rows)
    return results


def evaluate_import_fidelity(manifest: dict[str, Any], receipt: dict[str, Any] | None) -> tuple[str, str]:
    """Consume a published-SDK receipt. Missing evidence stays a fail; nothing is synthesized."""
    try:
        verdict = _import_fidelity().evaluate(manifest, receipt)
    except Exception as exc:
        return "fail", f"import fidelity gate could not evaluate the receipt: {exc}"
    return verdict["status"], verdict["reason"]


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
        "status": receipt_status(results),
        "results": results,
    }


def receipt_status(results: list[dict[str, Any]]) -> str:
    """pass only when every cell passes; blocked when every active cell passes and some are blocked."""
    statuses = {result["status"] for result in results}
    if not results or statuses - {"pass", "blocked"}:
        return "fail"
    return "blocked" if "blocked" in statuses else "pass"


def run_cell(
    cell: dict[str, Any],
    manifest: dict[str, Any],
    work: Path,
    import_fidelity_receipt: dict[str, Any] | None,
) -> tuple[str, str]:
    pins = manifest["clientArtifacts"]
    pin = pins[cell["artifact"]]
    driver = cell["driver"]
    if driver == "npm":
        ok, detail = install_npm(pin, work)
    elif driver == "npm-mcp":
        ok, detail = install_npm(
            pin, work, executables=cell["executables"], companion_pin=pins["honua-sdk-js"]
        )
    elif driver == "npm-mcp-setup-view":
        ok, detail = install_npm(pin, work, companion_pin=pins["honua-sdk-js"], sdk_probe=False)
        server = os.environ.get("HONUA_SERVER_URL")
        if ok and not server:
            ok, detail = False, "the setup-view exchange needs a live candidate (--live)"
        elif ok:
            proxy = work / "node_modules" / ".bin" / "honua-mcp-proxy"
            try:
                ok, detail = probe_setup_view(proxy, f"{server}/mcp", cell["expect"])
            except ExpectedBlocker as exc:
                return f"blocked:{SETUP_BLOCKER}", str(exc)
    elif driver in {"pypi", "pypi-admin"}:
        ok, detail = install_pypi(pin, work)
    elif driver == "nuget":
        ok, detail = install_nuget(pin, work)
    elif driver == "nuget-import-fidelity":
        if import_fidelity_receipt is None:
            return f"blocked:{IMPORT_BLOCKER}", "no published-.NET-SDK consumer receipt; missing evidence is not a pass"
        return evaluate_import_fidelity(manifest, import_fidelity_receipt)
    else:
        return "fail", f"no executor for driver {driver!r}"
    return ("pass" if ok else "fail"), detail


def classify(cell: dict[str, Any], observed: str, detail: str) -> tuple[str, str]:
    """Map an observed outcome onto the matrix expectation. A blocked cell never passes silently."""
    if cell["status"] != "blocked":
        return ("fail" if observed.startswith("blocked:") else observed), detail
    if observed == "pass":
        return "fail", (
            f"matrix marks this cell blocked by {cell['blockedBy']}, but it passed ({detail}); "
            "set it active in matrix.json"
        )
    if observed == f"blocked:{cell['blockedBy']}":
        return "blocked", f"blocked by {cell['blockedBy']}: {detail}"
    return "fail", detail


def verify_receipt(matrix: dict[str, Any], receipt: dict[str, Any]) -> list[str]:
    """Every expectation comes from the matrix: active cells pass, blocked cells report their blocker."""
    cells = matrix.get("cells", [])
    results = receipt.get("results", [])
    violations: list[str] = []
    if [result.get("cell") for result in results] != [cell["id"] for cell in cells]:
        violations.append("receipt cells do not match the matrix cells one-to-one and in order")
    by_id = {result.get("cell"): result for result in results}
    for cell in cells:
        result = by_id.get(cell["id"])
        if result is None:
            continue
        if cell["status"] == "active" and result.get("status") != "pass":
            violations.append(f"{cell['id']}: active cell is {result.get('status')}: {result.get('detail')}")
        if cell["status"] == "blocked" and (
            result.get("status") != "blocked" or result.get("blockedBy") != cell["blockedBy"]
        ):
            violations.append(
                f"{cell['id']}: blocked cell must report blocked by {cell['blockedBy']}, "
                f"got {result.get('status')}: {result.get('detail')}"
            )
    violations.extend(verify_suite_steps(cells, by_id))
    if receipt.get("status") != receipt_status(results):
        violations.append(f"receipt status {receipt.get('status')!r} does not follow its cell results")
    return violations


def verify_suite_steps(cells: list[dict[str, Any]], by_id: dict[Any, dict[str, Any]]) -> list[str]:
    """Every scenario step is reported once, in contract order, and only declared steps are blocked."""
    suite = [cell for cell in cells if cell.get("driver") in SUITE_DRIVERS]
    if not suite:
        return []
    scenarios = _regression().load_scenarios()
    violations = []
    for cell in suite:
        result = by_id.get(cell["id"])
        if result is None:
            continue
        rows = result.get("steps") if isinstance(result.get("steps"), list) else []
        expected = [step["id"] for step in scenarios[cell["scenario"]]["steps"]]
        if not (result.get("status") == "fail" and not rows) and [row.get("step") for row in rows] != expected:
            violations.append(f"{cell['id']}: receipt steps do not match the {cell['scenario']} contract")
        declared = set(cell.get("blockedSteps") or {})
        for row in rows:
            if row.get("status") not in {"pass", "fail", "blocked"} or set(row) - SUITE_STEP_FIELDS:
                violations.append(f"{cell['id']}: step {row.get('step')!r} carries a non-allowlisted field or status")
            if row.get("status") == "blocked" and row.get("step") not in declared:
                violations.append(f"{cell['id']}: step {row.get('step')!r} is blocked without a matrix declaration")
    return violations


def execute(
    manifest: dict[str, Any],
    matrix: dict[str, Any],
    evidence_uri: str,
    import_fidelity_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pins = manifest["clientArtifacts"]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="honua-installed-client-") as tmp:
        base = Path(tmp)
        # Blocked cells and blocked steps still execute so a fix is observed instead of assumed.
        suite = run_suite(manifest, matrix, base)
        for cell in matrix["cells"]:
            pin = pins[cell["artifact"]]
            steps = None
            if cell["driver"] in SUITE_DRIVERS:
                # The suite judges each step against the matrix itself (blocked steps included).
                status, detail, steps = suite.get(cell["id"], ("fail", "the suite produced no result for this cell", []))
            else:
                observed, detail = run_cell(cell, manifest, base / cell["id"], import_fidelity_receipt)
                status, detail = classify(cell, observed, detail)
            result = {
                "cell": cell["id"], "operationId": cell["scenario"], "target": cell["driver"],
                "package": pin["package"], "version": pin["version"],
                "integrity": pin.get("integrity") or pin.get("digest"), "sourceSha": pin["sourceSha"],
                "matrixStatus": cell["status"], "status": status, "detail": detail,
            }
            if steps is not None:
                result["steps"] = steps
            if cell["status"] == "blocked":
                result["blockedBy"] = cell["blockedBy"]
            results.append(result)
    return make_receipt(manifest, matrix, results, evidence_uri)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/installed-client-certification.json")
    parser.add_argument("--evidence-uri", help="durable CI artifact/run URI")
    parser.add_argument(
        "--verify-receipt",
        type=Path,
        help="check a receipt against the matrix expectations instead of running the cells",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--subset",
        choices=("all", "fast"),
        default="all",
        help="fast: only the install/probe cells (the PR check); all: also the SDK regression scenarios (the gate)",
    )
    parser.add_argument("--live", action="store_true", help="boot and seed the single real server/PostgreSQL target")
    parser.add_argument(
        "--import-fidelity-receipt",
        type=Path,
        help="receipt from a clean published .NET SDK consumer; omitted evidence fails that cell",
    )
    args = parser.parse_args()
    try:
        manifest, matrix = load_inputs(args.manifest, args.matrix)
        if args.subset == "fast":
            matrix["cells"] = [cell for cell in matrix["cells"] if cell.get("driver") not in SUITE_DRIVERS]
        validate_release_inputs(manifest, matrix)
        if args.verify_receipt:
            try:
                receipt = json.loads(args.verify_receipt.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CertificationError(f"receipt is not readable JSON: {exc}") from exc
            violations = verify_receipt(matrix, receipt)
            for violation in violations:
                print(f"matrix expectation failed: {violation}", file=sys.stderr)
            return 1 if violations else 0
        if not args.evidence_uri:
            raise CertificationError("--evidence-uri is required")
        if args.validate_only:
            return 0
        import_fidelity_receipt = None
        if args.import_fidelity_receipt:
            try:
                import_fidelity_receipt = json.loads(args.import_fidelity_receipt.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise CertificationError(f"import fidelity receipt is not readable JSON: {exc}") from exc
            if not isinstance(import_fidelity_receipt, dict):
                raise CertificationError("import fidelity receipt must be a JSON object")
        if args.live:
            compose = boot_candidate(manifest)
            try:
                seed = subprocess.run(["bash", str(ROOT / "e2e/harness/seed/seed.sh")], cwd=ROOT,
                                      env={**os.environ, "E2E_PSQL": " ".join([*compose, "exec", "-T", "db", "psql", "-U", "honua", "-d", "honua"])})
                if seed.returncode:
                    raise CertificationError("the immutable fixture could not be seeded")
                receipt = execute(
                    manifest, matrix, args.evidence_uri, import_fidelity_receipt=import_fidelity_receipt
                )
            finally:
                subprocess.run([*compose, "down", "-v"], cwd=ROOT, capture_output=True)
        else:
            receipt = execute(
                manifest, matrix, args.evidence_uri, import_fidelity_receipt=import_fidelity_receipt
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["status"] in {"pass", "blocked"} else 1
    except CertificationError as exc:
        print(f"certification input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
