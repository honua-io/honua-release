"""Offline safety/contract tests. These are not cloud qualification evidence."""
import copy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

import ecs_honua_recovery as recovery


def scope():
    source = Path(__file__).resolve().parents[1] / "certification/ecs-recovery/scope.review-template.json"
    value = json.loads(source.read_text())
    value.update(runId="honua-qual-unit", awsAccountId="123456789012", deployTargetId="isolated-target",
                 proposerPrincipalId="apikey:api-key:11111111-1111-4111-8111-111111111111")
    prefix = "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
    value.update(listenerRuleArn=prefix + "listener-rule/app/honua-qual-unit/rule",
                 canaryTargetGroupArn=prefix + "targetgroup/honua-qual-canary/1",
                 stableTargetGroupArn=prefix + "targetgroup/honua-qual-stable/2")
    for role in ("baseline", "candidate"):
        value[role].update(taskDefinition=f"arn:aws:ecs:us-east-1:123456789012:task-definition/honua-qual-{role}:1",
                           provenance={"kind": "platform-manifest", "path": f"/isolated/unit/{role}/platform-manifest.yaml",
                                       "bundlePath": f"/isolated/unit/{role}/platform-manifest.yaml.bundle", "digest": "sha256:" + "1" * 64,
                                       "workflow": "promote.yml", "sourceSha": "a" * 40, "sourceRef": "refs/heads/trunk"})
    value["dataset"].update(expectedCount=2, expectedFeaturesDigest="sha256:" + "2" * 64,
                            databaseIdentity="honua-qual-unit-db", snapshotReference="honua-qual-unit-snapshot",
                            connectionSecretArn="arn:aws:secretsmanager:us-east-1:123456789012:secret:unit-only")
    value["costScope"]["cleanupOwner"] = "unit-only"
    value["telemetry"]["goldenQueryResponseSha256"] = "3" * 64
    value.update(schemaCompatibilityReview="unit-only-no-live-evidence", telemetryConnection="unit-only-telemetry",
                 controlOrigin="https://control.example.invalid", servingOrigin="https://serving.example.invalid")
    return value


def test_distinct_published_honua_scope_is_admitted_but_not_executed():
    assert recovery.validate(scope())["costScope"]["createResources"] is False
    assert recovery.digest(scope()) == recovery.digest(copy.deepcopy(scope()))


@pytest.mark.parametrize("change", [
    lambda value: value.update(canaryService="production-service"),
    lambda value: value.update(environment="production"),
    lambda value: value["candidate"].update(image=value["baseline"]["image"]),
    lambda value: value["candidate"].update(image="public.ecr.aws/nginx/nginx:stable-alpine"),
    lambda value: value["candidate"]["provenance"].update(digest="unsigned"),
    lambda value: value["costScope"].update(createResources=True),
    lambda value: value["dataset"].update(queryPath=value["dataset"]["queryPath"] + "&%74oken=not-allowed"),
    lambda value: value["dataset"].update(queryPath=value["dataset"]["queryPath"].replace("resultRecordCount=256", "resultRecordCount=100000")),
])
def test_unreviewable_or_cross_scope_inputs_are_rejected(change):
    value = scope()
    change(value)
    with pytest.raises(recovery.QualificationError):
        recovery.validate(value)


def test_wrong_backend_or_unapproved_plan_is_not_accepted():
    class Control:
        def request(self, *_):
            return {"target": {"targetId": "isolated-target", "targetKind": "AwsEcs", "backend": "untrusted-executor", "environment": "qualification"}}
    with pytest.raises(recovery.QualificationError, match="identity mismatch"):
        recovery.target_plan(scope(), Control(), "candidate")


def test_changed_lock_is_rejected_before_provenance_process_or_provider_use():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "platform-lock.json"
        path.write_text("{}")
        value = scope()
        value["baseline"]["provenance"]["path"] = str(path)
        with patch.object(recovery.subprocess, "run") as process:
            with pytest.raises(recovery.QualificationError, match="signed release input bytes changed"):
                recovery.provenance(value)
            process.assert_not_called()


def test_exclusive_intent_prevents_mutation_replay():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "intent.json"
        recovery.atomic(path, {"intent": "first"}, exclusive=True)
        with pytest.raises(FileExistsError):
            recovery.atomic(path, {"intent": "second"}, exclusive=True)
        assert json.loads(path.read_text())["intent"] == "first"


def test_prepare_never_uses_credentials_network_or_cloud_and_reports_unrun(tmp_path, capsys):
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope()))
    with patch.object(recovery.subprocess, "run") as process, patch.object(recovery.Http, "request") as request:
        assert recovery.main(["prepare", "--scope", str(path), "--evidence", str(tmp_path / "evidence")]) == 0
        process.assert_not_called()
        request.assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "unrun"
    report = json.loads((tmp_path / "evidence/prepare.json").read_text())
    assert report["status"] == "unrun"
    assert set(report["credentialsPresent"]) == set(recovery.CREDENTIAL_ENVS)


def test_target_approval_is_preserved_rather_than_disabled_for_a_green_run():
    value = scope()
    parameters = {"aws.region": value["region"], "aws.ecs.canary_weight_percentage": str(value["canaryWeightPercent"]), "aws.ecs.cluster": value["cluster"], "aws.ecs.canary_service": value["canaryService"],
                  "aws.ecs.stable_service": value["stableService"], "aws.alb.listener_rule_arn": value["listenerRuleArn"],
                  "aws.alb.canary_target_group_arn": value["canaryTargetGroupArn"], "aws.alb.stable_target_group_arn": value["stableTargetGroupArn"],
                  "telemetry.connection": value["telemetryConnection"],
                  "telemetry.healthz.url": value["servingOrigin"] + "/healthz/ready",
                  "telemetry.golden_query.url": value["servingOrigin"] + value["telemetry"]["goldenQueryPath"],
                  "telemetry.golden_query.expected_sha256": value["telemetry"]["goldenQueryResponseSha256"],
                  "telemetry.prometheus.canary_job": value["telemetry"]["canaryMetricsJob"],
                  "telemetry.prometheus.job": value["telemetry"]["stableMetricsJob"]}
    plan = {"target": {"targetId": value["deployTargetId"], "targetKind": "AwsEcs", "backend": "honua-aws-ecs-alb",
                       "environment": value["environment"], "desiredRevision": value["candidate"]["taskDefinition"], "parameters": parameters},
            "requiresApproval": True, "readyToSubmit": False, "backendRegistered": True, "requiresOutOfBandMigrations": False}
    class Control:
        def request(self, *_):
            return plan
    assert recovery.target_plan(value, Control(), "candidate")["targetApprovalRequired"] is True
    plan.update(requiresApproval=False, readyToSubmit=True)
    with pytest.raises(recovery.QualificationError, match="governance"):
        recovery.target_plan(value, Control(), "candidate")


def test_signed_manifest_contract_binds_source_and_image_without_inventing_a_lock(tmp_path):
    value = scope()
    platforms = {}
    for role in ("baseline", "candidate"):
        artifact = value[role]
        path = tmp_path / f"{role}.yaml"
        payload = {"components": {"honua-server": {"repository": "https://github.com/honua-io/honua-server",
                   "sha": artifact["sourceSha"], "image": "ghcr.io/honua-io/honua-server:published",
                   "digest": artifact["image"].split("@")[1]}}}
        path.write_text(json.dumps(payload))
        artifact["provenance"].update(path=str(path), digest="sha256:" + recovery.hashlib.sha256(path.read_bytes()).hexdigest())
        platforms[payload["components"]["honua-server"]["digest"]] = {"amd64": artifact["amd64Digest"]}
    with patch.object(recovery.subprocess, "run", return_value=type("Verified", (), {"returncode": 0})()) as verify, \
            patch.object(recovery, "registry_image_platform_digests", side_effect=lambda coordinate, digest: platforms[digest]):
        result = recovery.provenance(value)
    assert len(result) == 2
    assert all(receipt["signatureVerified"] for receipt in result)
    for call in verify.call_args_list:
        arguments = call.args[0]
        assert arguments[0:2] == ["cosign", "verify-blob"]
        assert "https://token.actions.githubusercontent.com" in arguments
        assert "https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk" in arguments
        assert "--certificate-github-workflow-sha" in arguments
    value["candidate"]["sourceSha"] = "b" * 40
    with patch.object(recovery.subprocess, "run", return_value=type("Verified", (), {"returncode": 0})()), \
            patch.object(recovery, "registry_image_platform_digests", side_effect=lambda coordinate, digest: platforms[digest]):
        with pytest.raises(recovery.QualificationError, match="source binding differs"):
            recovery.provenance(value)


@pytest.mark.parametrize("mutation", [
    lambda definition: definition.update(cpu="4096"),
    lambda definition: definition["containerDefinitions"][0].update(image="public.ecr.aws/nginx/nginx:stable-alpine"),
    lambda definition: definition["containerDefinitions"][0].update(secrets=[]),
])
def test_candidate_task_definition_is_checked_before_any_proposal(mutation):
    value = scope()
    definition = {"taskDefinitionArn": value["candidate"]["taskDefinition"], "requiresCompatibilities": ["FARGATE"],
                  "cpu": "256", "memory": "512", "runtimePlatform": {"cpuArchitecture": "X86_64"},
                  "containerDefinitions": [{"name": value["honuaContainerName"], "image": value["candidate"]["image"],
                    "secrets": [{"name": value["dataset"]["connectionSecretName"], "valueFrom": value["dataset"]["connectionSecretArn"]}]}]}
    with patch.object(recovery, "aws", return_value={"taskDefinition": definition}):
        assert recovery.task_definition(value, "candidate")["databaseReferenceMatched"] is True
        mutation(definition)
        with pytest.raises(recovery.QualificationError):
            recovery.task_definition(value, "candidate")
