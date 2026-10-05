from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gate-artifact-consume.yml"
CONTRACT_WORKFLOW = ROOT / ".github" / "workflows" / "gate-contract.yml"
CLOUD_WORKFLOW = ROOT / ".github" / "workflows" / "e2e-cloud-aws.yml"
CLOUD_CELL_WORKFLOW = ROOT / ".github" / "workflows" / "e2e-cloud-aws-cell.yml"


def test_artifact_consume_never_uses_floating_staging_or_source_refs():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "@latest" not in workflow
    assert "git clone --depth 1" not in workflow
    assert 'docker pull "$SERVER_IMAGE:$TAG"' not in workflow
    assert "${PINNED_SERVER_IMAGE}@${PINNED_SERVER_DIGEST}" in workflow

    exact_source_checkouts = {
        "honua-sdk-js": "SDK_JS_SHA",
        "honua-sdk-dotnet": "SDK_DOTNET_SHA",
        "honua-sdk-python": "SDK_PYTHON_SHA",
        "honua-iac": "IAC_SHA",
        "honua-helm": "HELM_SHA",
        "honua-server": "SERVER_SHA",
        "geospatial-grpc": "GRPC_SHA",
    }
    for repository, variable in exact_source_checkouts.items():
        expected = f'checkout_component.sh" {repository} "${variable}" src'
        assert expected in workflow, f"{repository} fallback is not pinned through {variable}"


def test_artifact_consume_pins_registry_versions_from_manifest():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'npm install "${SDK_JS_PACKAGE}@${SDK_JS_VERSION}"' in workflow
    assert 'sdk_js_package: ${{ steps.pins.outputs.sdk_js_package }}' in workflow
    assert "@honua-io/sdk-js" not in workflow
    assert 'package "$SDK_DOTNET_PACKAGE" --version "$SDK_DOTNET_VERSION"' in workflow
    assert 'sdk_dotnet_package: ${{ steps.pins.outputs.sdk_dotnet_package }}' in workflow
    assert '"${SDK_PYTHON_PACKAGE}==${SDK_PYTHON_VERSION}"' in workflow
    assert 'sdk_python_package: ${{ steps.pins.outputs.sdk_python_package }}' in workflow
    assert "SDK_JS_INTEGRITY" in workflow
    assert "SDK_DOTNET_DIGEST" in workflow
    assert "SDK_PYTHON_DIGEST" in workflow
    assert 'cp "$NUPKG" "$PUBLISHED_FEED/"' in workflow
    assert 'NUGET_PACKAGES="$WORK/packages"' in workflow
    assert 'dotnet restore Consumer --configfile "$WORK/NuGet.published.config"' in workflow
    assert 'sha256sum "$RESTORED_NUPKG"' in workflow
    assert 'package "$SDK_DOTNET_PACKAGE" --version "$SDK_DOTNET_VERSION" --source "$STAGING_NUGET_SOURCE"' not in workflow
    assert 'buf.build/honua-io/geospatial:v${GRPC_VERSION}' in workflow


def test_artifact_consumers_select_runnable_roots_and_valid_runtime_config():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '<add key="honua-staging" value="$STAGING_NUGET_SOURCE" />' in workflow
    assert '--source "$WORK/localfeed"' in workflow
    assert 'NF==2 && $2=="Chart.yaml" && !root' in workflow
    assert 'END {print root}' in workflow
    assert "secret.env.ConnectionStrings__DefaultConnection=Host=postgres" in workflow
    assert "secret.env.ConnectionStrings__redis=redis:6379" in workflow
    assert "secret.env.HONUA_ADMIN_PASSWORD=Gate-Aa1!ArtifactConsume" in workflow
    assert "HELM_RUNTIME_ARGS[@]" in workflow
    assert "if [ -f src/buf.yaml ]" in workflow
    assert 'HONUA_ADMIN_PASSWORD="Gate-Aa1!' in workflow


def test_contract_gate_checks_out_manifest_pins_and_records_nonzero_results():
    workflow = CONTRACT_WORKFLOW.read_text(encoding="utf-8")

    for repository in ("honua-server", "honua-sdk-python", "honua-sdk-dotnet", "honua-sdk-js"):
        assert repository in workflow
    assert 'checkout_component.sh" "$comp" "$sha" "$ROOT/$comp"' in workflow
    assert 'git clone --quiet "https://x-access-token:' not in workflow
    assert 'if OUT="$(python tools/contract_surface.py check' in workflow
    assert "RC=$?" in workflow


def test_iac_live_receives_exact_manifest_server_candidate():
    workflow = CLOUD_WORKFLOW.read_text(encoding="utf-8")
    cell = CLOUD_CELL_WORKFLOW.read_text(encoding="utf-8")

    assert '"server_ref": str(server.get("sha", ""))' in workflow
    assert 'pins["server_image"] = f"{image}@{digest}"' in workflow
    assert 'pins["lambda_source"] = f"{lambda_image}@{lambda_digest}"' in workflow
    assert "ECR Lambda digest $RESOLVED does not match manifest ECR digest $EXPECTED_ECR_DIGEST" in workflow
    assert "ECR Lambda config $ECR_CONFIG does not match source config $SOURCE_CONFIG" in workflow
    assert 'crane copy "$SOURCE_IMAGE" "$TARGET"' in workflow
    assert "docker tag" not in workflow
    assert "docker push" not in workflow
    assert "must be x86_64 for the 2026.1 Lambda GA target" in workflow
    assert '.lambdaGaQualification = "pending"' in workflow
    # Each cell (e2e-cloud-aws-cell.yml) deploys exactly what the candidate job resolved.
    assert "lambda_image: ${{ needs.candidate.outputs.lambda_image }}" in workflow
    assert "server_image: ${{ needs.candidate.outputs.server_image }}" in workflow
    assert 'ecs_architecture = str(server.get("awsEcsArchitecture", ""))' in workflow
    assert "ecs_architecture: ${{ needs.candidate.outputs.ecs_architecture }}" in workflow
    assert "HONUA_LAMBDA_IMAGE_URI: ${{ inputs.lambda_image }}" in cell
    assert "HONUA_ECS_IMAGE: ${{ inputs.server_image }}" in cell
    assert "HONUA_ECS_ARCHITECTURE: ${{ inputs.ecs_architecture }}" in cell
    # The runner's own /32 is resolved for EVERY cell: serverless/ECS use it for RDS ingress, and the
    # EKS cell publishes both its API server and its load balancer to that address and nothing else.
    assert "HONUA_AWS_DB_INGRESS_CIDR=${RUNNER_IP}/32" in cell
    assert "HONUA_AWS_RUNNER_CIDR=${RUNNER_IP}/32" in cell
    for text in (workflow, cell):
        assert "HONUA_LAMBDA_IMAGE_URI: ${{ vars.HONUA_LAMBDA_IMAGE_URI }}" not in text
        assert "HONUA_ECS_IMAGE: ${{ vars.HONUA_ECS_IMAGE }}" not in text
    assert "inputs.target == '' || inputs.target == 'all'" in workflow
    assert "inputs.redis_mode == '' || inputs.redis_mode == 'both'" in workflow
    assert "github.event_name == 'schedule' || inputs.run_iac_live" in workflow
    assert "needs: [candidate, parity, iac-live]" in workflow
    assert "CANDIDATE_RESULT: ${{ needs.candidate.result }}" in workflow
    assert 'candidate prerequisite ended $CANDIDATE_RESULT' in workflow
    assert 'certifyingScope:($full == "true")' in workflow
    assert '.certifying = ($full == "true" and $enf == "true" and .status == "pass")' in workflow
    assert "focused dispatch is diagnostic only" in workflow
    assert "python e2e/cloud_journey.py --reports reports --output merged.json" in workflow
    aggregator = (ROOT / "e2e/cloud_journey.py").read_text()
    assert "full-scope cloud reports missing required cells" in aggregator
    assert "full-scope cloud reports did not all pass" in aggregator
    assert '-f honua_server_ref="$SERVER_REF"' in workflow
    assert '-f aws_ecs_image="$ECS_IMAGE"' in workflow


def _load_pins():
    import importlib.util

    import sys

    path = ROOT / "certification" / "terminal-journey" / "pins.py"
    name = "terminal_journey_pins_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_STRIPPED_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_SESSION_TOKEN": "aws-session",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-token",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
    "GITHUB_TOKEN": "ghs_example",
    "GH_TOKEN": "gh_example",
    "HONUA_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/release",
}


def test_npm_install_ignores_scripts_and_receives_no_cloud_credentials(monkeypatch, tmp_path):
    """Pinned tarballs are integrity-checked bytes. Their install must not run lifecycle scripts, and the environment handed to npm must not carry the cloud role."""
    import subprocess

    pins = _load_pins()
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pins.subprocess, "run", fake_run)
    for key, value in _STRIPPED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        pins,
        "CANDIDATE_ENV_ALLOWLIST",
        pins.CANDIDATE_ENV_ALLOWLIST + (
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "HONUA_AWS_ROLE_ARN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        ),
    )

    result = pins._npm_install(tmp_path, ["/verified/sdk.tgz"], ["--legacy-peer-deps"])

    assert result.returncode == 0
    assert "--ignore-scripts" in captured["argv"]
    assert captured["argv"].index("--ignore-scripts") > captured["argv"].index("--legacy-peer-deps")
    handed = captured["env"]
    assert handed["PATH"] == "/usr/bin"
    for key in handed:
        assert not key.startswith(("AWS_", "ACTIONS_ID_TOKEN_REQUEST_", "HONUA_AWS_"))
        assert key not in {"GITHUB_TOKEN", "GH_TOKEN"}
    for value in _STRIPPED_ENV.values():
        assert value not in handed.values()
