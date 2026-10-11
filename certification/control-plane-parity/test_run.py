import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
