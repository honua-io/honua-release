from __future__ import annotations

import copy
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

import release_inspect
import validate_compatibility_ledger
from test_platform_lock import DIGEST, REVISION, valid_lock


def ledger_for(lock, receipt=True):
    digest = release_inspect.canonical_digest(lock)
    receipts = []
    if receipt:
        receipts.append({"schema": "honua.certification-evidence-receipt/v2", "uri": "https://evidence.invalid/r", "sha256": DIGEST})
    return {
        "ledgerVersion": "compatibility-ledger.v1",
        "platformLocks": {digest: {"platformLock": copy.deepcopy(lock), "releaseArtifacts": [], "certifications": receipts}},
        "componentReleases": {"sdk": [digest]},
        "clientServerCertifications": [], "upgradeEdges": [], "experimentalExclusions": [],
    }


def test_exact_digest_resolves_identity_and_receipt():
    lock = valid_lock(); result = release_inspect.inspect(lock, ledger_for(lock))
    assert result["ledgerRecord"] == "resolved"
    assert result["certified"] is True
    assert result["components"][0]["artifacts"][0]["identity"] == "sha512-YWJjZA=="


def test_absent_receipt_is_never_certified_even_for_same_versions():
    lock = valid_lock(); result = release_inspect.inspect(lock, ledger_for(lock, receipt=False))
    assert result["ledgerRecord"] == "resolved"
    assert result["certified"] is False
    assert "no receipt" in release_inspect.render(result)
    changed = copy.deepcopy(lock); changed["notes"] = "different immutable release notes"
    assert release_inspect.inspect(changed, ledger_for(lock))["ledgerRecord"] == "absent"
    assert release_inspect.inspect(changed, ledger_for(lock))["certified"] is False


def test_server_endpoint_uses_well_known_lock(tmp_path):
    lock = valid_lock(); body = yaml.safe_dump(lock).encode()
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        loaded, _ = release_inspect.load_source(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown(); thread.join()
    assert loaded == lock
    assert seen == [release_inspect.WELL_KNOWN_PATH]


def test_client_server_certification_requires_receipt_edge():
    lock = valid_lock()
    image = {"kind": "image", "coordinate": "ghcr.io/honua/server", "version": "1.2.3", "sourceRevision": REVISION, "digest": DIGEST, "architectures": ["amd64"]}
    lock["components"]["sdk"]["artifacts"] = [image]
    ledger = ledger_for(lock, receipt=False)
    result = release_inspect.inspect(lock, ledger)
    assert result["clientServerCertifications"] == []


def test_ledger_validator_enforces_digest_key_and_reverse_edges():
    lock = valid_lock(); ledger = ledger_for(lock)
    assert validate_compatibility_ledger.validate(ledger) == []
    digest = release_inspect.canonical_digest(lock)
    ledger["platformLocks"]["sha256:" + "f" * 64] = ledger["platformLocks"].pop(digest)
    errors = validate_compatibility_ledger.validate(ledger)
    assert any("key does not match" in error for error in errors)
    assert any("exactly reverse" in error for error in errors)


def test_schema_references_packet_66_lock_schema():
    schema = yaml.safe_load((release_inspect.REPO_ROOT / "schemas/compatibility-ledger.v1.schema.json").read_text())
    lock_ref = schema["$defs"]["platformLockRecord"]["properties"]["platformLock"]["$ref"]
    assert lock_ref == "platform-lock.v1.schema.json"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ledger, digest: ledger["platformLocks"][digest].update(certifications=[{"bogus": True}]),
        lambda ledger, _digest: ledger.pop("clientServerCertifications"),
        lambda ledger, digest: ledger["platformLocks"][digest]["platformLock"].pop("components"),
    ],
)
def test_load_ledger_refuses_schema_invalid_evidence(tmp_path, mutate):
    lock = valid_lock()
    ledger = ledger_for(lock)
    mutate(ledger, release_inspect.canonical_digest(lock))
    path = tmp_path / "ledger.yaml"
    path.write_text(yaml.safe_dump(ledger))

    with pytest.raises(release_inspect.InspectError, match="invalid compatibility ledger"):
        release_inspect.load_ledger(path)
