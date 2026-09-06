"""Exercise customer commands against independently assembled package/ledger fixtures."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest
import yaml

import compat_check
import release_inspect
from test_compat_check import DIGEST, RECEIPT, endpoint_lock
from test_release_inspect import ledger_for

CLI = Path(__file__).resolve().parents[1] / "honua"


def run_cli(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          text=True, capture_output=True, check=False)


def package_bytes(kind, code=b"return 42\n"):
    stream = io.BytesIO()
    if kind == "tgz":
        # A dependency metadata file precedes the package's own metadata. The
        # expected identity comes from the package root, never archive order.
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, body in [
                ("package/node_modules/other/package.json", b'{"name":"other","version":"9.0.0"}'),
                ("package/package.json", b'{"name":"Example.Client","version":"1.0.0"}'),
                ("package/index.js", code),
            ]:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    else:
        with zipfile.ZipFile(stream, "w") as archive:
            if kind == "whl":
                archive.writestr("example.dist-info/METADATA", "Name: Example.Client\nVersion: 1.0.0\n")
            else:
                archive.writestr("example.nuspec", '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata><id>Example.Client</id><version>1.0.0</version></metadata></package>')
            archive.writestr("implementation", code)
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["tgz", "whl", "nupkg"])
def test_cli_certifies_only_the_receipted_package_bytes(tmp_path, kind):
    original = package_bytes(kind)
    expected_digest = "sha256:" + hashlib.sha256(original).hexdigest()
    archive = tmp_path / f"client.{kind}"
    archive.write_bytes(original)
    ledger = {"ledgerVersion": "compatibility-ledger.v1", "platformLocks": {},
              "componentReleases": {}, "artifactReceipts": [], "upgradeEdges": [],
              "experimentalExclusions": [], "clientServerCertifications": [{
                  "serverDigest": DIGEST, "client": {"component": "sdk",
                      "coordinate": "Example.Client", "identity": "1.0.0", "sha256": expected_digest},
                  "result": "certified", "receipt": RECEIPT}]}
    ledger_path = tmp_path / "ledger.yaml"
    ledger_path.write_text(yaml.safe_dump(ledger))
    args = ["compat", "check", DIGEST, archive, "--ledger", ledger_path, "--json"]
    result = run_cli(*args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "certified", "serverDigest": DIGEST,
        "client": {"coordinate": "Example.Client", "identity": "1.0.0", "sha256": expected_digest},
        "receipt": RECEIPT}

    # Change executable content, preserving package name and version.
    archive.write_bytes(package_bytes(kind, b"return -1\n"))
    result = run_cli(*args)
    assert result.returncode == 1, result.stderr
    rejected = json.loads(result.stdout)
    assert rejected["status"] == "not-certified"
    assert rejected["receipt"] is None
    assert rejected["client"]["sha256"] != expected_digest

    # Historical version-only receipts cannot certify a local artifact either.
    archive.write_bytes(original)
    del ledger["clientServerCertifications"][0]["client"]["sha256"]
    ledger_path.write_text(yaml.safe_dump(ledger))
    result = run_cli(*args)
    assert result.returncode == 1
    assert json.loads(result.stdout)["receipt"] is None


def test_ambiguous_wheel_is_refused(tmp_path):
    archive = tmp_path / "client.whl"
    with zipfile.ZipFile(archive, "w") as output:
        for name in ("one", "two"):
            output.writestr(f"{name}.dist-info/METADATA", f"Name: {name}\nVersion: 1.0.0\n")
    result = run_cli("compat", "check", DIGEST, archive)
    assert result.returncode == 2
    assert "exactly one unambiguous" in result.stderr
    assert not result.stdout


def test_coordinate_lookup_does_not_alias_different_package_case():
    from test_compat_check import ledger
    assert compat_check.check(DIGEST, {"coordinate": "example.client", "identity": "1.0.0"}, ledger())["status"] == "not-certified"


def test_inspect_cli_resolves_reverse_releases_and_upgrade_receipts(tmp_path):
    source = endpoint_lock()
    target = copy.deepcopy(source)
    target["platform"]["id"] = "honua-2026.1-rc.2"
    target["components"]["honua-server"]["artifacts"][0]["digest"] = "sha256:" + "7" * 64
    ledger = ledger_for(source)
    other = ledger_for(target)
    ledger["platformLocks"].update(other["platformLocks"])
    for name, digests in other["componentReleases"].items():
        ledger["componentReleases"][name].extend(digests)
    source_digest, target_digest = list(ledger["platformLocks"])
    edge = {"fromLockDigest": source_digest, "toLockDigest": target_digest,
            "receipt": RECEIPT, "rollback": {"result": "fail", "receipt": RECEIPT}}
    ledger["upgradeEdges"] = [edge]
    ledger_path, lock_path = tmp_path / "ledger.yaml", tmp_path / "lock.yaml"
    ledger_path.write_text(yaml.safe_dump(ledger))
    lock_path.write_text(yaml.safe_dump(source))
    result = run_cli("release", "inspect", lock_path, "--ledger", ledger_path, "--json")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["upgradeEdges"] == [edge]
    sdk = next(c for c in output["components"] if c["name"] == "sdk")
    assert [r["platform"] for r in sdk["certifiedReleases"]] == [
        "honua-2026.1-rc.1", "honua-2026.1-rc.2"]
    server = next(c for c in output["components"] if c["name"] == "honua-server")
    assert [r["lockDigest"] for r in server["certifiedReleases"]] == [source_digest]


def test_inspect_cannot_treat_console_image_as_server():
    lock = endpoint_lock()
    lock["components"]["honua-console"] = copy.deepcopy(lock["components"]["honua-server"])
    console_digest = "sha256:" + "9" * 64
    lock["components"]["honua-console"]["artifacts"][0]["digest"] = console_digest
    ledger = ledger_for(lock)
    ledger["clientServerCertifications"] = [{"serverDigest": console_digest, "client": {
        "component": "sdk", "coordinate": "Example.Client", "identity": "1.0.0"},
        "result": "certified", "receipt": RECEIPT}]
    assert release_inspect.inspect(lock, ledger)["clientServerCertifications"] == []
