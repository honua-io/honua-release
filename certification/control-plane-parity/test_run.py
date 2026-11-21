import hashlib
import http.server
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

import pytest
import copy
import requests
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cleanup import validate_owned_object
from loopback_cookies import LoopbackSecureContextPolicy, LoopbackSession

spec = importlib.util.spec_from_file_location("parity_run", HERE / "run.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

CLIENT_KEYS = ("honua-sdk-js", "honua-mcp-server", "honua-app-platform")
# Entry points @honua/sdk-js 0.4.0 removed in favour of @honua/app-platform.
MOVED_SDK_SUBPATHS = ("app", "app-controller", "app-workspace", "scene-workspace", "collaboration", "control-plane",
                      "replica-sync", "share", "operate", "generated-app", "studio", "operator", "controls",
                      "web-components")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mount permissions are enforced by the Linux replay")
def test_private_umask_does_not_hide_idp_mount_roots(tmp_path):
    run = tmp_path / "private"
    run.mkdir(mode=0o700)
    secret = run / "credentials.json"
    secret.write_text("private fixture credential")
    secret.chmod(0o600)
    for name in ("certs", "import"):
        (run / name).mkdir(mode=0o700)
    replay.make_idp_mounts_readable(run)
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    for name in ("certs", "import"):
        assert stat.S_IMODE((run / name).stat().st_mode) == 0o755


@pytest.mark.parametrize("image,source", [
    ("ghcr.io/honua-io/honua-server:nightly", "a" * 40),
    ("ghcr.io/another/server@sha256:" + "b" * 64, "a" * 40),
    ("ghcr.io/honua-io/honua-server@sha256:" + "b" * 64, "trunk"),
])
def test_mutable_or_foreign_image_refused(image, source):
    with pytest.raises(ValueError):
        replay.check_identity(image, source)


def test_immutable_source_and_digest_accepted():
    replay.check_identity("ghcr.io/honua-io/honua-server:nightly-a916167@sha256:" + "b" * 64, "a" * 40)


def test_pair_uses_reviewed_independent_versions_and_full_identities():
    manifest = yaml.safe_load((replay.ROOT / "platform-manifest.yaml").read_text())
    pins = replay.selected_clients(manifest)
    assert set(pins) == {"@honua/sdk-js", "@honua/mcp-server", "@honua/app-platform"}
    for key in CLIENT_KEYS:
        row = manifest["clientArtifacts"][key]
        assert pins[row["package"]] == {field: row[field] for field in ("version", "sourceSha", "integrity", "filename")}
    # MCP is independently reviewed, never advanced to SDK's version implicitly.
    manifest["clientArtifacts"]["honua-mcp-server"]["version"] = "0.1.999"
    assert replay.selected_clients(manifest)["@honua/mcp-server"]["version"] == "0.1.999"


@pytest.mark.parametrize("field,value", [
    ("version", "latest"), ("version", "^0.1.16"), ("sourceSha", "trunk"),
    ("publicationState", "unpublished"), ("package", "@foreign/sdk-js"),
    ("repository", "foreign/repository"), ("integrity", "sha512-missing"),
])
def test_pair_rejects_unpublished_mutable_or_foreign_identity(field, value):
    manifest = yaml.safe_load((replay.ROOT / "platform-manifest.yaml").read_text())
    for key in CLIENT_KEYS:
        changed = copy.deepcopy(manifest)
        changed["clientArtifacts"][key][field] = value
        with pytest.raises(ValueError):
            replay.selected_clients(changed)


def test_scripts_import_moved_entry_points_from_app_platform():
    # Importing a removed @honua/sdk-js forwarder crashes the whole replay script.
    imported = re.compile(r"""@honua/sdk-js/([^"'/]+)""")
    for script in sorted(HERE.glob("*.mjs")):
        for subpath in imported.findall(script.read_text()):
            assert subpath not in MOVED_SDK_SUBPATHS, f"{script.name}: @honua/sdk-js/{subpath}"


def test_pair_refuses_component_source_mismatch():
    manifest = yaml.safe_load((replay.ROOT / "platform-manifest.yaml").read_text())
    manifest["components"]["honua-sdk-js"]["sha"] = "a" * 40
    with pytest.raises(ValueError, match="component"):
        replay.selected_clients(manifest)


@pytest.mark.parametrize("verification_passes", [False, True])
def test_artifact_verification_precedes_runtime_and_only_it_receives_github_token(tmp_path, monkeypatch, verification_passes):
    run = tmp_path / "private"
    out = tmp_path / "public"
    for key, value in {
        "HONUA_SERVER_IMAGE": "ghcr.io/honua-io/honua-server@sha256:" + "b" * 64,
        "HONUA_SERVER_SOURCE": "a" * 40, "HONUA_PARITY_RUN": str(run),
        "HONUA_PARITY_OUT": str(out), "HONUA_PARITY_BUCKET": "owned",
        "GH_TOKEN": "test-only-verifier-credential",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(replay.subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(replay, "cleanup", lambda *args: {"status": "passed"})
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            assert args[1] == str(replay.ROOT / "tools/verify_client_artifacts.py")
            assert kwargs["env"]["GH_TOKEN"] == "test-only-verifier-credential"
            assert kwargs["check"]
            if verification_passes:
                return subprocess.CompletedProcess(args, 0)
        else:
            assert args[0] == "bash"  # fixture rendering is only reached after verification
            assert all(key not in kwargs["env"] for key in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_GH_TOKEN"))
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(replay.subprocess, "run", execute)
    assert replay.main() == 1
    assert len(calls) == (2 if verification_passes else 1)
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["stages"][0]["status"] == ("passed" if verification_passes else "failed")
    assert "test-only-verifier-credential" not in (run / "run.env").read_text()


def test_private_bearers_and_signed_urls_removed_from_retained_output():
    text = 'password=p455word https://bucket/x?X-Amz-Signature=abc eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl'
    retained = replay.sanitize(text, ["p455word"])
    assert "p455word" not in retained
    assert "X-Amz-" not in retained
    assert "eyJ" not in retained
    assert "<private-bearer>" in retained


def test_fixture_is_pinned_published_source_with_server_format():
    provenance = json.loads((HERE / "map-envelope-provenance.json").read_text())
    assert provenance["sourceSha"] == "c6484209dda9b1533fefcae1b0e47d87b56414c2"
    assert hashlib.sha256((HERE / "map-envelope.json").read_bytes()).hexdigest() == provenance["sha256"]


@pytest.mark.parametrize("owned", [
    {"bucket": "foreign", "key": "sdk1424/20261011/00000000-0000-0000-0000-000000000000/fixture.geojson"},
    {"bucket": "owned", "key": "another-prefix/fixture.geojson"},
])
def test_cleanup_cannot_delete_another_object(owned):
    with pytest.raises(ValueError):
        validate_owned_object(owned, "owned")

from principal_claims import check_principal


def _read_admin():
    return [{'type': kind, 'value': value} for kind, value in [
        ('sub', 'owned-subject'), ('preferred_username', 'admin-read-a'),
        ('tenant_id', 'tenant-a'), ('roles', 'admin'), ('roles', 'user'), ('scp', 'honua.mcp.read')]]


def test_genuine_read_scope_and_override_refusal_required():
    check_principal('admin-read-a', _read_admin(), 200, 403)
    with pytest.raises(AssertionError, match='override'):
        check_principal('admin-read-a', _read_admin(), 200, 200)


@pytest.mark.parametrize('kind,value', [('tenant_id', 'tenant-b'), ('scp', 'honua.mcp.create'), ('preferred_username', 'someone-else')])
def test_wrong_authenticated_identity_or_scope_refused(kind, value):
    claims = _read_admin()
    next(item for item in claims if item['type'] == kind)['value'] = value
    with pytest.raises(AssertionError):
        check_principal('admin-read-a', claims, 200, 403)


@pytest.mark.parametrize("status,body,expected", [
    (403, "", {"rawBody": ""}),
    (502, "upstream unavailable", {"rawBody": "upstream unavailable"}),
    (202, '{"jobId":"real-job"}', {"jobId": "real-job"}),
    (409, '{"status":409}', {"status": 409}),
])
def test_import_receipt_retains_wire_body_without_changing_status(status, body, expected):
    import requests
    from response_receipt import response_body
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    assert response_body(response) == expected
    assert response.status_code == status


@pytest.mark.parametrize("decoder_error", [True, False])
def test_import_receipt_catches_requests_decoder_only(decoder_error):
    from requests.exceptions import JSONDecodeError
    from response_receipt import response_body

    class Response:
        text = "not-json"

        def json(self):
            if decoder_error:
                raise JSONDecodeError("Invalid JSON", self.text, 0)
            raise ValueError("unrelated failure")

    if decoder_error:
        assert response_body(Response()) == {"rawBody": "not-json"}
    else:
        with pytest.raises(ValueError, match="unrelated failure"):
            response_body(Response())


class _SecureCookieServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (self.headers.get("Cookie") or "").encode()
        if self.path != "/redirect":
            self.send_response(200)
        if self.path == "/set":
            self.send_header("Set-Cookie", "honua_admin_pending=p; Path=/; Secure; HttpOnly; SameSite=Lax")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/echo")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def secure_cookie_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _SecureCookieServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path", ["/echo", "/redirect"])
@pytest.mark.parametrize("session_type,expected", [(LoopbackSession, "honua_admin_pending=p"), (requests.Session, "")])
def test_loopback_session_returns_secure_cookie_over_loopback_http(secure_cookie_server, session_type, expected, path):
    with session_type() as session:
        session.trust_env = False
        session.get(secure_cookie_server + "/set", timeout=5).close()
        assert session.get(secure_cookie_server + path, timeout=5).text == expected


def test_secure_cookie_policy_reaches_loopback_http_and_https():
    assert _secure_cookie_header("http://127.0.0.1:19816/api/v1/admin/auth/session") == "honua_admin_session=s"
    assert _secure_cookie_header("https://kc.example.test/realms/honua") == "honua_admin_session=s"


def _secure_cookie_header(url):
    jar = CookieJar(LoopbackSecureContextPolicy())
    host = urllib.parse.urlsplit(url).hostname
    jar.set_cookie(Cookie(0, "honua_admin_session", "s", None, False, host, False, False, "/", True, True,
                          None, False, None, None, {}))
    request = urllib.request.Request(url)
    jar.add_cookie_header(request)
    return request.get_header("Cookie")


def test_secure_cookie_policy_withholds_non_loopback_http():
    assert _secure_cookie_header("http://kc.example.test/realms/honua") is None


@pytest.mark.parametrize("name", ["run.py", "parity-published.mjs", "studio-tenants-published.mjs"])
def test_deployment_audit_reads_use_the_untenanted_admin_key(name):
    # Under multi-tenancy a tenant admin's bearer is refused on the deployment-wide audit routes.
    lines = [line for line in (HERE / name).read_text().splitlines() if "/observability/audit" in line]
    assert lines
    for line in lines:
        assert "Bearer" not in line and re.search(r"platform|X-API-Key", line), line


def test_session_approval_sends_only_the_servers_admin_cookies(monkeypatch):
    monkeypatch.syspath_prepend(str(replay.ROOT / "e2e/harness/lib"))
    spec = importlib.util.spec_from_file_location("approve_session", HERE / "approve-session.py")
    approve_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(approve_session)
    saved = {"honua_admin_session": "s", "honua_admin_csrf": "c", "KEYCLOAK_SESSION": "k", "AUTH_SESSION_ID": "a"}
    assert approve_session.session_cookies(saved) == {"honua_admin_session": "s", "honua_admin_csrf": "c"}
