"""Run-owned JWT fixture boundaries; no AWS deployment or third-party client execution."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_journey
import run_cloud
cloud_journey.drivers()
import cloud_target
import local_fixture
from transport import Transport

KEY = "ab" * 32
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-abcdef"
VERSION = "12345678-1234-1234-1234-123456789012"


def test_same_subject_role_signed_tenants_and_private_storage(tmp_path):
    template = json.loads((local_fixture.Path(local_fixture.__file__).parent / "targets/local-docker.json").read_text())
    target = cloud_target.build(template, cell="aws-eks/redis-on", endpoint="https://cell.example",
                                tenant_fixture=True)
    credentials = local_fixture.credentials(target, tmp_path, "https://cell.example", tenant_signing_key=KEY)
    claims = []
    for principal in ("proposer", "other-tenant"):
        token = credentials[principal]()
        header, payload, signature = token[7:].split(".")
        assert base64.urlsafe_b64decode(signature + "==") == hmac.new(
            KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        claims.append(json.loads(base64.urlsafe_b64decode(payload + "==")))
        assert credentials[principal]() != token
    assert claims[0]["sub"] == claims[1]["sub"] == "journey-author"
    assert claims[0]["role"] == claims[1]["role"] == local_fixture.AUTHOR_ROLE
    assert claims[0]["tenant_id"] != claims[1]["tenant_id"]
    assert claims[0]["exp"] - claims[0]["iat"] == 300
    assert (tmp_path / "private-principals.json").stat().st_mode & 0o777 == 0o600
    assert KEY not in (tmp_path / "private-principals.json").read_text()
    assert KEY not in json.dumps(target)
    assert not target["unavailablePrincipals"]


@pytest.mark.parametrize("key", [None, "", "a" * 63, "g" * 64, "secret"])
def test_malformed_signing_key_refused(key, tmp_path):
    with pytest.raises(ValueError):
        local_fixture.validate_tenant_signing_key(key)
    assert not (tmp_path / "private-principals.json").exists()


def test_sealed_handoff_preserves_datasource_and_s10_without_public_values():
    source = {"host": "db.private", "port": 5432, "databaseName": "honua", "username": "u", "password": "p"}
    value = cloud_journey.pack_handoff("admin-secret", source, tenant_signing_key=KEY)
    value = cloud_journey.attach_source_url(value, "https://s3.example/object?signature=private")
    assert cloud_journey.unpack_handoff(value) == ("admin-secret", source)
    assert cloud_journey.handoff_tenant_signing_key(value) == KEY
    assert cloud_journey.handoff_source_url(value).startswith("https://s3.example/")
    assert cloud_journey.unpack_handoff(cloud_journey.pack_handoff("admin-secret", tenant_signing_key=KEY)) == ("admin-secret", None)
    assert cloud_journey.unpack_handoff("legacy") == ("legacy", None)
    value = json.loads(value); value["tenantSigningKey"] = "invalid"
    with pytest.raises(ValueError):
        cloud_journey.unpack_handoff(json.dumps(value))


def test_opt_in_stores_secret_outside_terraform_and_binds_immutable_version(monkeypatch):
    target = mock.Mock(name="target"); target.name = "aws-serverless"
    target._root_declares.return_value = True
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"ARN": ARN, "VersionId": VERSION})))
    monkeypatch.delenv("HONUA_CLOUD_TENANT_FIXTURE", raising=False)
    run_cloud.configure_tenant_fixture(target, "aws-serverless/redis-on", run=run)
    run.assert_not_called()
    monkeypatch.setenv("HONUA_CLOUD_TENANT_FIXTURE", "true")
    run_cloud.configure_tenant_fixture(target, "aws-serverless/redis-on", run=run)
    command = run.call_args.args[0]
    assert "file:///dev/stdin" in command
    assert target.journey_signing_key == run.call_args.kwargs["input"]
    assert len(target.journey_signing_key) == 64
    assert target.journey_signing_key not in " ".join(command)
    assert target.journey_signing_secret == {"arn": ARN, "versionId": VERSION}
    target._root_declares.return_value = False
    run.reset_mock()
    with pytest.raises(ValueError, match="typed"):
        run_cloud.configure_tenant_fixture(target, "aws-serverless/redis-on", run=run)
    run.assert_not_called()


def test_cli_uses_bearer_environment_not_admin_key(tmp_path):
    exe = tmp_path / "honua"; exe.touch()
    transport = Transport("https://cell.example", None, exe, tmp_path,
                          {"proposer": lambda: "Bearer private-bearer"})
    with mock.patch("transport.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
        transport.cli_admin("operate", "read", "proposer")
    env = run.call_args.kwargs["env"]
    assert env["HONUA_BEARER_TOKEN"] == "private-bearer"
    assert "HONUA_ADMIN_KEY" not in env
    assert "private-bearer" in transport.secret_values()
    assert "private-bearer" not in " ".join(run.call_args.args[0])


def test_staged_capture_chains_exact_inputs_and_persists_before_actuation(monkeypatch, tmp_path):
    import types
    import deployment_capture as capture
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "platform-manifest.yaml").write_text("version: exact\n")
    (root / "docs/2026.1-operating-envelope.md").write_text("reviewed operating contract")
    monkeypatch.setattr(capture, "ROOT", root)
    monkeypatch.setattr(capture, "source", lambda path: "a" * 40)
    monkeypatch.setattr(cloud_journey, "manifest", lambda: {"version": "exact"})
    monkeypatch.setattr(cloud_journey, "cell_dir", lambda cell: tmp_path / "cell")
    # Shared producer/consumer shape has its own full canonical suite in release#578.
    monkeypatch.setitem(sys.modules, "tools.deployment_qualification", types.SimpleNamespace(
        execution_inputs=lambda manifest, deployment: {"manifest": manifest, "deployment": deployment}))
    monkeypatch.setenv("HONUA_DEPLOYMENT_PREPARED", "true")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "honua-io/honua-release/.github/workflows/e2e-cloud-aws.yml@refs/heads/trunk")
    target = types.SimpleNamespace()
    with pytest.raises(ValueError, match="original Terraform"):
        capture.capture(target, "helm", {"values": {}}, True)
    capture.capture(target, "terraform", {"arguments": ["-var=region=us-east-1"]}, True)
    directory = tmp_path / "cell"
    tf = json.loads((directory / "deployment-terraform-inputs.json").read_text())
    assert (directory / "executed-manifest.yaml").read_text() == "version: exact\n"
    assert not (directory / "deployment-actuation.json").exists()
    capture.actuating(target, "terraform", True)
    capture.capture(target, "helm", {"values": {"image": "immutable"}}, True)
    inputs = json.loads((directory / "deployment-execution-inputs.json").read_text())
    stages = inputs["deployment"]["configuration"]["stages"]
    assert stages["helm"]["previousStageDigest"] == capture.digest(tf)
    assert stages["terraform"]["configuration"]["arguments"] == ["-var=region=us-east-1"]
    assert inputs["deployment"]["configuration"]["redisMode"] == "on"
    assert not hasattr(target, "helmStartedAt")
    capture.actuating(target, "helm", True)
    actual = json.loads((directory / "deployment-actuation.json").read_text())
    assert tf["capturedAt"] <= actual["terraformStartedAt"] <= stages["helm"]["capturedAt"] <= actual["helmStartedAt"]


def test_secret_capture_requests_version_identity_only():
    import deployment_capture as capture
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"ARN": ARN, "VersionIdsToStages": {VERSION: ["AWSCURRENT"]}})))
    result = capture.secret_versions([f"-var=key={ARN}", f"-var=key2={ARN}"], run=run)
    assert result == [{"arn": ARN, "versionId": VERSION}]
    assert run.call_count == 1
    assert run.call_args.args[0][2] == "describe-secret"
    assert "get-secret-value" not in run.call_args.args[0]
    assert run.call_args.args[0][-4:] == ["--query", "{ARN:ARN,VersionIdsToStages:VersionIdsToStages}", "--output", "json"]


@pytest.mark.parametrize("author_allowed", [True, False])
def test_tenant_oracle_requires_exact_author_read_before_other_tenant_denial(author_allowed):
    import executor
    import stages
    from transport import ExecutionError
    transport = mock.Mock(credentials={"proposer": "signed-a", "other-tenant": "signed-b", "viewer": "viewer"})
    engine = executor.JourneyExecutor({"workspaceId": "workspace"},
        {"tenantFixture": local_fixture.TENANT_FIXTURE},
        stages.Observation(setup_view_present=True, setup_discovery={"tools": [], "metadata": {}}), transport)
    engine.resources.update(itemId="item", versionId="version", contentHash="hash")
    seen = []
    def http(method, path, *, principal, expected):
        seen.append((path, principal, expected))
        if principal == "proposer" and not author_allowed:
            raise ExecutionError("GET private resource", "HTTP 404")
        return b"{}", 404 if principal == "other-tenant" and expected == (403, 404) else 200
    transport.http.side_effect = http
    result = engine.verify_authority()["tenantIsolation"]
    assert result.status == ("pass" if author_allowed else "fail")
    assert seen[0] == (engine.map_path(), "proposer", (200,))
    if author_allowed:
        assert seen[1:3] == [("/api/v1/studio/package-families", "other-tenant", (200,)),
                            (engine.map_path(), "other-tenant", (403, 404))]
    else:
        assert not any(item[1] == "other-tenant" for item in seen)


def test_mcp_proxy_uses_only_bearer_for_signed_principal(tmp_path):
    import probes
    exe = tmp_path / "proxy"; exe.touch()
    transport = Transport("https://cell.example", exe, None, tmp_path,
                          {"proposer": lambda: "Bearer private-proxy-token"})
    view = {"metadata": {"view": "setup"}, "tools": [{"name": "honua_test"}]}
    session = mock.Mock()
    session.initialize.return_value = {}
    session.request.side_effect = [
        {"result": {"_meta": view["metadata"], "tools": view["tools"]}},
        {"result": {"structuredContent": {"passed": True}}}]
    context = mock.MagicMock(); context.__enter__.return_value = session
    with mock.patch.object(probes, "McpProxySession", return_value=context) as factory:
        assert transport.tool("honua_test", {}, view)["structuredContent"]["passed"]
    env = factory.call_args.args[2]
    assert env == {"HONUA_API_KEY": "", "HONUA_ADMIN_KEY": "", "HONUA_MCP_AUTH_TOKEN": "private-proxy-token"}
    assert "private-proxy-token" in transport.secret_values()


def test_runtime_secret_references_pin_the_version_actually_consumed():
    import deployment_capture as capture
    environment = {"AuditLog__ChainVerification__Key": "aws:secretsmanager:" + ARN,
                   "Other": "unchanged"}
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"ARN": ARN, "VersionIdsToStages": {VERSION: ["AWSCURRENT"]}})))
    result = capture.pin_secret_references(environment, run=run)
    assert result == {**environment, "AuditLog__ChainVerification__Key": "aws:secretsmanager:" + ARN + "?versionId=" + VERSION}
    assert environment["AuditLog__ChainVerification__Key"] == "aws:secretsmanager:" + ARN
    run.reset_mock()
    assert capture.secret_versions(result, run=run) == [{"arn": ARN, "versionId": VERSION}]
    assert run.call_args.args[0][2] == "describe-secret"
    # A pinned historical version remains valid even after AWSCURRENT rotates.
    run.return_value.stdout = json.dumps({"ARN": ARN, "VersionIdsToStages": {VERSION: [], "b" * 32: ["AWSCURRENT"]}})
    assert capture.secret_versions(result, run=run) == [{"arn": ARN, "versionId": VERSION}]
    run.return_value.stdout = json.dumps({"ARN": ARN, "VersionIdsToStages": {"b" * 32: ["AWSCURRENT"]}})
    with pytest.raises(ValueError, match="matching immutable"):
        capture.secret_versions(result, run=run)
    with pytest.raises(ValueError, match="exact ARN"):
        capture.pin_secret_references({"Key": "aws:secretsmanager:name?versionStage=AWSCURRENT"}, run=run)


@pytest.mark.parametrize("metadata", [
    {"ARN": ARN, "VersionIdsToStages": {}},
    {"ARN": ARN, "VersionIdsToStages": {VERSION: ["AWSCURRENT"], "b" * 32: ["AWSCURRENT"]}},
    {"ARN": ARN, "VersionIdsToStages": {"invalid": ["AWSCURRENT"]}},
    {"ARN": ARN + "other", "VersionIdsToStages": {VERSION: ["AWSCURRENT"]}},
])
def test_secret_version_metadata_refuses_missing_ambiguous_or_wrong_identity(metadata):
    import deployment_capture as capture
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata)))
    with pytest.raises(ValueError, match="matching immutable"):
        capture.secret_versions([ARN], run=run)


def test_runtime_secret_commitments_are_keyed_field_bound_and_fresh():
    import hashlib
    import hmac
    import deployment_capture as capture
    values = {"Password": "low-entropy-example", "OtherPassword": "low-entropy-example"}
    key = b"a" * 32
    with mock.patch.object(capture.secrets, "token_bytes", return_value=key) as random:
        committed = capture.secret_commitments(values)
    random.assert_called_once_with(32)
    assert committed["Password"] == "hmac-sha256:" + hmac.new(
        key, capture.canonical({"name": "Password", "value": values["Password"]}), hashlib.sha256).hexdigest()
    assert committed["Password"] != committed["OtherPassword"]
    assert committed != capture.secret_commitments(values)
    assert all(value.startswith("hmac-sha256:") and len(value) == 76 for value in committed.values())
    assert "low-entropy-example" not in json.dumps(committed)
    assert key.hex() not in json.dumps(committed)
