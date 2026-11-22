"""Exercise the HTTP probe and prove failed preservation cannot pass GA admission."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import threading

import pytest
import yaml

import compose_upgrade_credentials as credentials
import compose_upgrade_prior as cu
from test_compose_upgrade_version import evidence


CONNECTION_ID = "00000000-0000-0000-0000-000000000001"
KEY_ID = "00000000-0000-0000-0000-000000000002"
ADMIN = "fixture-admin-secret"
SECRET = "fixture-issued-secret"


@pytest.fixture
def service():
    state = {
        "connection": dict(connectionId=CONNECTION_ID, name="e2e-pg", host="db", port=5432,
                           databaseName="honua", username="honua", provider="PostGIS",
                           sslRequired=False, sslMode="Disable", storageType="Encrypted", isActive=True),
        "apiKey": dict(id=KEY_ID, name="upgrade-test", keyPrefix="test", permissions=["admin:read"],
                       status="active", createdAt="2026-11-22T00:00:00Z"),
        "healthy": True, "keyWorks": True, "keys": True, "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request(None)

        def do_POST(self):
            self.handle_request(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def handle_request(self, body):
            key = self.headers.get("X-API-Key")
            state["requests"].append((self.path, key, body))
            allowed = key == ADMIN or (key == SECRET and state["keyWorks"] and body is None)
            self.send_response(200 if allowed else 401)
            self.end_headers()
            result = response(self.path, body) if allowed else {"error": SECRET}
            self.wfile.write(json.dumps(result).encode())

    def response(path, body):
        if path.endswith("/test"):
            return {"data": {"connectionId": CONNECTION_ID, "isHealthy": state["healthy"]}}
        if "/connections/" in path:
            return {"data": state["connection"]}
        if body is not None:
            assert body["permissions"] == ["admin:read"]
            return {"data": {"key": SECRET, "apiKey": state["apiKey"]}}
        return {"data": [state["apiKey"]] if state["keys"] else []}

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def seeded(service):
    api = credentials.Api(service["base"], ADMIN)
    return api, credentials.seed(api, {"connectionId": CONNECTION_ID})


def test_verify_uses_old_key_and_tests_saved_credential_without_a_password(service):
    api, state = seeded(service)
    service["requests"].clear()
    proof = credentials.verify(api, state)
    assert proof["status"] == "pass"
    assert credentials.credential_evidence({"candidate_version": "2026.1.0"}, {"credentials": proof}) == {
        "status": "pass", "connectionId": CONNECTION_ID, "apiKeyId": KEY_ID,
        "checks": dict.fromkeys(credentials.CHECKS, True)}
    assert service["requests"] == [
        (f"/api/v1/admin/connections/{CONNECTION_ID}", SECRET, None),
        (f"/api/v1/admin/connections/{CONNECTION_ID}/test", ADMIN, {}),
        ("/api/v1/admin/api-keys/", ADMIN, None),
    ]
    assert SECRET not in json.dumps(proof) and ADMIN not in json.dumps(proof)


@pytest.mark.parametrize("fault", ["revoked-key", "missing-key", "lost-password", "changed-connection", "changed-key"])
def test_preservation_failures_are_rejected(service, fault):
    api, state = seeded(service)
    if fault == "revoked-key":
        service["keyWorks"] = False
    elif fault == "missing-key":
        service["keys"] = False
    elif fault == "lost-password":
        service["healthy"] = False
    elif fault == "changed-connection":
        service["connection"]["databaseName"] = "different"
    else:
        service["apiKey"]["permissions"] = ["admin:write"]
    with pytest.raises(credentials.ProbeError) as error:
        credentials.verify(api, state)
    assert SECRET not in str(error.value)
    assert ADMIN not in str(error.value)


def test_seed_refuses_an_unhealthy_starting_connection_before_minting_key(service):
    service["healthy"] = False
    with pytest.raises(credentials.ProbeError, match="did not authenticate"):
        seeded(service)
    assert not any(path.endswith("/api-keys/") for path, _, _ in service["requests"])


def test_cli_keeps_secrets_private_and_cannot_reuse_a_stale_pass(service, tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"connectionId": CONNECTION_ID}))
    private = tmp_path / "private-state.json"
    receipt = tmp_path / "public-receipt.json"
    monkeypatch.setenv("E2E_BASE", service["base"])
    monkeypatch.setenv("E2E_API_KEY", ADMIN)
    arguments = ["--state", str(private), "--manifest", str(manifest), "--receipt", str(receipt)]
    monkeypatch.setattr("sys.argv", ["probe", "seed", *arguments])
    assert credentials.main() == 0
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert SECRET in private.read_text()
    assert json.loads(receipt.read_text())["status"] == "fail"
    monkeypatch.setattr("sys.argv", ["probe", "verify", *arguments])
    assert credentials.main() == 0
    assert json.loads(receipt.read_text())["status"] == "pass"
    service["keyWorks"] = False
    assert credentials.main() == 1
    assert json.loads(receipt.read_text())["status"] == "fail"
    assert SECRET not in receipt.read_text() and ADMIN not in receipt.read_text()


def test_private_state_creation_never_overwrites_existing_file(tmp_path):
    private = tmp_path / "state"
    private.write_text("existing")
    with pytest.raises(FileExistsError):
        credentials.write_private(private, {"key": SECRET})
    assert private.read_text() == "existing"


def proof():
    return {"schema": 1, "status": "pass", "connectionId": CONNECTION_ID, "apiKeyId": KEY_ID,
            "checks": dict.fromkeys(credentials.CHECKS, True)}


def receipt(observed):
    pair, phases = evidence()
    return cu.build_receipt(pair, scenario="normal", status="pass", why="harness passed",
                            observed=observed, phases=phases, harness={}, run="run", commit="commit")


@pytest.mark.parametrize("value", [None, {}, [], "pass", {"status": "pass"}, {"schema": 1, "status": "fail"}])
def test_absent_or_malformed_proof_rejects_otherwise_passing_ga(value):
    result = receipt({"credentials": value})
    assert result["status"] == "fail"
    assert result["why"].startswith("credential preservation refused:")


@pytest.mark.parametrize("check", credentials.CHECKS)
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_each_functional_assertion_is_required_as_a_boolean(check, value):
    fragment = proof()
    fragment["checks"][check] = value
    assert receipt({"credentials": fragment})["status"] == "fail"


@pytest.mark.parametrize("field", ["connectionId", "apiKeyId"])
def test_proof_is_bound_to_real_persisted_identifiers(field):
    fragment = proof()
    fragment[field] = "missing"
    assert receipt({"credentials": fragment})["status"] == "fail"


def test_passing_receipt_only_contains_the_sanitized_evidence():
    fragment = proof()
    fragment["key"] = SECRET
    result = receipt({"credentials": fragment})
    assert result["status"] == "pass"
    assert SECRET not in json.dumps(result["credentialEvidence"])


def test_workflow_enables_probe_for_ga_and_secret_state_is_outside_artifacts():
    workflow = yaml.safe_load(Path(".github/workflows/gate-upgrade.yml").read_text())
    steps = workflow["jobs"]["compose-upgrade"]["steps"]
    smoke = next(step for step in steps if step.get("id") == "smoke")
    assert smoke["env"]["COMPOSE_UPGRADE_CREDENTIAL_PROOF"] == "${{ steps.pair.outputs.candidate_version == '2026.1.0' }}"
    shell = Path("e2e/harness/upgrade-chaos.sh").read_text()
    assert 'CU_CREDENTIAL_STATE="$(mktemp -d)"' in shell
    assert 'rm -rf -- "$CU_CREDENTIAL_STATE"' in shell
    assert shell.index("cu_credentials seed ||") < shell.index('cu_identity candidate "$upgrade_image"')
    assert shell.index('cu_identity candidate "$upgrade_image"') < shell.index("cu_credentials verify ||")
