"""One platform version on every surface: server, OCI label, Console, CLI and docs."""
import json
import urllib.error

import pytest

import no_redirect
import platform_version_surfaces as surfaces

REVISION = "c" * 40


def observed(version="2026.1.0"):
    return {"sourceRevision": REVISION,
            "server": {"capabilities": f"{version}+ccccccc", "admin": version},
            "oci": version,
            "console": {"name": "honua-console", "version": "0.1.0", "platformVersion": version},
            "cli": {"version": "5.0.1", "platformVersion": version},
            "docs": {"platformVersion": version}}


def test_matching_surfaces_pass_and_component_versions_keep_their_own_policy():
    receipt = surfaces.compare("2026.1.0", observed())
    assert receipt["status"] == "pass"
    assert receipt["expectedVersion"] == "2026.1.0"
    assert set(receipt["surfaces"]) == {"server.capabilities", "server.admin", "oci", "console", "cli", "docs"}
    assert all(row["match"] for row in receipt["surfaces"].values())


def test_rc_label_names_the_rc_artifact_version():
    assert surfaces.compare("honua-2026.1-rc.2", observed("2026.1.0-rc.2"))["status"] == "pass"


@pytest.mark.parametrize("path,value", [
    (("server", "capabilities"), "2026.1.0+ddddddd"),
    (("server", "admin"), "2026.1.0-rc.2"),
    (("oci",), "2026.1.0+ccccccc"),
    (("oci",), None),
    (("console", "platformVersion"), "0.1.0"),
    (("cli", "platformVersion"), 2026),
    (("docs", "platformVersion"), None),
])
def test_any_stale_missing_or_foreign_surface_fails(path, value):
    data = observed()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    receipt = surfaces.compare("2026.1.0", data)
    assert receipt["status"] == "fail"
    name = ".".join(path[:2]) if path[0] == "server" else path[0]
    assert receipt["why"] == f"surfaces do not report 2026.1.0: {name}"


@pytest.mark.parametrize("surface", ["server", "console", "cli", "docs"])
def test_an_absent_surface_fails(surface):
    data = observed()
    del data[surface]
    receipt = surfaces.compare("2026.1.0", data)
    assert receipt["status"] == "fail"
    assert surface in receipt["why"]


def test_console_package_version_does_not_stand_in_for_the_platform_version():
    data = observed()
    data["console"] = {"name": "honua-console", "version": "2026.1.0"}
    assert surfaces.compare("2026.1.0", data)["why"] == "surfaces do not report 2026.1.0: console"


def test_non_platform_label_and_non_object_observations_fail_closed():
    assert surfaces.compare("nightly", observed())["status"] == "fail"
    assert surfaces.compare("2026.1.0", [])["status"] == "fail"


def test_cli_writes_the_receipt_and_exits_by_status(tmp_path, capsys):
    source, out = tmp_path / "observed.json", tmp_path / "receipt.json"
    source.write_text(json.dumps(observed()), encoding="utf-8")
    assert surfaces.main(["compare", "--label", "2026.1.0", "--observed", str(source), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"
    source.write_text(json.dumps(observed("2026.1.1")), encoding="utf-8")
    assert surfaces.main(["compare", "--label", "2026.1.0", "--observed", str(source)]) == 1
    assert '"status": "fail"' in capsys.readouterr().out
    source.write_text("not json", encoding="utf-8")
    assert surfaces.main(["compare", "--label", "2026.1.0", "--observed", str(source)]) == 1


SERVER, CONSOLE, DOCS = "http://server:8080/", "http://console:5274", "https://docs.example/version.json"
IMAGE = "ghcr.io/honua-io/honua-server@sha256:" + "7" * 64


def documents():
    return {"http://server:8080/api/v1/admin/capabilities": {"data": {"serverVersion": "2026.1.0+ccccccc"}},
            "http://server:8080/api/v1/admin/version": {"data": {"version": "2026.1.0+ccccccc"}},
            "http://console:5274/version.json": {"version": "0.1.0", "platformVersion": "2026.1.0"},
            DOCS: {"platformVersion": "2026.1.0"}}


def collect_args(tmp_path, **overrides):
    cli = tmp_path / "cli.json"
    cli.write_text(json.dumps({"version": "5.0.1", "platformVersion": "2026.1.0"}), encoding="utf-8")
    values = dict(server=SERVER, image=IMAGE, source_revision=REVISION, console=CONSOLE, cli_json=cli, docs=DOCS)
    values.update(overrides)
    return type("Args", (), values)


def test_collect_observes_every_surface_without_writing_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HONUA_API_KEY", "secret-key")
    seen = {}

    def fetch(url, headers=None):
        seen[url] = headers
        return documents().get(url)

    result = surfaces.collect(collect_args(tmp_path), fetch=fetch, label=lambda image: "2026.1.0")
    assert surfaces.compare("2026.1.0", result)["status"] == "pass"
    assert seen["http://server:8080/api/v1/admin/version"] == {"X-API-Key": "secret-key"}
    assert "secret-key" not in json.dumps(result)


def test_collect_records_unreachable_surfaces_as_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("HONUA_API_KEY", raising=False)
    args = collect_args(tmp_path, console=None, cli_json=tmp_path / "missing.json", docs=None)
    result = surfaces.collect(args, fetch=lambda url, headers=None: None, label=lambda image: None)
    assert result["server"] == {"capabilities": None, "admin": None}
    assert result["oci"] is None and result["cli"] is None
    assert "console" not in result and "docs" not in result
    receipt = surfaces.compare("2026.1.0", result)
    assert receipt["why"] == ("surfaces do not report 2026.1.0: cli, console, docs, oci, "
                              "server.admin, server.capabilities")


def test_oci_label_reads_docker_and_treats_failure_as_absent():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "2026.1.0\n"})

    assert surfaces.oci_label(IMAGE, run=run) == "2026.1.0"
    assert calls[0][:3] == ["docker", "image", "inspect"] and calls[0][-1] == IMAGE
    failed = type("Result", (), {"returncode": 1, "stdout": "2026.1.0"})
    assert surfaces.oci_label(IMAGE, run=lambda command, **kwargs: failed) is None


def test_fetch_json_treats_transport_and_decoding_failures_as_absent():
    assert surfaces.fetch_json("http://127.0.0.1:9/version.json") is None


def test_redirects_are_refused_so_the_api_key_stays_on_its_origin():
    import urllib.request
    request = urllib.request.Request("https://candidate.example/api/v1/admin/version",
                                     headers={"X-API-Key": "secret-key"})
    handler = no_redirect.NoRedirect()
    assert handler.redirect_request(request, None, 302, "Found", {}, "http://other.example/version") is None
    redirect = urllib.error.HTTPError("https://candidate.example", 302, "Found", {}, None)

    class Redirecting:
        def open(self, request, timeout):
            raise redirect

    assert surfaces.fetch_json("https://candidate.example/v", {"X-API-Key": "k"}, opener=Redirecting()) is None


def test_collect_keeps_only_identity_fields_of_surface_documents(tmp_path, monkeypatch):
    monkeypatch.delenv("HONUA_API_KEY", raising=False)
    leaked = {"version": "0.1.0", "platformVersion": "2026.1.0", "token": "secret-token", "areas": ["x"]}
    result = surfaces.collect(collect_args(tmp_path), fetch=lambda url, headers=None: dict(leaked),
                              label=lambda image: "2026.1.0")
    assert result["console"] == result["docs"] == {"version": "0.1.0", "platformVersion": "2026.1.0"}
    assert "secret-token" not in json.dumps(result)
