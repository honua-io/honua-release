from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gate-artifact-consume.yml"


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
    assert "if [ -f src/buf.yaml ]" in workflow
    assert 'HONUA_ADMIN_PASSWORD="Gate-Aa1!' in workflow
