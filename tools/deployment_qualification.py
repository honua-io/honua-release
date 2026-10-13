"""Execution-input identities for deployment proof; never a replacement for a live producer.

The v1 identity is deliberately a denylist projection: new manifest/configuration fields
are bound by default. Only enumerated release-train output coordinates are removed.
Legacy candidateManifestDigest receipts continue to use their original whole-manifest
identity in validate_platform.py and cannot be reinterpreted as this version.
"""
from __future__ import annotations

import copy
import argparse
import hashlib
import json
import io
import math
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from dataclasses import dataclass

import yaml

INPUT_SCHEMA = "honua.deployment-execution-inputs/v1"
RECEIPT_SCHEMA = "honua.deployment-qualification/v1"
BUNDLE_SCHEMA = "honua.deployment-qualification-bundle/v1"
ENVELOPE_PATH = "docs/2026.1-operating-envelope.md"
MANIFEST_OUTPUT_PATHS = (
    ("platformRelease",),
    ("status",),
    ("snapshotDate",),
    ("protocolCertification", "candidateCutAt"),
    ("protocolCertification", "ledger", "status"),
    ("protocolCertification", "ledger", "commit"),
    ("protocolCertification", "ledger", "requirementsSourceRevision"),
    ("protocolCertification", "ledger", "sha256"),
    ("components", "honua-server", "artifactVersion"),
    ("components", "honua-server", "releaseVersion"),
    ("components", "honua-console", "artifactVersion"),
    ("components", "honua-helm", "artifactVersion"),
)
TARGETS = {
    "awsLambda": ("honua-server", "x86_64"),
    "awsEks": ("honua-helm", "x86_64"),
}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class QualificationError(ValueError):
    """Deployment proof is absent, malformed, or does not describe these inputs."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def execution_manifest(manifest: dict) -> dict:
    """Preserve every input, including unknown fields, without mutating the caller."""
    if not isinstance(manifest, dict):
        raise QualificationError("deployment manifest must be an object")
    result = copy.deepcopy(manifest)
    for path in MANIFEST_OUTPUT_PATHS:
        parent = result
        for key in path[:-1]:
            if not isinstance(parent, dict):
                break
            parent = parent.get(key)
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    return result


def execution_inputs(manifest: dict, deployment: dict) -> dict:
    """Bind the actual producer's full deployment configuration, not just image pins.

    Deployment configuration must be captured before execution by the producer. It
    includes its complete effective inputs (secret values may be represented by
    immutable secret-version identifiers); this function never infers configuration
    from a historical receipt. Unknown fields remain in the digest.
    """
    if not isinstance(deployment, dict):
        raise QualificationError("deployment execution configuration must be an object")
    target = deployment.get("target")
    if target not in TARGETS:
        raise QualificationError("unsupported deployment target")
    component, architecture = TARGETS[target]
    if deployment.get("component") != component or deployment.get("architecture") != architecture:
        raise QualificationError("deployment component/architecture does not match its target")
    if not isinstance(deployment.get("configuration"), dict) or not deployment["configuration"]:
        raise QualificationError("complete nonempty producer configuration is required")
    producer = deployment.get("producer")
    if not isinstance(producer, dict) or not producer:
        raise QualificationError("immutable deployment producer identity is required")
    require(producer.get("repository") in {"honua-io/honua-server", "honua-io/honua-release"}
            and SHA.fullmatch(str(producer.get("sourceRevision", ""))) is not None
            and re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml",
                             str(producer.get("workflow", ""))) is not None,
            "immutable deployment producer identity is invalid")
    trusted = {"awsLambda": ("honua-io/honua-server", ".github/workflows/lambda-preview-certification.yml"),
               "awsEks": ("honua-io/honua-release", ".github/workflows/e2e-cloud-aws.yml")}
    require((producer["repository"], producer["workflow"]) == trusted[target],
            "deployment producer is not the canonical target workflow")
    result = {"schema": INPUT_SCHEMA, "manifest": execution_manifest(manifest),
              "deployment": copy.deepcopy(deployment)}
    canonical_bytes(result)  # Refuse NaN and values that cannot be represented faithfully.
    return result


def input_binding(manifest: dict, deployment: dict) -> dict:
    inputs = execution_inputs(manifest, deployment)
    return {"schema": INPUT_SCHEMA, "digest": digest(inputs)}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationError(message)


def require_pass(value: Any, name: str) -> dict:
    require(isinstance(value, dict) and value.get("result") == "pass", f"{name} must pass")
    return value


def exact_integer(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def lambda_precut(raw: dict, manifest: dict, deployment: dict) -> None:
    """Verify actual Lambda bill items 1/2; item 5 is checked against the bound policy.

    This does not accept alias smoke as the two certified-lock upgrade/rollback bill.
    Full GA qualification is intentionally a different, still strict admission.
    """
    require_pass(raw, "Lambda producer receipt")
    require(raw.get("schema") == "honua.lambda-preview-certification/v1",
            "unsupported Lambda raw receipt schema")
    server = manifest["components"]["honua-server"]
    require(SHA.fullmatch(str(server.get("sha", ""))) is not None
            and all(DIGEST.fullmatch(str(server.get(key, ""))) is not None
                    for key in ("awsLambdaDigest", "awsLambdaEcrDigest")),
            "Lambda immutable source and image pins are required")
    require(raw.get("serverRevision") == server.get("sha"), "Lambda source revision mismatch")
    artifact = raw.get("artifact", {})
    require(all(DIGEST.fullmatch(str(artifact.get(key, ""))) is not None
                for key in ("sourcePlatformDigest", "sourceConfigDigest", "sourceRootfsFingerprint")),
            "Lambda source/config/rootfs identity is missing")
    require(artifact.get("sourcePlatformDigest") == server.get("awsLambdaDigest"),
            "Lambda source platform image mismatch")
    require(artifact.get("sourceDigest") == server.get("awsLambdaDigest"),
            "Lambda source image mismatch")
    require(artifact.get("ecrDigest") == server.get("awsLambdaEcrDigest"),
            "Lambda ECR image mismatch")
    require(raw.get("deployment", {}).get("architecture") == server.get("awsLambdaArchitecture") == "x86_64",
            "Lambda architecture mismatch")
    for key in ("configDigestPreserved", "rootfsPreserved", "runtimeAdapterVerified"):
        require(artifact.get(key) is True, f"Lambda {key} was not verified")
    serving = require_pass(raw.get("serving"), "Lambda serving")
    require(serving.get("candidateDigest") == server.get("awsLambdaDigest"),
            "Lambda serving candidate mismatch")
    deployed = require_pass(serving.get("deployed"), "Lambda deployed serving")
    migrations = deployed.get("migrations", {})
    require(migrations.get("status") == "succeeded" and exact_integer(migrations.get("pendingScripts"), 0)
            and migrations.get("upgradeRequired") is False, "Lambda migrations did not complete")
    fixture = deployed.get("fixture", {})
    fixture_digest = deployment["configuration"].get("fixtureSha256")
    require(DIGEST.fullmatch(str(fixture_digest)) is not None
            and fixture.get("sha256") == fixture_digest, "Lambda fixture bytes differ from execution inputs")
    require(fixture.get("name") == "client-compat-v1" and exact_integer(fixture.get("expectedRows"), 10)
            and exact_integer(fixture.get("actualRows"), 10) and fixture.get("namesVerified") is True,
            "Lambda exact serving fixture failed")
    write = deployed.get("write", {})
    require(all(exact_integer(write.get(key), expected) for key, expected in
                (("createdRows", 1), ("readBackRows", 1), ("deletedRows", 1), ("remainingRows", 0)))
            and write.get("distinctWriteUrl") is True, "Lambda write/read/delete failed")
    auth = deployed.get("authorization", {})
    require(exact_integer(auth.get("anonymousStatus"), 401) and exact_integer(auth.get("actualStatus"), 403)
            and exact_integer(auth.get("expectedStatus"), 403) and exact_integer(auth.get("records"), 0),
            "Lambda authorization controls failed")
    verification = raw.get("verification", {})
    cold = verification.get("coldStartInitDurationMs")
    require(isinstance(cold, (int, float)) and not isinstance(cold, bool)
            and math.isfinite(cold) and cold > 0
            and verification.get("coldStartEnvironmentForced") is True,
            "Lambda measured cold start is missing")
    require(verification.get("httpStatus") == 200
            and verification.get("responseVerified") is True
            and verification.get("cloudWatchLogsVerified") is True, "Lambda invocation verification failed")
    require(raw.get("teardown", {}).get("functionDeleted") is True
            and raw.get("teardown", {}).get("logGroupDeleted") is True,
            "Lambda owned teardown is incomplete")


def instant(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise QualificationError("invalid timestamp") from error
    require(result.tzinfo is not None, "timestamp requires timezone")
    return result


def verify_eks_stages(configuration: dict, actuation: dict) -> None:
    """Each complete configuration is captured before its own actuation boundary."""
    stages = configuration.get("stages")
    require(isinstance(stages, dict) and set(stages) == {"terraform", "helm"},
            "both before-actuation EKS stage captures are required")
    require(isinstance(actuation, dict) and set(actuation) == {"terraformStartedAt", "helmStartedAt"},
            "actual EKS actuation timestamps are required")
    for name, stage in stages.items():
        require(isinstance(stage, dict) and set(stage) ==
                {"schema", "stage", "capturedAt", "configuration", "previousStageDigest"}
                and stage.get("schema") == "honua.deployment-stage-inputs/v1"
                and stage.get("stage") == name
                and isinstance(stage.get("configuration"), dict) and bool(stage["configuration"]),
                "invalid complete EKS stage capture")
    terraform, helm = stages["terraform"], stages["helm"]
    require(terraform["previousStageDigest"] is None
            and helm["previousStageDigest"] == digest(terraform), "EKS stage hash chain mismatch")
    require(instant(terraform["capturedAt"]) <= instant(actuation["terraformStartedAt"])
            <= instant(helm["capturedAt"]) <= instant(actuation["helmStartedAt"]),
            "EKS configuration was not captured before its own actuation")


def verify_eks_configuration(configuration: dict, manifest: dict) -> None:
    """Verify complete known consumption boundaries; all additional fields stay digest-bound."""
    terraform = configuration["stages"]["terraform"]["configuration"]
    helm = configuration["stages"]["helm"]["configuration"]
    require({"arguments", "iacSource", "secretVersionObservations", "tenantFixture"} <= set(terraform),
            "complete Terraform configuration is required")
    require({"arguments", "values", "helmSource", "runtimeSecretValueCommitments", "runtimeSecretCommitmentAlgorithm",
             "immutableSecretVersionIds", "tenantSigningSecret"} <= set(helm),
            "complete Helm configuration is required")
    require(terraform["iacSource"] == manifest["components"]["honua-iac"]["sha"]
            and helm["helmSource"] == manifest["components"]["honua-helm"]["sha"],
            "executed Terraform/Helm sources differ from manifest")
    for arguments in (terraform["arguments"], helm["arguments"]):
        require(isinstance(arguments, list) and arguments and all(isinstance(arg, str) for arg in arguments),
                "actual deployment command arguments are required")
    require(isinstance(helm["values"], dict) and bool(helm["values"]), "actual Helm values are required")
    require(helm["runtimeSecretCommitmentAlgorithm"] == "hmac-sha256",
            "runtime Secret commitments must use the keyed producer algorithm")
    fingerprints = helm["runtimeSecretValueCommitments"]
    require(isinstance(fingerprints, dict) and fingerprints
            and all(isinstance(name, str) and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value))
                    for name, value in fingerprints.items()), "actual keyed runtime Secret value commitments are required")
    def identities(values):
        require(isinstance(values, list), "secret version identities must be an array")
        result = set()
        for value in values:
            require(isinstance(value, dict) and set(value) == {"arn", "versionId"}
                    and re.fullmatch(r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+",
                                     str(value.get("arn", ""))) is not None
                    and re.fullmatch(r"[A-Za-z0-9-]{32,64}", str(value.get("versionId", ""))) is not None,
                    "immutable secret version identity is invalid")
            pair = (value["arn"], value["versionId"])
            require(pair not in result, "duplicate secret version identity")
            result.add(pair)
        return result
    identities(terraform["secretVersionObservations"])  # Observations are not consumption claims.
    consumed = identities(helm["immutableSecretVersionIds"])
    def inspect(value):
        if isinstance(value, dict):
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
        elif isinstance(value, str) and value.lower().startswith("aws:secretsmanager:"):
            reference = value[len("aws:secretsmanager:"):]
            arn, separator, version = reference.partition("?versionId=")
            require(separator and (arn, version) in consumed,
                    "consumed server secret reference is not pinned to its observed immutable version")
    inspect(helm["values"])
    tenant = terraform["tenantFixture"]
    require(isinstance(tenant, dict) and set(tenant) == {"enabled", "signingSecret"}
            and type(tenant["enabled"]) is bool, "tenant fixture declaration is invalid")
    if tenant["enabled"]:
        identities([tenant["signingSecret"]])
        require(helm["tenantSigningSecret"] == tenant["signingSecret"]
                and "Oidc__TokenValidation__SymmetricSigningKey" in fingerprints,
                "actual tenant signing material is not bound across deployment stages")
    else:
        require(tenant["signingSecret"] is None and helm["tenantSigningSecret"] is None,
                "disabled tenant fixture carries unexpected signing material")


def eks_cell(raw: dict, inputs: dict) -> None:
    """Full real EKS cell, including its original pinned journey and owned teardown."""
    require_pass(raw, "EKS producer receipt")
    require(raw.get("schema") == "honua.eks-deployment-cell/v1", "unsupported EKS raw receipt schema")
    deployment = inputs["deployment"]
    redis = deployment["configuration"].get("redisMode")
    require(redis in {"off", "on"}, "EKS execution configuration must declare redisMode")
    verify_eks_stages(deployment["configuration"], raw.get("actuation"))
    verify_eks_configuration(deployment["configuration"], inputs["manifest"])
    cell = f"aws-eks/redis-{redis}"
    report = raw.get("report", {})
    require(report.get("cell") == cell and report.get("target") == "aws-eks"
            and report.get("redis") == f"redis-{redis}" and report.get("status") == "pass"
            and report.get("require_real") is True, "EKS full strict cell did not pass")
    require(report.get("teardown") == {"attempted": True, "completed": True},
            "EKS owned teardown is incomplete")
    require(report.get("readiness", {}).get("ready") is True and report.get("checks"),
            "EKS live readiness and serving checks are missing")
    require(all(isinstance(check, dict) and check.get("status") == "pass" for check in report["checks"]),
            "EKS serving checks did not all pass")
    executed_yaml = raw.get("executedManifestYaml")
    require(isinstance(executed_yaml, str), "EKS executed manifest bytes are missing")
    executed = yaml.safe_load(executed_yaml)
    require(execution_manifest(executed) == inputs["manifest"], "EKS executed manifest differs from captured inputs")
    manifest_digest = hashlib.sha256(executed_yaml.encode()).hexdigest()
    attempts = report.get("journeyAttempts", [])
    require(isinstance(attempts, list) and 1 <= len(attempts) <= 2
            and [item.get("number") for item in attempts] == list(range(1, len(attempts) + 1)),
            "EKS complete journey attempt history is required")
    documents = raw.get("journeyReceiptBytes")
    require(isinstance(documents, dict) and set(documents) == {item.get("receipt") for item in attempts},
            "EKS actual journey receipt inventory differs from attempt history")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
    import cloud_journey  # noqa: PLC0415
    passed = False
    producer = raw["producer"]
    for attempt in attempts:
        require(not passed, "EKS attempt follows a passed journey")
        text = documents[attempt["receipt"]]
        require(isinstance(text, str) and hashlib.sha256(text.encode()).hexdigest() == attempt.get("receiptSha256"),
                "EKS journey receipt bytes do not match their original digest")
        passed = cloud_journey.validate_attempt(
            attempt, parse_json(text), cell, run_id=producer["runId"], run_attempt=str(producer["runAttempt"]),
            pinned_manifest=executed, pinned_manifest_digest=manifest_digest)
    require(passed, "EKS full actual journey did not pass")
    cost = report.get("cost", {})
    require(cost.get("status") == "pass" and cost.get("scope") == "run"
            and cost.get("runId") == producer["runId"]
            and cost.get("runAttempt") == str(producer["runAttempt"])
            and cost.get("candidateDigest") == manifest_digest, "EKS run cost proof missing or wrong candidate")
    amount, ceiling = Decimal(str(cost.get("amountUsd"))), Decimal(str(cost.get("ceilingUsd")))
    require(amount.is_finite() and ceiling.is_finite() and 0 <= amount <= ceiling and ceiling > 0,
            "invalid or over-ceiling EKS cost evidence")
    require(0 <= (datetime.now(timezone.utc) - instant(cost.get("measuredAt"))).total_seconds() <= 86400,
            "stale EKS cost evidence")


def record(inputs: dict, raw: dict, producer_run: dict, artifact: dict) -> dict:
    """Called by the live producer after teardown, with its pre-execution capture.

    A returned document still requires immutable Actions provenance verification by
    the consumer. This function cannot turn local synthetic controls into evidence.
    """
    require(inputs.get("schema") == INPUT_SCHEMA, "unknown execution-input schema")
    require(execution_inputs(inputs.get("manifest"), inputs.get("deployment")) == inputs,
            "execution-input capture is not canonical")
    deployment = inputs["deployment"]
    expected = deployment["producer"]
    require(all(producer_run.get(key) == value for key, value in expected.items()),
            "producer run does not match the pre-execution capture")
    require(str(producer_run.get("runId", "")).isdigit()
            and int(producer_run["runId"]) > 0
            and type(producer_run.get("runAttempt")) is int and producer_run["runAttempt"] > 0,
            "actual Actions run and attempt are required")
    require(producer_run.get("runUrl") ==
            f"https://github.com/{expected['repository']}/actions/runs/{producer_run['runId']}",
            "producer run URL mismatch")
    require(raw.get("runUrl") == producer_run["runUrl"], "raw receipt belongs to another run")
    require(raw.get("producer") == producer_run, "raw receipt producer identity mismatch")
    require(raw.get("executionInputs") == {"schema": INPUT_SCHEMA, "digest": digest(inputs)},
            "raw producer did not bind these inputs before execution; historical receipts cannot be relabeled")
    require(isinstance(artifact, dict) and set(artifact) == {"id", "sha256", "inputsPath", "rawReceiptPath"}
            and type(artifact.get("id")) is int and artifact["id"] > 0
            and DIGEST.fullmatch(str(artifact.get("sha256", ""))) is not None,
            "immutable producer artifact identity is required")
    prefix = ""
    raw_path = "lambda-preview-receipt.json"
    if deployment["target"] == "awsEks":
        redis = deployment["configuration"].get("redisMode")
        require(redis in {"off", "on"}, "EKS redisMode must be off/on")
        prefix = (f"cloud-evidence/{producer_run['runId']}/{producer_run['runAttempt']}/"
                  f"aws-eks/redis-{redis}/")
        raw_path = prefix + "deployment-cell-receipt.json"
    require(artifact.get("inputsPath") == prefix + "deployment-execution-inputs.json"
            and artifact.get("rawReceiptPath") == raw_path,
            "unsupported producer artifact entries")
    if deployment["target"] == "awsLambda":
        lambda_precut(raw, inputs["manifest"], deployment)
        phase = "candidate-precut"
        scope = {"completedBillItems": [1, 2], "fullGaQualified": False,
                 "remaining": ["item 5 operating envelope admission", "certified two-lock upgrade/rollback",
                               "full GA bill and supported promotion"]}
    else:
        eks_cell(raw, inputs)
        phase = "full-deployment-cell"
        scope = {"fullGaQualified": False, "cellQualified": True,
                 "remaining": ["both Redis cells required for EKS target qualification"]}
    return {"schema": RECEIPT_SCHEMA, "phase": phase, "status": "passed",
            "target": deployment["target"], "component": deployment["component"],
            "architecture": deployment["architecture"],
            "executionInputs": {"schema": INPUT_SCHEMA, "digest": digest(inputs)},
            "producer": copy.deepcopy(producer_run),
            "artifact": copy.deepcopy(artifact),
            "rawReceiptDigest": digest(raw), "rawReceipt": copy.deepcopy(raw),
            "scope": scope}


def verify_receipt(receipt: dict, manifest: dict, deployment: dict, run: dict,
                   artifact_reader=None) -> None:
    """Recompute proof and verify metadata obtained from the authenticated Actions API."""
    require(receipt.get("schema") == RECEIPT_SCHEMA, "unknown or legacy deployment receipt schema")
    inputs = execution_inputs(manifest, deployment)
    expected = record(inputs, receipt.get("rawReceipt", {}), receipt.get("producer", {}),
                      receipt.get("artifact"))
    require(receipt == expected, "deployment receipt differs from verified producer facts")
    producer = receipt["producer"]
    require(run.get("repository", {}).get("full_name") == producer["repository"]
            and str(run.get("id")) == producer["runId"]
            and run.get("run_attempt") == producer["runAttempt"]
            and run.get("head_sha") == producer["sourceRevision"]
            and run.get("path") == producer["workflow"]
            and run.get("html_url") == producer["runUrl"]
            and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "trunk"
            and run.get("status") == "completed" and run.get("conclusion") == "success",
            "deployment proof lacks successful exact producer Actions provenance")
    if deployment["target"] == "awsEks":
        stages = deployment["configuration"]["stages"]
        actuation = receipt["rawReceipt"]["actuation"]
        require(instant(run.get("run_started_at")) <= instant(stages["terraform"]["capturedAt"])
                <= instant(actuation["helmStartedAt"]) <= instant(run.get("updated_at")),
                "EKS capture or actuation belongs to another producer attempt")
    captured, raw = (artifact_reader or fetch_producer_artifact)(producer, receipt["artifact"], run)
    require(captured == inputs and raw == receipt["rawReceipt"],
            "qualification bytes differ from immutable producer artifact")


def artifact_documents(archive: bytes, artifact: dict) -> tuple[dict, dict]:
    require(len(archive) <= 32 * 1024 * 1024, "producer artifact exceeds archive size limit")
    require("sha256:" + hashlib.sha256(archive).hexdigest() == artifact["sha256"],
            "producer artifact archive digest mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        names = zipped.namelist()
        require(len(names) <= 128 and len(set(names)) == len(names),
                "producer artifact has duplicate or excessive entries")
        result = []
        for path in (artifact["inputsPath"], artifact["rawReceiptPath"]):
            require(path in names, "producer artifact is missing a required proof entry")
            info = zipped.getinfo(path)
            require(not info.is_dir() and info.file_size <= 4 * 1024 * 1024,
                    "producer proof entry exceeds size limit")
            value = parse_json(zipped.read(path).decode("utf-8"))
            result.append(value)
    return result[0], result[1]


def fetch_producer_artifact(producer: dict, artifact: dict, run: dict) -> tuple[dict, dict]:
    route = f"repos/{producer['repository']}/actions/artifacts/{artifact['id']}"
    metadata = json.loads(subprocess.check_output(["gh", "api", route], text=True, timeout=60))
    require(metadata.get("id") == artifact["id"] and metadata.get("expired") is False
            and metadata.get("digest") == artifact["sha256"]
            and metadata.get("workflow_run", {}).get("id") == int(producer["runId"])
            and metadata.get("workflow_run", {}).get("head_sha") == producer["sourceRevision"],
            "artifact provenance does not match the exact successful producer")
    require(type(metadata.get("size_in_bytes")) is int
            and 0 < metadata["size_in_bytes"] <= 32 * 1024 * 1024,
            "producer artifact metadata has invalid size")
    require(instant(run.get("run_started_at")) <= instant(metadata.get("created_at"))
            <= instant(run.get("updated_at")), "artifact belongs to another run attempt")
    archive = subprocess.check_output(["gh", "api", route + "/zip"], timeout=60)
    return artifact_documents(archive, artifact)


def fetch_producer_run(producer: dict) -> dict:
    # record() validates the fixed repository and numeric run coordinates before
    # this URL is constructed. A receipt cannot supply a different API origin.
    return json.loads(subprocess.check_output([
        "gh", "api", f"repos/{producer['repository']}/actions/runs/{producer['runId']}"
        f"/attempts/{producer['runAttempt']}"], text=True, timeout=60))


@dataclass(frozen=True)
class VerifiedQualification:
    """In-memory result of raw evidence + independent Actions provenance checks."""
    inputs: dict
    receipt: dict

    def require_current(self, manifest: dict, envelope_bytes: bytes) -> None:
        require(input_binding(manifest, self.inputs["deployment"]) == self.receipt["executionInputs"],
                "verified qualification no longer matches execution inputs")
        expected_envelope = {"path": ENVELOPE_PATH,
                             "sha256": "sha256:" + hashlib.sha256(envelope_bytes).hexdigest()}
        require(self.inputs["deployment"].get("operatingEnvelope") == expected_envelope,
                "qualification does not bind the current operating envelope")


@dataclass(frozen=True)
class VerifiedEksQualification:
    cells: dict[str, VerifiedQualification]

    def require_current(self, manifest: dict, envelope_bytes: bytes) -> None:
        require(set(self.cells) == {"redis-off", "redis-on"}, "both complete EKS cells are required")
        producers = []
        for cell, proof in self.cells.items():
            proof.require_current(manifest, envelope_bytes)
            require(proof.receipt["phase"] == "full-deployment-cell"
                    and proof.inputs["deployment"]["configuration"].get("redisMode") == cell.removeprefix("redis-"),
                    "EKS cell scope mismatch")
            producers.append(proof.receipt["producer"])
        require(producers[0] == producers[1], "EKS cells must come from the same exact producer attempt")

    def reference(self) -> dict:
        bindings = {key: proof.receipt["executionInputs"] for key, proof in sorted(self.cells.items())}
        return {"schema": RECEIPT_SCHEMA,
                "executionInputs": {"schema": "honua.deployment-execution-set/v1", "digest": digest(bindings)},
                "url": self.cells["redis-off"].receipt["producer"]["runUrl"]}


def project_verified_matrix(matrix: dict, verified: dict) -> dict:
    """Promote only EKS's completed full target bill; Lambda remains explicitly pending."""
    result = copy.deepcopy(matrix)
    proof = verified.get("awsEks")
    require(isinstance(proof, VerifiedEksQualification) and set(proof.cells) == {"redis-off", "redis-on"},
            "full verified EKS proof is required for its supported projection")
    row = result["deploy"]["honua-helm"]["awsEks"]["architectures"]["x86_64"]
    require(row.get("releaseScope") == "ga-target", "EKS target is outside the reviewed GA scope")
    row.update(status="supported", qualification="passed", qualificationReceipt=proof.reference())
    return result


def verify_bundle(bundle: dict, manifest: dict, envelope_bytes: bytes,
                  run_reader=fetch_producer_run, artifact_reader=None) -> dict:
    require(isinstance(bundle, dict) and bundle.get("schema") == BUNDLE_SCHEMA,
            "unknown deployment qualification bundle schema")
    require(set(bundle) == {"schema", "records"} and isinstance(bundle.get("records"), dict),
            "malformed deployment qualification bundle")
    def verify_entry(target, entry):
        require(isinstance(entry, dict) and set(entry) == {"inputs", "receipt"},
                "unknown or malformed qualification target record")
        inputs, receipt = entry["inputs"], entry["receipt"]
        require(isinstance(inputs, dict) and isinstance(receipt, dict), "missing deployment evidence")
        deployment = inputs.get("deployment", {})
        require(deployment.get("target") == target, "qualification target differs from bundle key")
        require(execution_inputs(manifest, deployment) == inputs,
                "captured execution inputs differ from current candidate")
        # Validate all untrusted coordinates before calling the authenticated API.
        expected = record(inputs, receipt.get("rawReceipt", {}), receipt.get("producer", {}), receipt.get("artifact"))
        require(receipt == expected, "qualification envelope differs from producer proof")
        verify_receipt(receipt, manifest, deployment, run_reader(receipt["producer"]), artifact_reader)
        result = VerifiedQualification(copy.deepcopy(inputs), copy.deepcopy(receipt))
        result.require_current(manifest, envelope_bytes)
        return result
    verified = {}
    for target, entry in bundle["records"].items():
        require(target in TARGETS, "unknown qualification target")
        if target == "awsEks":
            require(isinstance(entry, dict) and set(entry) == {"cells"}
                    and isinstance(entry["cells"], dict)
                    and set(entry["cells"]) == {"redis-off", "redis-on"}, "both full EKS cells are required")
            proof = VerifiedEksQualification({name: verify_entry(target, value)
                                              for name, value in entry["cells"].items()})
            proof.require_current(manifest, envelope_bytes)
            verified[target] = proof
        else:
            verified[target] = verify_entry(target, entry)
    return verified


def parse_json(text: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QualificationError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    def constant(_value):
        raise QualificationError("nonfinite JSON value")
    value = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    require(isinstance(value, dict), "JSON document must be an object")
    return value


def read_json(path: Path) -> dict:
    return parse_json(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--manifest", type=Path, required=True)
    capture.add_argument("--deployment", type=Path, required=True)
    capture.add_argument("--out", type=Path, required=True)
    finish = commands.add_parser("record")
    finish.add_argument("--inputs", type=Path, required=True)
    finish.add_argument("--raw-receipt", type=Path, required=True)
    finish.add_argument("--artifact-id", type=int, required=True)
    finish.add_argument("--inputs-path", default="deployment-execution-inputs.json")
    finish.add_argument("--raw-receipt-path", default="lambda-preview-receipt.json")
    finish.add_argument("--out", type=Path, required=True)
    bundle_command = commands.add_parser("bundle")
    bundle_command.add_argument("--manifest", type=Path, required=True)
    bundle_command.add_argument("--inputs", type=Path, required=True)
    bundle_command.add_argument("--receipt", type=Path, required=True)
    bundle_command.add_argument("--operating-envelope", type=Path, required=True)
    bundle_command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        result = execution_inputs(yaml.safe_load(args.manifest.read_text()), read_json(args.deployment))
    elif args.command == "record":
        inputs = read_json(args.inputs)
        raw = read_json(args.raw_receipt)
        producer = inputs["deployment"]["producer"]
        require(args.artifact_id > 0, "actual producer artifact ID is required")
        metadata = json.loads(subprocess.check_output([
            "gh", "api", f"repos/{producer['repository']}/actions/artifacts/{args.artifact_id}"],
            text=True, timeout=60))
        result = record(inputs, raw, raw.get("producer", {}), {
            "id": args.artifact_id, "sha256": metadata.get("digest"),
            "inputsPath": args.inputs_path, "rawReceiptPath": args.raw_receipt_path})
        producer = result["producer"]
        run = fetch_producer_run(producer)
        verify_receipt(result, inputs["manifest"], inputs["deployment"], run)
    else:
        inputs, receipt = read_json(args.inputs), read_json(args.receipt)
        result = {"schema": BUNDLE_SCHEMA, "records": {
            inputs["deployment"]["target"]: {"inputs": inputs, "receipt": receipt}}}
        verify_bundle(result, yaml.safe_load(args.manifest.read_text()), args.operating_envelope.read_bytes())
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
