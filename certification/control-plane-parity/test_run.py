import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import copy
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cleanup import validate_owned_object

spec = importlib.util.spec_from_file_location("parity_run", HERE / "run.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


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
    assert set(pins) == {"@honua/sdk-js", "@honua/mcp-server"}
    for key in ("honua-sdk-js", "honua-mcp-server"):
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
    for key in ("honua-sdk-js", "honua-mcp-server"):
        changed = copy.deepcopy(manifest)
        changed["clientArtifacts"][key][field] = value
        with pytest.raises(ValueError):
            replay.selected_clients(changed)


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


def test_fixture_is_exact_published_source_input():
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
