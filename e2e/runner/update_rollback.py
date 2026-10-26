"""Real AWS update/rollback driver; diagnostic rehearsals never become qualifying receipts.

This module has no simulated provider. Its adapters use the supported deploy API, Terraform,
AWS readback and the canonical OGC job lifecycle. Missing proof is a failure, including a job
which completed before a switch. Unit tests inject adapters only at these I/O boundaries.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PENDING_ECR_MIRROR = "pending-ecr-mirror"
SHA = re.compile(r"[0-9a-f]{40}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
CELLS = {f"{target}/redis-{redis}" for target in ("aws-ecs", "aws-serverless") for redis in ("on", "off")}
# Founder re-tier 2026-10-25: only the AWS ECS cells are GA. A Lambda (aws-serverless) cell can still
# run as Preview, but the aggregate promotion proof is the GA set.
GA_CELLS = {f"aws-ecs/redis-{redis}" for redis in ("on", "off")}
ACTUATORS = {"aws-ecs/redis-off": "terraform-exact-apply", "aws-ecs/redis-on": "honua-aws-ecs-alb",
             "aws-serverless/redis-off": "honua-gitops-aws-lambda", "aws-serverless/redis-on": "honua-gitops-aws-lambda"}


class ProofError(ValueError):
    """A bounded diagnostic; never include response bodies, credentials or provider errors."""


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition, message):
    if not condition:
        raise ProofError(message)


def immutable(repository, value):
    require(isinstance(repository, str) and bool(repository) and DIGEST.fullmatch(str(value)),
            "immutable image repository/digest missing")
    repository = repository.split("@", 1)[0]
    repository = repository.rsplit(":", 1)[0] if ":" in repository.rsplit("/", 1)[-1] else repository
    require(re.fullmatch(r"[a-z0-9][a-z0-9./_-]+", repository) is not None, "invalid image repository")
    return repository + "@" + value


@dataclass(frozen=True)
class Release:
    manifest: dict
    manifest_digest: str
    lock_digest: str | None
    source: str
    schema: str
    image: str
    lambda_image: str
    lambda_digest: str
    version: str = ""
    lambda_mirrored: bool = True

    def _require_lambda(self, cell):
        require(not cell.startswith("aws-serverless/") or self.lambda_mirrored,
                "Lambda cell requires a recorded Lambda mirror digest")

    def image_for(self, cell):
        self._require_lambda(cell)
        return self.lambda_image if cell.startswith("aws-serverless/") else self.image

    def digest_for(self, cell):
        self._require_lambda(cell)
        return self.lambda_digest if cell.startswith("aws-serverless/") else self.image.rsplit("@", 1)[1]

    def public(self):
        return {"manifestDigest": self.manifest_digest, "lockDigest": self.lock_digest,
                "sourceRevision": self.source, "schemaVersion": self.schema, "releaseVersion": self.version,
                "imageDigest": self.image.rsplit("@", 1)[1], "lambdaImageDigest": self.lambda_digest}


def load_release(directory: Path, mode: str, *, run=subprocess.run):
    """Authenticate the actual bytes before any deployment mutation; no `verified: true` inputs."""
    raw = (directory / "platform-manifest.yaml").read_bytes()
    manifest = yaml.safe_load(raw)
    server = manifest["components"]["honua-server"]
    source = server.get("sha")
    require(isinstance(source, str) and SHA.fullmatch(source), "full immutable server source commit required")
    image = immutable(server.get("image"), server.get("digest"))
    lambda_image = immutable(server.get("awsLambdaImage"), server.get("awsLambdaDigest"))
    # A byte-identical mirror has the same manifest digest. A rewritten mirror needs a separately
    # authenticated mapping, which this producer deliberately does not infer from a declaration.
    # Lambda is Preview (2026-10-25 re-tier) and update/rollback deploys ECS cells only, so a fresh
    # pin may still carry the pending-ecr-mirror sentinel; it then cannot deploy a Lambda cell.
    mirror = str(server.get("awsLambdaEcrDigest"))
    lambda_mirrored = mirror != PENDING_ECR_MIRROR
    require(not lambda_mirrored or DIGEST.fullmatch(mirror) is not None, "observed Lambda mirror digest required")
    require(not lambda_mirrored or mirror == server["awsLambdaDigest"], "Lambda mirror must preserve attested manifest bytes")
    schema = str(server.get("dbSchema", ""))
    require(re.fullmatch(r"[1-9][0-9]*", schema) is not None, "exact database schema required")
    from check_image_attestation import check, AttestationMismatch
    # Lambda is deployed as a single-architecture child. The maintained build attests the
    # parent index; authenticate it and prove the child membership instead of demanding an
    # attestation for a subject the producer never signs.
    lambda_tag = lambda_image.split("@", 1)[0] + ":nightly-lambda-aot-" + source[:7]
    index_raw = checked(run, ["docker", "buildx", "imagetools", "inspect", lambda_tag, "--raw"],
                        "Lambda attested parent index unavailable")
    try:
        index = json.loads(index_raw)
        architecture = {"x86_64": "amd64", "arm64": "arm64"}[server["awsLambdaArchitecture"]]
        children = [child for child in index.get("manifests", [])
                    if child.get("platform", {}).get("os") == "linux" and child.get("platform", {}).get("architecture") == architecture]
        require(len(children) == 1 and children[0].get("digest") == server["awsLambdaDigest"],
                "Lambda child is not a member of the attested source index")
    except (ValueError, KeyError, TypeError) as exc:
        raise ProofError("Lambda attested parent index is invalid") from exc
    lambda_parent = lambda_image.split("@", 1)[0] + "@" + digest(index_raw.encode())
    for reference in (image, lambda_parent):
        verification = checked(run, ["gh", "attestation", "verify", "oci://" + reference,
                      "--repo", "honua-io/honua-server", "--format", "json", "--source-digest", source,
                      "--signer-workflow", "honua-io/honua-server/.github/workflows/nightly-container-build.yml"],
                "server build attestation refused")
        try:
            check(json.loads(verification), source, reference.rsplit("@", 1)[1])
        except (ValueError, AttestationMismatch) as exc:
            raise ProofError("server source/image provenance mismatch") from exc
    lock_digest = None
    version = str(manifest.get("platformRelease", ""))
    if mode == "qualification":
        path = directory / "platform-lock.json"
        lock_raw = path.read_bytes()
        checked(run, ["gh", "attestation", "verify", str(path), "--repo", "honua-io/honua-release",
                      "--source-ref", "refs/heads/trunk", "--signer-workflow",
                      "honua-io/honua-release/.github/workflows/nightly-certification.yml"],
                "certified lock signature refused")
        lock = json.loads(lock_raw)
        require(lock.get("sourceInputs", {}).get("platformManifest", {}).get("sha256") == digest(raw),
                "certified lock does not bind the retained manifest")
        component = lock.get("components", {}).get("honua-server", {})
        require(component.get("schemaVersions", {}).get("database") == schema,
                "lock schema differs from retained manifest")
        artifacts = [a for a in component.get("artifacts", []) if a.get("kind") == "image"]
        require(len(artifacts) == 1 and artifacts[0].get("digest") == server["digest"]
                and artifacts[0].get("sourceRevision") == source, "lock image/source differs from retained manifest")
        lock_digest = digest(lock_raw)
        version = str(lock.get("platform", {}).get("id", "")).removeprefix("honua-")
    else:
        require(mode == "rehearsal", "unknown update/rollback mode")
    require(bool(version) and len(version) <= 100, "declared platform release version missing")
    return Release(manifest, digest(raw), lock_digest, source, schema, image, lambda_image, server["awsLambdaDigest"], version,
                   lambda_mirrored)


def checked(run, argv, message, **kwargs):
    try:
        result = run(argv, text=True, capture_output=True, check=False, **({"timeout": 300} | kwargs))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProofError(message) from exc
    require(result.returncode == 0, message)
    return result.stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, endpoint, credential, *, opener=None, loopback=False, anonymous=False):
        parsed = urllib.parse.urlsplit(endpoint)
        require(parsed.scheme == "https" or loopback and parsed.scheme == "http" and parsed.hostname == "127.0.0.1",
                "qualification endpoint must use HTTPS")
        require(parsed.netloc and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in ("", "/"), "endpoint must be one credential-free origin")
        require(isinstance(credential, str) and (bool(credential) or anonymous) and not any(c in credential for c in "\r\n"), "application credential required")
        self.endpoint, self.credential = endpoint.rstrip("/"), credential
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None):
        require(path.startswith("/") and not path.startswith("//") and not urllib.parse.urlsplit(path).fragment,
                "API path must be same-origin")
        headers = {"Accept": "application/json", "Cache-Control": "no-cache", "Prefer": "respond-async"}
        if self.credential:
            headers["Authorization" if self.credential.startswith("Bearer ") else "X-API-Key"] = self.credential
        raw = None if body is None else json.dumps(body, allow_nan=False).encode()
        if path.endswith("/execution"):
            headers["Prefer"] = "respond-async"
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.endpoint + path, data=raw, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                status, payload = response.status, response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read(1024 * 1024 + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ProofError("candidate request unavailable") from exc
        self.last_body = payload
        require(len(payload) <= 16 * 1024 * 1024, "candidate response exceeds bound")
        try:
            document = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise ProofError("candidate response is not JSON") from exc
        require(isinstance(document, dict), "candidate response is not an object")
        return status, document

    def get(self, path):
        status, document = self.request("GET", path)
        require(status == 200, "candidate read refused")
        return document


class DeployApi:
    """Create, separately approve, observe and roll back one actual backend operation."""
    PREFIX = "/api/v1/admin/deploy"

    def __init__(self, api, target_id, backend, *, approval_api, sleep=time.sleep, polls=360):
        self.api, self.approval_api = api, approval_api
        self.target_id, self.backend, self.sleep, self.polls = target_id, backend, sleep, polls
        self.operation_id = None

    def plan(self, previous, desired):
        body = {"targetId": self.target_id, "currentRevision": previous, "desiredRevision": desired}
        code, plan = self.api.request("POST", self.PREFIX + "/plan", body)
        require(code == 200 and plan.get("backendRegistered") is True
                and plan.get("target", {}).get("backend") == self.backend
                and plan.get("target", {}).get("targetId") == self.target_id
                and not plan.get("blockingReasons") and plan.get("capabilities", {}).get("supportsRollback") is True,
                "native deployment plan refused or resolved another backend")
        require(plan.get("readyToSubmit") is True or plan.get("requiresApproval") is True,
                "native deployment plan is not ready")
        return body

    def submit(self, previous, desired, idempotency_key):
        body = self.plan(previous, desired)
        code, operation = self.api.request("POST", self.PREFIX + "/operations", {
            **body, "submitImmediately": False, "idempotencyKey": idempotency_key,
            "reason": "reviewed candidate-bound AWS update/rollback qualification"})
        identity = operation.get("operationId")
        require(code == 201 and isinstance(identity, str) and TOKEN.fullmatch(identity), "deploy operation creation failed")
        self.operation_id = identity
        code, _ = self.approval_api.request("POST", f"{self.PREFIX}/operations/{identity}/submit",
                                          {"reason": "operator approved qualification switch"})
        require(code == 200, "separate deployment approval refused")
        return self.wait(desired, rollback=False)

    def wait(self, desired, *, rollback):
        for _ in range(self.polls):
            row = self.api.get(f"{self.PREFIX}/operations/{self.operation_id}")
            require(row.get("operationId") == self.operation_id and row.get("target", {}).get("backend") == self.backend,
                    "deployment operation identity changed")
            status = row.get("status")
            require(status not in ("Failed", "ManualInterventionRequired", "Cancelled"), "native deployment failed")
            accepted = ("RolledBack",) if rollback else ("Succeeded", "Observing", "Protected")
            # During protection the status may remain Reconciling; only the explicit protection
            # state plus observed revision proves exposure, never a requested revision alone.
            exposed = not rollback and row.get("protection", {}).get("phase") in ("observing", "protected")
            if (status in accepted or exposed) and row.get("observedState") == desired:
                require(bool(row.get("providerOperationId")), "native provider operation identity missing")
                return {"operationId": self.operation_id, "status": status,
                        "observedRevision": desired, "providerOperationIdSha256": digest(row["providerOperationId"].encode())}
            self.sleep(2)
        raise ProofError("native deployment observation timed out")

    def rollback(self, previous):
        require(self.operation_id is not None, "no retained operation to roll back")
        code, _ = self.approval_api.request("POST", f"{self.PREFIX}/operations/{self.operation_id}/rollback",
                                          {"reason": "operator approved return to retained baseline"})
        require(code == 200, "native rollback refused")
        return self.wait(previous, rollback=True)


def preflight(prior, candidate, cell, mode):
    require(cell in CELLS, "unsupported update/rollback cell")
    require(prior.source != candidate.source and prior.image != candidate.image
            and (not cell.startswith("aws-serverless/") or prior.lambda_digest != candidate.lambda_digest),
            "two distinct immutable server builds required")
    if mode == "qualification":
        require(prior.lock_digest and candidate.lock_digest and prior.lock_digest != candidate.lock_digest,
                "qualification requires two distinct certified signed locks")
    else:
        require(mode == "rehearsal" and prior.lock_digest is None and candidate.lock_digest is None,
                "rehearsal must not claim certified-lock qualification")
    # The maintained build declares its exact schema, not an invented backward-readable range.
    # Until a signed compatibility range exists, a differing schema is refused BEFORE staging,
    # migration, job creation, Terraform apply, task registration or alias change.
    require(prior.schema == candidate.schema, "schema boundary incompatible or undeclared; no provider mutation allowed")


def job_identity(row):
    require(isinstance(row, dict) and isinstance(row.get("jobId"), str) and TOKEN.fullmatch(row["jobId"]),
            "durable job identity missing")
    require(row.get("attemptCount") == 1 and type(row.get("attemptCount")) is int,
            "durable job was not executed exactly one attempt")
    provider, worker = row.get("providerOperationId"), row.get("claimedBy")
    require(isinstance(row.get("backend"), str) and row["backend"]
            and (isinstance(provider, str) and provider or isinstance(worker, str) and worker),
            "durable provider or in-process worker identity missing")
    return {"jobId": row["jobId"], "attemptCount": row["attemptCount"], "backend": row["backend"],
            "providerOperationId": provider, "claimedBy": worker}


def run_cell(prior, candidate, cell, mode, runtime, *, run_id, run_attempt, sleep=time.sleep, polls=1800):
    """Each switch carries a genuinely running job; early completion fails closed.

    Separate real jobs per transition avoid imposing a same-job-across-cycle requirement.

    The runtime must independently observe routing/images, serving data, database journal,
    execution events and result contents. Returned receipts deliberately exclude raw output.
    Cleanup/cost are added only by the existing teardown controller, never guessed here.
    """
    started = now()
    receipt = {"schema": "honua.aws-update-rollback-cell/v1", "cell": cell, "mode": mode,
               "status": "fail", "updateStatus": "fail", "rollbackStatus": "fail",
               "actuator": ACTUATORS.get(cell), "runId": str(run_id), "runAttempt": str(run_attempt),
               "startedAt": started, "prior": prior.public(), "candidate": candidate.public(),
               "fromDigest": prior.digest_for(cell), "toDigest": candidate.digest_for(cell),
               "fromLockDigest": prior.lock_digest, "toLockDigest": candidate.lock_digest,
               "lockDigest": candidate.lock_digest, "observations": [], "functionalGpExecuted": False}
    try:
        preflight(prior, candidate, cell, mode)
        runtime.prerequisites(prior, candidate)
        store = getattr(runtime, "controller_store", None)
        if store:
            receipt["controllerStore"] = store
        receipt["observations"].append(runtime.observe(prior, "baseline"))
        receipt["incompatibleTarget"] = runtime.incompatible_target()
        revisions = runtime.stage(candidate)
        runtime.plan(revisions)
        def begin_job():
            submitted = runtime.submit_job()
            job_id = submitted.get("jobID")
            require(isinstance(job_id, str) and TOKEN.fullmatch(job_id), "GP submission omitted canonical job identity")
            for _ in range(polls):
                current = runtime.job(job_id)
                require(current.get("status") not in ("Succeeded", "Failed", "Cancelled"),
                        "job completed before switch; not an in-flight proof")
                if current.get("status") == "Running":
                    return job_id, job_identity(current)
                sleep(1)
            raise ProofError("job never became genuinely running")

        def still_running(job_id, identity):
            current = runtime.job(job_id)
            require(current.get("status") == "Running" and job_identity(current) == identity,
                    "job was not still running across the version switch")
            return {"jobId": job_id, "state": "running", "attemptCount": 1, "observedAt": now()}

        def finish_job(job_id, identity):
            for _ in range(polls):
                final = runtime.job(job_id)
                require(job_identity(final) == identity, "job/provider/attempt changed during handoff")
                if final.get("status") in ("Succeeded", "Failed", "Cancelled"):
                    require(final["status"] == "Succeeded" and isinstance(final.get("completedAt"), str),
                            "in-flight GP did not complete successfully")
                    break
                sleep(1)
            else:
                raise ProofError("in-flight GP completion timed out")
            output = runtime.output(job_id)
            executions = runtime.execution_events(job_id, started)
            require(type(executions.get("started")) is int and type(executions.get("completed")) is int
                    and executions.get("started") == executions.get("completed") == 1,
                    "independent worker events do not prove one execution and one terminal completion")
            require(runtime.job(job_id) == final and runtime.output(job_id) == output,
                    "terminal state or output changed after completion")
            return {"jobId": job_id, "initialState": "running", "terminalStates": ["succeeded"],
                    "attemptCount": 1, "providerOperationIdSha256": digest(json.dumps(identity, sort_keys=True).encode()),
                    "completedAt": final.get("completedAt"), "output": output, "executionEvents": executions}

        if cell.endswith("redis-on"):
            job_id, identity = begin_job()
            receipt["jobBeforeUpdate"] = still_running(job_id, identity)
        else:
            receipt["executionOutcome"] = "refused"
            receipt["refusals"] = [runtime.refusal()]
        receipt["updateOperation"] = runtime.update(revisions)
        receipt["observations"].append(runtime.observe(candidate, "candidate"))
        if cell.endswith("redis-on"):
            receipt["jobAfterUpdate"] = still_running(job_id, identity)
            receipt["inFlightJob"] = finish_job(job_id, identity)
            receipt["functionalGpExecuted"] = True
        else:
            receipt["refusals"].append(runtime.refusal())
        receipt["updateStatus"] = "pass"
        if cell.endswith("redis-on"):
            job_id, identity = begin_job()
            receipt["jobBeforeRollback"] = still_running(job_id, identity)
        receipt["rollbackOperation"] = runtime.rollback(revisions)
        receipt["observations"].append(runtime.observe(prior, "rollback"))
        if cell.endswith("redis-on"):
            receipt["jobAfterRollback"] = still_running(job_id, identity)
            receipt["rollbackInFlightJob"] = finish_job(job_id, identity)
            receipt["executionOutcome"] = "succeeded"
        else:
            receipt["refusals"].append(runtime.refusal())
        receipt["rollbackStatus"] = "pass"
        receipt["schemaBoundary"] = {"status": "pass", "outcome": "serving", "schemaVersion": candidate.schema,
                                      "imageDigest": prior.digest_for(cell)}
        receipt["status"] = "pass"
    except ProofError as error:
        receipt["finding"] = str(error)
    except Exception as error:
        # Unexpected diagnostics can contain secret coordinates; keep the type only.
        receipt["finding"] = "update/rollback producer raised " + type(error).__name__
    finally:
        receipt["completedAt"] = now()
    return receipt


def finalize_cell(receipt, cloud_report):
    """Attach real teardown and metered cost; an early switch pass alone cannot qualify."""
    require(cloud_report.get("cell") == receipt.get("cell"), "cleanup report is for another cell")
    require(str(cloud_report.get("cost", {}).get("runId")) == receipt.get("runId")
            and str(cloud_report.get("cost", {}).get("runAttempt")) == receipt.get("runAttempt"), "cleanup run identity differs")
    cleanup = cloud_report.get("teardown", {})
    cost = cloud_report.get("cost", {})
    require(cleanup.get("attempted") is True and cleanup.get("completed") is True
            and not cloud_report.get("cleanupFailures") and not receipt.get("controllerCleanupFailures"),
            "actual infrastructure/controller cleanup is incomplete")
    require(cost.get("status") == "pass", "actual bounded cost evidence missing or over ceiling")
    receipt = dict(receipt)
    receipt["cleanup"] = {"status": "pass", "teardown": cleanup}
    receipt["cost"] = cost
    receipt["cloudReportSha256"] = digest(json.dumps(cloud_report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    return receipt


def aggregate(cells):
    """Every independently finalized GA cell; rehearsal never emits the promotion evidence class."""
    require(isinstance(cells, list) and len(cells) == len(GA_CELLS) and {r.get("cell") for r in cells} == GA_CELLS,
            "exactly one update/rollback cell per GA cell required")
    binding = {(r.get("mode"), r.get("fromLockDigest"), r.get("toLockDigest"), r.get("runId"), r.get("runAttempt"),
                json.dumps(r.get("prior"), sort_keys=True), json.dumps(r.get("candidate"), sort_keys=True)) for r in cells}
    require(len(binding) == 1, "update/rollback cells do not bind the same builds, locks and workflow attempt")
    for row in cells:
        require(row.get("status") == row.get("updateStatus") == row.get("rollbackStatus") == "pass"
                and row.get("actuator") == ACTUATORS[row["cell"]] and row.get("cleanup", {}).get("status") == "pass"
                and row.get("cost", {}).get("status") == "pass", "a GA cell lacks actual switch/cleanup/cost proof")
    first = cells[0]
    output = {"schema": "honua.aws-update-rollback/v1", "mode": first["mode"], "status": "pass",
              "runId": first["runId"], "runAttempt": first["runAttempt"], "completedAt": now(),
              "updateStatus": "pass", "rollbackStatus": "pass", "cells": cells,
              "qualification": first["mode"] == "qualification"}
    if first["mode"] == "qualification":
        require(first.get("fromLockDigest") and first.get("toLockDigest")
                and first["fromLockDigest"] != first["toLockDigest"], "two certified signed locks required")
        from datetime import timedelta
        completed = datetime.fromisoformat(output["completedAt"].replace("Z", "+00:00"))
        output.update({"class": "update-rollback", "kind": "qualifying", "fromLockDigest": first["fromLockDigest"],
                       "toLockDigest": first["toLockDigest"], "lockDigest": first["toLockDigest"],
                       "freshUntil": (completed + timedelta(days=7)).isoformat().replace("+00:00", "Z")})
    else:
        require(first["mode"] == "rehearsal", "unknown rehearsal mode")
        # No class, lockDigest or qualifying kind exists on this diagnostic record.
        output["diagnosticOnly"] = True
    return output
