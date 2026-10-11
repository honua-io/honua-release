"""Source build closure never substitutes for installed candidate package identity."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blocks import extract
from registry_guard import Guard
import source_build as sb
from run import Session

SHA = "a" * 40
NAME = "@honua/sdk"
TARBALL = b"reviewed old source dependency"
SRI = "sha512-" + base64.b64encode(hashlib.sha512(TARBALL).digest()).decode()
URL = "https://registry.npmjs.org/@honua/sdk/-/sdk-0.1.2-beta.0.tgz"
TEXT = f"```bash\n{sb.CLONE}\ncd honua-sdk-js\nnpm ci\n```\n\n```bash\nnpm run demo:quickstart:mock -- --evidence-once\ncd ..\n```\n\n```bash\nnpm install @honua/sdk-js\n```\n"


def inputs():
    return {"package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
        "node_modules/@honua/sdk": {"version": "0.1.2-beta.0", "integrity": SRI, "resolved": URL}}}).encode(),
        "mcp/package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {"node_modules/@honua/sdk-js": {"link": True, "resolved": ".."}}}).encode(),
        "package.json": b'{"name":"@honua/sdk-js","version":"0.1.15"}'}


def binding():
    return sb.derive({"repo": sb.SDK_REPO, "path": "README.md", "sourceBuild": True}, SHA, TEXT, inputs().__getitem__)


def test_admission_binds_document_source_block_lock_and_dependency_paths():
    bound = binding()
    assert bound["sourceSha"] == SHA
    assert bound["documentSha256"] == sb.digest(TEXT.encode())
    assert [b["index"] for b in bound["blocks"]] == [0, 1]
    assert bound["locks"][0]["sha256"] == sb.digest(inputs()["package-lock.json"])
    assert bound["locks"][0]["packages"] == [{"path": "node_modules/@honua/sdk", "name": NAME,
        "version": "0.1.2-beta.0", "integrity": SRI, "resolved": URL}]
    assert bound["locks"][1]["packages"][0]["sourcePackageSha256"] == sb.digest(inputs()["package.json"])
    duplicate = extract(TEXT, "markdown")[0]
    duplicate.index = 99
    with pytest.raises(ValueError, match="outside"):
        sb.bind_code(bound, duplicate, duplicate.code, Path("/source"))
    with pytest.raises(ValueError, match="outside"):
        sb.bind_code(bound, extract(TEXT, "markdown")[2], extract(TEXT, "markdown")[2].code, Path("/source"))


@pytest.mark.parametrize("field,value", [("version", "^0.1.2"), ("integrity", ""), ("resolved", "file:../other")])
def test_unbound_source_dependency_is_refused(field, value):
    lock = json.loads(inputs()["package-lock.json"])
    lock["packages"]["node_modules/@honua/sdk"][field] = value
    with pytest.raises(ValueError):
        sb.lock_record("package-lock.json", json.dumps(lock).encode(), inputs().__getitem__)


def test_moving_source_and_unconfigured_document_are_refused():
    with pytest.raises(ValueError, match="immutable"):
        sb.derive({"repo": sb.SDK_REPO, "path": "README.md", "sourceBuild": True}, "trunk", TEXT, inputs().__getitem__)
    with pytest.raises(ValueError, match="not configured"):
        sb.derive({"repo": "other/repo", "path": "README.md", "sourceBuild": True}, SHA, TEXT, inputs().__getitem__)


def test_real_git_source_and_lock_preflight_refuses_changed_inputs(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, raw in inputs().items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True)
    bound = binding()
    bound["sourceSha"] = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    def verify():
        return subprocess.run(["bash", "-e", "-c", sb.verification_script(bound, tmp_path)]).returncode
    assert verify() == 0
    old = bound["sourceSha"]
    bound["sourceSha"] = SHA
    assert verify() != 0
    bound["sourceSha"] = old
    (tmp_path / "package-lock.json").write_text("{}")
    assert verify() != 0
    (tmp_path / "package-lock.json").unlink()
    assert verify() != 0


class Handler:
    def __init__(self, path):
        self.path, self.headers, self.code, self.body, self.wfile = path, {}, None, b"", self
    def send_response(self, code): self.code = code
    def send_header(self, *_): pass
    def end_headers(self): pass
    def write(self, data): self.body += data


def test_source_registry_is_exact_and_does_not_widen_candidate_namespace(tmp_path, monkeypatch):
    raw = sb.canonical(binding())
    identity = sb.digest(raw)
    path = tmp_path / f"source-{identity}.json"
    path.write_bytes(raw)
    guard = Guard({"npm": {NAME: "0.1.15"}, "pypi": {}, "nuget": {}}, source_dir=tmp_path)
    served = {"bytes": TARBALL}
    packument = {"versions": {"0.1.2-beta.0": {"dist": {"integrity": SRI}, "dependencies": {"@honua/unreviewed": "*"}}, "0.1.15": {"dist": {"integrity": "new"}}}}
    monkeypatch.setattr(guard, "_fetch", lambda url, accept=None: (200, served["bytes"] if url.endswith(".tgz") else json.dumps(packument).encode(), "application/json"))
    def get(route):
        handler = Handler(route)
        guard.handle(handler)
        return handler
    try:
        prefix = f"/source/{identity}/npm/"
        old = get(prefix + "@honua/sdk")
        assert old.code == 200
        assert list(json.loads(old.body)["versions"]) == ["0.1.2-beta.0"]
        assert get(prefix + "@honua/sdk/-/sdk-0.1.2-beta.0.tgz").code == 200
        assert get(prefix + "%40honua%2Fsdk").code == 200
        assert get(prefix + "@honua%2fsdk/-/sdk-0.1.2-beta.0.tgz").code == 200
        assert get(prefix + "@honua/sdk/-/sdk-0.1.3.tgz").code == 404
        assert get(prefix + "@honua/unreviewed").code == 404
        assert get(prefix + "@honua%252Fsdk").code == 404
        assert get(prefix + "%2F@honua/sdk").code == 404
        assert get(prefix + "other/../@honua/sdk").code == 404
        assert get("/npm/@honua/sdk/-/sdk-0.1.2-beta.0.tgz").code == 404
        assert list(json.loads(get("/npm/@honua/sdk").body)["versions"]) == ["0.1.15"]
        served["bytes"] = b"tampered"
        assert get(prefix + "@honua/sdk/-/sdk-0.1.2-beta.0.tgz").code == 404
        path.write_bytes(raw + b" ")
        assert get(prefix + "@honua/sdk").code == 404
    finally:
        guard.server.server_close()


def test_inventory_keeps_nested_versions_and_source_audit_checks_path_and_integrity(tmp_path):
    session = Session("paths", tmp_path, {"node": "node-pinned"}, "http://guard", tmp_path, False, "host", "paths", "5")
    session.containers["node"] = "unused"
    root = Path(session.cwd)
    packages = {"node_modules/@honua/sdk": "0.1.2-beta.0", "node_modules/tool/node_modules/@honua/sdk": "0.1.15"}
    for relative, version in packages.items():
        p = root / relative / "package.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"name": NAME, "version": version}))
    (root / "node_modules/.package-lock.json").write_text(json.dumps({"packages": {key: {"integrity": SRI} for key in packages}}))
    rows = session.installed_honua("node")
    assert {row["version"] for row in rows} == {"0.1.2-beta.0", "0.1.15"}
    assert len({row["path"] for row in rows}) == 2
    errors = sb.audit_source(binding(), root, rows)
    assert len(errors) == 1 and "tool/node_modules" in errors[0]
    expected = next(row for row in rows if row["version"] == "0.1.2-beta.0")
    assert sb.audit_source(binding(), root, [expected]) == []
    assert sb.audit_source(binding(), root, [{**expected, "integrity": "wrong"}])


@pytest.mark.parametrize("value", [[], None, {"locks": [None]}, {"locks": [{"packages": [None]}]},
    {"locks": [{"packages": ["not-an-object"]}]}, {"locks": [{"packages": [{"name": [], "version": 1}]}]}])
def test_malformed_source_admission_is_refused_without_upstream(tmp_path, monkeypatch, value):
    raw = json.dumps(value).encode()
    identity = sb.digest(raw)
    (tmp_path / f"source-{identity}.json").write_bytes(raw)
    guard = Guard({"npm": {}, "pypi": {}, "nuget": {}}, source_dir=tmp_path)
    def unexpected(*args):
        raise AssertionError("malformed admission must not fetch upstream")
    monkeypatch.setattr(guard, "_fetch", unexpected)
    try:
        handler = Handler(f"/source/{identity}/npm/@honua/sdk")
        guard.handle(handler)
        assert handler.code == 404
    finally:
        guard.server.server_close()


def test_inventory_keeps_mcp_link_path_and_verifies_target(tmp_path):
    session = Session("link", tmp_path, {"node": "node-pinned"}, "http://guard", tmp_path, False, "host", "link", "5")
    session.containers["node"] = "unused"
    root = Path(session.cwd) / "honua-sdk-js"
    root.mkdir()
    (root / "package.json").write_bytes(inputs()["package.json"])
    modules = root / "mcp/node_modules"
    link = modules / "@honua/sdk-js"
    link.parent.mkdir(parents=True)
    link.symlink_to(root, target_is_directory=True)
    rows = session.installed_honua("node", (modules,))
    assert [row["path"] for row in rows] == [str(link)]
    assert sb.audit_source(binding(), root, rows) == []
    other = tmp_path / "other"
    other.mkdir()
    (other / "package.json").write_bytes(inputs()["package.json"])
    link.unlink()
    link.symlink_to(other, target_is_directory=True)
    assert "link target" in sb.audit_source(binding(), root, rows)[0]


@pytest.mark.parametrize("failure", [None, "after-lock", "exception"])
def test_source_environment_is_restored_before_following_runtime_block(tmp_path, failure):
    from types import SimpleNamespace
    from run import Outcome, run_document
    calls = []
    state = {"done": False}
    session = SimpleNamespace(workdir=tmp_path, env={}, passed={}, container=lambda runtime: "node",
                              installed_honua=lambda runtime, extra=(): [])
    def shell(code, *args):
        calls.append((code, dict(session.env)))
        if failure == "exception" and not state["done"]:
            state["done"] = True
            raise RuntimeError("fixture failure")
        return Outcome("pass", "exit0", exit_code=0)
    session.run_shell = shell
    session._exec = lambda *args: Outcome("fail" if failure == "after-lock" else "pass", "lock verification")
    result, _ = run_document({"runtime": "node"}, TEXT, session,
        {"_pins": {}, "_source_build": binding(), "_source_registry": "http://guard/source/exact/npm/"},
        {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert calls[0][1]["npm_config_registry"].endswith("/source/exact/npm/")
    assert "fetch -q --depth 1 origin " + SHA in calls[0][0]
    assert "npm_config_registry" not in calls[-1][1]
    assert "YARN_NPM_REGISTRY_SERVER" not in session.env
    assert result["blocks"][-1].get("sourceBuild") is None
    if failure:
        assert result["blocks"][0]["status"] == "fail"


def test_real_http_guard_accepts_npm_encoded_scoped_requests(tmp_path, monkeypatch):
    import urllib.request
    bound = binding()
    raw = sb.canonical(bound)
    identity = sb.digest(raw)
    (tmp_path / f"source-{identity}.json").write_bytes(raw)
    packument = {"versions": {"0.1.2-beta.0": {"dist": {"integrity": SRI}}}}
    guard = Guard({"npm": {}, "pypi": {}, "nuget": {}}, source_dir=tmp_path)
    monkeypatch.setattr(guard, "_fetch", lambda url, accept=None: (
        200, TARBALL if url.endswith(".tgz") else json.dumps(packument).encode(), "application/json"))
    guard.start()
    try:
        with urllib.request.urlopen(guard.base + f"/source/{identity}/npm/@honua%2fsdk") as response:
            body = json.load(response)
        tarball = body["versions"]["0.1.2-beta.0"]["dist"]["tarball"]
        assert f"/source/{identity}/npm/" in tarball
        with urllib.request.urlopen(tarball.replace("@honua/sdk", "@honua%2fsdk")) as response:
            assert response.read() == TARBALL
    finally:
        guard.stop()


def test_source_registry_never_reads_caller_paths_or_symlinked_admissions(tmp_path, monkeypatch):
    raw = sb.canonical(binding())
    identity = sb.digest(raw)
    root = tmp_path / "guard"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    (root / f"source-{identity}.json").symlink_to(outside)
    guard = Guard({"npm": {}, "pypi": {}, "nuget": {}}, source_dir=root)
    monkeypatch.setattr(guard, "_fetch", lambda *_: pytest.fail("unadmitted request reached registry"))
    try:
        handler = Handler(f"/source/{identity}/npm/@honua/sdk")
        guard.handle(handler)
        assert handler.code == 404
        for untrusted in ["../../outside", "/outside", "../" + identity, "%2e%2e/outside"]:
            handler = Handler("unused")
            guard._source_npm(handler, untrusted, "@honua/sdk", False)
            assert handler.code == 404
    finally:
        guard.server.server_close()
