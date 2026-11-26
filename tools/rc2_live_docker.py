#!/usr/bin/env python3
"""Ephemeral PostGIS, Honua container, and Admin API publishing helpers for RC2 probes."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import subprocess
import time

import rc2_live_probes_common as common


@dataclass
class DockerStack:
    """Ephemeral Docker network and containers managed by one probe run."""

    network: str
    pg_container: str
    app_container: str
    port: int
    extra_containers: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class HonuaLaunchSpec:
    """Launch options for a Honua Server container attached to a DockerStack."""

    stack: DockerStack
    image: str
    env_vars: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishSpec:
    """Options for publishing a seeded PostGIS table through the Admin API."""

    base_url: str
    pg_host: str
    service_name: str
    table_name: str = "probe_sites"
    layer_name: str = "probe_sites"
    editable: bool = True
    allow_anonymous: bool = True
    auth_headers: dict[str, str] = field(default_factory=dict)


def create_network_and_postgis(
    prefix: str, postgis_image: str, timeout: float = common.DEFAULT_BOOT_TIMEOUT_SECONDS
) -> DockerStack:
    """Create an isolated Docker bridge network and boot a ready PostGIS container."""
    port = common.allocate_port()
    tag = f"{prefix}-{int(time.time())}-{port}"
    stack = DockerStack(f"{tag}-net", f"{tag}-pg", f"{tag}-app", port)
    try:
        common.run_docker(["network", "create", stack.network])
        common.run_docker([
            "run", "-d", "--name", stack.pg_container, "--network", stack.network,
            "-e", f"POSTGRES_DB={common.POSTGRES_DB}",
            "-e", f"POSTGRES_USER={common.POSTGRES_USER}",
            "-e", f"POSTGRES_PASSWORD={common.POSTGRES_PASSWORD}",
            postgis_image,
        ])
        wait_postgis(stack.pg_container, timeout)
        return stack
    except (subprocess.SubprocessError, RuntimeError, OSError):
        cleanup_stack(stack)
        raise


def wait_postgis(
    pg_container: str, timeout: float = common.DEFAULT_BOOT_TIMEOUT_SECONDS
) -> None:
    """Poll pg_isready inside the PostGIS container until it accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = common.run_docker_unchecked(
            ["exec", pg_container, "pg_isready", "-U", common.POSTGRES_USER, "-d", common.POSTGRES_DB]
        )
        if proc.returncode == 0:
            return
        time.sleep(common.POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"PostGIS container {pg_container} not ready within {timeout}s")


def postgres_connection_string(pg_host: str) -> str:
    """Build the Npgsql connection string targeting the ephemeral PostGIS container."""
    return (
        f"Server={pg_host};Port=5432;Database={common.POSTGRES_DB};"
        f"User Id={common.POSTGRES_USER};Password={common.POSTGRES_PASSWORD};"
        "Timeout=2;Command Timeout=15;"
    )


def start_honua_container(launch: HonuaLaunchSpec) -> None:
    """Start a hardened read-only Honua Server container attached to launch.stack."""
    conn = postgres_connection_string(launch.stack.pg_container)
    base_env = (
        "ASPNETCORE_ENVIRONMENT=Production",
        "AllowedHosts=127.0.0.1;localhost;proxy.honua.test",
        "DataSource__Provider=postgres",
        f"ConnectionStrings__DefaultConnection={conn}",
        f"ConnectionStrings__honua={conn}",
        f"HONUA_ADMIN_PASSWORD={common.ADMIN_PASSWORD}",
        f"Security__ConnectionEncryption__MasterKey={common.CONNECTION_MASTER_KEY}",
        f"Operations__SecretChannel__KeyRingSecret={common.KEY_RING_SECRET}",
        "Experimental__Features__RedshiftProvider=false",
        "Experimental__Features__SnowflakeProvider=false",
        "Experimental__Features__DatabricksProvider=false",
        *launch.env_vars,
    )
    env_args = [token for item in base_env for token in ("-e", item)]
    common.run_docker([
        "run", "-d", "--name", launch.stack.app_container, "--network", launch.stack.network,
        "-p", f"127.0.0.1:{launch.stack.port}:8080",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp/honua:rw,noexec,nosuid,size=256m,uid=1001,gid=1001,mode=750",
        *launch.extra_args, *env_args, launch.image,
    ])


def read_container_logs(container_name: str) -> str:
    """Return combined stdout and stderr logs from a Docker container."""
    proc = common.run_docker_unchecked(["logs", container_name])
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def container_is_running(container_name: str) -> bool:
    """Return True when the named Docker container is currently in running state."""
    proc = common.run_docker_unchecked(
        ["inspect", "--format", "{{.State.Running}}", container_name]
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def cleanup_stack(stack: DockerStack) -> None:
    """Force-remove all containers and the network associated with stack."""
    for name in (*stack.extra_containers, stack.app_container, stack.pg_container):
        common.run_docker_unchecked(["rm", "-f", name])
    common.run_docker_unchecked(["network", "rm", stack.network])


def exec_psql(pg_container: str, sql: str) -> str:
    """Execute SQL in the ephemeral PostGIS database with ON_ERROR_STOP=1."""
    proc = subprocess.run(
        [
            "docker", "exec", "-i", pg_container, "psql",
            "-v", "ON_ERROR_STOP=1", "-U", common.POSTGRES_USER, "-d", common.POSTGRES_DB,
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def seed_probe_table(pg_container: str, table_name: str = "probe_sites") -> int:
    """Create and seed a PostGIS point table with asymmetric coordinates and a secret column."""
    sql = f"""
    CREATE EXTENSION IF NOT EXISTS postgis;
    DROP TABLE IF EXISTS public.{table_name};
    CREATE TABLE public.{table_name} (
        id serial PRIMARY KEY,
        name text NOT NULL,
        category text NOT NULL,
        secret_note text NOT NULL,
        population integer NOT NULL,
        geom geometry(Point, 4326) NOT NULL
    );
    INSERT INTO public.{table_name} (name, category, secret_note, population, geom) VALUES
        ('Honolulu Harbor', 'port', 'classified-alpha-001', 350000, ST_SetSRID(ST_MakePoint(-157.86, 21.31), 4326)),
        ('Hilo Bay', 'port', 'classified-beta-002', 45000, ST_SetSRID(ST_MakePoint(-155.08, 19.72), 4326)),
        ('Kahului Pier', 'port', 'classified-gamma-003', 28000, ST_SetSRID(ST_MakePoint(-156.47, 20.89), 4326)),
        ('Nawiliwili Dock', 'port', 'classified-delta-004', 12000, ST_SetSRID(ST_MakePoint(-159.35, 21.96), 4326)),
        ('Kawaihae Landing', 'port', 'classified-epsilon-005', 8500, ST_SetSRID(ST_MakePoint(-155.83, 20.04), 4326));
    """
    exec_psql(pg_container, sql)
    return 5


def _admin_headers(spec: PublishSpec) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if spec.auth_headers:
        headers.update(spec.auth_headers)
    else:
        headers["X-API-Key"] = common.ADMIN_PASSWORD
    return headers


def _create_connection(spec: PublishSpec, headers: dict[str, str]) -> str:
    payload = {
        "name": f"conn-{spec.service_name}-{int(time.time())}",
        "host": spec.pg_host,
        "port": 5432,
        "databaseName": common.POSTGRES_DB,
        "username": common.POSTGRES_USER,
        "password": common.POSTGRES_PASSWORD,
        "provider": "postgis",
        "sslRequired": False,
        "sslMode": "Disable",
    }
    reply, parsed = common.json_request(
        common.HttpSpec(
            "POST", f"{spec.base_url}/api/v1/admin/connections", headers, json.dumps(payload).encode()
        )
    )
    if reply.status not in (common.HTTP_OK, common.HTTP_CREATED) or not isinstance(parsed, dict):
        raise RuntimeError(f"create connection failed: HTTP {reply.status} {reply.body[:300]!r}")
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    conn_id = data.get("connectionId") or data.get("id")
    if not isinstance(conn_id, str) or not conn_id:
        raise RuntimeError(f"create connection missing id: {parsed}")
    return conn_id


def _extract_job_layer_id(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    state = str(data.get("status", "")).lower()
    if state in ("failed", "error"):
        raise RuntimeError(f"publish job failed: {data}")
    if state not in ("completed", "succeeded"):
        return None
    layer_id = data.get("storageLayerId") or data.get("layerId")
    return layer_id if isinstance(layer_id, int) else None


def _poll_publish_job(base_url: str, status_url: str, headers: dict[str, str]) -> int:
    url = status_url if status_url.startswith("http") else f"{base_url.rstrip('/')}{status_url}"
    deadline = time.monotonic() + common.DEFAULT_BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reply, parsed = common.json_request(common.HttpSpec("GET", url, headers))
        data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
        layer_id = _extract_job_layer_id(data) if reply.status == common.HTTP_OK else None
        if layer_id is not None:
            return layer_id
        time.sleep(common.POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"publish job at {url} did not complete in time")


def _extract_layer_id(spec: PublishSpec, reply: common.HttpReply, parsed: object) -> int:
    if not isinstance(parsed, dict):
        raise RuntimeError(f"publish layer returned non-JSON: HTTP {reply.status} {reply.body[:300]!r}")
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    if reply.status == common.HTTP_ACCEPTED and isinstance(data.get("statusUrl"), str):
        return _poll_publish_job(spec.base_url, data["statusUrl"], _admin_headers(spec))
    if reply.status not in (common.HTTP_OK, common.HTTP_CREATED):
        raise RuntimeError(f"publish layer failed: HTTP {reply.status} {reply.body[:300]!r}")
    layer_id = data.get("layerId") or data.get("storageLayerId")
    if not isinstance(layer_id, int):
        raise RuntimeError(f"publish layer missing layerId: {parsed}")
    return layer_id


def _configure_service_access(spec: PublishSpec, headers: dict[str, str]) -> None:
    if not spec.allow_anonymous:
        return
    body = json.dumps({"allowAnonymous": True}).encode()
    reply = common.http_request(
        common.HttpSpec(
            "PUT", f"{spec.base_url}/api/v1/admin/services/{spec.service_name}/access-policy", headers, body
        )
    )
    if reply.status != common.HTTP_OK:
        raise RuntimeError(f"set access-policy failed: HTTP {reply.status} {reply.body[:300]!r}")


def publish_postgis_layer(spec: PublishSpec) -> common.PublishedLayer:
    """Create a PostGIS connection and publish a real layer via the Admin API."""
    headers = _admin_headers(spec)
    conn_id = _create_connection(spec, headers)
    caps = ["Query", "Extract", "Create", "Update", "Delete"] if spec.editable else ["Query", "Extract"]
    payload = {
        "schema": "public",
        "table": spec.table_name,
        "layerName": spec.layer_name,
        "serviceName": spec.service_name,
        "geometryColumn": "geom",
        "geometryType": "Point",
        "srid": 4326,
        "primaryKey": "id",
        "createEditableCopy": spec.editable,
        "capabilities": caps,
        "enabled": True,
    }
    reply, parsed = common.json_request(
        common.HttpSpec(
            "POST",
            f"{spec.base_url}/api/v1/admin/connections/{conn_id}/layers",
            headers,
            json.dumps(payload).encode(),
        )
    )
    layer_id = _extract_layer_id(spec, reply, parsed)
    _configure_service_access(spec, headers)
    return common.PublishedLayer(conn_id, spec.service_name, layer_id, spec.layer_name)
