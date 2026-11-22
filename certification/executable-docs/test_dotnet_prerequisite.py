"""A document that lists .NET as a prerequisite gets the pinned SDK image's .NET install."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run  # noqa: E402

SOURCES = json.loads((HERE / "sources.json").read_text())


def _record(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "cid\n" if args[0] == "create" else "", "")

    monkeypatch.setattr(run, "docker", docker)
    return calls


def _session(tmp_path, prerequisites: set[str]) -> run.Session:
    return run.Session("samples", tmp_path, SOURCES["runtimes"], "http://guard", tmp_path / "tools", False,
                       "host", "test", SOURCES["toolchain"]["typescript"], prerequisites=prerequisites)


def test_samples_readme_declares_the_dotnet_sdk_it_restores_with():
    doc = next(d for d in SOURCES["documents"] if d["repo"] == "honua-io/honua-samples" and d["path"] == "README.md")
    assert doc["runtime"] == "node"
    assert doc["prerequisites"]["dotnet"].startswith("README.md:")
    assert "The .NET 11 SDK" in doc["prerequisites"]["dotnet"]


def test_pinned_sdk_supports_the_samples_target_framework():
    sdk = re.fullmatch(r"mcr.microsoft.com/dotnet/sdk:(\d+)\.\d+@sha256:[0-9a-f]{64}",
                       SOURCES["runtimes"]["dotnet"])
    assert sdk is not None
    assert int(sdk.group(1)) >= 11, "The samples checkout targets net11.0"


def test_node_container_mounts_the_sdk_when_dotnet_is_a_prerequisite(tmp_path, monkeypatch):
    calls = _record(monkeypatch)
    session = _session(tmp_path, {"dotnet"})
    session.container("node")
    invocation = next(args for args in calls if args[0] == "run")
    assert f"{session.tools / 'dotnet'}:/opt/dotnet:ro" in invocation
    assert "DOTNET_ROOT=/opt/dotnet" in invocation
    assert any(arg.startswith("PATH=/opt/dotnet:") for arg in invocation)
    session.close()


def test_containers_without_the_prerequisite_get_no_sdk_mount(tmp_path, monkeypatch):
    calls = _record(monkeypatch)
    for prerequisites, runtime in ((set(), "node"), ({"dotnet"}, "dotnet")):
        session = _session(tmp_path / runtime, prerequisites)
        session.container(runtime)
        session.close()
    assert not any("/opt/dotnet" in arg for args in calls for arg in args)


def test_sdk_is_copied_once_from_the_pinned_image(tmp_path, monkeypatch):
    calls = _record(monkeypatch)
    image = SOURCES["runtimes"]["dotnet"]
    run.copy_dotnet_sdk(image, tmp_path)
    assert calls == [("create", image), ("cp", "cid:/usr/share/dotnet", str(tmp_path)), ("rm", "-f", "cid")]
    (tmp_path / "dotnet").mkdir()
    (tmp_path / "dotnet" / "dotnet").write_text("")
    calls.clear()
    run.copy_dotnet_sdk(image, tmp_path)
    assert calls == []


def test_samples_readme_xvfb_prerequisite_installs_xvfb_and_xauth(tmp_path, monkeypatch):
    doc = next(d for d in SOURCES["documents"] if d["repo"] == "honua-io/honua-samples" and d["path"] == "README.md")
    calls = _record(monkeypatch)
    session = _session(tmp_path, set(doc["prerequisites"]))
    session.container("node")
    installs = [args[-1] for args in calls if args[:3] == ("exec", "--user", "0")]
    assert len(installs) == 1 and installs[0].split(" && ")[1].endswith("jq python-is-python3 python3-venv sudo xauth xvfb")
    session.close()
