#!/usr/bin/env python3
"""Produce the local-Docker half of the 2026.1 AI delivery arc.

This is a producer, not the release verdict. It deliberately does not consume
AWS artifacts. The release train downloads this job's outputs together with the
cloud producer's outputs and runs the strict aggregate checker only after both
producers have finished.

The manifest-pinned SDK remains the journey implementation. This wrapper owns
only the local installation boundary, the Studio -> Console -> SDK handoff, and
safe teardown of the ephemeral Docker installation.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "e2e" / "out" / "ai-delivery-arc-local"
SHA = re.compile(r"[0-9a-f]{40}")
RUN_URL = re.compile(
    r"https://github\.com/honua-io/honua-release/actions/runs/[1-9][0-9]*"
)
PROVIDERS = {"anthropic", "bedrock", "openai"}
CLOUDFLARED_IMAGE = (
    # cloudflared 2026.8.2 multi-architecture manifest.
    "cloudflare/cloudflared@"
    "sha256:0aa26e284f05e6c77ae375b8c9c11d9eb6a448fb7bcd8d40f31cb6176189eb38"
)
QUICK_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CHILD_CREDENTIAL_ENV = (
    "HONUA_AI_ARC_PREPARE_CREDENTIAL",
    "HONUA_AI_ARC_CONSOLE_TOKEN",
    "HONUA_ADMIN_KEY",
    "HONUA_API_KEY",
    "HONUA_AI_PROVIDER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "HONUA_AWS_ROLE_ARN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)


class ProducerBlocked(RuntimeError):
    """A required candidate/input is not integrated yet."""


class ProducerFailed(RuntimeError):
    """An integrated producer ran and failed."""


@dataclass(frozen=True)
class Paths:
    out: Path
    install: Path
    checkpoint: Path
    sdk_receipt: Path
    console_receipt: Path
    sdk_console_receipt: Path
    console_evidence: Path
    model_handoff: Path
    model_evidence: Path
    model_receipt: Path
    report: Path
    tunnel_marker: Path


@dataclass(frozen=True)
class PublicTunnel:
    container: str
    origin: str
    target: str


def output_paths(out: Path) -> Paths:
    return Paths(
        out=out,
        install=out / "install",
        checkpoint=out / "checkpoint.json",
        sdk_receipt=out / "sdk-journey.json",
        console_receipt=out / "console-receipt.json",
        sdk_console_receipt=out / "sdk-console-receipt.json",
        console_evidence=out / "console-evidence.json",
        model_handoff=out / "real-model-handoff.json",
        model_evidence=out / "real-model-evidence.json",
        model_receipt=out / "real-model-receipt.json",
        report=out / "producer-report.json",
        tunnel_marker=out / "tunnel-container",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_head(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProducerBlocked(
            f"component checkout identity is unavailable: {path}"
        ) from error
    return result.stdout.strip()


def public_https_origin(value: str, label: str) -> str:
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as error:
        raise ProducerBlocked(f"{label} is not a valid URL") from error
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ProducerBlocked(f"{label} must use a canonical public hostname") from error
    if (
        parsed.scheme != "https"
        or not host
        or "%" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ProducerBlocked(f"{label} must be a credential-free HTTPS origin")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".localhost", ".local", ".internal", ".localdomain")
    ):
        raise ProducerBlocked(f"{label} must be publicly routable")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if all(re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", item) for item in labels):
            raise ProducerBlocked(
                f"{label} must not use an ambiguous numeric IP address"
            )
        if "." not in host:
            raise ProducerBlocked(f"{label} must be publicly routable")
        address = None
    if address is not None and (not address.is_global or address.is_multicast):
        raise ProducerBlocked(f"{label} must not use a non-public IP address")
    return value.rstrip("/")


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ProducerBlocked(
            f"{name} is required for the local AI delivery-arc producer"
        )
    return value


def _local_port(environment: dict[str, str]) -> str:
    port = environment.get("E2E_AI_LOCAL_PORT", "8080")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ProducerBlocked("E2E_AI_LOCAL_PORT must be a valid TCP port")
    return port


def _credential_isolated_environment(source: dict[str, str]) -> dict[str, str]:
    environment = dict(source)
    for name in CHILD_CREDENTIAL_ENV:
        environment.pop(name, None)
    environment.update(
        {
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return environment


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ProducerFailed("platform manifest is unavailable or invalid") from error
    if not isinstance(value, dict) or not isinstance(value.get("components"), dict):
        raise ProducerFailed("platform manifest has no component inventory")
    return value


def _component_root(
    env: dict[str, str], manifest: dict[str, Any], name: str, env_name: str
) -> Path:
    root = Path(_required(env, env_name)).resolve()
    expected = str((manifest["components"].get(name) or {}).get("sha", ""))
    if not SHA.fullmatch(expected) or _git_head(root) != expected:
        raise ProducerBlocked(f"{name} checkout is not at its exact manifest pin")
    return root


def _producer_contract_ready(studio: Path, console: Path) -> None:
    studio_source = studio / "scripts" / "real-model-ai-arc.mjs"
    console_source = console / "e2e" / "playwright" / "live" / "console-receipt-cli.mjs"
    try:
        studio_text = studio_source.read_text(encoding="utf-8")
        console_text = console_source.read_text(encoding="utf-8")
    except OSError as error:
        raise ProducerBlocked(
            "manifest-pinned Studio/Console receipt producers are absent"
        ) from error
    studio_library = studio / "scripts" / "lib" / "real-model-ai-arc.mjs"
    try:
        studio_library_text = studio_library.read_text(encoding="utf-8")
    except OSError as error:
        raise ProducerBlocked(
            "manifest-pinned Studio real-model library is absent"
        ) from error
    if (
        "HONUA_AI_ARC_REAL_MODEL_HANDOFF" not in studio_text
        or "resume is credential-free" not in studio_text
        or 'id: "local-docker-real-model-ai-arc"' not in studio_library_text
        or "HONUA_AI_ARC_CONSOLE_EVIDENCE" not in console_text
    ):
        raise ProducerBlocked(
            "manifest-pinned Studio/Console revisions predate the sealed local receipt handoff"
        )


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    label: str,
    cwd: Path | None = None,
    expected: tuple[int, ...] = (0,),
) -> None:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, check=False)
    except OSError as error:
        raise ProducerFailed(f"{label} could not start") from error
    if result.returncode not in expected:
        raise ProducerFailed(
            f"{label} exited {result.returncode}; expected {', '.join(map(str, expected))}"
        )


def _sdk_command(
    sdk: Path,
    paths: Paths,
    *,
    endpoint: str,
    fixture_url: str,
    candidate_id: str,
    release_id: str,
    resume: bool,
) -> list[str]:
    command = [
        "npm",
        "run",
        "release:zero-to-map",
        "--",
        "--execute",
        "--yes",
        "--target",
        "local-docker",
        "--mcp-url",
        f"{endpoint}/mcp",
        "--honua-command",
        str(Path(__file__).resolve()),
        "--var",
        f"installDirectory={paths.install}",
        "--var",
        "dbHost=postgres",
        "--var",
        f"fixtureBaseUrl={fixture_url}",
        "--var-env",
        "dbPassword=HONUA_ZERO_TO_MAP_DB_PASSWORD",
        "--var",
        f"candidateId={candidate_id}",
        "--var",
        f"releaseId={release_id}",
        "--checkpoint",
        str(paths.checkpoint),
        "--output",
        str(paths.sdk_receipt),
    ]
    if resume:
        checkpoint = json.loads(paths.checkpoint.read_text(encoding="utf-8"))
        digest = (checkpoint.get("integrity") or {}).get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProducerFailed("SDK checkpoint has no valid integrity digest")
        command.extend(
            (
                "--checkpoint-digest",
                digest,
                "--console-receipt",
                str(paths.sdk_console_receipt),
            )
        )
    return command


def _write_install_environment(
    paths: Paths,
    manifest: dict[str, Any],
    environment: dict[str, str],
    *,
    db_password: str,
    admin_key: str,
) -> None:
    server = manifest["components"].get("honua-server") or {}
    image = str(server.get("image", ""))
    digest = str(server.get("digest", ""))
    if not image or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ProducerBlocked(
            "local producer requires the exact manifest server image@digest"
        )
    paths.install.mkdir(parents=True, exist_ok=True)
    http_port = _local_port(environment)
    env_file = paths.install / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"HONUA_SERVER_IMAGE={image}@{digest}",
                f"HONUA_HTTP_PORT={http_port}",
                f"POSTGRES_PASSWORD={db_password}",
                f"HONUA_ADMIN_PASSWORD={admin_key}",
                f"HONUA_CONNECTION_ENCRYPTION_MASTER_KEY={secrets.token_hex(32)}",
                "",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    # The manifest-pinned SDK owns the journey, but its generated local installer
    # can legitimately lag the release manifest's server image. The release repo
    # therefore owns this one ephemeral installation boundary and feeds its
    # command result back into the unchanged SDK journey.
    compose = {
        "name": "honua-ai-arc-local",
        "services": {
            "postgres": {
                "image": "pgrouting/pgrouting:17-3.5-3.7.3",
                "environment": {
                    "POSTGRES_DB": "honua",
                    "POSTGRES_USER": "honua",
                    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
                },
                "volumes": ["postgres_data:/var/lib/postgresql/data"],
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U honua -d honua"],
                    "interval": "5s",
                    "timeout": "5s",
                    "retries": 20,
                },
            },
            "redis": {
                "image": "redis:7.4-alpine",
                "command": [
                    "redis-server",
                    "--appendonly",
                    "yes",
                    "--maxmemory",
                    "64mb",
                    "--maxmemory-policy",
                    "noeviction",
                ],
                "volumes": ["redis_data:/data"],
                "healthcheck": {
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 20,
                },
            },
            "honua": {
                "image": "${HONUA_SERVER_IMAGE}",
                "ports": ["127.0.0.1:${HONUA_HTTP_PORT}:8080"],
                "environment": {
                    "ASPNETCORE_ENVIRONMENT": "Development",
                    "ConnectionStrings__DefaultConnection": (
                        "Host=postgres;Database=honua;Username=honua;"
                        "Password=${POSTGRES_PASSWORD}"
                    ),
                    "ConnectionStrings__Redis": "redis:6379",
                    "HONUA_ADMIN_PASSWORD": "${HONUA_ADMIN_PASSWORD}",
                    "Security__ConnectionEncryption__MasterKey": (
                        "${HONUA_CONNECTION_ENCRYPTION_MASTER_KEY}"
                    ),
                    "Database__MigrationSafety__ContractApplyPolicy": "Gate",
                    "Licensing__DevGrantEdition": "Pro",
                    "FileStorage__Provider": "Local",
                    "FileStorage__LocalStorage__BasePath": "/var/lib/honua/storage",
                    "Kestrel__Endpoints__Http__Url": "http://+:8080",
                    "Kestrel__Endpoints__Http__Protocols": "Http1",
                },
                "depends_on": {
                    "postgres": {"condition": "service_healthy"},
                    "redis": {"condition": "service_healthy"},
                },
                "volumes": ["honua_storage:/var/lib/honua/storage"],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "wget",
                        "--no-verbose",
                        "--tries=1",
                        "--spider",
                        "http://localhost:8080/healthz/live",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 20,
                },
            },
        },
        "volumes": {
            "postgres_data": None,
            "redis_data": None,
            "honua_storage": None,
        },
    }
    compose_path = paths.install / "compose.yaml"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _patch_local_compose(compose_path, environment)


def _producer_environment(
    source: dict[str, str],
    paths: Paths,
    *,
    manifest_path: Path,
    sdk: Path,
    endpoint: str,
    console_origin: str,
    evidence_url: str,
) -> dict[str, str]:
    environment = _credential_isolated_environment(source)
    environment.update(
        {
            "HONUA_PLATFORM_MANIFEST": str(manifest_path),
            "HONUA_AI_ARC_SDK_PLAN": str(
                sdk / "mcp" / "release" / "zero-to-map" / "journey.v1.json"
            ),
            "HONUA_AI_ARC_CHECKPOINT": str(paths.checkpoint),
            "HONUA_AI_ARC_ENDPOINT": endpoint,
            "HONUA_AI_ARC_CONSOLE_ORIGIN": console_origin,
            "HONUA_AI_ARC_CONSOLE_RECEIPT": str(paths.console_receipt),
            "HONUA_AI_ARC_SDK_CONSOLE_RECEIPT": str(paths.sdk_console_receipt),
            "HONUA_AI_ARC_CONSOLE_EVIDENCE": str(paths.console_evidence),
            "HONUA_AI_ARC_CONSOLE_RECEIPT_SCHEMA": str(
                sdk
                / "mcp"
                / "release"
                / "zero-to-map"
                / "contracts"
                / "console-receipt.schema.json"
            ),
            "HONUA_AI_ARC_REAL_MODEL_HANDOFF": str(paths.model_handoff),
            "HONUA_AI_ARC_REAL_MODEL_EVIDENCE": str(paths.model_evidence),
            "HONUA_AI_ARC_REAL_MODEL_RECEIPT": str(paths.model_receipt),
            "HONUA_AI_ARC_EVIDENCE_URL": evidence_url,
        }
    )
    return environment


def _studio_resume_environment(source: dict[str, str]) -> dict[str, str]:
    environment = dict(source)
    for name in (
        "HONUA_ADMIN_KEY",
        "HONUA_API_KEY",
        "HONUA_AI_ARC_PREPARE_CREDENTIAL",
        "HONUA_AI_ARC_CONSOLE_TOKEN",
        "HONUA_AI_PROVIDER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        environment.pop(name, None)
    # The sealed-handoff Studio contract validated by _producer_contract_ready()
    # makes resume explicitly credential-free. The scoped prepare credential is
    # never exposed after the immutable handoff has been created.
    return environment


def _write_report(
    paths: Paths,
    status: str,
    why: str,
    manifest: dict[str, Any] | None,
    manifest_path: Path,
    tunnel: PublicTunnel | None = None,
) -> None:
    report = {
        "schemaVersion": "honua.ai-delivery-arc-local-producer/v1",
        "status": status,
        "why": why,
        "target": "local-docker",
        **(
            {
                "candidateId": f"manifest-sha256:{_sha256(manifest_path)}",
                "releaseId": manifest.get("platformRelease"),
            }
            if manifest is not None
            else {}
        ),
        **(
            {
                "publicRoute": {
                    "origin": tunnel.origin,
                    "target": tunnel.target,
                    "tunnelImage": CLOUDFLARED_IMAGE,
                }
            }
            if tunnel is not None
            else {}
        ),
    }
    paths.out.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _start_public_tunnel(paths: Paths, env: dict[str, str]) -> PublicTunnel:
    target = f"http://127.0.0.1:{_local_port(env)}"
    container = f"honua-ai-arc-tunnel-{secrets.token_hex(8)}"
    command = [
        "docker",
        "run",
        "--detach",
        "--network",
        "host",
        "--name",
        container,
        CLOUDFLARED_IMAGE,
        "tunnel",
        "--no-autoupdate",
        "--protocol",
        "http2",
        "--url",
        target,
    ]
    try:
        result = subprocess.run(
            command,
            env=_credential_isolated_environment(env),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ProducerFailed("public tunnel could not start") from error
    if result.returncode != 0:
        raise ProducerFailed("pinned public tunnel container failed to start")
    paths.out.mkdir(parents=True, exist_ok=True)
    paths.tunnel_marker.write_text(container + "\n", encoding="utf-8")

    for _ in range(45):
        logs = subprocess.run(
            ["docker", "logs", container],
            env=_credential_isolated_environment(env),
            check=False,
            capture_output=True,
            text=True,
        )
        match = QUICK_TUNNEL_URL.search(logs.stdout + "\n" + logs.stderr)
        if match is not None:
            origin = public_https_origin(match.group(0), "generated local tunnel")
            return PublicTunnel(container=container, origin=origin, target=target)
        running = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            env=_credential_isolated_environment(env),
            check=False,
            capture_output=True,
            text=True,
        )
        if running.returncode != 0 or running.stdout.strip() != "true":
            raise ProducerFailed("public tunnel exited before publishing an origin")
        time.sleep(1)
    raise ProducerFailed("public tunnel did not publish an origin within 45 seconds")


def _start_candidate(paths: Paths, env: dict[str, str]) -> None:
    compose = paths.install / "compose.yaml"
    _run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(paths.install),
            "--file",
            str(compose),
            "up",
            "-d",
            "--wait",
        ],
        env=env,
        label="local candidate installation",
    )


def _verify_public_route(
    tunnel: PublicTunnel, env: dict[str, str], *, attempts: int = 30
) -> None:
    inspected = subprocess.run(
        ["docker", "inspect", tunnel.container],
        env=_credential_isolated_environment(env),
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        records = json.loads(inspected.stdout)
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            raise ValueError("docker inspect did not return an object")
        record = records[0]
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProducerFailed("public tunnel identity is unavailable") from error
    command = (record.get("Config") or {}).get("Cmd") or []
    if (
        inspected.returncode != 0
        or not (record.get("State") or {}).get("Running")
        or (record.get("Config") or {}).get("Image") != CLOUDFLARED_IMAGE
        or (record.get("HostConfig") or {}).get("NetworkMode") != "host"
        or tunnel.target not in command
    ):
        raise ProducerFailed("public tunnel is not bound to the exact local candidate port")

    local_url = f"{tunnel.target}/healthz/ready"
    public_url = f"{tunnel.origin}/healthz/ready"
    try:
        with urlopen(local_url, timeout=10) as response:
            local_ready = (
                response.status == 200
                and response.read().strip().lower() == b"ready"
            )
    except OSError as error:
        raise ProducerFailed("local candidate readiness route is unavailable") from error
    if not local_ready:
        raise ProducerFailed("local candidate readiness route did not return ready")

    for attempt in range(attempts):
        try:
            with urlopen(public_url, timeout=10) as response:
                if (
                    response.status == 200
                    and response.read().strip().lower() == b"ready"
                ):
                    return
        except OSError:
            pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise ProducerFailed(
        "public tunnel does not route to the local candidate readiness endpoint"
    )


def _teardown(paths: Paths, env: dict[str, str]) -> None:
    errors: list[str] = []
    compose = paths.install / "compose.yaml"
    if compose.is_file():
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(paths.install),
                "--file",
                str(compose),
                "down",
                "--volumes",
            ],
            env=env,
            check=False,
        )
        if result.returncode != 0:
            errors.append("local candidate teardown failed")
    if paths.tunnel_marker.is_file():
        try:
            container = paths.tunnel_marker.read_text(encoding="utf-8").strip()
        except OSError:
            container = ""
        if not re.fullmatch(r"honua-ai-arc-tunnel-[0-9a-f]{16}", container):
            errors.append("public tunnel cleanup marker is invalid")
        else:
            result = subprocess.run(
                ["docker", "rm", "--force", container],
                env=_credential_isolated_environment(env),
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                errors.append("public tunnel teardown failed")
            else:
                try:
                    paths.tunnel_marker.unlink()
                except OSError:
                    errors.append("public tunnel cleanup marker could not be removed")
    if errors:
        raise ProducerFailed("; ".join(errors))


def produce(env: dict[str, str] | None = None) -> int:
    environment = dict(os.environ if env is None else env)
    paths = output_paths(
        Path(environment.get("E2E_AI_LOCAL_OUT", str(DEFAULT_OUT))).resolve()
    )
    manifest_path = Path(
        environment.get(
            "HONUA_PLATFORM_MANIFEST", str(REPO_ROOT / "platform-manifest.yaml")
        )
    )
    manifest: dict[str, Any] | None = None
    tunnel: PublicTunnel | None = None
    exit_code = 1
    try:
        manifest = _load_manifest(manifest_path)
        sdk = _component_root(environment, manifest, "honua-sdk-js", "E2E_SDK_JS_DIR")
        studio = _component_root(
            environment, manifest, "honua-studio", "E2E_STUDIO_DIR"
        )
        console = _component_root(
            environment, manifest, "honua-console", "E2E_CONSOLE_DIR"
        )
        _producer_contract_ready(studio, console)

        console_origin = public_https_origin(
            _required(environment, "HONUA_AI_ARC_CONSOLE_ORIGIN"),
            "HONUA_AI_ARC_CONSOLE_ORIGIN",
        )
        evidence_url = _required(environment, "HONUA_RUN_URL")
        if not RUN_URL.fullmatch(evidence_url):
            raise ProducerBlocked(
                "HONUA_RUN_URL must identify this immutable honua-release Actions run"
            )
        provider = _required(environment, "HONUA_AI_PROVIDER")
        if provider not in PROVIDERS:
            raise ProducerBlocked(
                "HONUA_AI_PROVIDER must be anthropic, bedrock, or openai"
            )
        _required(environment, "HONUA_AI_MODEL")
        if provider in {"anthropic", "openai"}:
            _required(environment, "HONUA_AI_PROVIDER_API_KEY")
        elif not all(
            environment.get(name)
            for name in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
            )
        ):
            raise ProducerBlocked(
                "Bedrock requires temporary AWS credentials for the local candidate container"
            )
        console_token = _required(environment, "HONUA_AI_ARC_LOCAL_CONSOLE_TOKEN")
        prepare_credential = _required(
            environment, "HONUA_AI_ARC_LOCAL_PREPARE_CREDENTIAL"
        )
        if console_token == prepare_credential:
            raise ProducerBlocked(
                "local prepare and Console credentials must be distinct"
            )

        tunnel = _start_public_tunnel(paths, environment)
        endpoint = tunnel.origin
        environment["HONUA_AI_ARC_LOCAL_ORIGIN"] = endpoint

        candidate_id = f"manifest-sha256:{_sha256(manifest_path)}"
        sdk_sha = manifest["components"]["honua-sdk-js"]["sha"]
        fixture_url = (
            environment.get("HONUA_AI_ARC_FIXTURE_BASE_URL")
            or (
                "https://raw.githubusercontent.com/honua-io/honua-sdk-js/"
                f"{sdk_sha}/mcp/release/zero-to-map/fixtures"
            )
        ).rstrip("/")
        if not fixture_url.startswith("https://"):
            raise ProducerBlocked("HONUA_AI_ARC_FIXTURE_BASE_URL must use HTTPS")

        db_password = secrets.token_urlsafe(32)
        _write_install_environment(
            paths,
            manifest,
            environment,
            db_password=db_password,
            admin_key=prepare_credential,
        )
        _start_candidate(paths, environment)
        _verify_public_route(tunnel, environment)
        sdk_env = {
            **_credential_isolated_environment(environment),
            "E2E_AI_LOCAL_INSTALL": str(paths.install),
            "HONUA_SOURCE_REVISION": sdk_sha,
            "HONUA_ZERO_TO_MAP_DB_PASSWORD": db_password,
            "HONUA_ADMIN_KEY": prepare_credential,
        }
        prepare_sdk = _sdk_command(
            sdk,
            paths,
            endpoint=endpoint,
            fixture_url=fixture_url,
            candidate_id=candidate_id,
            release_id=str(manifest.get("platformRelease", "")),
            resume=False,
        )
        _run(
            prepare_sdk,
            env=sdk_env,
            label="local deterministic SDK prepare",
            cwd=sdk / "mcp",
            expected=(2,),
        )

        producer_env = _producer_environment(
            environment,
            paths,
            manifest_path=manifest_path,
            sdk=sdk,
            endpoint=endpoint,
            console_origin=console_origin,
            evidence_url=evidence_url,
        )
        producer_env["HONUA_AI_ARC_PREPARE_CREDENTIAL"] = prepare_credential
        _run(
            [
                "npm",
                "run",
                "release:real-model-ai-arc",
                "--",
                "prepare",
                "--execute",
                "--yes",
            ],
            env=producer_env,
            label="local real-model Studio prepare",
            cwd=studio,
            expected=(2,),
        )

        console_env = dict(producer_env)
        for name in (
            "HONUA_ADMIN_KEY",
            "HONUA_API_KEY",
            "HONUA_AI_ARC_PREPARE_CREDENTIAL",
            "HONUA_AI_PROVIDER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            console_env.pop(name, None)
        console_env["HONUA_AI_ARC_CONSOLE_TOKEN"] = console_token
        _run(
            ["npm", "--prefix", "e2e/playwright", "run", "receipt:console"],
            env=console_env,
            label="local focused Console producer",
            cwd=console,
        )

        resume_env = _studio_resume_environment(producer_env)
        _run(
            [
                "npm",
                "run",
                "release:real-model-ai-arc",
                "--",
                "resume",
                "--execute",
                "--yes",
            ],
            env=resume_env,
            label="local real-model Studio resume",
            cwd=studio,
        )

        _run(
            _sdk_command(
                sdk,
                paths,
                endpoint=endpoint,
                fixture_url=fixture_url,
                candidate_id=candidate_id,
                release_id=str(manifest.get("platformRelease", "")),
                resume=True,
            ),
            env=sdk_env,
            label="local deterministic SDK resume",
            cwd=sdk / "mcp",
        )
        receipt = json.loads(paths.model_receipt.read_text(encoding="utf-8"))
        if (
            receipt.get("schemaVersion") != "honua.local-docker.real-model-ai-arc/v1"
            or receipt.get("id") != "local-docker-real-model-ai-arc"
            or receipt.get("status") != "passed"
        ):
            raise ProducerFailed(
                "Studio did not emit the governed local real-model receipt"
            )
        _write_report(
            paths,
            "pass",
            "local producer completed; aggregate verdict deferred",
            manifest,
            manifest_path,
            tunnel,
        )
        exit_code = 0
    except ProducerBlocked as error:
        _write_report(paths, "blocked", str(error), manifest, manifest_path, tunnel)
        print(f"AI delivery arc local producer: BLOCKED ({error})")
        exit_code = 0
    except (OSError, ValueError, json.JSONDecodeError, ProducerFailed) as error:
        _write_report(paths, "fail", str(error), manifest, manifest_path, tunnel)
        print(f"AI delivery arc local producer: FAIL ({error})", file=sys.stderr)
        exit_code = 1
    finally:
        try:
            _teardown(paths, environment)
        except ProducerFailed as error:
            _write_report(paths, "fail", str(error), manifest, manifest_path, tunnel)
            print(f"AI delivery arc local producer: FAIL ({error})", file=sys.stderr)
            exit_code = 1
    return exit_code


def _patch_local_compose(compose_path: Path, env: dict[str, str]) -> None:
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        server = compose["services"]["honua"]
        server_env = server.setdefault("environment", {})
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        raise ProducerFailed(
            "SDK local installer did not produce the expected compose service"
        ) from error
    provider = _required(env, "HONUA_AI_PROVIDER")
    model = _required(env, "HONUA_AI_MODEL")
    origin = public_https_origin(
        _required(env, "HONUA_AI_ARC_LOCAL_ORIGIN"),
        "HONUA_AI_ARC_LOCAL_ORIGIN",
    )
    server_env.update(
        {
            "Public__BaseUrl": origin,
            "StudioAiProxy__Enabled": "true",
            "StudioAiProxy__DefaultProvider": provider,
            f"StudioAiProxy__Providers__{provider}__Kind": provider,
            f"StudioAiProxy__Providers__{provider}__Model": model,
        }
    )
    if provider in {"anthropic", "openai"}:
        if env.get("HONUA_AI_UPSTREAM_ENDPOINT", "").strip():
            raise ProducerBlocked(
                "HONUA_AI_UPSTREAM_ENDPOINT is not accepted by certifying runs; "
                "the declared provider must use its official API origin"
            )
        default_endpoint = (
            "https://api.anthropic.com"
            if provider == "anthropic"
            else "https://api.openai.com/v1"
        )
        server_env[f"StudioAiProxy__Providers__{provider}__Endpoint"] = default_endpoint
        server_env[f"HONUA_STUDIOAI_{provider.upper()}_API_KEY"] = (
            "${HONUA_AI_PROVIDER_API_KEY}"
        )
    else:
        region = env.get("HONUA_AI_REGION") or env.get("AWS_REGION") or "us-west-2"
        server_env[f"StudioAiProxy__Providers__{provider}__Region"] = region
        server_env["AWS_REGION"] = region
        server_env["AWS_DEFAULT_REGION"] = region
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        ):
            if env.get(name):
                server_env[name] = f"${{{name}}}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")


def install_wrapper(argv: list[str], env: dict[str, str] | None = None) -> int:
    environment = dict(os.environ if env is None else env)
    try:
        directory = Path(argv[argv.index("--directory") + 1]).resolve()
    except (ValueError, IndexError) as error:
        raise ProducerFailed("local install command omitted --directory") from error
    expected_directory = Path(_required(environment, "E2E_AI_LOCAL_INSTALL")).resolve()
    if directory != expected_directory:
        raise ProducerFailed("local install command targeted an unexpected directory")
    compose_path = directory / "compose.yaml"
    if not compose_path.is_file():
        raise ProducerFailed("local install command has no release-owned compose file")
    if argv[:3] == ["admin", "install", "local"]:
        running = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.docker.compose.project=honua-ai-arc-local",
                "--format",
                '{{.Label "com.docker.compose.service"}}',
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        services = set(running.stdout.split())
        return (
            0
            if running.returncode == 0
            and {"postgres", "redis", "honua"} <= services
            else 1
        )
    if argv[:3] == ["admin", "install", "status"]:
        port = environment.get("E2E_AI_LOCAL_PORT", "8080")
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/healthz/ready", timeout=10
            ) as response:
                return (
                    0
                    if response.status == 200
                    and response.read().strip().lower() == b"ready"
                    else 1
                )
        except OSError:
            return 1
    raise ProducerFailed("local journey requested an unsupported control-plane command")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["install-wrapper"]:
        return install_wrapper(arguments[1:])
    # The SDK calls this executable directly as its honua command.
    if arguments[:2] == ["admin", "install"]:
        return install_wrapper(arguments)
    if arguments:
        print("usage: local_ai_delivery_arc.py [install-wrapper ...]", file=sys.stderr)
        return 2
    return produce()


if __name__ == "__main__":
    raise SystemExit(main())
