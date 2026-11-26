"""Unit tests for RC2 live probe producers, fail-closed checks, and mock HTTP flows."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import ssl
import subprocess
import threading
import urllib.parse

import pytest
import yaml

import rc2_live_nginx as nginx_env
import rc2_live_probes_common as common
import rc2_live_proxy_probe as proxy_probe
import rc2_live_tenant_probe as tenant_probe
import rc2_live_upgrade_probe as upgrade_probe
import rc2_rigor as rigor

SHA = "a" * 40
INDEX_DIGEST = "sha256:" + "1" * 64
AMD64_DIGEST = "sha256:" + "2" * 64
LOCK_DIGEST = "sha256:" + "3" * 64
PRIOR_DIGEST = "sha256:" + "9" * 64
CANDIDATE = common.CandidateSpec(SHA, INDEX_DIGEST, AMD64_DIGEST, LOCK_DIGEST)
VALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR" + (b"\x00" * 48) + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
ALT_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR" + (b"\x01" * 48) + b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
DRAFT_ID = "11111111-1111-1111-1111-111111111111"


@dataclass
class _MockState:
    count: int = 5
    rate_hits: int = 0


def _proc(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], 0, stdout, "")


def test_verify_candidate_image_accepts_valid_index_and_native_aot_labels(monkeypatch):
    index_doc = {
        "manifests": [
            {"digest": AMD64_DIGEST, "platform": {"os": "linux", "architecture": "amd64"}}
        ]
    }
    labels = {
        "org.opencontainers.image.revision": SHA,
        "honua.runtime.compilation": "native-aot",
    }

    def fake_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["buildx", "imagetools", "inspect"]:
            return _proc(json.dumps(index_doc))
        return _proc(json.dumps(labels))

    monkeypatch.setattr(common, "run_docker", fake_docker)
    assert common.verify_candidate_image(CANDIDATE) == f"{common.HONUA_IMAGE_REPO}@{AMD64_DIGEST}"


@pytest.mark.parametrize(
    ("labels", "index_digest"),
    [
        ({"org.opencontainers.image.revision": "b" * 40, "honua.runtime.compilation": "native-aot"}, AMD64_DIGEST),
        ({"org.opencontainers.image.revision": SHA, "honua.runtime.compilation": "jit"}, AMD64_DIGEST),
        ({"org.opencontainers.image.revision": SHA, "honua.runtime.compilation": "native-aot"}, "sha256:" + "f" * 64),
    ],
)
def test_verify_candidate_image_rejects_mismatched_sha_non_aot_or_unlisted_digest(
    monkeypatch, labels, index_digest
):
    index_doc = {
        "manifests": [
            {"digest": index_digest, "platform": {"os": "linux", "architecture": "amd64"}}
        ]
    }

    def fake_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["buildx", "imagetools", "inspect"]:
            return _proc(json.dumps(index_doc))
        return _proc(json.dumps(labels))

    monkeypatch.setattr(common, "run_docker", fake_docker)
    with pytest.raises(ValueError):
        common.verify_candidate_image(CANDIDATE)


def test_mint_hs256_jwt_encodes_expected_claims():
    token = common.mint_hs256_jwt({"sub": "platform-admin", "role": ["admin", "platform_admin"]})
    parts = token.split(".")
    assert len(parts) == 3
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    assert claims["iss"] == common.OIDC_ISSUER
    assert claims["aud"] == common.OIDC_AUDIENCE
    assert claims["sub"] == "platform-admin"


def test_upgrade_proof_validates_and_rejects_failures(tmp_path):
    good = upgrade_probe.build_upgrade_proof(
        CANDIDATE,
        upgrade_probe.UpgradeObservation(
            prior_rc_digest=PRIOR_DIGEST,
            pre_count=5,
            post_count=6,
            migration_lock_failures=0,
            recovered_ready_status=200,
            degraded_startup_survived=True,
            degraded_initial_not_ready=True,
            editable_after_upgrade=True,
        ),
    )
    out = tmp_path / "postgis-upgrade-recovery-proof.json"
    common.validate_and_write_proof(common.ProofTarget(upgrade_probe.ROW_ID, CANDIDATE, out), good)
    assert out.is_file()
    assert upgrade_probe.count_lock_failures("advisory lock error: timeout\nall good") == 1
    assert upgrade_probe.count_lock_failures("acquired migration lock successfully") == 0

    bad = {**good, "migrationLockFailures": 1}
    bad_target = common.ProofTarget(upgrade_probe.ROW_ID, CANDIDATE, tmp_path / "bad.json")
    with pytest.raises(ValueError):
        common.validate_and_write_proof(bad_target, bad)


def test_proxy_proof_validates_and_rejects_missing_cors_code(tmp_path):
    clients = {name: {"status": "PASS", "evidence": f"{name}=200"} for name in rigor.REQUIRED_CLIENTS}
    good = proxy_probe.build_proxy_proof(
        CANDIDATE,
        proxy_probe.ProxyObservation(
            clients=clients,
            cors_on_error_codes=[400, 401, 402, 403, 429],
            forwarded_proto_https=True,
            ogc_next_links_https=True,
            capabilities_online_resource_https=True,
            axis_order_verified=True,
            geoservices_error_envelope_verified=True,
        ),
    )
    out = tmp_path / "gis-clients-proxy-proof.json"
    common.validate_and_write_proof(common.ProofTarget(proxy_probe.ROW_ID, CANDIDATE, out), good)
    assert out.is_file()

    bad = {**good, "corsOnErrorCodes": [400, 401, 402, 403]}
    bad_target = common.ProofTarget(proxy_probe.ROW_ID, CANDIDATE, tmp_path / "bad.json")
    with pytest.raises(ValueError):
        common.validate_and_write_proof(bad_target, bad)


def test_tenant_proof_validates_and_rejects_leaks(tmp_path):
    good = tenant_probe.build_tenant_proof(
        CANDIDATE,
        tenant_probe.TenantObservation(
            single_tenant_zero_403=True,
            single_tenant_workflows_passed=10,
            platform_admin_succeeded=True,
            tenant_admin_allowed_host_routes=0,
            cross_tenant_leaks=0,
            platform_admin_routes=sorted(rigor.REQUIRED_HOST_ADMIN_ROUTES),
            hidden_field_protocols_blocked=sorted(rigor.REQUIRED_HIDDEN_PROTOCOLS),
        ),
    )
    out = tmp_path / "tenant-isolation-parity-proof.json"
    common.validate_and_write_proof(common.ProofTarget(tenant_probe.ROW_ID, CANDIDATE, out), good)
    assert out.is_file()

    bad = {**good, "crossTenantLeaks": 1}
    bad_target = common.ProofTarget(tenant_probe.ROW_ID, CANDIDATE, tmp_path / "bad.json")
    with pytest.raises(ValueError):
        common.validate_and_write_proof(bad_target, bad)


def _mock_wms_response(qs: dict[str, list[str]]) -> tuple[int, bytes, str]:
    if qs.get("REQUEST", [""])[0] == "GetCapabilities":
        xml = (
            b'<WMS_Capabilities xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<OnlineResource xlink:href="https://proxy.honua.test/wms"/></WMS_Capabilities>'
        )
        return 200, xml, "application/xml"
    if qs.get("CRS") == ["EPSG:4326"] and qs.get("BBOX") == ["-160.5,18.5,-154.5,22.5"]:
        return 200, ALT_PNG, "image/png"
    return 200, VALID_PNG, "image/png"


def _mock_featureserver_response(
    state: _MockState, qs: dict[str, list[str]], headers: dict[str, str]
) -> tuple[int, bytes, str]:
    if qs.get("returnCountOnly") == ["true"]:
        return 200, json.dumps({"count": state.count}).encode(), "application/json"
    if qs.get("f") == ["pbf"]:
        return 200, b"pbf-bytes", "application/x-protobuf"
    if headers.get("x-honua-tenant") == "tenant-a":
        body = {"features": [{"attributes": {"secret_note": "classified-alpha-001"}}]}
        return 200, json.dumps(body).encode(), "application/json"
    body = {"fields": [{"name": "secret_note"}], "features": [{"attributes": {"name": "Honolulu"}}]}
    return 200, json.dumps(body).encode(), "application/json"


def _mock_admin_or_studio_get(path: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
    auth = headers.get("authorization", "")
    if path.startswith("/api/v1/studio/package-drafts"):
        if "tenant-a" in auth:
            return 200, json.dumps({"draftId": DRAFT_ID}).encode(), "application/json"
        if path.endswith(DRAFT_ID):
            return 404, b'{"detail":"not found"}', "application/json"
        return 200, b'{"items":[]}', "application/json"
    if headers.get("x-api-key") == common.ADMIN_PASSWORD or "platform-admin" in auth:
        return 200, b'{"data":[]}', "application/json"
    return (403, b'{"detail":"forbidden"}', "application/json") if auth else (
        401, b'{"detail":"unauthorized"}', "application/json"
    )


STATIC_MOCK_GET_ROUTES: dict[str, tuple[int, bytes, str]] = {
    "/ogc/features": (200, json.dumps({"links": [{"rel": "self", "href": "https://proxy.honua.test/ogc/features"}]}).encode(), "application/json"),
    "/ogc/maps/collections/1/map": (200, VALID_PNG, "image/png"),
    "/tiles/1/tile.json": (
        200,
        json.dumps({
            "tilejson": "3.0.0",
            "tiles": ["https://proxy.honua.test/tiles/1/{z}/{x}/{y}.mvt"],
            "vector_layers": [{"id": "layer"}],
        }).encode(),
        "application/json",
    ),
    "/tiles/1/0/0/0.mvt": (200, b"\x1a\x18\n\x05layerHonolulu Harbor", "application/vnd.mapbox-vector-tile"),
    "/rest/services/nonexistent_rc2/FeatureServer/0/query": (200, b'{"error":{"code":404,"message":"Service not found"}}', "application/json"),
    "/odata/Features(1)": (200, json.dumps({"value": [{"ObjectId": 1, "name": "Honolulu"}]}).encode(), "application/json"),
    "/stac/collections": (200, json.dumps({"collections": [{"id": "1"}]}).encode(), "application/json"),
    "/stac/collections/1/items": (200, json.dumps({"features": [{"id": "1", "properties": {"name": "Honolulu"}}]}).encode(), "application/json"),
}


def _route_mock_get(
    state: _MockState, raw_path: str, headers: dict[str, str]
) -> tuple[int, bytes, str]:
    parsed = urllib.parse.urlsplit(raw_path)
    qs = urllib.parse.parse_qs(parsed.query)
    path = parsed.path
    if path == "/ogc/features/conformance":
        state.rate_hits += 1
        return (429 if state.rate_hits >= 3 else 200), b'{"conformsTo":[]}', "application/json"
    if path == "/ogc/features/collections/1/items":
        if qs.get("limit") == ["-5"]:
            return 400, b'{"detail":"bad limit"}', "application/json"
        body = {
            "features": [{"id": 1, "properties": {"name": "Honolulu"}}],
            "links": [{"rel": "next", "href": "https://proxy.honua.test/ogc/features/collections/1/items?offset=1"}],
        }
        return 200, json.dumps(body).encode(), "application/json"
    if path == "/ogc/services/proxy_probe/wms":
        return _mock_wms_response(qs)
    if path.endswith("/FeatureServer/1/query"):
        return _mock_featureserver_response(state, qs, headers)
    if path.startswith(("/api/v1/admin/", "/api/v1/console/", "/api/v1/studio/")):
        return _mock_admin_or_studio_get(path, headers)
    return STATIC_MOCK_GET_ROUTES.get(path, (404, b'{"detail":"not found"}', "application/json"))


def _route_mock_post(state: _MockState, path: str) -> tuple[int, bytes, str]:
    if path.endswith("/items"):
        state.count += 1
        return 201, json.dumps({"id": 6, "type": "Feature"}).encode(), "application/geo+json"
    if path == "/ogc/features/collections/1/clusters":
        return 402, b'{"detail":"pro required"}', "application/json"
    if path.endswith("/queryClusters"):
        return 200, json.dumps({"features": [{"properties": {"name": "Honolulu"}}]}).encode(), "application/json"
    if path == "/api/v1/studio/package-drafts":
        return 201, json.dumps({"data": {"draftId": DRAFT_ID}}).encode(), "application/json"
    if path == "/api/v1/admin/field-mask-policies":
        return 201, b'{"data":{"policyId":"p1"}}', "application/json"
    return 404, b"{}", "application/json"


def _make_handler(state: _MockState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _reply(self, response: tuple[int, bytes, str]) -> None:
            code, body, ctype = response
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if self.headers.get("Origin") == proxy_probe.CORS_ORIGIN:
                self.send_header("Access-Control-Allow-Origin", proxy_probe.CORS_ORIGIN)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            self._reply(_route_mock_get(state, self.path, hdrs))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self._reply(_route_mock_post(state, self.path))

    return Handler


class _MockProbeServer:
    """In-process HTTP server exercising upgrade, proxy, and tenant probe HTTP flows."""

    def __init__(self) -> None:
        self.state = _MockState()
        self.httpd = HTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _MockProbeServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_mock_http_flows_cover_upgrade_proxy_and_tenant_probes():
    with _MockProbeServer() as server:
        assert upgrade_probe.query_feature_count(server.base_url, "upgrade_probe", 1) == 5
        assert upgrade_probe.add_post_upgrade_feature(server.base_url, "upgrade_probe", 1) is True
        assert upgrade_probe.query_feature_count(server.base_url, "upgrade_probe", 1) == 6

        proxy = nginx_env.ProxyEndpoint(server.base_url, ssl.create_default_context(), "mock-nginx")
        pub = common.PublishedLayer("c1", "proxy_probe", 1, "probe_sites")
        fwd, nxt, _ = proxy_probe._check_forwarded_proto_and_next(proxy, 1)
        caps_ok, caps_detail = proxy_probe._check_wms_capabilities_https(proxy, "proxy_probe")
        axis_ok, axis_detail = proxy_probe._check_wms_axis_order(proxy, "proxy_probe", 1)
        assert (fwd, nxt, caps_ok, axis_ok) == (True, True, True, True)
        assert proxy_probe._check_geoservices_error_envelope(proxy) is True
        clients = proxy_probe._probe_gis_clients(proxy, pub, f"{caps_detail},{axis_detail}")
        assert all(clients[name]["status"] == "PASS" for name in rigor.REQUIRED_CLIENTS)
        assert set(proxy_probe._probe_cors_error_codes(proxy, pub)) == rigor.REQUIRED_CORS_ERROR_CODES

        zero_403, st_passed = tenant_probe._probe_single_tenant_parity(server.base_url)
        assert zero_403 is True and st_passed >= rigor.MIN_SINGLE_TENANT_WORKFLOWS
        tokens = tenant_probe.TenantTokens(
            platform_admin="platform-admin-tok",
            tenant_a_admin="tenant-a-tok",
            tenant_b_admin="tenant-b-tok",
            masked_reader="masked-reader-tok",
        )
        ok, allowed, routes = tenant_probe._probe_host_admin_routes(server.base_url, tokens)
        assert ok is True and allowed == 0 and set(routes) == rigor.REQUIRED_HOST_ADMIN_ROUTES
        assert tenant_probe._probe_studio_tenant_isolation(server.base_url, tokens, pub) == 0
        blocked = tenant_probe._probe_hidden_field_protocols(server.base_url, pub, tokens)
        assert set(blocked) == rigor.REQUIRED_HIDDEN_PROTOCOLS


def test_maplibre_validator_rejects_empty_tilejson_and_non_mvt_payloads():
    empty_tj = common.HttpReply(200, {"content-type": "application/json"}, b"{}")
    assert proxy_probe._extract_tilejson_layer_id(empty_tj, {}) is None
    bad_mvt = common.HttpReply(200, {"content-type": "application/vnd.mapbox-vector-tile"}, b"not a vector tile")
    empty_204 = common.HttpReply(204, {"content-type": "application/vnd.mapbox-vector-tile"}, b"")
    assert proxy_probe._is_valid_mvt_payload(bad_mvt, "layer") is False
    assert proxy_probe._is_valid_mvt_payload(empty_204, "layer") is False


def test_tenant_isolation_fails_closed_on_inconclusive_500_or_transport_error(monkeypatch):
    tokens = tenant_probe.TenantTokens("p", "a", "b", "m")
    pub = common.PublishedLayer("c1", "mask_probe", 1, "probe_sites")
    monkeypatch.setattr(tenant_probe, "_create_tenant_a_draft", lambda *_: DRAFT_ID)
    replies = iter([
        common.HttpReply(200, {}, b"{}"),
        common.HttpReply(500, {}, b'{"detail":"db down"}'),
    ])
    monkeypatch.setattr(common, "http_request", lambda _spec: next(replies))
    with pytest.raises(RuntimeError, match="inconclusive cross-tenant draft GET"):
        tenant_probe._probe_studio_tenant_isolation("http://127.0.0.1:1", tokens, pub)


def test_live_probes_workflow_contract_matches_gate_artifacts():
    root = Path(__file__).resolve().parents[1]
    wf = yaml.safe_load((root / ".github/workflows/rc2-rigor-live-probes.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on") or wf.get(True)
    inputs = triggers["workflow_dispatch"]["inputs"]
    expected = {
        "server_sha", "server_digest", "amd64_digest",
        "platform_lock_digest", "prior_rc_image_digest", "postgis_image", "only",
    }
    assert expected <= set(inputs)
    assert set(wf["jobs"]) == {"postgis-upgrade-recovery", "gis-clients-proxy", "tenant-isolation-parity"}
    for job in wf["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
