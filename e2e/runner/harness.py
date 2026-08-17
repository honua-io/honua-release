"""Harness: bring up the composed server+DB, wait for health, install SDKs from staging, scrape
metrics, and run per-SDK probe subprocesses. NO mocks at the seam — every probe talks to the real
composed server.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest

COMPOSE_DIR = Path(__file__).resolve().parents[1] / "local-docker"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"


class ImageUnavailable(RuntimeError):
    """The pinned server image is a placeholder or cannot be pulled — scenarios are BLOCKED."""


@dataclass
class Ctx:
    """Everything a scenario needs. Passed to each scenario's run(ctx)."""
    manifest: Manifest
    server_url: str
    metrics_url: str
    probes_python: str          # interpreter used for python probes
    require_real: bool

    def sdk_available(self, short: str) -> bool:
        return sdk_toolchain_available(short)


# --------------------------------------------------------------------------------------------------
# docker-compose lifecycle
# --------------------------------------------------------------------------------------------------
def _compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        cmd, env=env, check=check, text=True,
        capture_output=capture,
    )


def compose_config_ok() -> tuple[bool, str]:
    """Static validation of the compose file — a gate that can FAIL on a broken edit, with no images."""
    if not shutil.which("docker"):
        return False, "docker CLI not found"
    try:
        _compose("config", check=True, capture=True)
        return True, "docker compose config valid"
    except subprocess.CalledProcessError as e:
        return False, f"docker compose config failed: {e.stderr or e}"


def compose_up(manifest: Manifest, server_url: str, timeout_s: int = 180) -> None:
    """Pull + start the stack and block until the server reports healthy.

    Raises ImageUnavailable when the manifest pin is a placeholder or the image cannot be pulled, so
    the caller can mark server-dependent scenarios BLOCKED (TODO) rather than fail spuriously.
    """
    if not manifest.server.is_real and not os.environ.get("HONUA_SERVER_IMAGE"):
        raise ImageUnavailable(
            f"server image pin is a placeholder ({manifest.server.image!r}); "
            "publish a real image or set HONUA_SERVER_IMAGE — see platform-manifest.yaml"
        )

    os.environ.setdefault("HONUA_SERVER_IMAGE", manifest.server_image)
    try:
        _compose("pull", check=True, capture=True)
    except subprocess.CalledProcessError as e:
        raise ImageUnavailable(f"failed to pull {manifest.server_image}: {e}") from e

    _compose("up", "-d", check=True)
    try:
        _wait_for_health(server_url, timeout_s)
    except ImageUnavailable as e:
        logs = _compose("logs", "--no-color", "--tail", "80", "server", check=False, capture=True)
        detail = (logs.stdout or logs.stderr or "server emitted no container logs").strip()
        raise ImageUnavailable(f"{e}\nserver container logs:\n{detail}") from e


def compose_down() -> None:
    try:
        _compose("down", "-v", check=False)
    except Exception:  # best-effort teardown
        pass


def _wait_for_health(server_url: str, timeout_s: int) -> None:
    # /healthz is Development-only and is not the deployable health contract. The published image
    # consistently exposes the readiness endpoint used by the cloud canary and Slice-1 harness.
    url = server_url.rstrip("/") + "/healthz/ready"
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return
                last = f"status {r.status}"
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last = str(e)
        time.sleep(3)
    raise ImageUnavailable(f"server never became healthy at {url}: {last}")


# --------------------------------------------------------------------------------------------------
# Prometheus metric scrape (stdlib only) — ties the observability gate (server#2243)
# --------------------------------------------------------------------------------------------------
# A Prometheus sample value: an int/float (optionally in scientific notation) or one of the
# special float literals the exposition format allows.
_SAMPLE_VALUE = r"(?:[0-9eE+.\-]+|[+-]?Inf|NaN)"
# The exposition format allows an OPTIONAL trailing millisecond timestamp after the value, and the
# OpenTelemetry .NET Prometheus exporter emits one on every sample by default. The original pattern
# anchored the value at end-of-line, so it silently returned None for every real Honua scrape —
# indistinguishable from "the metric does not exist". Making the timestamp optional is what lets
# this parser read an actual honua-server /metrics response (honua-release#5).
_SAMPLE_TIMESTAMP = r"(?:\s+-?[0-9]+)?"


def parse_metric_total(body: str, name: str) -> float | None:
    """Sum every sample (across label sets) of a Prometheus series in an exposition `body`.

    `name` is the exposition series name, so histogram children work too: pass
    ``honua_serving_request_duration_ms_count`` to total a histogram's observation count (the
    canonical Honua SLO denominator). Prefix collisions are still excluded — asking for
    ``honua_serving_request_duration_ms`` does not pick up its ``_count``/``_sum``/``_bucket``
    children.

    Pure + side-effect free so it is unit-testable without a live server (see test_runner.py).
    Returns None when the metric is absent entirely — see scrape_metric for why that is meaningful.
    """
    total = None
    pat = re.compile(
        rf"^{re.escape(name)}(\{{[^}}]*\}})?\s+({_SAMPLE_VALUE}){_SAMPLE_TIMESTAMP}\s*$"
    )
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        m = pat.match(line.strip())
        if m:
            total = (total or 0.0) + float(m.group(2))
    return total


def scrape_metric(metrics_url: str, name: str) -> float | None:
    """Sum all samples of a Prometheus counter. Returns None if the metric is absent.

    None is meaningful: if honua_geoservices_error_total is missing entirely, the in-band error metric
    isn't wired (the very blindness server#2243 is about) — the scenario treats that as a failure when
    real, BLOCKED while placeholder.
    """
    try:
        request = urllib.request.Request(metrics_url)
        request.add_header("X-API-Key", os.environ.get("HONUA_ADMIN_PASSWORD", "honua-console-dev-key"))
        with urllib.request.urlopen(request, timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None
    return parse_metric_total(body, name)


# --------------------------------------------------------------------------------------------------
# SDK install from staging (artifact-consumption seam, plan §4d) + probe execution
# --------------------------------------------------------------------------------------------------
def sdk_toolchain_available(short: str) -> bool:
    return {
        "python": bool(shutil.which("python") or shutil.which("python3")),
        "js": bool(shutil.which("node") and shutil.which("npm")),
        "dotnet": bool(shutil.which("dotnet")),
    }.get(short, False)


def install_sdks(manifest: Manifest) -> dict[str, str]:
    """Install each SDK from its staging source so probes consume the REAL staged artifact.

    Returns {short: note}. TODO(#7): the actual install commands are stubbed below until the SDK
    versions are real in the manifest and the staging registries are reachable from CI.
    """
    notes: dict[str, str] = {}
    for short, pin in manifest.sdks.items():
        if not pin.is_real:
            notes[short] = f"SKIP: {pin.name} version is a placeholder ({pin.version})"
            continue
        if not sdk_toolchain_available(short):
            notes[short] = f"SKIP: toolchain for {short} not present"
            continue
        # TODO(#7): real install, e.g.
        #   js:     npm install --registry $HONUA_NPM_REGISTRY {coord}@{version}
        #   python: pip install --index-url $HONUA_PIP_INDEX_URL {coord}=={version}
        #   dotnet: dotnet add package {coord} -v {version} -s $HONUA_NUGET_SOURCE
        notes[short] = f"TODO: install {pin.coord}@{pin.version} from staging"
    return notes


@dataclass
class ProbeResult:
    sdk: str
    exit_code: int
    stdout: str
    stderr: str

    # Probe exit-code contract (shared by every language probe):
    #   0 = PASS  (expected behaviour observed, e.g. SDK raised on a 200+{error})
    #   1 = FAIL  (the bug: SDK swallowed the error / returned success)
    #   2 = SKIP  (SDK/toolchain not available)
    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def skipped(self) -> bool:
        return self.exit_code == 2

    @property
    def failed(self) -> bool:
        return self.exit_code == 1


def run_probe(short: str, probe_path: Path, ctx: Ctx) -> ProbeResult:
    """Run one language probe as a subprocess against the composed server."""
    env = dict(os.environ)
    env["HONUA_SERVER_URL"] = ctx.server_url
    env["HONUA_METRICS_URL"] = ctx.metrics_url

    if short == "python":
        cmd = [ctx.probes_python, str(probe_path)]
    elif short == "js":
        cmd = ["node", str(probe_path)]
    elif short == "dotnet":
        # TODO(#7): `dotnet run` against the probe project once the NuGet SDK pin is real.
        cmd = ["dotnet", "run", "--project", str(probe_path)]
    else:
        return ProbeResult(short, 2, "", f"unknown sdk {short}")

    try:
        p = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=120)
        return ProbeResult(short, p.returncode, p.stdout, p.stderr)
    except FileNotFoundError as e:
        return ProbeResult(short, 2, "", f"toolchain missing: {e}")
    except subprocess.TimeoutExpired:
        return ProbeResult(short, 1, "", "probe timed out")
