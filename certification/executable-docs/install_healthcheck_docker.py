"""Check the two customer Compose probes after real Compose and shell expansion."""

import http.server
import io
import json
import os
import re
import subprocess
import tarfile
import threading
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
GUIDES = ("docs/INSTALL-2026.1.md", "docs/CUSTOMER-INSTALL.md")
DUMMY_KEY = "dummy-install-healthcheck-key"


def _compose_example(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<!-- doc-run: file=compose\.yaml -->\s*```yaml\n(.*?)\n```", text, re.DOTALL)
    assert match is not None, f"{path.name}: missing executable Compose example"
    return match.group(1) + "\n"


class _LicenseStub(http.server.BaseHTTPRequestHandler):
    """Stands in for the server: records each raw request and answers with a fixed licensing mode."""

    requests: list[str] = []
    mode = "disabled"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).requests.append(self.requestline + "\r\n" + str(self.headers))
        body = json.dumps({"data": {"mode": type(self).mode}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _run_probe(argv: list[str], directory: Path, mode: str = "disabled") -> tuple[int, list[str]]:
    # The documented probe opens /dev/tcp/127.0.0.1/8080 inside the server container. Point that one
    # address at a local stub; the probe cannot reach a server or print a credential.
    _LicenseStub.requests = []
    _LicenseStub.mode = mode
    server = http.server.HTTPServer(("127.0.0.1", 0), _LicenseStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert argv[:2] == ["bash", "-c"], argv
        script = argv[2].replace("/dev/tcp/127.0.0.1/8080", f"/dev/tcp/127.0.0.1/{server.server_port}")
        assert script != argv[2], "the probe no longer targets the container's own port 8080"
        env = {
            "HOME": str(directory),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "HONUA_ADMIN_PASSWORD": DUMMY_KEY,
        }
        result = subprocess.run(["bash", "-c", script, *argv[3:]], cwd=directory, env=env,
                                capture_output=True, text=True, timeout=5, check=False)
        return result.returncode, list(_LicenseStub.requests)
    finally:
        server.shutdown()
        server.server_close()


def _created_healthcheck(directory: Path) -> list[str]:
    # Use the real Compose container-create path, which resolves $$ before sending
    # Config.Healthcheck.Test to Docker. `compose config` serializes $$ for another
    # Compose read and is not the command a created container receives.
    tag = f"honua-execdocs-health-probe:{uuid.uuid4().hex}"
    project = f"execdocsprobe{uuid.uuid4().hex[:12]}"
    archive = directory / "scratch.tar"
    with tarfile.open(archive, "w") as tar:
        marker = tarfile.TarInfo("probe-marker")
        marker.size = 1
        tar.addfile(marker, io.BytesIO(b"x"))

    env = {
        "HOME": str(directory),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "HONUA_ADMIN_PASSWORD": DUMMY_KEY,
        "HONUA_MASTER_KEY": "a" * 64,
        "HONUA_SERVER_IMAGE": tag,
    }

    def docker(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", *args], cwd=directory, env=env,
                              capture_output=True, text=True, timeout=30, check=False)

    imported = docker("import", "--change", 'CMD ["/probe-marker"]', str(archive), tag)
    assert imported.returncode == 0, "could not import the local scratch probe image"
    compose = ("compose", "--project-name", project, "-f", "compose.yaml")
    try:
        created = docker(*compose, "up", "--no-start", "--no-deps", "--pull", "never", "honua")
        assert created.returncode == 0, "Compose could not create the documented Honua service"
        container = docker(*compose, "ps", "-aq", "honua")
        assert container.returncode == 0 and container.stdout.strip(), "Compose created no Honua container"
        inspected = docker("inspect", "--format", "{{json .Config.Healthcheck.Test}}",
                           container.stdout.strip())
        assert inspected.returncode == 0, "could not inspect the created healthcheck"
        command_type, *argv = json.loads(inspected.stdout)
        assert command_type == "CMD"
        return argv
    finally:
        docker(*compose, "down", "--remove-orphans")
        docker("image", "rm", "-f", tag)


@pytest.mark.parametrize("guide", GUIDES)
def test_install_probe_expands_admin_key_after_compose(guide: str, tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(_compose_example(ROOT / guide), encoding="utf-8")
    argv = _created_healthcheck(tmp_path)
    # The server image ships bash but no wget or curl (honua-server#6798).
    assert not any(tool in arg for arg in argv for tool in ("wget", "curl"))
    code, requests = _run_probe(argv, tmp_path)
    assert code == 0, "health probe did not accept the dummy disabled-mode response"
    assert len(requests) == 1 and requests[0].startswith("GET /api/v1/admin/license HTTP/1.0")
    assert requests[0].count(f"X-API-Key: {DUMMY_KEY}") == 1
    assert "${HONUA_ADMIN_PASSWORD}" not in requests[0] and "$HONUA_ADMIN_PASSWORD" not in requests[0]

    # An enabled licensing mode must keep the server unhealthy.
    code, _ = _run_probe(argv, tmp_path, mode="enabled")
    assert code != 0, "health probe accepted an enabled licensing mode"

    # A single-quoted key reference must remain a failing control: it sends the literal name.
    quoted = argv[:2] + [argv[2].replace('"$HONUA_ADMIN_PASSWORD"', "'$HONUA_ADMIN_PASSWORD'")] + argv[3:]
    assert quoted != argv
    _, old_requests = _run_probe(quoted, tmp_path)
    assert f"X-API-Key: {DUMMY_KEY}" not in old_requests[0]
    assert "X-API-Key: $HONUA_ADMIN_PASSWORD" in old_requests[0]
