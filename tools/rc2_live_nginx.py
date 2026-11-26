#!/usr/bin/env python3
"""TLS-terminating nginx reverse-proxy container helper for RC2 proxy probes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ssl
import subprocess
import time

import rc2_live_docker as docker_env
import rc2_live_probes_common as common

PROXY_HOST = "proxy.honua.test"
PROXY_READY_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProxyEndpoint:
    """HTTPS reverse-proxy connection details and TLS trust context."""

    base_url: str
    ssl_context: ssl.SSLContext
    nginx_container: str


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


def proxy_get(
    proxy: ProxyEndpoint, path: str, headers: dict[str, str] | None = None
) -> common.HttpReply:
    """Send a GET request through the HTTPS nginx reverse proxy."""
    return common.http_request(
        common.HttpSpec(
            "GET", f"{proxy.base_url}{path}", headers or {}, ssl_context=proxy.ssl_context
        )
    )


def proxy_json(
    proxy: ProxyEndpoint, path: str, headers: dict[str, str] | None = None
) -> tuple[common.HttpReply, object]:
    """Send a GET request through the HTTPS proxy and parse its JSON body."""
    return common.json_request(
        common.HttpSpec(
            "GET", f"{proxy.base_url}{path}", headers or {}, ssl_context=proxy.ssl_context
        )
    )


def _wait_proxy_live(proxy: ProxyEndpoint) -> None:
    deadline = time.monotonic() + PROXY_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proxy_get(proxy, "/healthz/live").status == common.HTTP_OK:
            return
        time.sleep(common.POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"nginx proxy at {proxy.base_url} did not become live in time")


def start_nginx_proxy(
    stack: docker_env.DockerStack, work_dir: Path, nginx_image: str
) -> ProxyEndpoint:
    """Boot a TLS nginx container in front of stack.app_container and wait until live."""
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
    proxy = ProxyEndpoint(f"https://127.0.0.1:{https_port}", ctx, nginx_name)
    _wait_proxy_live(proxy)
    return proxy
