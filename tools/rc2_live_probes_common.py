#!/usr/bin/env python3
"""Shared candidate verification, HTTP, JWT, and fail-closed proof helpers for RC2 probes."""
from __future__ import annotations

import argparse
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


def run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute one docker CLI command and raise on non-zero exit."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True)


def run_docker_unchecked(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute one docker CLI command without raising on non-zero exit."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


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


def verify_candidate_image(spec: CandidateSpec) -> str:
    """Verify candidate OCI index membership, commit SHA, and Native AOT label."""
    _validate_candidate_format(spec)
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


def build_identity_fields(spec: CandidateSpec, observed_at: str | None = None) -> dict[str, str]:
    """Build the candidate identity header required by tools/rc2_rigor.py."""
    return {
        "serverSha": spec.server_sha,
        "imageDigest": spec.server_digest,
        "platformLockDigest": spec.lock_digest,
        "observedAt": observed_at or utc_now_iso(),
    }


def build_candidate_parser(description: str) -> argparse.ArgumentParser:
    """Create an argument parser pre-populated with shared RC2 candidate flags."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--server-sha", required=True)
    parser.add_argument("--server-digest", required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--postgis-image", default=DEFAULT_POSTGIS_IMAGE)
    parser.add_argument("--boot-timeout", type=float, default=DEFAULT_BOOT_TIMEOUT_SECONDS)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def candidate_from_args(args: argparse.Namespace) -> CandidateSpec:
    """Build a CandidateSpec from parsed shared RC2 candidate CLI arguments."""
    return CandidateSpec(
        args.server_sha,
        args.server_digest,
        args.amd64_digest,
        args.lock_digest,
        args.postgis_image,
    )


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
