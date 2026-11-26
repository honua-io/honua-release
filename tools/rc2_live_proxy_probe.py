#!/usr/bin/env python3
"""Live Docker probe for RC2 GIS clients and HTTPS reverse-proxy behavior."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

import rc2_live_probes_common as common

ROW_ID = "gis-clients-proxy"
SERVICE_NAME = "proxy_probe"
PROXY_HOST = "proxy.honua.test"
CORS_ORIGIN = "https://clients.honua.test"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 64
RATE_LIMIT_METADATA_RPM = 12
RATE_LIMIT_BURST_MAX = 40


@dataclass(frozen=True)
class ProxyProbeConfig:
    """Configuration for executing the live GIS clients and reverse-proxy probe."""

    candidate: common.CandidateSpec
    out_path: Path
    nginx_image: str = common.DEFAULT_NGINX_IMAGE
    boot_timeout: float = common.DEFAULT_BOOT_TIMEOUT_SECONDS
    verify_index: bool = True


@dataclass(frozen=True)
class ProxyEndpoint:
    """HTTPS reverse-proxy connection details and TLS trust context."""

    base_url: str
    ssl_context: ssl.SSLContext
    nginx_container: str


@dataclass(frozen=True)
class ProxyObservation:
    """Raw live observations from GIS clients, reverse-proxy URLs, and CORS errors."""

    clients: dict[str, dict[str, str]]
    cors_on_error_codes: list[int]
    forwarded_proto_https: bool
    ogc_next_links_https: bool
    capabilities_online_resource_https: bool
    axis_order_verified: bool
    geoservices_error_envelope_verified: bool


def _generate_tls_materials(work_dir: Path) -> tuple[Path, Path]:
    cert_path = work_dir / "tls.crt"
    key_path = work_dir / "tls.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path), "-days", "1",
            "-subj", f"/CN={PROXY_HOST}",
            "-addext", f"subjectAltName=DNS:{PROXY_HOST},IP:127.0.0.1",
        ],
        capture_output=True, text=True, check=True,
    )
    return cert_path, key_path


def _write_nginx_config(work_dir: Path, upstream_host: str) -> Path:
    conf_path = work_dir / "nginx.conf"
    conf_path.write_text(
        "events {}\n"
        "http {\n"
        "  server {\n"
        "    listen 8443 ssl;\n"
        f"    server_name {PROXY_HOST};\n"
        "    ssl_certificate /etc/nginx/certs/tls.crt;\n"
        "    ssl_certificate_key /etc/nginx/certs/tls.key;\n"
        "    location / {\n"
        f"      proxy_pass http://{upstream_host}:8080;\n"
        f"      proxy_set_header Host {PROXY_HOST};\n"
        "      proxy_set_header X-Forwarded-Proto https;\n"
        f"      proxy_set_header X-Forwarded-Host {PROXY_HOST};\n"
        "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"      proxy_set_header Forwarded \"proto=https;host={PROXY_HOST}\";\n"
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return conf_path


def _start_nginx_proxy(
    stack: common.DockerStack, work_dir: Path, nginx_image: str
) -> ProxyEndpoint:
    cert_path, _ = _generate_tls_materials(work_dir)
    conf_path = _write_nginx_config(work_dir, stack.app_container)
    https_port = common.allocate_port()
    nginx_name = f"{stack.app_container}-nginx"
    stack.extra_containers.append(nginx_name)
    common.run_docker([
        "run", "-d", "--name", nginx_name, "--network", stack.network,
        "-p", f"127.0.0.1:{https_port}:8443",
        "-v", f"{conf_path}:/etc/nginx/nginx.conf:ro",
        "-v", f"{work_dir}:/etc/nginx/certs:ro",
        nginx_image,
    ])
    ctx = ssl.create_default_context(cafile=str(cert_path))
    ctx.check_hostname = False
    base_url = f"https://127.0.0.1:{https_port}"
    proxy = ProxyEndpoint(base_url, ctx, nginx_name)
    _wait_proxy_live(proxy)
    return proxy


def _wait_proxy_live(proxy: ProxyEndpoint, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = _proxy_get(proxy, "/healthz/live")
        if reply.status == common.HTTP_OK:
            return
        time.sleep(common.POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"nginx proxy at {proxy.base_url} did not become live within {timeout}s")


def _proxy_get(
    proxy: ProxyEndpoint, path: str, headers: dict[str, str] | None = None
) -> common.HttpReply:
    return common.http_request(
        common.HttpSpec(
            "GET", f"{proxy.base_url}{path}", headers or {}, ssl_context=proxy.ssl_context
        )
    )


def _proxy_json(
    proxy: ProxyEndpoint, path: str, headers: dict[str, str] | None = None
) -> tuple[common.HttpReply, object]:
    return common.json_request(
        common.HttpSpec(
            "GET", f"{proxy.base_url}{path}", headers or {}, ssl_context=proxy.ssl_context
        )
    )


def _check_forwarded_proto_and_next(
    proxy: ProxyEndpoint, layer_id: int
) -> tuple[bool, bool, str]:
    landing_reply, landing = _proxy_json(proxy, "/ogc/features")
    landing_links = landing.get("links", []) if isinstance(landing, dict) else []
    forwarded_ok = (
        landing_reply.status == common.HTTP_OK
        and bool(landing_links)
        and all(
            str(link.get("href", "")).startswith("https://")
            for link in landing_links if isinstance(link, dict)
        )
    )
    items_reply, items = _proxy_json(proxy, f"/ogc/features/collections/{layer_id}/items?limit=1")
    next_links = [
        str(link.get("href", ""))
        for link in (items.get("links", []) if isinstance(items, dict) else [])
        if isinstance(link, dict) and link.get("rel") == "next"
    ]
    next_ok = (
        items_reply.status == common.HTTP_OK
        and bool(next_links)
        and all(href.startswith("https://") for href in next_links)
    )
    evidence = f"ogc-landing={landing_reply.status},ogc-items={items_reply.status},next={next_links[:1]}"
    return forwarded_ok, next_ok, evidence


def _check_wms_capabilities_https(proxy: ProxyEndpoint, service_name: str) -> tuple[bool, str]:
    query = urllib.parse.urlencode(
        {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities"}
    )
    reply = _proxy_get(proxy, f"/ogc/services/{service_name}/wms?{query}")
    if reply.status != common.HTTP_OK:
        return False, f"wms-caps=HTTP {reply.status}"
    try:
        root = ET.fromstring(reply.body)
    except ET.ParseError:
        return False, "wms-caps=invalid-xml"
    hrefs = [
        value for elem in root.iter() if elem.tag.endswith("OnlineResource")
        for key, value in elem.attrib.items() if key.endswith("href")
    ]
    ok = bool(hrefs) and all(href.startswith("https://") for href in hrefs)
    return ok, f"wms-caps=200,online-resources={len(hrefs)}"


def _is_valid_png(body: bytes) -> bool:
    return (
        body.startswith(PNG_MAGIC)
        and len(body) >= MIN_PNG_BYTES
        and b"IHDR" in body[:32]
        and b"IEND" in body[-16:]
    )


def _check_wms_axis_order(proxy: ProxyEndpoint, service_name: str, layer_id: int) -> tuple[bool, str]:
    base = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": str(layer_id), "STYLES": "", "WIDTH": "128", "HEIGHT": "128",
        "FORMAT": "image/png", "TRANSPARENT": "true",
    }
    epsg_q = urllib.parse.urlencode({**base, "CRS": "EPSG:4326", "BBOX": "18.5,-160.5,22.5,-154.5"})
    crs84_q = urllib.parse.urlencode({**base, "CRS": "CRS:84", "BBOX": "-160.5,18.5,-154.5,22.5"})
    swapped_q = urllib.parse.urlencode({**base, "CRS": "EPSG:4326", "BBOX": "-160.5,18.5,-154.5,22.5"})
    r_epsg = _proxy_get(proxy, f"/ogc/services/{service_name}/wms?{epsg_q}")
    r_crs84 = _proxy_get(proxy, f"/ogc/services/{service_name}/wms?{crs84_q}")
    r_swap = _proxy_get(proxy, f"/ogc/services/{service_name}/wms?{swapped_q}")
    valid_pair = (
        r_epsg.status == common.HTTP_OK
        and r_crs84.status == common.HTTP_OK
        and _is_valid_png(r_epsg.body)
        and _is_valid_png(r_crs84.body)
    )
    swapped_differs = r_swap.status != common.HTTP_OK or r_swap.body != r_epsg.body
    ok = valid_pair and r_epsg.body == r_crs84.body and swapped_differs
    detail = (
        f"epsg4326={r_epsg.status}({len(r_epsg.body)}B),"
        f"crs84={r_crs84.status}({len(r_crs84.body)}B),"
        f"swapped={r_swap.status}({len(r_swap.body)}B)"
    )
    return ok, detail


def _check_geoservices_error_envelope(proxy: ProxyEndpoint) -> bool:
    reply, parsed = _proxy_json(proxy, "/rest/services/nonexistent_rc2/FeatureServer/0/query?f=json")
    if not isinstance(parsed, dict):
        return False
    err = parsed.get("error")
    return (
        isinstance(err, dict)
        and type(err.get("code")) is int
        and isinstance(err.get("message"), str)
        and bool(err["message"].strip())
    )


def _probe_gis_clients(
    proxy: ProxyEndpoint, published: common.PublishedLayer, wms_detail: str
) -> dict[str, dict[str, str]]:
    svc, lid = published.service_name, published.layer_id
    mvt = _proxy_get(proxy, f"/tiles/{lid}/0/0/0.mvt")
    tilejson_r, tilejson = _proxy_json(proxy, f"/tiles/{lid}/tile.json")
    ogc_map_q = urllib.parse.urlencode(
        {"bbox": "-160.5,18.5,-154.5,22.5", "width": "128", "height": "128", "f": "png"}
    )
    cesium_r = _proxy_get(proxy, f"/ogc/maps/collections/{lid}/map?{ogc_map_q}")
    fs_json_r, fs_json = _proxy_json(
        proxy, f"/rest/services/{svc}/FeatureServer/{lid}/query?where=1=1&outFields=*&f=json"
    )
    fs_pbf_r = _proxy_get(
        proxy, f"/rest/services/{svc}/FeatureServer/{lid}/query?where=1=1&f=pbf"
    )
    ogc_r, ogc_body = _proxy_json(proxy, f"/ogc/features/collections/{lid}/items")
    qgis_ok = ogc_r.status == common.HTTP_OK and isinstance(ogc_body, dict) and bool(ogc_body.get("features"))
    maplibre_ok = (
        tilejson_r.status == common.HTTP_OK
        and isinstance(tilejson, dict)
        and mvt.status in (common.HTTP_OK, 204)
    )
    cesium_ok = cesium_r.status == common.HTTP_OK and _is_valid_png(cesium_r.body)
    geo_ok = (
        fs_json_r.status == common.HTTP_OK
        and isinstance(fs_json, dict)
        and bool(fs_json.get("features"))
        and fs_pbf_r.status == common.HTTP_OK
        and bool(fs_pbf_r.body)
    )
    return {
        "qgis": {"status": "PASS" if qgis_ok else "FAIL", "evidence": f"ogc={ogc_r.status},{wms_detail}"},
        "maplibre": {"status": "PASS" if maplibre_ok else "FAIL", "evidence": f"tilejson={tilejson_r.status},mvt={mvt.status}"},
        "cesium": {"status": "PASS" if cesium_ok else "FAIL", "evidence": f"ogc-maps={cesium_r.status}({len(cesium_r.body)}B)"},
        "geoservices": {"status": "PASS" if geo_ok else "FAIL", "evidence": f"json={fs_json_r.status},pbf={fs_pbf_r.status}({len(fs_pbf_r.body)}B)"},
    }


def _has_cors_origin(reply: common.HttpReply) -> bool:
    return reply.headers.get("access-control-allow-origin", "").strip() == CORS_ORIGIN


def _probe_cors_error_codes(proxy: ProxyEndpoint, published: common.PublishedLayer) -> list[int]:
    observed: set[int] = set()
    origin_hdr = {"Origin": CORS_ORIGIN}
    viewer_jwt = common.mint_hs256_jwt({"sub": "viewer-1", "role": "viewer"})
    checks = (
        common.HttpSpec(
            "GET", f"{proxy.base_url}/ogc/features/collections/{published.layer_id}/items?limit=-5",
            origin_hdr, ssl_context=proxy.ssl_context,
        ),
        common.HttpSpec(
            "GET", f"{proxy.base_url}/api/v1/admin/connections",
            origin_hdr, ssl_context=proxy.ssl_context,
        ),
        common.HttpSpec(
            "POST", f"{proxy.base_url}/ogc/features/collections/{published.layer_id}/clusters",
            {**origin_hdr, "Content-Type": "application/json"},
            json.dumps({"algorithm": "dbscan"}).encode(),
            ssl_context=proxy.ssl_context,
        ),
        common.HttpSpec(
            "GET", f"{proxy.base_url}/api/v1/admin/connections",
            {**origin_hdr, "Authorization": f"Bearer {viewer_jwt}"},
            ssl_context=proxy.ssl_context,
        ),
    )
    for spec in checks:
        reply = common.http_request(spec)
        if _has_cors_origin(reply) and reply.status in (400, 401, 402, 403):
            observed.add(reply.status)
    for _ in range(RATE_LIMIT_BURST_MAX):
        burst_reply = _proxy_get(proxy, "/ogc/features/conformance", origin_hdr)
        if burst_reply.status == common.HTTP_TOO_MANY_REQUESTS and _has_cors_origin(burst_reply):
            observed.add(common.HTTP_TOO_MANY_REQUESTS)
            break
    return sorted(observed)


def build_proxy_proof(
    spec: common.CandidateSpec, obs: ProxyObservation, observed_at: str | None = None
) -> dict[str, object]:
    """Assemble the fail-closed gis-clients-proxy proof JSON payload."""
    return {
        **common.build_identity_fields(spec, observed_at),
        "clients": obs.clients,
        "corsOnErrorCodes": obs.cors_on_error_codes,
        "forwardedProtoHttps": obs.forwarded_proto_https,
        "ogcNextLinksHttps": obs.ogc_next_links_https,
        "capabilitiesOnlineResourceHttps": obs.capabilities_online_resource_https,
        "axisOrderVerified": obs.axis_order_verified,
        "geoservicesErrorEnvelopeVerified": obs.geoservices_error_envelope_verified,
    }


def _proxy_server_env() -> tuple[str, ...]:
    return (
        "ForwardedHeaders__Enabled=true",
        "ForwardedHeaders__ForwardLimit=2",
        "ForwardedHeaders__KnownNetworks__0=10.0.0.0/8",
        "ForwardedHeaders__KnownNetworks__1=172.16.0.0/12",
        "ForwardedHeaders__KnownNetworks__2=192.168.0.0/16",
        f"Cors__AllowedOrigins__0={CORS_ORIGIN}",
        "Licensing__Enabled=true",
        "Licensing__Edition=Community",
        "RateLimiting__Enabled=true",
        "RateLimiting__GlobalRequestsPerMinute=500",
        "RateLimiting__QueryRequestsPerMinute=200",
        f"RateLimiting__MetadataRequestsPerMinute={RATE_LIMIT_METADATA_RPM}",
        *common.oidc_env_vars(),
    )


def run_proxy_probe(config: ProxyProbeConfig) -> dict[str, object]:
    """Execute the live HTTPS reverse-proxy, GIS client, and CORS error probe."""
    candidate_image = common.verify_candidate_image(config.candidate, config.verify_index)
    stack = common.create_network_and_postgis(
        "rc2-proxy", config.candidate.postgis_image, config.boot_timeout
    )
    try:
        common.start_honua_container(
            common.HonuaLaunchSpec(stack, candidate_image, _proxy_server_env())
        )
        common.wait_http_status(f"{stack.base_url}/healthz/ready", common.HTTP_OK, config.boot_timeout)
        common.seed_probe_table(stack.pg_container)
        published = common.publish_postgis_layer(
            common.PublishSpec(stack.base_url, stack.pg_container, SERVICE_NAME, editable=False)
        )
        with tempfile.TemporaryDirectory(prefix="rc2-proxy-") as tmp_dir:
            proxy = _start_nginx_proxy(stack, Path(tmp_dir), config.nginx_image)
            fwd_ok, next_ok, _ = _check_forwarded_proto_and_next(proxy, published.layer_id)
            caps_ok, caps_detail = _check_wms_capabilities_https(proxy, published.service_name)
            axis_ok, axis_detail = _check_wms_axis_order(proxy, published.service_name, published.layer_id)
            err_env_ok = _check_geoservices_error_envelope(proxy)
            clients = _probe_gis_clients(proxy, published, f"{caps_detail},{axis_detail}")
            cors_codes = _probe_cors_error_codes(proxy, published)
    finally:
        common.cleanup_stack(stack)
    obs = ProxyObservation(
        clients=clients,
        cors_on_error_codes=cors_codes,
        forwarded_proto_https=fwd_ok,
        ogc_next_links_https=next_ok,
        capabilities_online_resource_https=caps_ok,
        axis_order_verified=axis_ok,
        geoservices_error_envelope_verified=err_env_ok,
    )
    proof = build_proxy_proof(config.candidate, obs)
    target = common.ProofTarget(ROW_ID, config.candidate, config.out_path)
    return common.validate_and_write_proof(target, proof)


def _parse_args(argv: list[str] | None = None) -> ProxyProbeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-sha", required=True)
    parser.add_argument("--server-digest", required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--postgis-image", default=common.DEFAULT_POSTGIS_IMAGE)
    parser.add_argument("--nginx-image", default=common.DEFAULT_NGINX_IMAGE)
    parser.add_argument("--boot-timeout", type=float, default=common.DEFAULT_BOOT_TIMEOUT_SECONDS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    candidate = common.CandidateSpec(
        args.server_sha, args.server_digest, args.amd64_digest,
        args.lock_digest, args.postgis_image,
    )
    return ProxyProbeConfig(candidate, args.out, args.nginx_image, args.boot_timeout)


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    proof = run_proxy_probe(config)
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
