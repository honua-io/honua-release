"""Saved maps must remain readable and unchanged; missing proof fails GA admission."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import threading

import pytest
import yaml

import compose_upgrade_maps as maps
import compose_upgrade_prior as cu


IDS = dict(draftId="00000000-0000-0000-0000-000000000001",
           itemId="00000000-0000-0000-0000-000000000002",
           versionId="00000000-0000-0000-0000-000000000003")
MANIFEST = {"slice1": {"e2e_src_fs": {"service": "e2e", "layerId": 17}}}
SECRET = "fixture-admin-secret"


@pytest.fixture
def service():
    state = {"durable": True, "requests": [], "missing": False, "authorized": True}

    def response(path, body):
        if path.endswith("/package-families"):
            return {"durable": state["durable"]}
        if body is not None and path.endswith("/package-drafts"):
            state["draft"] = {**IDS, **body, "family": "map", "generation": 1,
                              "ownerId": "admin", "createdAt": "2026-11-22T00:00:00Z",
                              "updatedAt": "2026-11-22T00:00:00Z"}
            return state["draft"]
        if body is not None:
            state["version"] = {**deepcopy(state["draft"]), "versionNumber": 1,
                                "contentHash": "f" * 64, "sourceDraftId": IDS["draftId"], **body}
            return state["version"]
        return state["version"] if "/versions/" in path else state["draft"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request(None)

        def do_POST(self):
            self.handle_request(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def handle_request(self, body):
            state["requests"].append((self.path, body))
            allowed = state["authorized"] and self.headers.get("X-API-Key") == SECRET
            status = 401 if not allowed else 404 if state["missing"] else 200
            self.send_response(status)
            self.end_headers()
            value = {"data": response(self.path, body)} if status == 200 else {"error": SECRET}
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def seeded(service):
    api = maps.StudioApi(service["base"], SECRET)
    return api, maps.seed(api, MANIFEST)


def test_reads_same_draft_and_immutable_version_without_recreating_them(service):
    api, state = seeded(service)
    service["requests"].clear()
    proof = maps.verify(api, state)
    assert proof["status"] == "pass"
    assert service["requests"] == [
        ("/api/v1/studio/package-families", None),
        (f"/api/v1/studio/package-drafts/{IDS['draftId']}", None),
        (f"/api/v1/studio/content-items/{IDS['itemId']}/versions/{IDS['versionId']}", None)]
    assert proof["draftSnapshotSha256"] == maps.snapshot_hash(state["draft"])
    assert proof["versionSnapshotSha256"] == maps.snapshot_hash(state["version"])
    assert SECRET not in json.dumps(proof)
    source = state["draft"]["envelope"]["body"]["sourceBindings"][0]
    assert source["locator"] == {"serviceId": "e2e", "layerId": 17}


@pytest.mark.parametrize("kind", ["draft", "version"])
@pytest.mark.parametrize("fault", ["body", "owner", "identifier", "missing-field"])
def test_changed_map_content_or_metadata_fails(service, kind, fault):
    api, state = seeded(service)
    value = service[kind]
    if fault == "body":
        value["envelope"]["body"]["initialView"]["zoom"] = 12
    elif fault == "owner":
        value["ownerId"] = "other-owner"
    elif fault == "identifier":
        value["itemId"] = IDS["draftId"]
    else:
        del value["envelope"]
    with pytest.raises(maps.ProbeError):
        maps.verify(api, state)


@pytest.mark.parametrize("fault", ["missing", "authorized", "durable"])
def test_unreadable_or_non_durable_store_cannot_pass(service, fault):
    api, state = seeded(service)
    service[fault] = fault == "missing"
    with pytest.raises(maps.ProbeError) as error:
        maps.verify(api, state)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("value", [None, "17", True, 0, -1])
def test_invalid_seeded_source_is_rejected(service, value):
    manifest = deepcopy(MANIFEST)
    manifest["slice1"]["e2e_src_fs"]["layerId"] = value
    with pytest.raises(maps.ProbeError):
        maps.seed(maps.StudioApi(service["base"], SECRET), manifest)
    assert not any(body is not None for _, body in service["requests"])


def test_cli_private_state_and_stale_success_invalidation(service, tmp_path, monkeypatch):
    manifest, private, receipt = (tmp_path / name for name in ("manifest", "state", "receipt"))
    manifest.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv("E2E_BASE", service["base"])
    monkeypatch.setenv("E2E_API_KEY", SECRET)
    arguments = ["--state", str(private), "--manifest", str(manifest), "--receipt", str(receipt)]
    monkeypatch.setattr("sys.argv", ["probe", "seed", *arguments])
    assert maps.main() == 0
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert json.loads(receipt.read_text())["status"] == "fail"
    monkeypatch.setattr("sys.argv", ["probe", "verify", *arguments])
    assert maps.main() == 0
    assert json.loads(receipt.read_text())["status"] == "pass"
    service["missing"] = True
    assert maps.main() == 1
    assert json.loads(receipt.read_text())["status"] == "fail"
    assert SECRET not in receipt.read_text()


def proof():
    return {"schema": 1, "status": "pass", **IDS, "checks": dict.fromkeys(maps.CHECKS, True),
            "draftSnapshotSha256": "a" * 64, "versionSnapshotSha256": "b" * 64}


def receipt(value, **overrides):
    from test_compose_upgrade_credentials import proof as credential_proof
    from test_compose_upgrade_version import evidence
    pair, phases = evidence()
    inputs = dict(scenario="normal", status="pass", why="harness passed", phases=phases,
                  observed={"maps": value, "credentials": credential_proof()}, harness={}, run="run", commit="commit")
    inputs.update(overrides)
    return cu.build_receipt(pair, **inputs)


@pytest.mark.parametrize("value", [None, {}, [], "pass", {"status": "pass"}])
def test_missing_map_proof_demotes_success(value):
    result = receipt(value)
    assert result["status"] == "fail"
    assert result["why"].startswith("saved map preservation refused:")


@pytest.mark.parametrize("check", maps.CHECKS)
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_all_map_checks_require_boolean_true(check, value):
    fragment = proof()
    fragment["checks"][check] = value
    assert receipt(fragment)["status"] == "fail"


@pytest.mark.parametrize("field", [*IDS, *maps.HASH_FIELDS])
def test_proof_requires_persisted_ids_and_content_hashes(field):
    fragment = proof()
    fragment[field] = "unverified"
    assert receipt(fragment)["status"] == "fail"


def test_valid_proof_cannot_promote_failure_and_exports_only_evidence():
    fragment = {**proof(), "envelope": {"private": SECRET}}
    assert receipt(fragment)["status"] == "pass"
    assert SECRET not in json.dumps(receipt(fragment)["mapEvidence"])
    assert receipt(fragment, status="fail")["status"] == "fail"
    assert receipt(fragment, status="blocked")["status"] == "blocked"
    assert maps.map_evidence({"candidate_version": "2026.1.0-rc.2"}, {})["status"] == "not-applicable"


def test_workflow_runs_map_probe_around_candidate_upgrade_and_cleans_private_state():
    workflow = yaml.safe_load(Path(".github/workflows/gate-upgrade.yml").read_text())
    smoke = next(step for step in workflow["jobs"]["compose-upgrade"]["steps"] if step.get("id") == "smoke")
    assert smoke["env"]["COMPOSE_UPGRADE_MAP_PROOF"] == "${{ steps.pair.outputs.candidate_version == '2026.1.0' }}"
    shell = Path("e2e/harness/upgrade-chaos.sh").read_text()
    assert 'CU_MAP_STATE="$(mktemp -d)"' in shell
    assert 'rm -rf -- "$CU_MAP_STATE"' in shell
    assert shell.index("cu_maps seed ||") < shell.index('cu_identity candidate "$upgrade_image"')
    assert shell.index('cu_identity candidate "$upgrade_image"') < shell.index("cu_maps verify ||")
