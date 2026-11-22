"""Published API preservation must fail closed independently of database checksums."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import stat
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

import compose_upgrade_services as services
import compose_upgrade_prior as cu


SOURCE = {"service": "e2e", "layerId": 17}
MANIFEST = {"service": "different-root-service", "slice1": {"e2e_src_fs": SOURCE}}
SECRET = "fixture-private-feature-content"


@pytest.fixture
def service():
    state = {"status": 200, "error": False, "requests": [], "count": 2,
             "catalog": {"serviceName": "e2e", "layers": [{"id": 17, "name": "seed"}]},
             "layer": {"id": 17, "name": "seed", "type": "Feature Layer", "geometryType": "esriGeometryPoint",
                       "spatialReference": {"wkid": 4326}, "objectIdField": "objectid",
                       "fields": [{"name": "objectid", "type": "esriFieldTypeOID"}]},
             "query": {"exceededTransferLimit": False, "features": [
                 {"attributes": {"objectid": 1, "name": SECRET}, "geometry": {"x": 1, "y": 2}},
                 {"attributes": {"objectid": 2, "name": "second"}, "geometry": {"x": 3, "y": 4}}]}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            state["requests"].append((parsed.path, parse_qs(parsed.query)))
            status = state["status"] if self.headers.get("X-API-Key") == SECRET else 401
            self.send_response(status)
            self.end_headers()
            if status != 200 or state["error"]:
                value = {"error": {"message": SECRET}}
            elif parsed.path.endswith("/query"):
                value = {"count": state["count"]} if "returnCountOnly" in parsed.query else state["query"]
            elif parsed.path.endswith("/17"):
                value = state["layer"]
            else:
                value = state["catalog"]
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
    api = services.FeatureApi(service["base"], SECRET)
    return api, services.seed(api, MANIFEST)


def test_reads_existing_publication_and_all_features_without_recreating(service):
    api, state = seeded(service)
    service["requests"].clear()
    service["query"]["features"].reverse()
    proof = services.verify(api, state)
    assert proof["status"] == "pass"
    assert [path for path, _ in service["requests"]] == [
        "/rest/services/e2e/FeatureServer", "/rest/services/e2e/FeatureServer/17",
        "/rest/services/e2e/FeatureServer/17/query", "/rest/services/e2e/FeatureServer/17/query"]
    assert service["requests"][-1][1] == {"f": ["json"], "where": ["1=1"], "outFields": ["*"],
                                               "returnGeometry": ["true"], "resultRecordCount": ["3"]}
    assert proof["featuresSnapshotSha256"] == services.snapshot_hash(state["features"])
    assert SECRET not in json.dumps(proof)


@pytest.mark.parametrize("fault", ["service", "catalog", "layer", "fields", "attribute", "geometry", "count",
                                       "truncated", "missing-feature", "missing-geometry", "duplicate-id", "error", "http"])
def test_missing_or_changed_published_content_fails(service, fault):
    api, state = seeded(service)
    if fault == "service":
        service["catalog"]["serviceName"] = "other"
    elif fault == "catalog":
        service["catalog"]["layers"] = []
    elif fault == "layer":
        service["layer"]["name"] = "renamed"
    elif fault == "fields":
        service["layer"]["fields"][0]["type"] = "esriFieldTypeString"
    elif fault in ("attribute", "geometry"):
        row = service["query"]["features"][0]
        row["attributes" if fault == "attribute" else "geometry"]["name" if fault == "attribute" else "x"] = 99
    elif fault == "count":
        service["count"] = 1
    elif fault == "truncated":
        service["query"]["exceededTransferLimit"] = True
    elif fault == "missing-feature":
        service["query"]["features"].pop()
    elif fault == "missing-geometry":
        del service["query"]["features"][0]["geometry"]
    elif fault == "duplicate-id":
        service["query"]["features"][1]["attributes"]["objectid"] = 1
    elif fault == "error":
        service["error"] = True
    else:
        service["status"] = 403
    with pytest.raises(services.ProbeError) as error:
        services.verify(api, state)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("value", [None, True, 0, -1, "17"])
def test_invalid_manifest_source_never_reaches_api(service, value):
    manifest = deepcopy(MANIFEST)
    manifest["slice1"]["e2e_src_fs"]["layerId"] = value
    with pytest.raises(services.ProbeError):
        services.seed(services.FeatureApi(service["base"], SECRET), manifest)
    assert service["requests"] == []


def test_cli_private_state_and_failed_retry_replace_stale_success(service, tmp_path, monkeypatch):
    manifest, private, receipt = (tmp_path / name for name in ("manifest", "state", "receipt"))
    manifest.write_text(json.dumps(MANIFEST))
    monkeypatch.setenv("E2E_BASE", service["base"])
    monkeypatch.setenv("E2E_API_KEY", SECRET)
    arguments = ["--state", str(private), "--manifest", str(manifest), "--receipt", str(receipt)]
    monkeypatch.setattr("sys.argv", ["probe", "seed", *arguments])
    assert services.main() == 0
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert json.loads(receipt.read_text())["status"] == "fail"
    monkeypatch.setattr("sys.argv", ["probe", "verify", *arguments])
    assert services.main() == 0
    assert json.loads(receipt.read_text())["status"] == "pass"
    service["status"] = 404
    assert services.main() == 1
    assert json.loads(receipt.read_text())["status"] == "fail"
    assert SECRET not in receipt.read_text()


def proof():
    return {"schema": 1, "status": "pass", **SOURCE, "featureCount": 2,
            "checks": dict.fromkeys(services.CHECKS, True),
            "serviceSnapshotSha256": "a" * 64, "featuresSnapshotSha256": "b" * 64}


def receipt(value, **overrides):
    from test_compose_upgrade_credentials import proof as credential_proof
    from test_compose_upgrade_maps import proof as map_proof
    from test_compose_upgrade_version import evidence, forward_observed
    pair, phases = evidence()
    inputs = dict(scenario="normal", status="pass", why="harness passed", phases=phases,
                  observed={**forward_observed(), "services": value, "credentials": credential_proof(), "maps": map_proof()},
                  harness={}, run="run", commit="commit")
    inputs.update(overrides)
    return cu.build_receipt(pair, **inputs)


@pytest.mark.parametrize("value", [None, {}, [], "pass", {"status": "pass"}])
def test_missing_service_proof_demotes_success(value):
    result = receipt(value)
    assert result["status"] == "fail"
    assert result["why"].startswith("published service preservation refused:")


@pytest.mark.parametrize("check", services.CHECKS)
@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_service_checks_require_boolean_true(check, value):
    fragment = proof()
    fragment["checks"][check] = value
    assert receipt(fragment)["status"] == "fail"


@pytest.mark.parametrize("field,value", [("service", "../private"), ("layerId", True), ("featureCount", True),
                                        ("featureCount", 1), ("serviceSnapshotSha256", "invalid"),
                                        ("featuresSnapshotSha256", "invalid")])
def test_unverified_identity_or_content_cannot_certify(field, value):
    fragment = proof()
    fragment[field] = value
    assert receipt(fragment)["status"] == "fail"


def test_proof_exports_only_evidence_and_cannot_promote_failure():
    fragment = {**proof(), "features": [{"private": SECRET}]}
    assert receipt(fragment)["status"] == "pass"
    assert SECRET not in json.dumps(receipt(fragment)["serviceEvidence"])
    assert receipt(fragment, status="fail")["status"] == "fail"
    assert receipt(fragment, status="blocked")["status"] == "blocked"
    assert services.service_evidence({"candidate_version": "2026.1.0-rc.2"}, {})["status"] == "not-applicable"


def test_workflow_brackets_upgrade_with_service_probe_and_cleans_private_state():
    workflow = yaml.safe_load(Path(".github/workflows/gate-upgrade.yml").read_text())
    smoke = next(step for step in workflow["jobs"]["compose-upgrade"]["steps"] if step.get("id") == "smoke")
    assert smoke["env"]["COMPOSE_UPGRADE_SERVICE_PROOF"] == "${{ steps.pair.outputs.candidate_version == '2026.1.0' }}"
    shell = Path("e2e/harness/upgrade-chaos.sh").read_text()
    assert 'state="$(mktemp -d)"' in shell
    assert 'rm -rf -- "$CU_SERVICE_STATE"' in shell
    assert shell.index("cu_services seed ||") < shell.index('cu_identity candidate "$upgrade_image"')
    assert shell.index('cu_identity candidate "$upgrade_image"') < shell.index("cu_services verify ||")
