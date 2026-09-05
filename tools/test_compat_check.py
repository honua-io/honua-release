from __future__ import annotations

import io
import json
import tarfile
import copy
import zipfile

import pytest

from test_platform_lock import valid_lock
from unittest.mock import patch

import compat_check


DIGEST = "sha256:" + "1" * 64
RECEIPT = {"schema": "github-actions-run/v1", "uri": "https://github.com/honua/run/1", "sha256": "sha256:" + "2" * 64}


def ledger(result="certified", identity="1.0.0"):
    return {"clientServerCertifications": [{"serverDigest": DIGEST, "client": {
        "component": "sdk", "coordinate": "Example.Client", "identity": identity},
        "result": result, "receipt": RECEIPT}]}


def test_certified_exact_receipt_hit():
    result = compat_check.check(DIGEST, {"coordinate": "Example.Client", "identity": "1.0.0"}, ledger())
    assert result["status"] == "certified"
    assert result["receipt"] == RECEIPT


def test_absence_is_not_certified():
    result = compat_check.check("sha256:" + "3" * 64, {"coordinate": "Example.Client", "identity": "1.0.0"}, ledger())
    assert result["status"] == "not-certified"
    assert result["receipt"] is None


def test_version_match_without_pair_receipt_is_not_certified():
    value = {"clientServerCertifications": [], "artifactReceipts": [{"component": "sdk",
        "coordinate": "Example.Client", "identity": "1.0.0", "receipt": RECEIPT}]}
    assert compat_check.check(DIGEST, {"coordinate": "Example.Client", "identity": "1.0.0"}, value)["status"] == "not-certified"


def test_explicit_negative_receipt_is_incompatible():
    assert compat_check.check(DIGEST, {"coordinate": "Example.Client", "identity": "1.0.0"}, ledger("incompatible"))["status"] == "incompatible"


def test_server_endpoint_selects_honua_server_image():
    server_digest = "sha256:" + "4" * 64
    console_digest = "sha256:" + "5" * 64
    lock = endpoint_lock(server_digest)
    lock["components"]["honua-console"] = copy.deepcopy(lock["components"]["honua-server"])
    lock["components"]["honua-console"]["artifacts"][0]["digest"] = console_digest
    with patch.object(compat_check.release_inspect, "load_source", return_value=(lock, "endpoint")):
        assert compat_check.resolve_server("https://example.invalid") == server_digest


def test_scoped_coordinate_and_local_npm_package(tmp_path):
    assert compat_check.resolve_client("@honua/sdk-js@0.0.12-alpha.0") == {
        "coordinate": "@honua/sdk-js", "identity": "0.0.12-alpha.0"}
    package = tmp_path / "sdk.tgz"
    payload = json.dumps({"name": "@honua/sdk-js", "version": "0.0.12-alpha.0"}).encode()
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json"); info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    assert compat_check.resolve_client(str(package))["identity"] == "0.0.12-alpha.0"


def endpoint_lock(digest=DIGEST):
    lock = valid_lock()
    server = copy.deepcopy(lock["components"]["sdk"])
    server["artifacts"][0].update(kind="image", digest=digest,
                                architectures=["amd64"], platformDigests={"amd64": digest})
    lock["components"]["honua-server"] = server
    return lock


@pytest.mark.parametrize("field", ["Name", "Version"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_wheel_missing_identity_is_refused(tmp_path, capsys, field, value):
    headers = {"Name": "Example.Client", "Version": "1.0.0"}
    headers[field] = value
    package = tmp_path / "client.whl"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("client.dist-info/METADATA", "".join(
            f"{key}: {val}\n" for key, val in headers.items() if val is not None))
    with pytest.raises(compat_check.CompatError, match="Name and Version"):
        compat_check.resolve_client(str(package))
    assert compat_check.main([DIGEST, str(package)]) == 2
    output = capsys.readouterr()
    assert "REFUSED:" in output.err
    assert "Traceback" not in output.err
    assert not output.out


def test_valid_wheel_identity(tmp_path):
    package = tmp_path / "client.whl"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("client.dist-info/METADATA", "Name: Example.Client\nVersion: 1.0.0\n")
    assert compat_check.resolve_client(str(package)) == {"coordinate": "Example.Client", "identity": "1.0.0"}


@pytest.mark.parametrize("mutation", ["truncated", "version", "components", "baseline"])
def test_endpoint_invalid_lock_cannot_use_matching_receipt(capsys, mutation):
    lock = endpoint_lock()
    if mutation == "truncated":
        lock = {"components": lock["components"]}
    elif mutation == "version":
        lock["lockVersion"] = "platform-lock.v2"
    elif mutation == "components":
        lock["components"] = ["honua-server"]
    else:
        del lock["components"]["honua-sdk-js"]["serverCompatibility"]
    with patch.object(compat_check.release_inspect, "load_source", return_value=(lock, "endpoint")), \
         patch.object(compat_check.release_inspect, "load_ledger", return_value=ledger()):
        assert compat_check.main(["https://example.invalid", "Example.Client@1.0.0"]) == 2
    output = capsys.readouterr()
    assert "REFUSED: invalid server platform lock" in output.err
    assert not output.out
