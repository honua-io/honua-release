import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

import run


@pytest.fixture
def github_cli_archive(tmp_path, monkeypatch):
    release = f"gh_{run.GH_VERSION}_linux_amd64"
    data = b"example GitHub CLI binary"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as package:
        member = tarfile.TarInfo(f"{release}/bin/gh")
        member.size = len(data)
        package.addfile(member, io.BytesIO(data))
    payload = archive.getvalue()

    def download(url, timeout):
        assert url == f"https://github.com/cli/cli/releases/download/v{run.GH_VERSION}/{release}.tar.gz"
        return io.BytesIO(payload)

    monkeypatch.setattr(run, "urlopen", download)
    monkeypatch.setattr(run, "docker", lambda *args, **kwargs: SimpleNamespace(stdout="x86_64\n"))
    monkeypatch.setitem(run.GH_LINUX_SHA256, "amd64", hashlib.sha256(payload).hexdigest())
    return data


def test_document_github_cli_is_mounted_read_only(tmp_path, github_cli_archive):
    run.prepare_github_cli(tmp_path)
    binary = tmp_path / "gh"
    assert binary.read_bytes() == github_cli_archive
    assert binary.stat().st_mode & 0o111

    session = run.Session("docs", tmp_path / "session", {}, "", tmp_path, False, "host", "test", "",
                          prerequisites={"gh"})
    assert session.prerequisite_runtimes("python") == (["-v", f"{binary}:/opt/github-cli/gh:ro"],
                                                        "/opt/github-cli:")
    session.prerequisites.clear()
    assert session.prerequisite_runtimes("python") == ([], "")


def test_github_cli_rejects_an_unverified_archive(tmp_path, monkeypatch, github_cli_archive):
    monkeypatch.setitem(run.GH_LINUX_SHA256, "amd64", "0" * 64)
    with pytest.raises(run.RunError, match="archive checksum mismatch"):
        run.prepare_github_cli(tmp_path)
    assert not (tmp_path / "gh").exists()
