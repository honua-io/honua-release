"""Real virtualenv metadata controls; no registry, server, or virtualenv code is trusted by the probe."""
from pathlib import Path
import importlib.metadata as metadata
import json
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import python_environment_audit as probe
import run


def environment(root, version):
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    site = next((root / "lib").glob("python*/site-packages"))
    distribution = site / f"honua_sdk-{version}.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Metadata-Version: 2.1\nName: honua-sdk\nVersion: {version}\n")
    return site, distribution


@pytest.fixture
def admitted_default(monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    actual = metadata.distributions
    admitted = SimpleNamespace(metadata={"Name": "honua-sdk"}, version="0.1.16",
                               locate_file=lambda value: Path("/published/site-packages"))
    monkeypatch.setattr(probe.metadata, "distributions", lambda **kwargs: actual(**kwargs) if kwargs else [admitted])


@pytest.mark.parametrize("version,expected", [("0.1.16", "pass"), ("0.1.17", "fail")])
def test_executed_source_environment_cannot_hide_from_package_guard(tmp_path, monkeypatch, admitted_default, version, expected):
    root = tmp_path / "app" / ".venv-source"
    site, _ = environment(root, version)
    session = run.Session("venv", tmp_path, {"python": "python-pinned"}, "http://guard", tmp_path,
                          False, "host", "venv", "5.9.3")
    session.containers["python"] = "python-container"
    session.python_audit_interpreters["python"] = sys.executable
    executable = str(root / "bin/python")
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        assert args[args.index("python-container") + 1] == sys.executable
        result = probe.inventory([tmp_path], sorted(session.python_invocations.get("python", set())))
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    def shell(code, *args, **kwargs):
        session.retain_python_invocations("python", [executable])
        result = subprocess.run(["bash", "-c", code],
                                text=True, capture_output=True, check=True)
        return run.Outcome("pass", "exit code 0", stdout=result.stdout, exit_code=0)

    monkeypatch.setattr(run, "docker", docker)
    session.run_shell = shell
    report, _ = run.run_document({"runtime": "python"}, f'```bash\n{executable} -c "import importlib.metadata as m; print(m.version(\'honua-sdk\'))"\n```', session,
                                 {"_pins": {"honua-sdk": "0.1.16"}}, {"env": {}, "substitute": {}},
                                 "sha256:test", [], set())
    assert report["status"] == expected, report
    block = report["blocks"][0]
    assert any(row["path"] == str(site) and row["version"] == version for row in block["installedPackages"])
    assert block["pythonEnvironments"][0]["environment"] == str(root)
    assert block["exitCode"] == 0
    assert calls
    if expected == "fail":
        assert "honua-sdk 0.1.17 (admitted version: 0.1.16)" in block["detail"]


def test_broken_honua_metadata_refuses_inventory(tmp_path, admitted_default):
    _, distribution = environment(tmp_path / ".venv-source", "0.1.17")
    (distribution / "METADATA").write_text("Name: honua-sdk\n")
    with pytest.raises(ValueError, match="metadata is incomplete"):
        probe.inventory([tmp_path])


def test_missing_invoked_environment_refuses_inventory(tmp_path, admitted_default):
    with pytest.raises(ValueError, match="interpreter is unavailable"):
        probe.inventory([tmp_path], [str(tmp_path / ".venv-source/bin/python")])


def test_escaped_metadata_refuses_but_normal_interpreter_symlink_is_legal(tmp_path, admitted_default):
    _, distribution = environment(tmp_path / ".venv-source", "0.1.17")
    outside = tmp_path / "outside-metadata"
    outside.write_text("Name: honua-sdk\nVersion: 0.1.17\n")
    (distribution / "METADATA").unlink()
    (distribution / "METADATA").symlink_to(outside)
    with pytest.raises(ValueError, match="metadata escapes"):
        probe.inventory([tmp_path], [str(tmp_path / ".venv-source/bin/python")])


def test_directory_bound_refuses_instead_of_skipping(tmp_path, monkeypatch, admitted_default):
    (tmp_path / "nested").mkdir()
    monkeypatch.setattr(probe, "MAX_DIRECTORIES", 1)
    with pytest.raises(ValueError, match="directory bound"):
        probe.inventory([tmp_path])


def test_empty_positive_inventory_runs_without_venvs(tmp_path, admitted_default):
    result = probe.inventory([tmp_path])
    assert result["environments"] == []
    assert any(row["name"] == "honua-sdk" and row["version"] == "0.1.16" for row in result["packages"])


def test_dynamic_shell_paths_are_discovered_not_bound_as_literal_paths(tmp_path):
    session = run.Session("literal", tmp_path, {"python": "python-pinned"}, "http://guard", tmp_path,
                          False, "host", "literal", "5.9.3")
    session.retain_python_invocations("python", ["${VENV}/bin/python", "$VENV/bin/python", "./.venv-source/bin/python"])
    assert session.python_invocations["python"] == {str(tmp_path / "app/.venv-source/bin/python")}


@pytest.mark.parametrize("kind,text", [
    ("dist", "Name: \nVersion: 0.1.17\n"),
    ("dist", "Name: another-package\nVersion: 0.1.17\n"),
    ("dist", "Name: honua-sdk\nVersion: \n"),
    ("dist", "Name: honua-sdk\nName: another-package\nVersion: 0.1.17\n"),
    ("dist", "Name: honua-sdk\nVersion: 0.1.18\n"),
    ("egg-directory", "Name: \nVersion: 0.1.17\n"),
    ("egg-file", "Name: honua-sdk\nVersion: \n"),
])
def test_honua_metadata_cannot_disappear_or_contradict_installed_identity(tmp_path, admitted_default, kind, text):
    site_path, original = environment(tmp_path / ".venv-source", "0.1.17")
    if kind == "dist":
        target = original / "METADATA"
    else:
        target = site_path / "honua_sdk.egg-info"
        if kind == "egg-directory":
            target.mkdir()
            target /= "PKG-INFO"
    target.write_text(text)
    with pytest.raises(ValueError, match="metadata.*(incomplete|contradict)"):
        probe.inventory([tmp_path])


@pytest.mark.parametrize("directory", [False, True])
def test_valid_egg_metadata_is_retained(tmp_path, admitted_default, directory):
    site_path, _ = environment(tmp_path / ".venv-source", "0.1.17")
    target = site_path / "honua_admin.egg-info"
    if directory:
        target.mkdir()
        target /= "PKG-INFO"
    target.write_text("Metadata-Version: 2.1\nName: honua-admin\nVersion: 0.1.12\n")
    result = probe.inventory([tmp_path])
    assert any(row["name"] == "honua-admin" and row["version"] == "0.1.12" for row in result["packages"])


def test_trusted_cli_positive_inventory_executes(tmp_path):
    import os
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run([sys.executable, "-I", str(Path(probe.__file__)), str(tmp_path)],
                            text=True, capture_output=True, check=True,
                            env={"HOME": str(home), "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath})
    assert json.loads(result.stdout)["environments"] == []


@pytest.mark.parametrize("origin", [
    {"url": "https://reader:SECRET@example.invalid/source", "vcs_info": {"vcs": "git", "commit_id": "abc"}},
    {"url": "file:///private/source", "dir_info": {"editable": False}},
])
def test_same_version_known_source_origin_refuses_without_exposing_url(tmp_path, admitted_default, origin):
    _, distribution = environment(tmp_path / ".venv-source", "0.1.16")
    (distribution / "direct_url.json").write_text(json.dumps(origin))
    with pytest.raises(ValueError, match="direct-origin installation") as captured:
        probe.inventory([tmp_path])
    assert "SECRET" not in str(captured.value)
    assert origin["url"] not in str(captured.value)


@pytest.mark.parametrize("content", ["broken-json", "[]"])
def test_uninspectable_origin_refuses(tmp_path, admitted_default, content):
    _, distribution = environment(tmp_path / ".venv-source", "0.1.16")
    (distribution / "direct_url.json").write_text(content)
    with pytest.raises(ValueError, match="origin metadata"):
        probe.inventory([tmp_path])


def test_real_offline_pip_local_install_with_admitted_version_refuses(tmp_path, admitted_default):
    root = tmp_path / ".venv-source"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    project = tmp_path / "source"
    project.mkdir()
    (project / "pyproject.toml").write_text('[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n')
    (project / "backend.py").write_text('''import pathlib, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "honua_sdk-0.1.16-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as archive:
        archive.writestr("honua_sdk/__init__.py", "")
        archive.writestr("honua_sdk-0.1.16.dist-info/METADATA", "Metadata-Version: 2.1\\nName: honua-sdk\\nVersion: 0.1.16\\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/WHEEL", "Wheel-Version: 1.0\\nGenerator: offline-control\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/RECORD", "")
    return name
''')
    result = subprocess.run([sys.executable, "-m", "pip", "--python", str(root / "bin/python"), "install", "--no-index",
                             "--no-build-isolation", "--no-deps", str(project)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    origin = next((root / "lib").glob("python*/site-packages/honua_sdk-0.1.16.dist-info/direct_url.json"))
    assert "dir_info" in json.loads(origin.read_text())
    with pytest.raises(ValueError, match="direct-origin installation"):
        probe.inventory([tmp_path], [str(root / "bin/python")])


def test_real_offline_same_version_direct_wheel_cannot_bypass_registry_guard(tmp_path, admitted_default):
    import zipfile
    root = tmp_path / ".venv-wheel"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    wheel = tmp_path / "honua_sdk-0.1.16-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("honua_sdk/__init__.py", "ARBITRARY_CONTENT = True\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/METADATA", "Metadata-Version: 2.1\nName: honua-sdk\nVersion: 0.1.16\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: offline-control\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/RECORD", "")
    result = subprocess.run([sys.executable, "-m", "pip", "--python", str(root / "bin/python"), "install",
                             "--no-index", "--no-deps", str(wheel)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    origin = next((root / "lib").glob("python*/site-packages/honua_sdk-0.1.16.dist-info/direct_url.json"))
    assert "archive_info" in json.loads(origin.read_text())
    with pytest.raises(ValueError, match="direct-origin installation"):
        probe.inventory([tmp_path], [str(root / "bin/python")])


def test_https_archive_origin_refuses_without_exposing_credentials(tmp_path, admitted_default):
    _, distribution = environment(tmp_path / ".venv-wheel", "0.1.16")
    (distribution / "direct_url.json").write_text(json.dumps({
        "url": "https://reader:SECRET@example.invalid/honua.whl", "archive_info": {"hash": "sha256=unadmitted"}}))
    with pytest.raises(ValueError, match="direct-origin installation") as captured:
        probe.inventory([tmp_path])
    assert "SECRET" not in str(captured.value)
    assert "https://" not in str(captured.value)


@pytest.mark.parametrize("origin", [{}, {"url": "file:///unknown", "future_origin_info": {}}, {"url": "https://example.invalid/wheel"}])
def test_unknown_direct_origin_shapes_refuse(tmp_path, admitted_default, origin):
    _, distribution = environment(tmp_path / ".venv-source", "0.1.16")
    (distribution / "direct_url.json").write_text(json.dumps(origin))
    with pytest.raises(ValueError, match="direct-origin installation"):
        probe.inventory([tmp_path])
