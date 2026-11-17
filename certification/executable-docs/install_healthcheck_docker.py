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
    """Stands in for the server: records each raw request and sends one configured response."""

    requests: list[str] = []
    status = 200
    headers_out: dict[str, str] = {}
    body = b""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        cls = type(self)
        cls.requests.append(self.requestline + "\r\n" + str(self.headers))
        self.send_response(cls.status)
        for name, value in cls.headers_out.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(cls.body)))
        self.end_headers()
        self.wfile.write(cls.body)

    def log_message(self, *args) -> None:
        pass


def _license(mode: str) -> bytes:
    return json.dumps({"data": {"mode": mode}}).encode()


def _serve(status: int = 200, body: bytes = _license("disabled"),
           headers: dict[str, str] | None = None) -> http.server.HTTPServer:
    _LicenseStub.requests = []
    _LicenseStub.status, _LicenseStub.body = status, body
    _LicenseStub.headers_out = {"Content-Type": "application/json", **(headers or {})}
    server = http.server.HTTPServer(("127.0.0.1", 0), _LicenseStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _probe_env(directory: Path) -> dict[str, str]:
    return {"HOME": str(directory), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C",
            "HONUA_ADMIN_PASSWORD": DUMMY_KEY}


# Responses no probe may accept: an HTTP failure carrying the right body, the mode only in a header,
# and an enabled mode.
REFUSED = (
    {"status": 401},
    {"status": 503},
    {"status": 503, "body": b"", "headers": {"X-Diagnostic": '"mode":"disabled"'}},
    {"status": 200, "body": b"{}", "headers": {"X-Diagnostic": '"mode":"disabled"'}},
    {"status": 200, "body": _license("enabled")},
)


def _post_start_assertion(path: Path) -> str:
    """The pasted post-start block: a host-side request to the published port."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```sh\n(python3 - <<'PY'\n.*?/api/v1/admin/license.*?\nPY)\n```", text, re.DOTALL)
    assert match is not None, f"{path.name}: missing post-start licensing assertion"
    return match.group(1)


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
def test_install_healthcheck_uses_the_image_probe(guide: str, tmp_path: Path) -> None:
    # The server image has no shell, wget or curl; only its compiled probe can run.
    (tmp_path / "compose.yaml").write_text(_compose_example(ROOT / guide), encoding="utf-8")
    assert _created_healthcheck(tmp_path) == ["/app/honua-healthcheck", "--ready"]


@pytest.mark.parametrize("guide", GUIDES)
def test_post_start_assertion_requires_authenticated_disabled_mode(guide: str, tmp_path: Path) -> None:
    command = _post_start_assertion(ROOT / guide)
    assert "docker compose exec" not in command and "wget" not in command and "curl" not in command

    def run(**response) -> tuple[int, list[str]]:
        server = _serve(**response)
        try:
            local = command.replace("http://localhost:8080/", f"http://127.0.0.1:{server.server_port}/")
            assert local != command, "the assertion no longer targets the published port 8080"
            result = subprocess.run(["sh", "-c", local], cwd=tmp_path, env=_probe_env(tmp_path),
                                    capture_output=True, text=True, timeout=15, check=False)
            return result.returncode, list(_LicenseStub.requests)
        finally:
            server.shutdown()
            server.server_close()

    code, requests = run()
    assert code == 0, "post-start assertion did not accept the disabled-mode response"
    assert len(requests) == 1 and requests[0].startswith("GET /api/v1/admin/license ")
    # Header names are case-insensitive; the key must be sent once, expanded.
    assert requests[0].lower().count(f"x-api-key: {DUMMY_KEY}") == 1
    for response in REFUSED:
        code, _ = run(**response)
        assert code != 0, f"post-start assertion accepted {response}"
