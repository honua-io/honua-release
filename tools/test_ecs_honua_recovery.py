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
        assert arguments[arguments.index("--certificate-oidc-issuer") + 1] == "https://token.actions.githubusercontent.com"
        assert arguments[arguments.index("--certificate-identity") + 1] == "https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk"
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


@pytest.mark.parametrize("service,role", [("canaryService", "candidate"), ("stableService", "baseline")])
@pytest.mark.parametrize("mutation", [
    lambda response: response.update(tasks=[]),
    lambda response: response.update(failures=[{"reason": "MISSING"}]),
    lambda response: response["tasks"][0].update(taskArn="unlisted-task"),
    lambda response: response["tasks"][0].update(group="service:another-service"),
    lambda response: response["tasks"][0].update(lastStatus="STOPPED"),
])
def test_runtime_task_evidence_requires_complete_exact_service_membership(service, role, mutation):
    value = scope()
    task = {"taskArn": "reviewed-task", "taskDefinitionArn": value[role]["taskDefinition"],
            "group": "service:" + value[service], "lastStatus": "RUNNING",
            "containers": [{"name": value["honuaContainerName"], "imageDigest": value[role]["amd64Digest"]}]}
    response = {"tasks": [task], "failures": []}
    with patch.object(recovery, "aws", side_effect=[{"taskArns": ["reviewed-task"]}, response]):
        assert recovery.running_tasks(value, value[service], role) == [task]
    mutation(response)
    with patch.object(recovery, "aws", side_effect=[{"taskArns": ["reviewed-task"]}, response]):
        with pytest.raises(recovery.QualificationError):
            recovery.running_tasks(value, value[service], role)


def runtime_service(value, role="candidate", service_field="canaryService"):
    task = {"taskArn": "reviewed-task", "taskDefinitionArn": value[role]["taskDefinition"],
            "group": "service:" + value[service_field], "lastStatus": "RUNNING", "desiredStatus": "RUNNING",
            "launchType": "FARGATE", "containers": [{"name": value["honuaContainerName"],
            "imageDigest": value[role]["amd64Digest"], "networkInterfaces": [{"privateIpv4Address": "10.0.1.7"}]}]}
    service = {"serviceName": value[service_field], "status": "ACTIVE",
               "taskDefinition": value[role]["taskDefinition"], "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
               "deployments": [{"status": "PRIMARY", "taskDefinition": value[role]["taskDefinition"],
                                "desiredCount": 1, "runningCount": 1, "pendingCount": 0, "rolloutState": "COMPLETED"}],
               "loadBalancers": [{"targetGroupArn": value["canaryTargetGroupArn"],
                                  "containerName": value["honuaContainerName"], "containerPort": 8080}]}
    return service, task


@pytest.mark.parametrize("mutation", [
    lambda service: service.update(taskDefinition="another-revision"),
    lambda service: service.update(desiredCount=0),
    lambda service: service.update(desiredCount=2),
    lambda service: service.update(pendingCount=1),
    lambda service: service.update(status="DRAINING"),
    lambda service: service.update(serviceName="another-service"),
    lambda service: service["deployments"][0].update(taskDefinition="another-revision"),
    lambda service: service["deployments"][0].update(rolloutState="IN_PROGRESS"),
    lambda service: service["deployments"][0].update(pendingCount=1),
    lambda service: service["deployments"].append(dict(service["deployments"][0])),
])
def test_old_running_tasks_cannot_certify_divergent_service_desired_state(mutation):
    value = scope()
    service, task = runtime_service(value)
    responses = [{"services": [service]}, {"taskArns": [task["taskArn"]]}, {"tasks": [task]}]
    with patch.object(recovery, "aws", side_effect=responses):
        assert recovery.converged_service(value, value["canaryService"], "candidate") == (service, [task])
    mutation(service)
    with patch.object(recovery, "aws", side_effect=responses):
        with pytest.raises(recovery.QualificationError):
            recovery.converged_service(value, value["canaryService"], "candidate")


@pytest.mark.parametrize("mutation", [
    lambda service, task, health: service.update(loadBalancers=[]),
    lambda service, task, health: service["loadBalancers"][0].update(targetGroupArn="another-group"),
    lambda service, task, health: service["loadBalancers"][0].update(containerName="sidecar"),
    lambda service, task, health: service["loadBalancers"][0].update(containerPort=9090),
    lambda service, task, health: task["containers"][0].update(networkInterfaces=[]),
    lambda service, task, health: health[0]["Target"].update(Id="10.0.9.99"),
    lambda service, task, health: health[0]["Target"].update(Id="i-unrelated-instance"),
    lambda service, task, health: health[0]["Target"].update(Port=9090),
    lambda service, task, health: health[0]["TargetHealth"].update(State="draining"),
    lambda service, task, health: health.append(dict(health[0])),
])
def test_healthy_targets_must_identify_the_reviewed_service_tasks(mutation):
    value = scope()
    service, task = runtime_service(value)
    health = [{"Target": {"Id": "10.0.1.7", "Port": 8080}, "TargetHealth": {"State": "healthy"}}]
    with patch.object(recovery, "aws", return_value={"TargetHealthDescriptions": health}):
        recovery.service_target_health(value, service, [task], value["canaryTargetGroupArn"])
        mutation(service, task, health)
        with pytest.raises(recovery.QualificationError):
            recovery.service_target_health(value, service, [task], value["canaryTargetGroupArn"])


def test_duplicate_healthy_address_cannot_hide_missing_second_task():
    value = scope()
    service, task = runtime_service(value)
    _, other = runtime_service(value)
    other["taskArn"] = "second-task"
    other["containers"][0]["networkInterfaces"] = [{"privateIpv4Address": "10.0.1.8"}]
    health = [{"Target": {"Id": "10.0.1.7", "Port": 8080}, "TargetHealth": {"State": "healthy"}}] * 2
    with patch.object(recovery, "aws", return_value={"TargetHealthDescriptions": health}):
        with pytest.raises(recovery.QualificationError, match="uniquely"):
            recovery.service_target_health(value, service, [task, other], value["canaryTargetGroupArn"])


def test_ipv6_targets_are_joined_by_address_value():
    value = scope()
    service, task = runtime_service(value)
    task["containers"][0]["networkInterfaces"] = [{"ipv6Address": "fd00:0:0:0:0:0:0:7"}]
    health = [{"Target": {"Id": "fd00::7", "Port": 8080}, "TargetHealth": {"State": "healthy"}}]
    with patch.object(recovery, "aws", return_value={"TargetHealthDescriptions": health}):
        recovery.service_target_health(value, service, [task], value["canaryTargetGroupArn"])


@pytest.mark.parametrize("role,canary_weight", [("candidate", 100), ("baseline", 100), ("baseline", 0)])
def test_image_state_checks_both_converged_services_and_their_actual_targets(role, canary_weight):
    value = scope()
    canary, task = runtime_service(value, role)
    stable, stable_task = runtime_service(value, "baseline", "stableService")
    stable["loadBalancers"][0]["targetGroupArn"] = value["stableTargetGroupArn"]
    stable_task["taskArn"] = "stable-task"
    stable_task["containers"][0]["networkInterfaces"] = [{"privateIpv4Address": "10.0.1.8"}]
    services = {value["canaryService"]: (canary, task), value["stableService"]: (stable, stable_task)}
    groups = {value["canaryTargetGroupArn"]: "10.0.1.7", value["stableTargetGroupArn"]: "10.0.1.8"}
    inspected_groups = []
    def provider(_scope, *args):
        command = args[:2]
        if command == ("sts", "get-caller-identity"):
            return {"Account": value["awsAccountId"]}
        if command == ("ecs", "describe-services"):
            return {"services": [services[args[-1]][0]]}
        if command == ("ecs", "list-tasks"):
            return {"taskArns": [services[args[-1]][1]["taskArn"]]}
        if command == ("ecs", "describe-tasks"):
            return {"tasks": [task if args[-1] == task["taskArn"] else stable_task]}
        if command == ("elbv2", "describe-rules"):
            return {"Rules": [{"IsDefault": False, "Actions": [{"Type": "forward", "ForwardConfig": {
                "TargetGroups": [{"TargetGroupArn": value["canaryTargetGroupArn"], "Weight": canary_weight},
                                 {"TargetGroupArn": value["stableTargetGroupArn"], "Weight": 100 - canary_weight}]}}]}]}
        if command == ("elbv2", "describe-target-health"):
            inspected_groups.append(args[-1])
            return {"TargetHealthDescriptions": [{"Target": {"Id": groups[args[-1]], "Port": 8080},
                                                   "TargetHealth": {"State": "healthy"}}]}
        raise AssertionError(command)
    with patch.object(recovery, "aws", side_effect=provider), patch.object(recovery, "task_definition"):
        result = recovery.image_state(value, role)
        assert result["servingTargetsHealthy"] is True
        assert set(inspected_groups) == set(groups)
        # Retained baseline tasks alone cannot disguise a pending replacement.
        stable["desiredCount"] = 2
        with pytest.raises(recovery.QualificationError, match="desired state"):
            recovery.image_state(value, role)
