#!/usr/bin/env python3
"""Shared Docker, HTTP, JWT, and fail-closed proof helpers for RC2 live probes."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

import rc2_rigor as rigor

HONUA_IMAGE_REPO = "ghcr.io/honua-io/honua-server"
DEFAULT_POSTGIS_IMAGE = (
    "imresamu/postgis:17-3.5"
    "@sha256:35fe7dceda62bfe7d2965b0ba78d915ff9f901b5e0466d633ecc58421b5b5210"
)
DEFAULT_NGINX_IMAGE = (
    "nginx:1.27-alpine"
    "@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
)
POSTGRES_DB = "honua_rc2"
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "honua_rc2_probe_password"
ADMIN_PASSWORD = "HonuaRc2ProbeAdmin!2026"
CONNECTION_MASTER_KEY = "honua-rc2-probe-encryption-key-0123456789abcdef"
KEY_RING_SECRET = "honua-rc2-probe-operation-keyring-secret-0123456789abcdef"
OIDC_SIGNING_KEY = "honua-rc2-probe-oidc-hs256-signing-secret-key-2026"
OIDC_ISSUER = "https://idp.honua.test"
OIDC_AUDIENCE = "honua-rc2-probe"
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_PAYMENT_REQUIRED = 402
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
DEFAULT_BOOT_TIMEOUT_SECONDS = 90.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 1.0
JWT_LIFETIME_SECONDS = 3600


@dataclass(frozen=True)
class CandidateSpec:
    """Immutable candidate image and lock identifiers bound into every proof."""

    server_sha: str
    server_digest: str
    amd64_digest: str
    lock_digest: str
    postgis_image: str = DEFAULT_POSTGIS_IMAGE


@dataclass(frozen=True)
class HttpSpec:
    """Parameters for a single live HTTP request."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    ssl_context: ssl.SSLContext | None = None


@dataclass(frozen=True)
class HttpReply:
    """Observed HTTP status, lowercased headers, raw body, and transport error."""

    status: int
    headers: dict[str, str]
    body: bytes
    error: str = ""


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


@dataclass(frozen=True)
class PublishedLayer:
    """Metadata returned after publishing a PostGIS layer through the Admin API."""

    connection_id: str
    service_name: str
    layer_id: int
    layer_name: str


@dataclass(frozen=True)
class ProofTarget:
    """Destination and fail-closed validation context for an emitted proof file."""

    row: str
    candidate: CandidateSpec
    out_path: Path


def utc_now_iso() -> str:
    """Return the current UTC timestamp in RFC-3339 Z format."""
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def allocate_port() -> int:
    """Reserve a currently unused local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Execute one docker CLI command and capture text output."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _validate_candidate_format(spec: CandidateSpec) -> None:
    if not rigor.SHA_RE.fullmatch(spec.server_sha):
        raise ValueError(f"invalid server_sha: {spec.server_sha!r}")
    for name, value in (
        ("server_digest", spec.server_digest),
        ("amd64_digest", spec.amd64_digest),
        ("lock_digest", spec.lock_digest),
    ):
        if not rigor.DIGEST_RE.fullmatch(value):
            raise ValueError(f"invalid {name}: {value!r}")


def _verify_index_contains_amd64(server_digest: str, amd64_digest: str) -> None:
    index_ref = f"{HONUA_IMAGE_REPO}@{server_digest}"
    raw = run_docker(["buildx", "imagetools", "inspect", index_ref, "--raw"]).stdout
    doc = json.loads(raw)
    matches = [
        manifest["digest"]
        for manifest in doc.get("manifests", [])
        if manifest.get("platform", {}).get("os") == "linux"
        and manifest.get("platform", {}).get("architecture") == "amd64"
    ]
    if amd64_digest not in matches:
        raise ValueError(
            f"index {server_digest} does not list linux/amd64 {amd64_digest}; found {matches}"
        )


def _verify_image_labels(image_ref: str, expected_sha: str) -> None:
    raw = run_docker(
        ["image", "inspect", "--format", "{{json .Config.Labels}}", image_ref]
    ).stdout.strip()
    labels = json.loads(raw) if raw and raw != "null" else {}
    revision = labels.get("org.opencontainers.image.revision", "")
    if revision != expected_sha:
        raise ValueError(
            f"image {image_ref} revision {revision!r} does not match {expected_sha!r}"
        )
    compilation = labels.get("honua.runtime.compilation", "")
    if compilation != "native-aot":
        raise ValueError(
            f"image {image_ref} honua.runtime.compilation {compilation!r} != 'native-aot'"
        )


def verify_candidate_image(spec: CandidateSpec, verify_index: bool = True) -> str:
    """Verify candidate OCI index membership, commit SHA, and Native AOT label."""
    _validate_candidate_format(spec)
    if verify_index:
        _verify_index_contains_amd64(spec.server_digest, spec.amd64_digest)
    image_ref = f"{HONUA_IMAGE_REPO}@{spec.amd64_digest}"
    _verify_image_labels(image_ref, spec.server_sha)
    return image_ref


def http_request(spec: HttpSpec) -> HttpReply:
    """Execute one HTTP(S) request without raising on HTTP error status codes."""
    req = urllib.request.Request(
        spec.url, data=spec.body, headers=dict(spec.headers), method=spec.method
    )
    try:
        with urllib.request.urlopen(req, timeout=spec.timeout, context=spec.ssl_context) as resp:
            headers = {key.lower(): value for key, value in resp.headers.items()}
            return HttpReply(resp.status, headers, resp.read())
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
        return HttpReply(exc.code, headers, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HttpReply(0, {}, b"", error=str(exc))


def json_request(spec: HttpSpec) -> tuple[HttpReply, object]:
    """Send an HTTP request and parse JSON when the response body is valid JSON."""
    reply = http_request(spec)
    if not reply.body:
        return reply, None
    try:
        return reply, json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return reply, None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_hs256_jwt(claims: dict[str, object], secret: str = OIDC_SIGNING_KEY) -> str:
    """Mint an RFC-7519 HS256 JWT signed with the configured symmetric test key."""
    now_ts = int(time.time())
    payload = {
        "iss": OIDC_ISSUER,
        "aud": OIDC_AUDIENCE,
        "iat": now_ts,
        "nbf": now_ts - 5,
        "exp": now_ts + JWT_LIFETIME_SECONDS,
        **claims,
    }
    header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


def oidc_env_vars() -> tuple[str, ...]:
    """Return Honua Server environment variables enabling HS256 JWT authentication."""
    return (
        "Oidc__Enabled=true",
        "Oidc__RequireHttps=true",
        "Oidc__Generic__Enabled=true",
        f"Oidc__Generic__Authority={OIDC_ISSUER}",
        f"Oidc__Generic__ClientId={OIDC_AUDIENCE}",
        "Oidc__Generic__AcceptClientIdAsAudience=true",
        f"Oidc__TokenValidation__SymmetricSigningKey={OIDC_SIGNING_KEY}",
        f"Oidc__TokenValidation__ValidIssuers__0={OIDC_ISSUER}",
        f"Oidc__TokenValidation__ValidAudiences__0={OIDC_AUDIENCE}",
        "Oidc__TokenValidation__ValidateIssuer=true",
        "Oidc__TokenValidation__ValidateAudience=true",
        "Oidc__TokenValidation__ValidateLifetime=true",
        "Oidc__TokenValidation__ValidateIssuerSigningKey=true",
        "Oidc__TokenValidation__EnableTokenReplayProtection=false",
    )


def create_network_and_postgis(
    prefix: str, postgis_image: str, timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS
) -> DockerStack:
    """Create an isolated Docker bridge network and boot a ready PostGIS container."""
    port = allocate_port()
    tag = f"{prefix}-{int(time.time())}-{port}"
    stack = DockerStack(f"{tag}-net", f"{tag}-pg", f"{tag}-app", port)
    try:
        run_docker(["network", "create", stack.network])
        run_docker([
            "run", "-d", "--name", stack.pg_container, "--network", stack.network,
            "-e", f"POSTGRES_DB={POSTGRES_DB}",
            "-e", f"POSTGRES_USER={POSTGRES_USER}",
            "-e", f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
            postgis_image,
        ])
        wait_postgis(stack.pg_container, timeout)
        return stack
    except (subprocess.SubprocessError, RuntimeError, OSError):
        cleanup_stack(stack)
        raise


def wait_postgis(pg_container: str, timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS) -> None:
    """Poll pg_isready inside the PostGIS container until it accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = run_docker(
            ["exec", pg_container, "pg_isready", "-U", POSTGRES_USER, "-d", POSTGRES_DB],
            check=False,
        )
        if proc.returncode == 0:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"PostGIS container {pg_container} not ready within {timeout}s")


def postgres_connection_string(pg_host: str) -> str:
    """Build the Npgsql connection string targeting the ephemeral PostGIS container."""
    return (
        f"Server={pg_host};Port=5432;Database={POSTGRES_DB};"
        f"User Id={POSTGRES_USER};Password={POSTGRES_PASSWORD};"
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
        f"HONUA_ADMIN_PASSWORD={ADMIN_PASSWORD}",
        f"Security__ConnectionEncryption__MasterKey={CONNECTION_MASTER_KEY}",
        f"Operations__SecretChannel__KeyRingSecret={KEY_RING_SECRET}",
        "Experimental__Features__RedshiftProvider=false",
        "Experimental__Features__SnowflakeProvider=false",
        "Experimental__Features__DatabricksProvider=false",
        *launch.env_vars,
    )
    env_args = [token for item in base_env for token in ("-e", item)]
    run_docker([
        "run", "-d", "--name", launch.stack.app_container, "--network", launch.stack.network,
        "-p", f"127.0.0.1:{launch.stack.port}:8080",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp/honua:rw,noexec,nosuid,size=256m,uid=1001,gid=1001,mode=750",
        *launch.extra_args, *env_args, launch.image,
    ])


def wait_http_status(
    url: str, expected: int = HTTP_OK, timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS
) -> int:
    """Wait until GET url returns expected status code, or raise RuntimeError."""
    deadline = time.monotonic() + timeout
    last_status = 0
    while time.monotonic() < deadline:
        reply = http_request(HttpSpec("GET", url, timeout=3.0))
        last_status = reply.status
        if reply.status == expected:
            return reply.status
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"{url} did not return HTTP {expected} within {timeout}s (last={last_status})")


def read_container_logs(container_name: str) -> str:
    """Return combined stdout and stderr logs from a Docker container."""
    proc = run_docker(["logs", container_name], check=False)
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def container_is_running(container_name: str) -> bool:
    """Return True when the named Docker container is currently in running state."""
    proc = run_docker(
        ["inspect", "--format", "{{.State.Running}}", container_name], check=False
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def cleanup_stack(stack: DockerStack) -> None:
    """Force-remove all containers and the network associated with stack."""
    for name in (*stack.extra_containers, stack.app_container, stack.pg_container):
        run_docker(["rm", "-f", name], check=False)
    run_docker(["network", "rm", stack.network], check=False)


def exec_psql(pg_container: str, sql: str) -> str:
    """Execute SQL in the ephemeral PostGIS database with ON_ERROR_STOP=1."""
    proc = subprocess.run(
        [
            "docker", "exec", "-i", pg_container, "psql",
            "-v", "ON_ERROR_STOP=1", "-U", POSTGRES_USER, "-d", POSTGRES_DB,
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
        headers["X-API-Key"] = ADMIN_PASSWORD
    return headers


def _create_connection(spec: PublishSpec, headers: dict[str, str]) -> str:
    payload = {
        "name": f"conn-{spec.service_name}-{int(time.time())}",
        "host": spec.pg_host,
        "port": 5432,
        "databaseName": POSTGRES_DB,
        "username": POSTGRES_USER,
        "password": POSTGRES_PASSWORD,
        "provider": "postgis",
        "sslRequired": False,
        "sslMode": "Disable",
    }
    reply, parsed = json_request(
        HttpSpec("POST", f"{spec.base_url}/api/v1/admin/connections", headers, json.dumps(payload).encode())
    )
    if reply.status not in (HTTP_OK, HTTP_CREATED) or not isinstance(parsed, dict):
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
    deadline = time.monotonic() + DEFAULT_BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reply, parsed = json_request(HttpSpec("GET", url, headers))
        data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
        layer_id = _extract_job_layer_id(data) if reply.status == HTTP_OK else None
        if layer_id is not None:
            return layer_id
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"publish job at {url} did not complete in time")


def _extract_layer_id(spec: PublishSpec, reply: HttpReply, parsed: object) -> int:
    if not isinstance(parsed, dict):
        raise RuntimeError(f"publish layer returned non-JSON: HTTP {reply.status} {reply.body[:300]!r}")
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    if reply.status == HTTP_ACCEPTED and isinstance(data.get("statusUrl"), str):
        return _poll_publish_job(spec.base_url, data["statusUrl"], _admin_headers(spec))
    if reply.status not in (HTTP_OK, HTTP_CREATED):
        raise RuntimeError(f"publish layer failed: HTTP {reply.status} {reply.body[:300]!r}")
    layer_id = data.get("layerId") or data.get("storageLayerId")
    if not isinstance(layer_id, int):
        raise RuntimeError(f"publish layer missing layerId: {parsed}")
    return layer_id


def _configure_service_access(spec: PublishSpec, headers: dict[str, str]) -> None:
    if not spec.allow_anonymous:
        return
    body = json.dumps({"allowAnonymous": True}).encode()
    reply = http_request(
        HttpSpec("PUT", f"{spec.base_url}/api/v1/admin/services/{spec.service_name}/access-policy", headers, body)
    )
    if reply.status != HTTP_OK:
        raise RuntimeError(f"set access-policy failed: HTTP {reply.status} {reply.body[:300]!r}")


def publish_postgis_layer(spec: PublishSpec) -> PublishedLayer:
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
    reply, parsed = json_request(
        HttpSpec(
            "POST",
            f"{spec.base_url}/api/v1/admin/connections/{conn_id}/layers",
            headers,
            json.dumps(payload).encode(),
        )
    )
    layer_id = _extract_layer_id(spec, reply, parsed)
    _configure_service_access(spec, headers)
    return PublishedLayer(conn_id, spec.service_name, layer_id, spec.layer_name)


def build_identity_fields(spec: CandidateSpec, observed_at: str | None = None) -> dict[str, str]:
    """Build the candidate identity header required by tools/rc2_rigor.py."""
    return {
        "serverSha": spec.server_sha,
        "imageDigest": spec.server_digest,
        "platformLockDigest": spec.lock_digest,
        "observedAt": observed_at or utc_now_iso(),
    }


def validate_and_write_proof(target: ProofTarget, proof: dict[str, object]) -> dict[str, object]:
    """Validate proof fail-closed via rc2_rigor.build_receipt before writing JSON."""
    spec = target.candidate
    context = rigor.ReceiptContext(target.row, spec.server_sha, spec.server_digest, spec.lock_digest)
    try:
        rigor.build_receipt(context, proof)
    except ValueError as exc:
        raise ValueError(f"{exc}: {json.dumps(proof, sort_keys=True)}") from exc
    target.out_path.parent.mkdir(parents=True, exist_ok=True)
    target.out_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof
