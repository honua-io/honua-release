"""Focused tests for the dual-target AI delivery-arc entrypoint."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import ai_delivery_arc as arc  # noqa: E402
import local_ai_delivery_arc as local  # noqa: E402


def test_checker_forwards_every_required_external_receipt(monkeypatch, tmp_path: Path):
    paths = {}
    for receipt_id, env_name in arc.EXTERNAL_RECEIPT_ENV:
        path = tmp_path / f"{receipt_id}.json"
        path.write_text("{}", encoding="utf-8")
        paths[receipt_id] = path
        monkeypatch.setenv(env_name, str(path))
    evidence_paths = {}
    evidence_env_paths = {}
    for receipt_id, env_name in arc.EXTERNAL_EVIDENCE_ENV:
        path = evidence_env_paths.setdefault(
            env_name, tmp_path / f"{env_name.lower()}-evidence.json"
        )
        if not path.exists():
            path.write_text("{}", encoding="utf-8")
        evidence_paths[receipt_id] = path
        monkeypatch.setenv(env_name, str(path))
    aws_sdk_receipt = tmp_path / "aws-sdk-journey.json"
    aws_sdk_receipt.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("E2E_AI_AWS_SDK_RECEIPT", str(aws_sdk_receipt))

    captured: list[str] = []

    def run(command, **_kwargs):
        captured.extend(command)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(arc.subprocess, "run", run)
    assert arc.checker(mode="live", include_receipt=True, require_real=True) == 0

    forwarded = {
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--external-receipt"
    }
    assert forwarded == {
        f"{receipt_id}={path}"
        for receipt_id, path in paths.items()
    }
    forwarded_evidence = {
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--external-evidence"
    }
    assert forwarded_evidence == {
        f"{receipt_id}={path}"
        for receipt_id, path in evidence_paths.items()
    }
    target_receipts = {
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--target-sdk-receipt"
    }
    assert target_receipts == {f"aws-ecs={aws_sdk_receipt}"}
    assert "--require-real" in captured


def test_full_aws_arc_receipt_is_a_distinct_required_input():
    assert ("aws-ecs-provision", "E2E_AI_AWS_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert ("aws-ecs-ai-delivery-arc", "E2E_AI_AWS_ARC_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert ("aws-ecs-real-model-ai-arc", "E2E_AI_AWS_MODEL_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert ("aws-ecs-real-model-ai-arc", "E2E_AI_AWS_MODEL_EVIDENCE") in arc.EXTERNAL_EVIDENCE_ENV
    assert ("aws-ecs-provision", "E2E_AI_AWS_EVIDENCE") in arc.EXTERNAL_EVIDENCE_ENV
    assert ("aws-ecs-ai-delivery-arc", "E2E_AI_AWS_EVIDENCE") in arc.EXTERNAL_EVIDENCE_ENV
    assert ("local-docker-real-model-ai-arc", "E2E_AI_LOCAL_MODEL_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert ("local-docker-real-model-ai-arc", "E2E_AI_LOCAL_MODEL_EVIDENCE") in arc.EXTERNAL_EVIDENCE_ENV
    assert ("aws-ecs", "E2E_AI_AWS_SDK_RECEIPT") in arc.TARGET_SDK_RECEIPT_ENV
    assert len({env_name for _, env_name in arc.EXTERNAL_RECEIPT_ENV}) == len(arc.EXTERNAL_RECEIPT_ENV)


def test_local_producer_never_accepts_internal_or_credentialed_origins():
    for value in (
        "https://localhost./",
        "https://honua.internal/",
        "https://honua.localdomain/",
        "https://honua.%69nternal/",
        "https://honua.%6cocal/",
        "https://user:password@candidate.example.com/",
        "https://@candidate.example.com/",
        "https://127.0.0.1/",
        "http://candidate.example.com/",
    ):
        try:
            local.public_https_origin(value, "test origin")
        except local.ProducerBlocked:
            pass
        else:
            raise AssertionError(f"accepted non-public origin {value}")

    assert local.public_https_origin(
        "https://candidate.example.com/", "test origin"
    ) == "https://candidate.example.com"


def test_local_install_override_keeps_the_model_key_out_of_compose(tmp_path: Path):
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  honua:\n    image: candidate@example\n    environment: {}\n",
        encoding="utf-8",
    )
    local._patch_local_compose(
        compose,
        {
            "HONUA_AI_PROVIDER": "openai",
            "HONUA_AI_MODEL": "gpt-release-model",
            "HONUA_AI_PROVIDER_API_KEY": "must-not-be-serialized",
            "HONUA_AI_ARC_LOCAL_ORIGIN": "https://candidate.example.com",
        },
    )

    rendered = compose.read_text(encoding="utf-8")
    assert "must-not-be-serialized" not in rendered
    assert "${HONUA_AI_PROVIDER_API_KEY}" in rendered
    assert "Public__BaseUrl: https://candidate.example.com" in rendered
    assert "StudioAiProxy__Providers__openai__Endpoint: https://api.openai.com/v1" in rendered


def test_local_install_rejects_a_model_upstream_override(tmp_path: Path):
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  honua:\n    image: candidate@example\n    environment: {}\n",
        encoding="utf-8",
    )

    with __import__("pytest").raises(
        local.ProducerBlocked, match="official API origin"
    ):
        local._patch_local_compose(
            compose,
            {
                "HONUA_AI_PROVIDER": "openai",
                "HONUA_AI_MODEL": "gpt-release-model",
                "HONUA_AI_ARC_LOCAL_ORIGIN": "https://candidate.example.com",
                "HONUA_AI_UPSTREAM_ENDPOINT": "https://model-proxy.example.com/v1",
            },
        )


def test_local_install_is_release_owned_and_uses_the_manifest_image(tmp_path: Path):
    paths = local.output_paths(tmp_path / "out")
    image = "ghcr.io/honua-io/honua-server:reviewed"
    digest = "sha256:" + "a" * 64
    environment = {
        "E2E_AI_LOCAL_PORT": "18080",
        "HONUA_AI_PROVIDER": "openai",
        "HONUA_AI_MODEL": "gpt-release-model",
        "HONUA_AI_ARC_LOCAL_ORIGIN": "https://candidate.example.com",
    }
    local._write_install_environment(
        paths,
        {
            "components": {
                "honua-server": {"image": image, "digest": digest},
            }
        },
        environment,
        db_password="ephemeral-db-password",
        admin_key="ephemeral-admin-key",
    )

    install_environment = (paths.install / ".env").read_text(encoding="utf-8")
    compose = (paths.install / "compose.yaml").read_text(encoding="utf-8")
    assert f"HONUA_SERVER_IMAGE={image}@{digest}" in install_environment
    assert "image: ${HONUA_SERVER_IMAGE}" in compose
    assert "127.0.0.1:${HONUA_HTTP_PORT}:8080" in compose
    assert "ephemeral-db-password" not in compose
    assert "ephemeral-admin-key" not in compose


def test_local_sdk_producer_command_has_no_cloud_receipt_dependency(tmp_path: Path):
    paths = local.output_paths(tmp_path / "out")
    command = local._sdk_command(
        tmp_path / "sdk",
        paths,
        endpoint="https://candidate.example.com",
        fixture_url="https://fixtures.example.com/reviewed",
        candidate_id="manifest-sha256:" + "a" * 64,
        release_id="2026.1",
        resume=False,
    )

    joined = " ".join(command)
    assert "--target local-docker" in joined
    assert "--mcp-url https://candidate.example.com/mcp" in joined
    assert "aws-ecs" not in joined
    assert "external-receipt" not in joined


def test_local_producer_children_do_not_inherit_provider_or_cloud_credentials(
    tmp_path: Path,
):
    paths = local.output_paths(tmp_path / "out")
    environment = local._producer_environment(
        {
            "PATH": "/reviewed/bin",
            "HONUA_AI_PROVIDER": "bedrock",
            "HONUA_AI_MODEL": "reviewed-model",
            "HONUA_AI_PROVIDER_API_KEY": "provider-secret",
            "AWS_ACCESS_KEY_ID": "cloud-secret",
            "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/ambient-oidc",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "actions-secret",
        },
        paths,
        manifest_path=tmp_path / "platform-manifest.yaml",
        sdk=tmp_path / "sdk",
        endpoint="https://candidate.example.com",
        console_origin="https://console.example.com",
        evidence_url=(
            "https://github.com/honua-io/honua-release/actions/runs/12345"
        ),
    )

    assert environment["PATH"] == "/reviewed/bin"
    assert environment["HONUA_AI_PROVIDER"] == "bedrock"
    assert environment["HONUA_AI_MODEL"] == "reviewed-model"
    for name in (
        "HONUA_AI_PROVIDER_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        assert name not in environment
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert environment["AWS_CONFIG_FILE"] == os.devnull
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"


def test_local_studio_resume_is_credential_free_after_the_sealed_handoff():
    environment = local._studio_resume_environment(
        {
            "PATH": "/reviewed/bin",
            "HONUA_AI_ARC_PREPARE_CREDENTIAL": "stale-value",
            "HONUA_AI_ARC_CONSOLE_TOKEN": "console-secret",
            "HONUA_AI_PROVIDER_API_KEY": "provider-secret",
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }
    )

    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert "HONUA_AI_ARC_PREPARE_CREDENTIAL" not in environment
    assert "HONUA_AI_ARC_CONSOLE_TOKEN" not in environment
    assert "HONUA_AI_PROVIDER_API_KEY" not in environment


def test_local_producer_starts_a_pinned_tunnel_to_its_exact_port(
    monkeypatch, tmp_path: Path
):
    paths = local.output_paths(tmp_path / "out")
    commands: list[list[str]] = []
    child_environments: list[dict[str, str]] = []

    def run(command, **kwargs):
        commands.append(command)
        child_environments.append(kwargs["env"])
        if command[:2] == ["docker", "logs"]:
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "https://reviewed-route.trycloudflare.com",
                },
            )()
        return type(
            "Completed", (), {"returncode": 0, "stdout": "container-id", "stderr": ""}
        )()

    monkeypatch.setattr(local.subprocess, "run", run)
    monkeypatch.setattr(local.secrets, "token_hex", lambda _size: "a" * 16)
    tunnel = local._start_public_tunnel(
        paths,
        {
            "PATH": "/reviewed/bin",
            "E2E_AI_LOCAL_PORT": "18080",
            "HONUA_AI_PROVIDER_API_KEY": "must-not-reach-tunnel",
        },
    )

    assert tunnel.origin == "https://reviewed-route.trycloudflare.com"
    assert tunnel.target == "http://127.0.0.1:18080"
    assert commands[0] == [
        "docker",
        "run",
        "--detach",
        "--network",
        "host",
        "--name",
        "honua-ai-arc-tunnel-" + "a" * 16,
        local.CLOUDFLARED_IMAGE,
        "tunnel",
        "--no-autoupdate",
        "--protocol",
        "http2",
        "--url",
        "http://127.0.0.1:18080",
    ]
    assert paths.tunnel_marker.read_text(encoding="utf-8").strip() == tunnel.container
    assert all(
        "HONUA_AI_PROVIDER_API_KEY" not in environment
        for environment in child_environments
    )


def test_local_producer_verifies_tunnel_identity_and_public_readiness(
    monkeypatch, tmp_path: Path
):
    tunnel = local.PublicTunnel(
        container="honua-ai-arc-tunnel-" + "b" * 16,
        origin="https://reviewed-route.trycloudflare.com",
        target="http://127.0.0.1:18080",
    )
    inspect_record = {
        "Config": {
            "Image": local.CLOUDFLARED_IMAGE,
            "Cmd": ["tunnel", "--url", tunnel.target],
        },
        "HostConfig": {"NetworkMode": "host"},
        "State": {"Running": True},
    }
    monkeypatch.setattr(
        local.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps([inspect_record]), "stderr": ""},
        )(),
    )
    observed_urls: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return b"ready"

    def open_url(url, **_kwargs):
        observed_urls.append(url)
        return Response()

    monkeypatch.setattr(local, "urlopen", open_url)
    local._verify_public_route(tunnel, {"PATH": "/reviewed/bin"}, attempts=1)

    assert observed_urls == [
        "http://127.0.0.1:18080/healthz/ready",
        "https://reviewed-route.trycloudflare.com/healthz/ready",
    ]


def test_local_producer_rejects_a_tunnel_bound_to_another_port(monkeypatch):
    tunnel = local.PublicTunnel(
        container="honua-ai-arc-tunnel-" + "c" * 16,
        origin="https://reviewed-route.trycloudflare.com",
        target="http://127.0.0.1:18080",
    )
    inspect_record = {
        "Config": {
            "Image": local.CLOUDFLARED_IMAGE,
            "Cmd": ["tunnel", "--url", "http://127.0.0.1:9999"],
        },
        "HostConfig": {"NetworkMode": "host"},
        "State": {"Running": True},
    }
    monkeypatch.setattr(
        local.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps([inspect_record]), "stderr": ""},
        )(),
    )

    try:
        local._verify_public_route(tunnel, {"PATH": "/reviewed/bin"}, attempts=1)
    except local.ProducerFailed as error:
        assert "exact local candidate port" in str(error)
    else:
        raise AssertionError("accepted a tunnel bound to a different local port")


def test_local_teardown_removes_a_started_tunnel_without_a_compose_file(
    monkeypatch, tmp_path: Path
):
    paths = local.output_paths(tmp_path / "out")
    paths.out.mkdir(parents=True)
    container = "honua-ai-arc-tunnel-" + "d" * 16
    paths.tunnel_marker.write_text(container + "\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        return type(
            "Completed", (), {"returncode": 0, "stdout": container, "stderr": ""}
        )()

    monkeypatch.setattr(local.subprocess, "run", run)
    local._teardown(paths, {"PATH": "/reviewed/bin"})

    assert commands == [["docker", "rm", "--force", container]]
    assert not paths.tunnel_marker.exists()


def test_local_producer_blocks_old_component_handoff_before_execution(tmp_path: Path):
    studio = tmp_path / "studio"
    console = tmp_path / "console"
    (studio / "scripts" / "lib").mkdir(parents=True)
    (console / "e2e" / "playwright" / "live").mkdir(parents=True)
    (studio / "scripts" / "real-model-ai-arc.mjs").write_text(
        'const handoff = "HONUA_AI_ARC_REAL_MODEL_EVIDENCE";\n', encoding="utf-8"
    )
    (studio / "scripts" / "lib" / "real-model-ai-arc.mjs").write_text(
        'const receipt = {id: "studio-real-model"};\n', encoding="utf-8"
    )
    (console / "e2e" / "playwright" / "live" / "console-receipt-cli.mjs").write_text(
        'const output = "HONUA_AI_ARC_CONSOLE_RECEIPT";\n', encoding="utf-8"
    )

    try:
        local._producer_contract_ready(studio, console)
    except local.ProducerBlocked as error:
        assert "predate the sealed local receipt handoff" in str(error)
    else:
        raise AssertionError("old producer contract was accepted")


def test_local_producer_accepts_only_the_credential_free_sealed_handoff(
    tmp_path: Path,
):
    studio = tmp_path / "studio"
    console = tmp_path / "console"
    (studio / "scripts" / "lib").mkdir(parents=True)
    (console / "e2e" / "playwright" / "live").mkdir(parents=True)
    (studio / "scripts" / "real-model-ai-arc.mjs").write_text(
        "\n".join(
            (
                'const handoff = "HONUA_AI_ARC_REAL_MODEL_HANDOFF";',
                'const policy = "resume is credential-free";',
            )
        ),
        encoding="utf-8",
    )
    (studio / "scripts" / "lib" / "real-model-ai-arc.mjs").write_text(
        'const receipt = {id: "local-docker-real-model-ai-arc"};\n',
        encoding="utf-8",
    )
    (console / "e2e" / "playwright" / "live" / "console-receipt-cli.mjs").write_text(
        'const evidence = "HONUA_AI_ARC_CONSOLE_EVIDENCE";\n',
        encoding="utf-8",
    )

    local._producer_contract_ready(studio, console)
