"""Concrete AWS/Terraform/HTTP observations for the update/rollback producer.

Only the maintained server image receives the independent controller credentials. Candidate
clients never run here. Public results contain digests and counts, not DSNs, keys or log bodies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from .update_rollback import Api, DeployApi, ProofError, checked, digest, require


def centroid_workload(vertices=65536):
    """Reviewed symmetric polygon: independent centroid oracle is the literal origin.

    This is real work, not a hold hook. Fast completion is an honest failed handoff. A larger
    reviewed workload can be selected within the fixed request bound; no fabricated delay exists.
    """
    require(type(vertices) is int and 4 <= vertices <= 262144 and vertices % 4 == 0, "invalid centroid fixture size")
    points = [(10 * math.cos(2 * math.pi * i / vertices), 10 * math.sin(2 * math.pi * i / vertices))
              for i in range(vertices)]
    raw = struct.pack("<BIII", 1, 3, 1, vertices + 1)
    raw += b"".join(struct.pack("<dd", *point) for point in (*points, points[0]))
    return {"inputs": {"wkb": base64.b64encode(raw).decode(), "srid": "4326"}}


def env_key(line):
    """Match .NET's case-insensitive colon/double-underscore configuration names."""
    return line.partition("=")[0].replace(":", "__").lower()


class AwsRuntime:
    def __init__(self, target, state, *, admin_key, approval_key, serving_fixture, controller_env=None,
                 mirror_repository=None, vertices=65536, workload_path=None, run=subprocess.run, sleep=time.sleep):
        self.target, self.state, self.run, self.sleep = target, state, run, sleep
        self.cell = state["cell"]
        self.api = Api(state["endpoint"], admin_key)
        self.approval_key, self.controller_env = approval_key, controller_env
        self.serving_fixture, self.mirror_repository = Path(serving_fixture), mirror_repository
        self.workload = centroid_workload(vertices)
        self.process_id = "geometry.centroid"
        self.workload_path, self.expected_feature = workload_path, None
        self.workload_binding = digest(json.dumps(self.workload, sort_keys=True, separators=(",", ":")).encode())
        self.submitted_job = None
        self.controller_name = None
        self.registered_tasks = []
        self.retained_batch_definitions = []
        self.deploy = None
        self.safety = None
        self.updated = False
        self.probe_cleanup_failures = []

    def aws(self, *argv):
        return json.loads(checked(self.run, ["aws", *argv, "--region", self.target.region, "--output", "json"], "AWS observation or actuator failed", timeout=180))

    def output_value(self, name):
        root = self.target._workdir or self.target._iac_root()
        require(root is not None, "restored Terraform working directory missing")
        try:
            result = self.target._tf(root, "output", "-json", name)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProofError("required Terraform output unavailable") from exc
        return json.loads(result.stdout)

    def prerequisites(self, prior, candidate):
        require(self.state.get("ready") is True and self.state.get("provisionAttempted") is True,
                "restored ready baseline provisioning obligation required")
        require(self.api.credential != self.approval_key and self.approval_key,
                "independent operator approval credential required")
        # Reuse the authored fixture parser/oracle used by the journey, never use the candidate's
        # response to define expected data. Both source script and publication manifest are hashed.
        import cloud_journey
        cloud_journey.drivers()
        import refusal_profile
        self.fixture = refusal_profile.fixture_binding(self.serving_fixture)
        expected = self.state.get("servingFixtureSha256")
        require(expected == hashlib.sha256(self.serving_fixture.read_bytes()).hexdigest(),
                "serving fixture does not match the sealed provision handoff")
        self.prior = prior
        if self.workload_path is not None:
            raw = Path(self.workload_path).read_bytes()
            binding = {"sha256": digest(raw)}
            require(prior.manifest.get("updateRollbackWorkload") == candidate.manifest.get("updateRollbackWorkload") == binding,
                    "reviewed GP workload bytes are not bound by both retained manifests")
            fixture = json.loads(raw)
            require(set(fixture) == {"schema", "processId", "execution", "expectedFeature"}
                    and fixture["schema"] == "honua.update-rollback-workload/v1"
                    and isinstance(fixture["processId"], str) and fixture["processId"].startswith("geometry.")
                    and all(c.islower() or c in ".-" for c in fixture["processId"])
                    and len(raw) <= 16 * 1024 * 1024 and isinstance(fixture["execution"], dict)
                    and set(fixture["execution"]) == {"inputs"} and isinstance(fixture["execution"]["inputs"], dict)
                    and isinstance(fixture["expectedFeature"], dict) and fixture["expectedFeature"].get("type") == "Feature",
                    "reviewed real GP workload contract is malformed")
            self.process_id, self.workload, self.expected_feature = fixture["processId"], fixture["execution"], fixture["expectedFeature"]
            self.workload_binding = digest(raw)
        self.journal = self.schema_journal()
        if self.cell == "aws-ecs/redis-on":
            contract = self.output_value("operations_contract")
            self.safety = contract.get("resilience", {}).get("protection_profile", {}).get("execution")
            # Current maintained contract nests the profile under protection; refuse an older or
            # incomplete pinned root rather than silently substituting ordinary ECS rolling apply.
            if self.safety is None:
                self.safety = contract.get("protection", {}).get("execution")
            require(isinstance(self.safety, dict) and self.safety.get("backend_name") == "honua-aws-ecs-alb"
                    and self.safety.get("controller_topology") == "external-retained-controller",
                    "IaC retained ECS safety contract unavailable")
        self.schema_probe(candidate)
        if self.cell != "aws-ecs/redis-off":
            require(self.controller_env and Path(self.controller_env).is_file(), "protected independent controller environment required")
            self.start_controller(prior, candidate)

    def schema_journal(self, api=None):
        # The supported preflight computes BOTH directions of the live database/embedded-script
        # set difference. Empty pending + extra sets, with a successful plan, proves the database
        # matches this booted immutable build's migration set without opening database ingress.
        view = (api or self.api).get("/api/v1/admin/deploy/preflight?includeDiagnostics=true")
        migration = view.get("migration", {})
        require(view.get("readyForCoordinatedDeploy") is True and migration.get("planAvailable") is True
                and migration.get("upgradeRequired") is False and migration.get("pendingScripts") == []
                and migration.get("executedButNotDiscoveredScripts") == [] and not migration.get("planError"),
                "live database/immutable build migration-set comparison refused or incomplete")
        return {"method": "live-bidirectional-migration-plan", "pendingScripts": 0, "extraScripts": 0,
                "readyForCoordinatedDeploy": True}

    def schema_probe(self, candidate):
        """Boot the authenticated B image against the actual cell DB with migrations disabled.

        Only a /32 database ingress rule is added, never an image/alias/task/deploy mutation. The
        rule and probe are owned here and removed even when the compatibility check fails. The
        probe has no Redis/target catalog and receives no runner AWS credentials.
        Startup secret resolution uses only the explicitly approved controller role.
        """
        require(self.controller_env and Path(self.controller_env).is_file(), "protected startup configuration required for schema preflight")
        protected = Path(self.controller_env)
        require(not protected.is_symlink() and protected.stat().st_mode & 0o077 == 0,
                "schema probe configuration must be private")
        connection = self.target._connection_string()
        parts = {k.strip().lower(): v.strip() for k, v in (p.split("=", 1) for p in connection.split(";") if "=" in p)}
        host, port = parts.get("host"), int(parts.get("port", "5432"))
        state = json.loads(self.target._tf(self.target._workdir, "show", "-json").stdout)
        def resources(module):
            yield from module.get("resources", [])
            for child in module.get("child_modules", []):
                yield from resources(child)
        databases = [r["values"] for r in resources(state["values"]["root_module"])
                     if r.get("type") == "aws_db_instance" and r.get("values", {}).get("address") == host]
        require(len(databases) == 1 and databases[0].get("publicly_accessible") is True
                and databases[0].get("vpc_security_group_ids"), "owned public cell database identity unavailable")
        cidr = self.target._host_cidr(os.environ.get("HONUA_AWS_RUNNER_CIDR", ""), "schema probe runner CIDR")
        rules, probe = [], "honua-schema-probe-" + str(os.getpid())
        self.schema_probe_name = probe
        try:
            for group in databases[0]["vpc_security_group_ids"]:
                # Do not revoke pre-existing admission. AWS rule IDs identify only rules this
                # invocation actually created, and describe-before-create avoids duplicate writes.
                existing = self.aws("ec2", "describe-security-group-rules", "--filters", "Name=group-id,Values=" + group).get("SecurityGroupRules", [])
                if any(r.get("IsEgress") is False and r.get("IpProtocol") == "tcp" and r.get("FromPort") == port
                       and r.get("ToPort") == port and r.get("CidrIpv4") == cidr for r in existing):
                    continue
                added = self.aws("ec2", "authorize-security-group-ingress", "--group-id", group,
                                 "--ip-permissions", json.dumps([{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                 "IpRanges": [{"CidrIp": cidr, "Description": "owned update rollback schema probe"}]}]))
                ids = [r["SecurityGroupRuleId"] for r in added.get("SecurityGroupRules", [])]
                require(bool(ids), "owned database admission identity unavailable")
                rules.extend((group, identity) for identity in ids)
            lines = protected.read_text().splitlines()
            # Disable durable dispatch and do not connect to the operator controller's DB/Redis.
            lines = [line for line in lines if not env_key(line).startswith(("controlplane__", "connectionstrings__", "honua_admin_password",
                     "licensing__mode", "honua_skip_migrations", "aspnetcore_urls", "aws_"))]
            derived = {"ASPNETCORE_URLS": "http://127.0.0.1:18083", "HONUA_ADMIN_PASSWORD": self.api.credential,
                       "ConnectionStrings__DefaultConnection": connection, "HONUA_SKIP_MIGRATIONS": "true", "Licensing__Mode": "Disabled"}
            keys = self.target._state_secrets(("master_key",))
            if len(keys) == 1:
                derived["Security__ConnectionEncryption__MasterKey"] = keys[0]
            derived.update(self.controller_credentials())
            with tempfile.NamedTemporaryFile(mode="w", prefix="honua-schema-", delete=False) as temporary:
                path = Path(temporary.name); path.chmod(0o600)
                temporary.write("\n".join(lines + [key + "=" + str(value) for key, value in derived.items()]) + "\n")
            try:
                checked(self.run, ["docker", "run", "-d", "--name", probe, "--network", "host", "--env-file", str(path),
                                  "--cap-drop", "ALL", "--security-opt", "no-new-privileges", candidate.image],
                        "candidate read-only schema probe could not start", timeout=180)
            finally:
                path.unlink(missing_ok=True)
            api = Api("http://127.0.0.1:18083", self.api.credential, loopback=True)
            for _ in range(90):
                try:
                    view = self.schema_journal(api)
                    require(view == self.journal, "candidate migration set differs from the actual baseline database")
                    self.journal["candidateImageDigest"] = candidate.image.rsplit("@", 1)[1]
                    return
                except ProofError:
                    self.sleep(2)
            raise ProofError("candidate schema compatibility preflight refused before deployment mutation")
        finally:
            failures = []
            try:
                checked(self.run, ["docker", "rm", "-f", probe], "schema probe cleanup failed", timeout=60)
            except ProofError:
                failures.append("candidate-schema-probe")
            for group, identity in rules:
                try:
                    self.aws("ec2", "revoke-security-group-ingress", "--group-id", group, "--security-group-rule-ids", identity)
                except ProofError:
                    failures.append("candidate-schema-probe-admission")
            self.probe_cleanup_failures.extend(failures)
            require(not failures, "candidate schema preflight cleanup incomplete")

    def controller_credentials(self):
        approved_role = os.environ.get("HONUA_UPDATE_ROLLBACK_CONTROLLER_ROLE_ARN", "")
        require(approved_role.startswith("arn:aws:iam::") and ":role/" in approved_role,
                "operator-approved independent controller role ARN required")
        if self.safety:
            require(approved_role == self.safety["controller_role_arn"], "approved controller role differs from maintained IaC safety output")
        credentials = self.aws("sts", "assume-role", "--role-arn", approved_role,
            "--role-session-name", "honua-update-rollback-controller", "--duration-seconds", "3600")["Credentials"]
        return {"AWS_ACCESS_KEY_ID": credentials["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
                "AWS_SESSION_TOKEN": credentials["SessionToken"], "AWS_REGION": self.target.region}

    def start_controller(self, prior, candidate):
        """Retain a separate server process/durable store for the entire switch and rollback.

        The protected env file supplies normal Production startup/audit/storage/Redis and telemetry
        configuration. Target identity/backend parameters come only from this restored IaC root.
        An explicitly approved role ARN must match the maintained safety output; only that
        controller assumes it. The runner keeps the provisioning identity for exact Terraform staging.
        """
        protected = Path(self.controller_env)
        require(not protected.is_symlink() and protected.stat().st_mode & 0o077 == 0, "controller env must be a private regular file")
        env = protected.read_text()
        require(self.api.credential and not self.api.credential.startswith("Bearer "), "controller bootstrap requires the owned API key")
        entries = {env_key(line): line.split("=", 1)[1] for line in env.splitlines() if "=" in line and not line.startswith("#")}
        require(entries.get("connectionstrings__redis") and entries.get("connectionstrings__defaultconnection"),
                "retained controller requires its protected persistent database and Redis")
        controller_redis = entries["connectionstrings__redis"]
        def redis_store(value):
            parts = value.split(",")
            options = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
            database = next((v for k, v in options.items() if k.lower() == "defaultdatabase"), "0")
            return parts[0].strip().lower(), database.strip()
        for cell_redis in self.target._state_secrets(("redis_connection",)):
            require(redis_store(controller_redis) != redis_store(cell_redis),
                    "controller must use an independent durable Redis store, never the candidate execution queue")
        require(not any(env_key(line).startswith(("controlplane__deploytargets__", "controlplane__platformrelease__", "aspnetcore_urls"))
                        for line in env.splitlines()), "protected controller env must not override derived target/release wiring")
        target_id = self.safety["target_id"] if self.safety else self.output_value("control_plane_target_id")
        target_name = (self.safety["parameters"]["aws.ecs.canary_service"] if self.safety
                       else self.output_value("control_plane_target_name"))
        backend = "honua-aws-ecs-alb" if self.safety else "honua-gitops-aws-lambda"
        parameters = dict(self.safety["parameters"]) if self.safety else {
            "aws.lambda.function_name": self.output_value("lambda_function_name"), "aws.lambda.alias_name": "live",
            "aws.region": self.target.region}
        derived = {"ASPNETCORE_URLS": "http://127.0.0.1:18082", "HONUA_ADMIN_PASSWORD": self.api.credential,
            "Licensing__Mode": "Disabled", "HONUA_SKIP_MIGRATIONS": "true", "ControlPlane__DeployTargets__0__TargetId": target_id,
            "ControlPlane__DeployTargets__0__TargetKind": "AwsEcs" if self.safety else "AwsLambda",
            "ControlPlane__DeployTargets__0__Backend": backend, "ControlPlane__DeployTargets__0__TargetName": target_name,
            "ControlPlane__DeployTargets__0__Environment": "cert", "ControlPlane__DeployTargets__0__RequiresApproval": "true", "ControlPlane__PlatformRelease__Version":
                candidate.version,
            "ControlPlane__PlatformRelease__ServingArtifactReference": candidate.image_for(self.cell),
            "ControlPlane__PlatformRelease__Workers__0__RuntimeProfile": "managed",
            "ControlPlane__PlatformRelease__Workers__0__ArtifactReference": candidate.image}
        for key, value in parameters.items():
            derived["ControlPlane__DeployTargets__0__Parameters__" + key] = str(value)
        derived.update(self.controller_credentials())
        self.controller_name = "honua-rollback-" + hashlib.sha256((self.cell + str(os.getpid())).encode()).hexdigest()[:16]
        with tempfile.NamedTemporaryFile(mode="w", prefix="honua-controller-", delete=False) as temporary:
            path = Path(temporary.name)
            path.chmod(0o600)
            temporary.write(env.rstrip() + "\n" + "\n".join(key + "=" + str(value) for key, value in derived.items()) + "\n")
        try:
            checked(self.run, ["docker", "run", "-d", "--name", self.controller_name, "--network", "host",
                    "--env-file", str(path), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", prior.image],
                    "retained controller startup failed", timeout=180)
        finally:
            path.unlink(missing_ok=True)
        controller = Api("http://127.0.0.1:18082", self.api.credential, loopback=True)
        for _ in range(90):
            try:
                preflight = controller.get("/api/v1/admin/deploy/preflight")
                if preflight.get("readyForCoordinatedDeploy") is True:
                    break
            except ProofError:
                pass
            self.sleep(2)
        else:
            raise ProofError("independent controller did not become ready")
        approval = Api("http://127.0.0.1:18082", self.approval_key, loopback=True)
        self.deploy = DeployApi(controller, target_id, backend, approval_api=approval, sleep=self.sleep)

    def incompatible_target(self):
        # Planning is read-only. A nonexistent target must fail without creating a deploy operation.
        api = self.deploy.api if self.deploy else self.api
        code, _ = api.request("POST", "/api/v1/admin/deploy/plan", {
            "targetId": "qualification-nonexistent-target", "desiredRevision": "invalid-schema-target"})
        require(code in (400, 404, 409), "incompatible deployment target was not refused by preflight")
        return {"status": "pass", "outcome": "refused-before-mutation", "httpStatus": code}

    def stage(self, candidate):
        self.candidate = candidate
        if self.cell == "aws-ecs/redis-off":
            return {}
        if self.safety:
            cluster, service = self.safety["parameters"]["aws.ecs.cluster"], self.safety["parameters"]["aws.ecs.canary_service"]
            services = self.aws("ecs", "describe-services", "--cluster", cluster, "--services", service)
            require(not services.get("failures") and len(services.get("services", [])) == 1, "native ECS target missing")
            previous = services["services"][0]["taskDefinition"]
            description = self.aws("ecs", "describe-task-definition", "--task-definition", previous)
            definition = description["taskDefinition"]
            # Preserve the entire existing execution contract. Only the server image changes;
            # register-task-definition output-only fields are removed by the AWS input shape.
            fields = {"family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes",
                      "placementConstraints", "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode",
                      "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage", "runtimePlatform", "enableFaultInjection"}
            definition = {key: value for key, value in definition.items() if key in fields}
            containers = [c for c in definition["containerDefinitions"] if c.get("essential") is True]
            require(len(containers) == 1, "ambiguous ECS server container")
            containers[0]["image"] = candidate.image
            environment = [entry for entry in containers[0].get("environment", [])
                           if not entry["name"].lower().startswith("controlplane__platformrelease__")]
            environment.extend({"name": key, "value": value} for key, value in {
                "ControlPlane__PlatformRelease__Version": candidate.version,
                "ControlPlane__PlatformRelease__ServingArtifactReference": candidate.image,
                "ControlPlane__PlatformRelease__Workers__0__RuntimeProfile": "managed",
                "ControlPlane__PlatformRelease__Workers__0__ArtifactReference": candidate.image}.items())
            containers[0]["environment"] = environment
            result = self.aws("ecs", "register-task-definition", "--cli-input-json", json.dumps(definition))
            desired = result["taskDefinition"]["taskDefinitionArn"]
            self.registered_tasks.append(desired)
        else:
            previous = self.output_value("control_plane_current_revision")
            require(str(previous).isdigit() and int(previous) > 0, "published baseline Lambda version missing")
            require(self.mirror_repository, "approved same-region ECR mirror repository required")
            image = self.mirror_repository + "@" + candidate.lambda_digest
            if self.cell.endswith("redis-on"):
                require(self.output_value("gp_batch_prior_definitions_retained") is True,
                        "maintained IaC must retain prior Batch revisions before worker image staging")
                definitions = self.output_value("gp_job_definition_arns")
                require(isinstance(definitions, dict) and set(definitions) == {"s", "m", "l", "xl"}
                        and all(isinstance(value, str) and value.startswith("arn:aws:batch:") and ":job-definition/" in value
                                for value in definitions.values()), "owned retained Batch revision identities missing")
                self.retained_batch_definitions = list(definitions.values())
            self.target.stage_lambda(image, str(previous), redis_enabled=self.cell.endswith("redis-on"), release=candidate)
            desired = self.output_value("control_plane_desired_revision")
        require(desired != previous, "staged revision did not change")
        return {"previous": previous, "desired": desired}

    def plan(self, revisions):
        if self.deploy:
            self.deploy.plan(revisions["previous"], revisions["desired"])

    def update(self, revisions):
        self.updated = True
        if self.deploy:
            return self.deploy.submit(revisions["previous"], revisions["desired"], self.controller_name + "-update")
        return self.target.update(self.candidate.image, redis_enabled=False, release=self.candidate, capture_evidence=True)

    def rollback(self, revisions):
        if self.deploy:
            return self.deploy.rollback(revisions["previous"])
        return self.target.rollback(capture_evidence=True)

    def observe(self, release, phase):
        if self.cell.startswith("aws-serverless/"):
            function = self.output_value("lambda_function_name")
            alias = self.aws("lambda", "get-alias", "--function-name", function, "--name", "live")
            require(not alias.get("RoutingConfig", {}).get("AdditionalVersionWeights"), "Lambda alias still splits candidate traffic")
            result = self.aws("lambda", "get-function", "--function-name", function, "--qualifier", alias["FunctionVersion"])
            require(result.get("Code", {}).get("ResolvedImageUri", "").endswith("@" + release.lambda_digest),
                    "actual alias version does not run selected Lambda image")
            native = {"kind": "lambda-alias", "version": alias["FunctionVersion"], "imageDigest": release.lambda_digest}
        else:
            cluster, service = self.output_value("ecs_cluster_name"), self.output_value("ecs_service_name")
            if self.safety:
                p = self.safety["parameters"]
                rules = self.aws("elbv2", "describe-rules", "--rule-arns", p["aws.alb.listener_rule_arn"])["Rules"]
                groups = [g for rule in rules for a in rule.get("Actions", []) for g in a.get("ForwardConfig", {}).get("TargetGroups", [])
                          if g.get("Weight", 0) > 0]
                expected = p["aws.alb.canary_target_group_arn"] if phase == "candidate" else p["aws.alb.stable_target_group_arn"]
                require(len(groups) == 1 and groups[0]["TargetGroupArn"] == expected, "actual ECS routing is not entirely on the expected revision")
                if phase == "candidate":
                    service = p["aws.ecs.canary_service"]
            arns = self.aws("ecs", "list-tasks", "--cluster", cluster, "--service-name", service, "--desired-status", "RUNNING")["taskArns"]
            require(bool(arns), "no actually running ECS tasks")
            result = self.aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", *arns)
            require(not result.get("failures") and len(result.get("tasks", [])) == len(arns), "incomplete native ECS task observation")
            server = release.manifest["components"]["honua-server"]
            architecture = {"x86_64": "amd64", "arm64": "arm64"}.get(server.get("awsEcsArchitecture"))
            require(architecture is not None, "declared ECS architecture missing")
            accepted = {server["digest"], server.get("platformDigests", {}).get(architecture)} - {None}
            for task in result["tasks"]:
                containers = [c for c in task.get("containers", []) if c.get("image", "").split("@", 1)[0] == release.image.split("@", 1)[0]]
                require(task.get("lastStatus") == "RUNNING" and len(containers) == 1
                        and containers[0].get("lastStatus") == "RUNNING" and containers[0].get("image") == release.image
                        and containers[0].get("imageDigest") in accepted,
                        "actual serving ECS image differs from selected build")
            native = {"kind": "ecs-native-routing", "taskCount": len(arns), "imageDigest": server["digest"]}
        metadata = self.api.get("/api/v1/capabilities/manifest")
        identity = metadata.get("server", {})
        source = identity.get("deploymentRevision")
        require(identity.get("deploymentRevisionSource") in ("commit-sha", "assembly-metadata")
                and source == release.source, "served application source revision mismatch")
        capabilities = self.api.get("/api/v1/admin/capabilities")
        capabilities = capabilities.get("data", capabilities)
        require(capabilities.get("contractVersions") == release.manifest["components"]["honua-server"].get("contractVersions"),
                "booted contract versions differ from the retained input manifest")
        require(str(capabilities.get("schemaVersions", {}).get("database")) == release.schema, "booted schema declaration differs from the retained build")
        query = urllib.parse.urlencode({"f": "geojson", "where": "1=1", "outFields": "*", "outSR": 4326, "returnGeometry": "true"})
        path = f"/rest/services/e2e/FeatureServer/{self.fixture['layerId']}/query?{query}"
        features = self.api.get(path).get("features")
        require(isinstance(features, list) and len(features) == 2, "authored serving fixture missing")
        prove_serving_features({"features": features}, self.fixture)
        require(self.schema_journal() == {k: v for k, v in self.journal.items() if k != "candidateImageDigest"}, "schema changed unexpectedly across image switch")
        return {"phase": phase, "observedAt": time.time(), "native": native, "sourceRevision": source,
                "schemaJournal": self.journal, "servingFixture": {k: v for k, v in self.fixture.items() if k != "expectedFeatures"}}

    def submit_job(self):
        status, result = self.api.request("POST", "/ogc/processes/processes/" + self.process_id + "/execution", self.workload)
        require(status == 201 and result.get("status") in ("accepted", "running"), "real durable centroid submission refused")
        self.submitted_job = result.get("jobID")
        return result

    def job(self, identity):
        return self.api.get("/api/v1/admin/jobs/" + urllib.parse.quote(identity, safe=""))

    def refusal(self):
        code, result = self.api.request("POST", "/ogc/processes/processes/geometry.centroid/execution", centroid_workload(4))
        forbidden = {"jobId", "jobID", "operationId", "operationInstanceId", "resourceUri", "jobCreatedAt", "jobStatus"}
        def execution_evidence(value):
            if isinstance(value, dict):
                return any(key in forbidden and item or execution_evidence(item) for key, item in value.items())
            if isinstance(value, list):
                return any(execution_evidence(item) for item in value)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    return False
                return isinstance(parsed, (dict, list)) and execution_evidence(parsed)
            return False
        require(code == 503 and result.get("code") == "dependency-unavailable"
                and result.get("capability") == "jobs.runner" and result.get("missingDependency") == "redis"
                and not execution_evidence(result), "Redis-off request did not return a typed nonexecuted Redis refusal")
        manifest = self.api.get("/api/v1/capabilities/manifest")
        rows = manifest.get("capabilities", [])
        runner = [row for row in rows if row.get("id") == "jobs.runner"]
        require(len(runner) == 1 and runner[0].get("reasonCode") == "dependency-unavailable"
                and runner[0].get("available") is False, "independent jobs.runner capability does not corroborate refusal")
        return {"status": "pass", "outcome": "refused", "functionalGpExecuted": False,
                "code": "dependency-unavailable", "capability": "jobs.runner", "missingDependency": "redis"}

    def output(self, identity):
        document = self.api.get("/ogc/processes/jobs/" + urllib.parse.quote(identity, safe="") + "/results")
        outputs = document.get("outputs", document)
        require(isinstance(outputs, dict) and len(outputs) == 1, "GP result is missing or duplicated")
        artifact = next(iter(outputs.values()))
        feature = artifact.get("value")
        if feature is None:
            href = artifact.get("href", "")
            require(href.startswith("data:application/geo+json;base64,"), "centroid output is not the canonical bounded artifact")
            try:
                feature = json.loads(base64.b64decode(href.split(",", 1)[1], validate=True))
            except (ValueError, TypeError) as exc:
                raise ProofError("centroid artifact is invalid") from exc
        if self.expected_feature is not None:
            require(feature == self.expected_feature, "real GP output differs from the independently authored reviewed feature")
            oracle = "retained-reviewed-feature"
        else:
            geometry, properties = feature.get("geometry", {}), feature.get("properties", {})
            xy = geometry.get("coordinates")
            require(feature.get("type") == "Feature" and geometry.get("type") == "Point"
                    and isinstance(xy, list) and len(xy) == 2 and all(type(n) in (int, float) and math.isfinite(n) and abs(n) <= 1e-8 for n in xy)
                    and properties.get("processId") == "geometry.centroid" and properties.get("inputSrid") == 4326,
                    "centroid output differs from independently authored origin oracle")
            oracle = "symmetric-polygon-centroid-origin"
        return {"sha256": digest(json.dumps(feature, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()),
                "fixtureSha256": self.workload_binding,
                "oracle": oracle, "featureCount": 1}

    def execution_events(self, identity, started):
        # CloudWatch publication is asynchronous. Wait for independent events, then re-read
        # after a bounded settle interval; missing or duplicate events never become success.
        for _ in range(30):
            events = self._execution_events(identity, started)
            if events["started"] > 1 or events["completed"] > 1:
                return events
            if events["started"] == events["completed"] == 1:
                self.sleep(5)
                repeated = self._execution_events(identity, started)
                if repeated == events:
                    return events
                if repeated["started"] > 1 or repeated["completed"] > 1:
                    return repeated
            self.sleep(2)
        raise ProofError("independent execution events did not settle within the bounded publication window")

    def _execution_events(self, identity, started):
        # Query all streams in every declared worker log group, with AWS CLI automatic pagination.
        # Restrict to this exact job id, deduplicate provider event ids, never count status reads.
        job = self.job(identity)
        groups = set()
        provider = job.get("providerOperationId")
        if self.cell.startswith("aws-serverless/"):
            require(provider, "Batch execution provider identity missing")
            jobs = self.aws("batch", "describe-jobs", "--jobs", provider).get("jobs", [])
            require(len(jobs) == 1 and jobs[0].get("jobId") == provider and jobs[0].get("status") == "SUCCEEDED"
                    and len(jobs[0].get("attempts", [])) == 1, "Batch provider does not prove one successful attempt")
            actual = jobs[0]
            all_jobs = self.aws("batch", "list-jobs", "--job-queue", actual["jobQueue"], "--filters",
                json.dumps([{"name": "JOB_NAME", "values": [actual["jobName"]]}])).get("jobSummaryList", [])
            matching = [j["jobId"] for j in all_jobs if j.get("jobName") == actual["jobName"]]
            require(matching == [provider], "Batch job-name discovery found duplicate or missing executions")
            definitions = self.aws("batch", "describe-job-definitions", "--job-definitions", actual["jobDefinition"])["jobDefinitions"]
            require(len(definitions) == 1, "Batch execution definition is ambiguous")
            options = definitions[0].get("containerProperties", {}).get("logConfiguration", {}).get("options", {})
            groups.add(options.get("awslogs-group", "/aws/batch/job"))
        else:
            cluster = self.output_value("ecs_cluster_name")
            services = [self.output_value("ecs_service_name")]
            if self.safety:
                services.append(self.safety["parameters"]["aws.ecs.canary_service"])
            description = self.aws("ecs", "describe-services", "--cluster", cluster, "--services", *services)
            require(not description.get("failures") and len(description.get("services", [])) == len(services),
                    "complete ECS worker service set could not be observed")
            for service in description["services"]:
                # Include every active deployment, retaining the old worker stream through drain.
                revisions = {d["taskDefinition"] for d in service.get("deployments", [])} | {service["taskDefinition"]}
                for revision in revisions:
                    definition = self.aws("ecs", "describe-task-definition", "--task-definition", revision)["taskDefinition"]
                    for container in definition.get("containerDefinitions", []):
                        log = container.get("logConfiguration", {})
                        if container.get("essential") is True:
                            require(log.get("logDriver") == "awslogs" and log.get("options", {}).get("awslogs-group"),
                                    "every possible ECS job worker must have observable CloudWatch logs")
                            groups.add(log["options"]["awslogs-group"])
        require(bool(groups), "native worker log group coverage missing")
        from datetime import datetime
        start_ms = int(datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() * 1000)
        started_events, completed_events = set(), set()
        for group in groups:
            result = self.aws("logs", "filter-log-events", "--log-group-name", group,
                              "--start-time", str(start_ms), "--filter-pattern", '"' + identity + '"')
            for event in result.get("events", []):
                message = event.get("message", "")
                if "Job execution started: " + identity + "," in message:
                    started_events.add(event["eventId"])
                if "Job execution completed: " + identity + ", Status=Succeeded" in message:
                    completed_events.add(event["eventId"])
        return {"started": len(started_events), "completed": len(completed_events),
                "eventIdsSha256": digest(json.dumps(sorted(started_events | completed_events)).encode())}

    def close(self):
        failures = list(self.probe_cleanup_failures)
        if self.submitted_job:
            try:
                row = self.job(self.submitted_job)
                if row.get("status") not in ("Succeeded", "Failed", "Cancelled"):
                    code, _ = self.api.request("DELETE", "/ogc/processes/jobs/" + urllib.parse.quote(self.submitted_job, safe=""))
                    require(code in (200, 202), "owned GP cleanup cancellation refused")
                    for _ in range(60):
                        if self.job(self.submitted_job).get("status") in ("Succeeded", "Failed", "Cancelled"):
                            break
                        self.sleep(2)
                    else:
                        raise ProofError("owned GP cancellation not confirmed")
            except Exception:
                failures.append("owned-inflight-job")
        if self.controller_name:
            try:
                checked(self.run, ["docker", "rm", "-f", self.controller_name], "retained controller cleanup failed", timeout=60)
            except Exception:
                failures.append("retained-controller")
        # Only the exact baseline revisions created by this run are retained out of Terraform's
        # current resource state. All owned jobs are terminal before these are deregistered.
        if "owned-inflight-job" not in failures:
            for definition in self.retained_batch_definitions:
                try:
                    self.aws("batch", "deregister-job-definition", "--job-definition", definition)
                except Exception:
                    failures.append("retained-batch-definition")
        elif self.retained_batch_definitions:
            failures.append("retained-batch-definition-awaiting-job")
        for task in self.registered_tasks:
            try:
                self.aws("ecs", "deregister-task-definition", "--task-definition", task)
            except Exception:
                failures.append("staged-task-definition")
        return failures


def serving_path(fixture):
    query = urllib.parse.urlencode({"f": "geojson", "where": "1=1", "outFields": "*", "outSR": 4326, "returnGeometry": "true"})
    return f"/rest/services/e2e/FeatureServer/{fixture['layerId']}/query?{query}"


def prove_serving_features(document, fixture):
    features = document.get("features")
    require(isinstance(features, list) and len(features) == 2, "authored serving fixture missing")
    actual = sorted(({"id": str(f.get("id")), "name": f.get("properties", {}).get("name"), "geometry": f.get("geometry")}
                     for f in features), key=lambda f: f["id"])
    require(actual == fixture["expectedFeatures"], "served data differs from independent authored fixture")


def configure_ecs_safety(target, endpoint, serving_fixture):
    """Wire the maintained root after seeding, before handing a baseline to any candidate client.

    Role/telemetry identities and bounded timing policy are operator approved. The functional URL
    targets this run's authored data; its expected body is captured from A only AFTER verifying
    every feature against the independent source SQL oracle. It never trusts B to define success.
    """
    configured = os.environ.get("HONUA_UPDATE_ROLLBACK_ECS_SAFETY", "")
    require(bool(configured), "operator-approved retained ECS safety/telemetry inputs required")
    settings = json.loads(configured)
    allowed = {"controller_role_name", "telemetry_connection_id", "prometheus_canary_job", "observation_window_seconds",
               "recovery_timeout_seconds", "warmup_seconds", "evidence_grace_seconds", "max_staleness_seconds", "exposure_deadline_seconds"}
    require(isinstance(settings, dict) and set(settings) <= allowed
            and {"controller_role_name", "telemetry_connection_id", "prometheus_canary_job"} <= set(settings),
            "operator ECS safety fields are incomplete or override derived functional proof")
    import cloud_journey
    cloud_journey.drivers()
    import refusal_profile
    fixture = refusal_profile.fixture_binding(serving_fixture)
    anonymous = Api(endpoint, "", anonymous=True)
    document = anonymous.get(serving_path(fixture))
    prove_serving_features(document, fixture)
    settings.update(functional_probe_url=endpoint.rstrip("/") + serving_path(fixture),
                    functional_expected_sha256=hashlib.sha256(anonymous.last_body).hexdigest())
    require(target._root_declares("deployment_safety"), "pinned ECS root lacks retained controller safety wiring")
    target._exact_image_apply(os.environ[target.spec.image_env], redis_enabled=True,
                             extra_vars=("-var=deployment_safety=" + json.dumps(settings, separators=(",", ":")),))
