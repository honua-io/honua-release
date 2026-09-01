from __future__ import annotations

import io
import json
import tarfile

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


def test_scoped_coordinate_and_local_npm_package(tmp_path):
    assert compat_check.resolve_client("@honua/sdk-js@0.0.12-alpha.0") == {
        "coordinate": "@honua/sdk-js", "identity": "0.0.12-alpha.0"}
    package = tmp_path / "sdk.tgz"
    payload = json.dumps({"name": "@honua/sdk-js", "version": "0.0.12-alpha.0"}).encode()
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json"); info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    assert compat_check.resolve_client(str(package))["identity"] == "0.0.12-alpha.0"
