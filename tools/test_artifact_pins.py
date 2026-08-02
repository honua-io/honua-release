from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gate-artifact-consume.yml"
CONTRACT_WORKFLOW = ROOT / ".github" / "workflows" / "gate-contract.yml"
CLOUD_WORKFLOW = ROOT / ".github" / "workflows" / "e2e-cloud-aws.yml"


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

    assert "@honua-io/sdk-js@${SDK_JS_VERSION}" in workflow
    assert 'Honua.Sdk --version "$SDK_DOTNET_VERSION"' in workflow
    assert "honua-sdk==${SDK_PYTHON_VERSION}" in workflow
    assert 'buf.build/honua-io/geospatial:v${GRPC_VERSION}' in workflow


def test_artifact_consumers_select_runnable_roots_and_valid_runtime_config():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '--source "$STAGING_NUGET_SOURCE"' in workflow
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

    assert '"server_ref": str(server.get("sha", ""))' in workflow
    assert 'pins["server_image"] = f"{image}@{digest}"' in workflow
    assert 'pins["lambda_source"] = f"{lambda_image}@{lambda_digest}"' in workflow
    assert "ECR Lambda digest $RESOLVED does not match manifest ECR digest $EXPECTED_ECR_DIGEST" in workflow
    assert "ECR Lambda config $ECR_CONFIG does not match source config $SOURCE_CONFIG" in workflow
    assert "HONUA_LAMBDA_IMAGE_URI: ${{ needs.candidate.outputs.lambda_image }}" in workflow
    assert "HONUA_ECS_IMAGE: ${{ needs.candidate.outputs.server_image }}" in workflow
    assert 'ecs_architecture = str(server.get("awsEcsArchitecture", ""))' in workflow
    assert "HONUA_ECS_ARCHITECTURE: ${{ needs.candidate.outputs.ecs_architecture }}" in workflow
    assert "HONUA_LAMBDA_IMAGE_URI: ${{ vars.HONUA_LAMBDA_IMAGE_URI }}" not in workflow
    assert "HONUA_ECS_IMAGE: ${{ vars.HONUA_ECS_IMAGE }}" not in workflow
    assert "inputs.target == '' || inputs.target == 'all'" in workflow
    assert "inputs.redis_mode == '' || inputs.redis_mode == 'both'" in workflow
    assert "github.event_name == 'schedule' || inputs.run_iac_live" in workflow
    assert "needs: [candidate, parity, iac-live]" in workflow
    assert "CANDIDATE_RESULT: ${{ needs.candidate.result }}" in workflow
    assert 'candidate prerequisite ended $CANDIDATE_RESULT' in workflow
    assert 'certifyingScope:($full == "true")' in workflow
    assert '.certifying = ($full == "true" and $enf == "true" and .status == "pass")' in workflow
    assert "focused dispatch is diagnostic only" in workflow
    assert "full-scope cloud reports missing required cells" in workflow
    assert "full-scope cloud reports did not all pass" in workflow
    assert '-f honua_server_ref="$SERVER_REF"' in workflow
    assert '-f aws_ecs_image="$ECS_IMAGE"' in workflow
