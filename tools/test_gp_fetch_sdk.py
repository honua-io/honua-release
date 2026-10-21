"""Exact SDK selection and archive integrity remain mandatory on source-free runners."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gp_fetch_sdk as sdk

VERSION = "10.0.302"
ARCHIVE = b"complete official SDK archive fixture"
URL = f"https://builds.dotnet.microsoft.com/dotnet/Sdk/{VERSION}/dotnet-sdk-{VERSION}-linux-x64.tar.gz"


def metadata():
    entry = {"version": VERSION, "files": [{"rid": "linux-x64", "name": "dotnet-sdk-linux-x64.tar.gz",
             "url": URL, "hash": hashlib.sha512(ARCHIVE).hexdigest()}]}
    return {"releases": [{"sdk": entry, "sdks": [copy.deepcopy(entry)]}]}


def test_exact_sdk_can_appear_in_both_primary_and_sdk_list():
    assert sdk.select(metadata(), VERSION) == (URL, hashlib.sha512(ARCHIVE).hexdigest())


@pytest.mark.parametrize("version", ["latest", "10.0.x", "10.0.302-preview.1", "9.0.302", "10.0.999"])
def test_missing_or_nonexact_versions_are_rejected(version):
    with pytest.raises(sdk.InputError):
        sdk.select(metadata(), version)


@pytest.mark.parametrize("url", [
    URL.replace("builds.dotnet.microsoft.com", "example.com"),
    URL.replace("https://", "http://"),
    URL.replace("https://", "https://user@"),
    URL.replace("10.0.302/", "10.0.301/"),
    URL + "?token=credential",
    URL + "#fragment",
])
def test_archive_cannot_escape_official_exact_version_path(url):
    document = metadata()
    for entry in [document["releases"][0]["sdk"], *document["releases"][0]["sdks"]]:
        entry["files"][0]["url"] = url
    with pytest.raises(sdk.InputError, match="official release path"):
        sdk.select(document, VERSION)


def test_conflicting_official_entries_fail_closed():
    document = metadata()
    document["releases"][0]["sdks"][0]["files"][0]["hash"] = "0" * 128
    with pytest.raises(sdk.InputError, match="one exact"):
        sdk.select(document, VERSION)


def install_opener(monkeypatch, archive):
    class Opener:
        def open(self, url, timeout):
            assert timeout == 90
            if url == sdk.METADATA:
                return io.BytesIO(json.dumps(metadata()).encode())
            assert url == URL
            return io.BytesIO(archive)
    monkeypatch.setattr(sdk, "build_opener", lambda *_: Opener())


def test_acquisition_records_independent_hashes_and_exact_download(monkeypatch, tmp_path):
    install_opener(monkeypatch, ARCHIVE)
    output = tmp_path / "sdk"
    receipt = sdk.acquire(VERSION, output)
    assert (output / receipt["file"]).read_bytes() == ARCHIVE
    assert receipt["sha256"] == hashlib.sha256(ARCHIVE).hexdigest()
    assert receipt["sha512"] == hashlib.sha512(ARCHIVE).hexdigest()
    assert json.loads((output / "official-receipt.json").read_text()) == receipt
    assert hashlib.sha256((output / "official-metadata.json").read_bytes()).hexdigest() == receipt["metadataSha256"]


def test_tampered_download_never_becomes_an_installable_archive(monkeypatch, tmp_path):
    install_opener(monkeypatch, b"substituted archive")
    output = tmp_path / "sdk"
    with pytest.raises(sdk.InputError, match="Microsoft's SHA-512"):
        sdk.acquire(VERSION, output)
    assert not list(output.glob("*.tar.gz"))
    assert not list(output.glob("*.partial"))
    assert not (output / "official-receipt.json").exists()
