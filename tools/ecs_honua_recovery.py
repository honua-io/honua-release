#!/usr/bin/env python3
"""Existing-target Honua ECS upgrade/recovery qualification; never provisions infrastructure.

Proposal commands only seal server-owned proposals. A separate human uses the canonical inbox.
There is deliberately no AWS mutation, approval, arbitrary command, or automatic retry executor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

IMAGE = re.compile(r"ghcr\.io/honua-io/honua-server@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from release_rollback import atomic_write  # noqa: E402
from image_platforms import registry_image_platform_digests, verify_image_platform_digests  # noqa: E402

CREDENTIAL_ENVS = ("HONUA_ECS_QUAL_PROPOSER_KEY", "HONUA_ECS_QUAL_DATA_KEY")


class QualificationError(ValueError):
    pass


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def url(value):
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment, "HTTPS origin required; no credentials or query allowed")
    require(parsed.path in ("", "/"), "host binding must be an origin")


def validate(scope):
    require(scope.get("schema") == "honua.ecs-recovery-scope/v1", "unsupported scope schema")
    require("REVIEW-REQUIRED" not in json.dumps(scope), "scope still contains unreviewed placeholders")
    require(re.fullmatch(r"honua-qual-[a-z0-9-]{1,48}", scope.get("runId", "")), "isolated runId required")
    for field in ("controlOrigin", "servingOrigin"):
        url(scope[field])
    for field in ("cluster", "canaryService", "stableService"):
        require(scope[field].startswith("honua-qual-"), "only explicitly named honua-qual-* substrate is admitted")
    require(scope["canaryService"] != scope["stableService"], "separate stable/canary services required")
    require(re.fullmatch(r"\d{12}", scope.get("awsAccountId", "")), "explicit AWS account required")
    require(re.fullmatch(r"[a-z]{2}-[a-z]+-\d", scope.get("region", "")), "explicit AWS region required")
    require(re.fullmatch(r"apikey:api-key:[0-9a-f-]{36}", scope.get("proposerPrincipalId", "")), "exact stored proposer principal required")
    require(scope.get("deployTargetId") and scope.get("environment") == "qualification", "qualification deploy target required")
    for field in ("listenerRuleArn", "stableTargetGroupArn", "canaryTargetGroupArn"):
        require(scope[field].startswith(f"arn:aws:elasticloadbalancing:{scope['region']}:{scope['awsAccountId']}:"), "ALB account/region mismatch")
    for role in ("baseline", "candidate"):
        artifact = scope[role]
        proof = artifact.get("provenance", {})
        require(proof.get("kind") in ("platform-manifest", "platform-lock") and Path(proof.get("path", "")).is_absolute() and Path(proof.get("bundlePath", "")).is_absolute()
                and DIGEST.fullmatch(proof.get("digest", "")), "exact signed release manifest or lock required")
        require(proof.get("workflow") in ("promote.yml", "nightly-certification.yml", "release-train.yml") and
                SHA.fullmatch(proof.get("sourceSha", "")) and re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9._/-]+", proof.get("sourceRef", "")), "trusted exact release signer identity required")
        require(IMAGE.fullmatch(artifact.get("image", "")), "published immutable Honua image required")
        require(SHA.fullmatch(artifact.get("sourceSha", "")), "immutable source SHA required")
        require(DIGEST.fullmatch(artifact.get("amd64Digest", "")), "exact runnable amd64 image digest required")
        require(re.fullmatch(rf"arn:aws:ecs:{scope['region']}:{scope['awsAccountId']}:task-definition/honua-qual-[A-Za-z0-9_-]+:\d+", artifact.get("taskDefinition", "")), "isolated immutable task definition revision required")
    require(scope["baseline"]["image"] != scope["candidate"]["image"], "two distinct images required")
    require(scope["baseline"]["sourceSha"] != scope["candidate"]["sourceSha"], "two distinct source revisions required")
    require(scope["baseline"]["taskDefinition"] != scope["candidate"]["taskDefinition"], "two distinct task revisions required")
    dataset = scope["dataset"]
    path = dataset["queryPath"]
    require(path.startswith("/rest/services/") and "/FeatureServer/" in path and "/query?" in path
            and "resultRecordCount=" in path and "orderByFields=" in path, "bounded ordered FeatureServer query required")
    parsed = urlsplit(path)
    query = parse_qs(parsed.query)
    require(not parsed.netloc and not parsed.fragment and
            not any(key.lower().replace("_", "") in {"token", "key", "apikey", "accesstoken", "password", "authorization"} for key in query), "query must not contain credentials or redirects")
    require(query.get("f") == ["json"] and len(query.get("resultRecordCount", [])) == 1 and
            query["resultRecordCount"][0].isdigit() and 0 < int(query["resultRecordCount"][0]) <= 256,
            "bounded JSON feature query required")
    require(type(dataset["expectedCount"]) is int and 0 < dataset["expectedCount"] <= 256, "retained nonempty bounded dataset required")
    require(DIGEST.fullmatch(dataset["expectedFeaturesDigest"]), "reviewed retained dataset checksum required")
    require(dataset.get("databaseIdentity", "").startswith("honua-qual-") and dataset.get("snapshotReference", "").startswith("honua-qual-")
            and dataset.get("connectionSecretArn") and dataset.get("connectionSecretName"), "isolated retained database and restore snapshot references required")
    require(scope.get("schemaCompatibilityReview") and scope.get("telemetryConnection"), "schema review and telemetry binding required")
    telemetry = scope.get("telemetry", {})
    require(telemetry.get("canaryMetricsJob", "").startswith("honua-qual-") and
            telemetry.get("stableMetricsJob", "").startswith("honua-qual-") and
            re.fullmatch(r"[0-9a-f]{64}", telemetry.get("goldenQueryResponseSha256", "")), "bounded canary/stable metric jobs and golden response checksum required")
    golden = telemetry.get("goldenQueryPath", "")
    require(golden.startswith("/rest/services/") and "/FeatureServer/" in golden and "/query?" in golden and
            "resultRecordCount=" in golden and "orderByFields=" in golden, "bounded telemetry golden query required")
    golden_parts = urlsplit(golden)
    golden_query = parse_qs(golden_parts.query)
    require(not golden_parts.netloc and not golden_parts.fragment and golden_query.get("f") == ["json"] and
            len(golden_query.get("resultRecordCount", [])) == 1 and golden_query["resultRecordCount"][0].isdigit() and
            0 < int(golden_query["resultRecordCount"][0]) <= 256 and
            not any(key.lower().replace("_", "") in {"token", "key", "apikey", "accesstoken", "password", "authorization"} for key in golden_query),
            "telemetry golden query must be bounded and credential-free")
    require(scope.get("costScope", {}).get("createResources") is False, "harness never provisions paid resources")
    require(type(scope["costScope"].get("maxElapsedMinutes")) is int and 0 < scope["costScope"]["maxElapsedMinutes"] <= 60, "reviewed bounded run budget required")
    require(type(scope["costScope"].get("maxTasksPerService")) is int and 0 < scope["costScope"]["maxTasksPerService"] <= 2, "at most two tasks per isolated service admitted")
    return scope


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise QualificationError("redirect refused")


class Http:
    def __init__(self, origin, key=None):
        self.origin, self.key = origin.rstrip("/"), key
        self.session = None
        self.sequence = 0
        self.protocol = "2025-03-26"

    def request(self, path, body=None, method=None):
        require(path.startswith("/") and not path.startswith("//"), "relative route required")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if path == "/mcp":
            headers["MCP-Protocol-Version"] = self.protocol
        if self.key:
            headers["X-API-Key"] = self.key
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = Request(self.origin + path, None if body is None else json.dumps(body).encode(), headers,
                          method=method or ("GET" if body is None else "POST"))
        try:
            with build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, "response size exceeded")
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if not raw:
                    return None
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    messages = [json.loads(line[5:].strip()) for line in raw.decode().splitlines() if line.startswith("data:")]
                    matches = [message for message in messages if message.get("id") == body.get("id")]
                    require(len(matches) == 1, "ambiguous MCP response")
                    return matches[0]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode()
        except HTTPError as error:
            # Neither raw provider errors nor credential-bearing request headers enter receipts.
            raise QualificationError(f"HTTP status {error.code}; inspect server audit with request identity") from None

    def tool(self, name, arguments):
        self.sequence += 1
        if self.sequence == 1:
            initialized = self.request("/mcp", {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "honua-ecs-recovery-qualification", "version": "1"}}})
            require(initialized and "result" in initialized, "MCP initialization failed")
            negotiated = initialized["result"].get("protocolVersion")
            require(negotiated in ("2025-03-26", "2025-06-18", "2025-11-25"), "unsupported negotiated MCP protocol")
            self.protocol = negotiated
            self.request("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = self.request("/mcp", {"jsonrpc": "2.0", "id": self.sequence, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        result = reply.get("result", {})
        require(not result.get("isError") and "error" not in reply, "canonical tool refused; inspect server audit")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        content = result.get("content", [])
        require(len(content) == 1 and content[0].get("type") == "text", "structured tool result required")
        return json.loads(content[0]["text"])


def aws(scope, *arguments):
    result = subprocess.run(["aws", *arguments, "--region", scope["region"], "--output", "json"],
                            capture_output=True, timeout=45, env={**os.environ, "AWS_PAGER": ""}, check=False)
    require(result.returncode == 0, "AWS read-only inspection failed; private provider error withheld")
    return json.loads(result.stdout)


def proposer_binding(scope, control):
    key_id = scope["proposerPrincipalId"].split(":")[-1]
    envelope = control.request("/api/v1/admin/api-keys")
    records = envelope.get("data", [])
    matches = [record for record in records if record.get("id") == key_id and record.get("status") == "active" and
               record.get("keyPrefix") and control.key.startswith(record["keyPrefix"])]
    require(len(matches) == 1, "control credential does not match the reviewed stored proposer identity")
    effective = control.request(f"/api/v1/admin/api-keys/{key_id}/effective-permissions").get("data", {})
    require(effective.get("id") == key_id and effective.get("status") == "active" and effective.get("canAuthenticate") is True,
            "proposer credential is not currently active")


def target_plan(scope, control, role):
    artifact = scope[role]
    plan = control.request("/api/v1/admin/deploy/plan", {"targetId": scope["deployTargetId"], "desiredRevision": artifact["taskDefinition"]})
    target = plan["target"]
    require(target["targetId"] == scope["deployTargetId"] and target["targetKind"] == "AwsEcs" and target["backend"] == "honua-aws-ecs-alb"
            and target["environment"] == scope["environment"] and target["desiredRevision"] == artifact["taskDefinition"], "canonical target identity mismatch")
    parameters = target["parameters"]
    expected = {"aws.ecs.cluster": scope["cluster"], "aws.ecs.canary_service": scope["canaryService"],
                "aws.ecs.stable_service": scope["stableService"], "aws.alb.listener_rule_arn": scope["listenerRuleArn"],
                "aws.alb.canary_target_group_arn": scope["canaryTargetGroupArn"], "aws.alb.stable_target_group_arn": scope["stableTargetGroupArn"],
                "telemetry.connection": scope["telemetryConnection"],
                "telemetry.healthz.url": scope["servingOrigin"].rstrip("/") + "/healthz/ready",
                "telemetry.golden_query.url": scope["servingOrigin"].rstrip("/") + scope["telemetry"]["goldenQueryPath"],
                "telemetry.golden_query.expected_sha256": scope["telemetry"]["goldenQueryResponseSha256"],
                "telemetry.prometheus.canary_job": scope["telemetry"]["canaryMetricsJob"],
                "telemetry.prometheus.job": scope["telemetry"]["stableMetricsJob"]}
    require(all(parameters.get(key) == value for key, value in expected.items()), "canonical cloud/telemetry scope mismatch")
    require(plan.get("backendRegistered") is True and plan.get("readyToSubmit") is False and not plan.get("requiresOutOfBandMigrations")
            and not plan.get("blockingReasons") and plan.get("requiresApproval") is True, "approval-required target governance must be preserved")
    return {"digest": digest(plan), "targetId": target["targetId"], "canonicalInboxRequired": True, "targetApprovalRequired": True}


def provenance(scope):
    receipts = []
    for role in ("baseline", "candidate"):
        declared = scope[role]
        proof = declared["provenance"]
        path = Path(proof["path"])
        raw = path.read_bytes()
        observed = "sha256:" + hashlib.sha256(raw).hexdigest()
        require(observed == proof["digest"], "signed release input bytes changed")
        workflow = "honua-io/honua-release/.github/workflows/" + proof["workflow"]
        if proof["kind"] == "platform-lock" and proof["workflow"] == "release-train.yml":
            command = ["gh", "attestation", "verify", str(path), "--bundle", proof["bundlePath"],
                       "--repo", "honua-io/honua-release", "--signer-workflow", workflow,
                       "--source-digest", proof["sourceSha"], "--source-ref", proof["sourceRef"], "--deny-self-hosted-runners"]
        else:
            command = ["cosign", "verify-blob", str(path), "--bundle", proof["bundlePath"],
                       "--certificate-identity", "https://github.com/" + workflow + "@" + proof["sourceRef"],
                       "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
                       "--certificate-github-workflow-repository", "honua-io/honua-release",
                       "--certificate-github-workflow-sha", proof["sourceSha"], "--certificate-github-workflow-ref", proof["sourceRef"]]
        verified = subprocess.run(command, capture_output=True, timeout=90, check=False)
        require(verified.returncode == 0, "release signature verification failed; no unsigned fallback")
        if proof["kind"] == "platform-lock":
            component = json.loads(raw)["components"]["honua-server"]
            require(component.get("artifactIdentityModel") == "published" and
                    component["source"]["repository"] == "https://github.com/honua-io/honua-server" and
                    component["source"]["revision"] == declared["sourceSha"], "attested component source binding differs")
            artifacts = [artifact for artifact in component["artifacts"] if artifact.get("kind") == "image" and
                         artifact.get("coordinate") == "ghcr.io/honua-io/honua-server" and
                         artifact.get("digest") == declared["image"].split("@", 1)[1]]
            require(len(artifacts) == 1 and artifacts[0].get("sourceRevision") == declared["sourceSha"] and
                    artifacts[0]["platformDigests"]["amd64"] == declared["amd64Digest"], "attested image binding differs")
            verify_image_platform_digests(artifacts[0])
        else:
            import yaml
            component = yaml.safe_load(raw)["components"]["honua-server"]
            require(component.get("repository") == "https://github.com/honua-io/honua-server" and
                    component.get("artifactSourceRevision", component.get("sha")) == declared["sourceSha"] and
                    component["digest"] == declared["image"].split("@", 1)[1] and
                    component["image"].split("@", 1)[0].split(":", 1)[0] == "ghcr.io/honua-io/honua-server", "signed manifest image/source binding differs")
            platforms = registry_image_platform_digests("ghcr.io/honua-io/honua-server", component["digest"])
            require(platforms["amd64"] == declared["amd64Digest"], "signed manifest runnable image binding differs")
            if component.get("platformDigests"):
                require(component["platformDigests"] == platforms, "signed manifest platform labels differ from registry bytes")
        receipts.append({"role": role, "signedInputDigest": observed, "signatureVerified": True,
                         "sourceSha": declared["sourceSha"], "amd64Digest": declared["amd64Digest"]})
    return receipts


def denied_query(scope):
    request = Request(scope["servingOrigin"].rstrip("/") + scope["dataset"]["queryPath"], headers={"Accept": "application/json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(4097)
            require(len(raw) <= 4096, "anonymous query unexpectedly returned feature data")
            payload = json.loads(raw)
            require(payload.get("error", {}).get("code") in (401, 403), "anonymous query did not deny access")
    except HTTPError as error:
        require(error.code in (401, 403), "anonymous query failed for a reason other than authorization")
    return True


def serving(scope, data, role):
    require(str(data.request("/healthz/ready")).strip().lower() == "ready", "Honua readiness failed")
    manifest = data.request("/api/v1/capabilities/manifest")
    revision = manifest["server"].get("deploymentRevision")
    artifact = scope[role]
    require(revision in (artifact["sourceSha"], artifact["amd64Digest"], artifact["image"].split("@", 1)[1]), "serving version differs from immutable artifact")
    payload = data.request(scope["dataset"]["queryPath"])
    require("features" in payload and not payload.get("exceededTransferLimit") and "error" not in payload, "complete FeatureServer query required")
    features = payload["features"]
    require(len(features) == scope["dataset"]["expectedCount"], "retained dataset count changed")
    require(digest(features) == scope["dataset"]["expectedFeaturesDigest"], "retained dataset checksum changed")
    return {"revision": revision, "featureCount": len(features), "featuresDigest": digest(features), "ready": True, "anonymousQueryDenied": denied_query(scope)}


def snapshot_state(scope):
    response = aws(scope, "rds", "describe-db-snapshots", "--db-snapshot-identifier", scope["dataset"]["snapshotReference"])
    snapshots = response.get("DBSnapshots", [])
    require(len(snapshots) == 1 and snapshots[0].get("Status") == "available" and
            snapshots[0].get("DBInstanceIdentifier") == scope["dataset"]["databaseIdentity"], "retained isolated database snapshot is not available")
    arn = snapshots[0].get("DBSnapshotArn", "")
    require(arn.startswith(f"arn:aws:rds:{scope['region']}:{scope['awsAccountId']}:snapshot:"), "snapshot account/region differs")
    return {"snapshotReferenceDigest": digest(arn), "available": True,
            "createdAt": snapshots[0].get("SnapshotCreateTime")}


def image_state(scope, role):
    identity = aws(scope, "sts", "get-caller-identity")
    require(identity["Account"] == scope["awsAccountId"], "AWS account differs from reviewed scope")
    definition = aws(scope, "ecs", "describe-task-definition", "--task-definition", scope[role]["taskDefinition"])["taskDefinition"]
    containers = definition["containerDefinitions"]
    honua = [container for container in containers if container.get("name") == scope["honuaContainerName"]]
    require(len(honua) == 1 and honua[0]["image"] == scope[role]["image"], "task definition does not pin the reviewed Honua image")
    secret = scope["dataset"]
    references = {entry["name"]: entry["valueFrom"] for entry in honua[0].get("secrets", [])}
    require(references.get(secret["connectionSecretName"]) == secret["connectionSecretArn"], "task definition does not bind the retained database secret reference")
    task_arns = aws(scope, "ecs", "list-tasks", "--cluster", scope["cluster"], "--service-name", scope["canaryService"])["taskArns"]
    require(task_arns and len(task_arns) <= scope["costScope"]["maxTasksPerService"], "bounded running canary task set required")
    tasks = aws(scope, "ecs", "describe-tasks", "--cluster", scope["cluster"], "--tasks", *task_arns)["tasks"]
    require(all(task["taskDefinitionArn"] == scope[role]["taskDefinition"] and task.get("lastStatus") == "RUNNING" for task in tasks), "ECS task revision has not converged")
    require(all(any(container.get("name") == scope["honuaContainerName"] and container.get("imageDigest") == scope[role]["amd64Digest"] for container in task["containers"]) for task in tasks), "ECS runnable image digest mismatch")
    rule = aws(scope, "elbv2", "describe-rules", "--rule-arns", scope["listenerRuleArn"])["Rules"]
    require(len(rule) == 1 and rule[0].get("IsDefault") is False, "exact non-default certification rule required")
    forwards = [action for action in rule[0]["Actions"] if action["Type"] == "forward"]
    require(len(forwards) == 1, "one explicit weighted traffic action required")
    groups = {group["TargetGroupArn"]: group["Weight"] for group in forwards[0]["ForwardConfig"]["TargetGroups"]}
    expected = {scope["canaryTargetGroupArn"]: 100, scope["stableTargetGroupArn"]: 0}
    baseline = {scope["canaryTargetGroupArn"]: 0, scope["stableTargetGroupArn"]: 100}
    require(groups == expected or (role == "baseline" and groups == baseline), "unexpected traffic group/weights")
    active_group = scope["canaryTargetGroupArn"] if groups == expected else scope["stableTargetGroupArn"]
    health = aws(scope, "elbv2", "describe-target-health", "--target-group-arn", active_group)["TargetHealthDescriptions"]
    require(health and all(item["TargetHealth"]["State"] == "healthy" for item in health), "serving target health has not converged")
    stable = aws(scope, "ecs", "describe-services", "--cluster", scope["cluster"], "--services", scope["stableService"])
    stable_services = stable.get("services", [])
    require(not stable.get("failures") and len(stable_services) == 1 and
            stable_services[0]["taskDefinition"] == scope["baseline"]["taskDefinition"] and
            stable_services[0]["runningCount"] > 0 and stable_services[0]["pendingCount"] == 0,
            "stable service no longer retains the reviewed baseline task definition")
    stable_arns = aws(scope, "ecs", "list-tasks", "--cluster", scope["cluster"], "--service-name", scope["stableService"])["taskArns"]
    require(stable_arns and len(stable_arns) <= scope["costScope"]["maxTasksPerService"], "bounded retained stable task set required")
    stable_tasks = aws(scope, "ecs", "describe-tasks", "--cluster", scope["cluster"], "--tasks", *stable_arns)["tasks"]
    require(all(task["taskDefinitionArn"] == scope["baseline"]["taskDefinition"] and task.get("lastStatus") == "RUNNING" and
                any(container.get("name") == scope["honuaContainerName"] and container.get("imageDigest") == scope["baseline"]["amd64Digest"]
                    for container in task["containers"]) for task in stable_tasks), "retained stable runnable baseline image differs")
    return {"taskDefinition": scope[role]["taskDefinition"], "runningTaskCount": len(tasks),
            "imageDigest": scope[role]["amd64Digest"], "trafficWeightsDigest": digest(groups), "servingTargetsHealthy": True}


def atomic(path, value, exclusive=False):
    if not exclusive:
        atomic_write(path, value)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "baseline", "propose-upgrade", "observe-upgrade", "propose-recovery", "observe-recovery", "workflow-upgrade-approval", "workflow-recovery-approval"])
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--reviewed-scope-digest")
    args = parser.parse_args(argv)
    scope = validate(json.loads(args.scope.read_text()))
    binding = digest(scope)
    base = {"schema": "honua.ecs-recovery-evidence/v1", "scopeDigest": binding,
            "at": datetime.now(timezone.utc).isoformat(), "runId": scope["runId"]}
    if args.command == "prepare":
        result = {**base, "status": "unrun", "credentialsPresent": {name: bool(os.getenv(name)) for name in CREDENTIAL_ENVS},
                  "next": "Review exact target/data/snapshot/schema/cost scope before baseline/proposal commands; use separate human approval inbox."}
    else:
        require(args.reviewed_scope_digest == binding, "exact root-reviewed scope digest required")
        require(all(os.getenv(name) for name in CREDENTIAL_ENVS), "required scoped transport credentials absent")
        require(os.environ[CREDENTIAL_ENVS[0]] != os.environ[CREDENTIAL_ENVS[1]], "separate control and data credentials required")
        control = Http(scope["controlOrigin"], os.environ[CREDENTIAL_ENVS[0]])
        data = Http(scope["servingOrigin"], os.environ[CREDENTIAL_ENVS[1]])
        proposer_binding(scope, control)
        def evidence(name):
            value = json.loads((args.evidence / f"{name}.json").read_text())
            require(value.get("scopeDigest") == binding, "prior evidence belongs to another scope")
            return value
        if args.command == "baseline":
            result = {**base, "status": "baseline-observed", "provenance": provenance(scope), "serving": serving(scope, data, "baseline"),
                      "cloud": image_state(scope, "baseline"), "snapshot": snapshot_state(scope), "plan": target_plan(scope, control, "candidate")}
        elif args.command.startswith("propose-"):
            recovery = args.command == "propose-recovery"
            evidence("observe-upgrade" if recovery else "baseline")
            role = "baseline" if recovery else "candidate"
            provenance_receipts = provenance(scope)
            if not recovery:
                started = datetime.fromisoformat(evidence("baseline")["at"])
                require((datetime.now(timezone.utc) - started).total_seconds() <= scope["costScope"]["maxElapsedMinutes"] * 60,
                        "reviewed qualification budget expired before upgrade")
            serving(scope, data, "candidate" if recovery else "baseline")
            image_state(scope, "candidate" if recovery else "baseline")
            plan = target_plan(scope, control, role)
            # Persist intent BEFORE the network request. A lost result requires server reconciliation,
            # never an automatic re-proposal or remote mutation replay.
            atomic(args.evidence / f"{args.command}.intent.json", {**base, "plan": plan}, exclusive=True)
            tool = "honua_propose_rollback" if recovery else "honua_propose_deploy_operation"
            arguments = {"targetId": scope["deployTargetId"], "reason": f"Isolated version recovery qualification {scope['runId']}",
                         "idempotencyKey": f"{scope['runId']}:{args.command}:{binding[-24:]}"}
            arguments["toRevision" if recovery else "desiredRevision"] = scope[role]["taskDefinition"]
            output = control.tool(tool, arguments)
            require(output.get("requiresApproval") is True and output.get("proposalId") and not output.get("executionOperationId"), "canonical proposal bypassed human approval")
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,200}", output["proposalId"]), "invalid canonical proposal reference")
            result = {**base, "status": "awaiting-human-inbox", "proposalId": output["proposalId"], "tool": tool, "provenance": provenance_receipts}
        else:
            recovery = args.command in ("observe-recovery", "workflow-recovery-approval")
            approval_checkpoint = args.command.startswith("workflow-")
            proposal = evidence("propose-recovery" if recovery else "propose-upgrade")
            detail = control.request("/api/v1/admin/proposals/" + proposal["proposalId"])
            require(detail.get("status", "").lower() in ("applied", "executed", "succeeded") and detail.get("resolvedBy"), "separate human approval/execution not complete")
            require(detail.get("requestedBy") in (scope["proposerPrincipalId"], scope["proposerPrincipalId"].split(":")[-1]) and
                    detail["resolvedBy"] not in (scope["proposerPrincipalId"], scope["proposerPrincipalId"].split(":")[-1]), "proposer must not approve its own operation")
            operation_id = detail.get("executionOperationId")
            require(isinstance(operation_id, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", operation_id), "canonical execution reference absent or malformed")
            require(isinstance(detail["resolvedBy"], str) and len(detail["resolvedBy"]) <= 256 and
                    not any(os.environ[name] in detail["resolvedBy"] for name in CREDENTIAL_ENVS), "invalid approver identity")
            operation = control.request("/api/v1/admin/deploy/operations/" + operation_id)
            role = "baseline" if recovery else "candidate"
            expected_status = "AwaitingApproval" if approval_checkpoint else "Succeeded"
            require(operation.get("status") == expected_status and operation["target"]["targetId"] == scope["deployTargetId"]
                    and operation["target"]["targetKind"] == "AwsEcs" and operation["target"]["backend"] == "honua-aws-ecs-alb"
                    and operation["target"]["environment"] == scope["environment"] and operation["target"]["desiredRevision"] == scope[role]["taskDefinition"], "canonical workflow has not succeeded for exact target/revision")
            if approval_checkpoint:
                before_role = "candidate" if recovery else "baseline"
                result = {**base, "status": "awaiting-workflow-manual-approval", "proposalId": proposal["proposalId"],
                          "executionOperationId": operation_id, "resolvedBy": detail["resolvedBy"], "targetApprovalRequired": True,
                          "cloudBeforeManualApproval": image_state(scope, before_role), "servingBeforeManualApproval": serving(scope, data, before_role)}
                atomic(args.evidence / f"{args.command}.json", result)
                print(json.dumps({"scopeDigest": binding, "status": result["status"]}, sort_keys=True))
                return 0
            checkpoint = evidence("workflow-recovery-approval" if recovery else "workflow-upgrade-approval")
            require(checkpoint["executionOperationId"] == operation_id, "manual target approval checkpoint absent or mismatched")
            result = {**base, "status": "recovered" if recovery else "upgraded", "proposalId": proposal["proposalId"], "executionOperationId": operation_id,
                      "resolvedBy": detail["resolvedBy"], "cloud": image_state(scope, role), "serving": serving(scope, data, role),
                      "scenario": "successful-upgrade-then-reviewed-version-revert"}
            started = datetime.fromisoformat(evidence("baseline")["at"])
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
            result["elapsedSeconds"] = elapsed
            result["budgetSeconds"] = scope["costScope"]["maxElapsedMinutes"] * 60
            result["withinBudget"] = elapsed <= result["budgetSeconds"]
            if recovery and not result["withinBudget"]:
                result["status"] = "recovered-over-budget"
    atomic(args.evidence / f"{args.command}.json", result)
    print(json.dumps({key: result[key] for key in ("scopeDigest", "status")}, sort_keys=True))
    return 2 if result["status"] == "recovered-over-budget" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, OSError, KeyError, ValueError, subprocess.SubprocessError):
        print("ECS qualification stopped; inspect scope and canonical state. No automatic retry was issued.", file=sys.stderr)
        raise SystemExit(1)
