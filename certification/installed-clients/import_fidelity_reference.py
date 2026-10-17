"""Authenticate independent native observations; never import a reference verdict.

The Windows owner implements the producer. This module only consumes its signed
response to a fresh live import request and binds repository, source, run, bytes
and exact source/target identities before comparing raw observations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
import sys

import import_fidelity_journeys as journeys

REPOSITORY = "honua-io/honua-esri-compat"
WORKFLOW = ".github/workflows/esri-sdk-certification.yml"
PREDICATE = "https://slsa.dev/provenance/v1"
REQUEST_SCHEMA = "honua.import-fidelity-reference-request/v1"
RESPONSE_SCHEMA = "honua.import-fidelity-reference-response/v1"
CROSSCHECKS = ("counts-values", "geometry-tolerances", "schema-metadata", "attachment-hashes",
               "relationships", "honua-and-esri-clients")


class ReferenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReferenceError(message)


def command_json(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=60, check=False)
    require(result.returncode == 0, "native reference authentication failed; no response body retained")
    return json.loads(result.stdout)


def api(path):
    return command_json(["gh", "api", path])


def request_for(score, states):
    complete = set(states) == set(journeys.ROLES) and all(
        score.cells["fixture." + role]["verdict"] == "pass" for role in journeys.ROLES)
    if not complete:
        raise journeys.PrerequisiteMissing("native crosschecks require every live authored fixture to pass first")
    return {"schema": REQUEST_SCHEMA, "challenge": secrets.token_hex(32),
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "server": score.receipt()["server"], "consumer": score.consumer,
            "requirementsRevision": score.requirements["revision"],
            "fixtures": {role: {"sourceUrl": state["spec"]["serviceUrl"], "sourceLayerId": state["spec"]["layerId"],
                                "targetUrl": state["targetUrl"], "targetLayerId": state["targetLayer"],
                                "sourceDigest": journeys.digest(state["source"]), "targetDigest": journeys.digest(state["target"])}
                         for role, state in sorted(states.items())}}


def authenticate(path: Path, request: dict):
    require(path.stat().st_size <= 64 * 1024 * 1024, "native response exceeds the explicit 64-MiB bound")
    raw = path.read_bytes()
    response = json.loads(raw)
    require(response.get("schema") == RESPONSE_SCHEMA, "unknown native response schema")
    require(response.get("requestDigest") == journeys.digest(request), "native response is stale or belongs to another live request")
    producer = response.get("producer", {})
    source = producer.get("sourceSha", "")
    run_id, attempt = producer.get("runId"), producer.get("runAttempt")
    require(re.fullmatch(r"[0-9a-f]{40}", source) is not None and type(run_id) is int and run_id > 0
            and type(attempt) is int and attempt > 0, "native producer must identify exact source/run/attempt")
    repository = api(f"repos/{REPOSITORY}")
    require(repository.get("full_name") == REPOSITORY and repository.get("default_branch") == "trunk", "native repository identity changed")
    trunk = api(f"repos/{REPOSITORY}/branches/trunk")
    require(trunk.get("protected") is True, "native workflow source must be protected trunk")
    head = trunk["commit"]["sha"]
    comparison = api(f"repos/{REPOSITORY}/compare/{source}...{head}")
    require(comparison.get("status") in {"ahead", "identical"}
            and comparison.get("base_commit", {}).get("sha") == source
            and comparison.get("merge_base_commit", {}).get("sha") == source,
            "native producer source is not an authenticated protected-trunk ancestor")
    run = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}")
    require(run.get("id") == run_id and run.get("run_attempt") == attempt and run.get("head_sha") == source
            and run.get("head_branch") == "trunk" and run.get("path") == WORKFLOW
            and run.get("event") in {"workflow_dispatch", "push", "schedule"}
            and run.get("status") == "completed" and run.get("conclusion") == "success"
            and run.get("repository", {}).get("full_name") == REPOSITORY,
            "native producing workflow run is not completed, successful and bound to the approved trunk source")
    verified = command_json(["gh", "attestation", "verify", str(path), "--repo", REPOSITORY,
        "--signer-workflow", f"{REPOSITORY}/{WORKFLOW}", "--source-digest", source,
        "--source-ref", "refs/heads/trunk", "--predicate-type", PREDICATE, "--format", "json"])
    bind_verified(verified, hashlib.sha256(raw).hexdigest(), source, run_id, attempt)
    response["authentication"] = {"responseSha256": hashlib.sha256(raw).hexdigest(), "sourceSha": source,
        "workflow": WORKFLOW, "runId": run_id, "runAttempt": attempt, "protectedTrunk": head}
    return response


def bind_verified(verified, digest, source, run_id, attempt):
    require(isinstance(verified, list) and verified, "missing cryptographically verified native attestation")
    for entry in verified:
        result = entry.get("verificationResult", {})
        statement = result.get("statement", {})
        certificate = result.get("signature", {}).get("certificate", {})
        require([subject.get("digest", {}).get("sha256") for subject in statement.get("subject", [])] == [digest],
                "native attestation signs different bytes")
        require(statement.get("predicateType") == PREDICATE, "native predicate type differs")
        require(certificate.get("sourceRepositoryURI") == f"https://github.com/{REPOSITORY}"
                and certificate.get("buildSignerURI") == f"https://github.com/{REPOSITORY}/{WORKFLOW}@refs/heads/trunk"
                and certificate.get("sourceRepositoryDigest") == source
                and certificate.get("sourceRepositoryRef") == "refs/heads/trunk"
                and certificate.get("runInvocationURI") == f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}",
                "native attestation repository/workflow/source/run identity differs")
        commits = {dependency.get("digest", {}).get("gitCommit") for dependency in
                   statement.get("predicate", {}).get("buildDefinition", {}).get("resolvedDependencies", [])} - {None}
        require(commits == {source}, "native attestation resolved source differs")


def crosscheck(name, response, request, states, equal):
    require(set(response.get("fixtures", {})) == set(request["fixtures"]), "native response omits authored fixtures")
    for role, state in states.items():
        result = response["fixtures"][role]
        require(result.get("identity") == request["fixtures"][role], "native observed source/target identity differs")
        observations = result.get("observations", {})
        require(set(observations) == {"arcpy", "pyqgis"}, "native response requires both independent native readers")
        for reader, observation in observations.items():
            require(observation.get("runtimeVersion") and observation.get("methods") and observation.get("live") is True,
                    "native reader must identify real runtime and executed methods")
            for side in ("source", "target"):
                snapshot = state[side]
                actual = observation[side]
                if name == "counts-values":
                    equal({"count": snapshot["count"], "rows": journeys.rows(snapshot)},
                          {"count": actual.get("count"), "rows": actual.get("rows")}, "native " + reader + " counts/exact values")
                elif name == "geometry-tolerances":
                    equal(journeys.shapes(snapshot), actual.get("shapes"), "native " + reader + " geometry including Z/M/curves", 1e-8)
                elif name == "schema-metadata":
                    equal(journeys.schema(snapshot), actual.get("schema"), "native " + reader + " schema/renderer/time metadata")
                elif name == "attachment-hashes" and role == "attachments":
                    equal(state[side + "Attachments"], actual.get("attachments"), "native " + reader + " attachment byte hashes")
                elif name == "relationships" and role == "related-records":
                    equal(state["relationships"], actual.get("relationships"), "native " + reader + " actual related-record edges")
    if name == "honua-and-esri-clients":
        # Merely naming a runtime is insufficient: require every preceding independent comparison.
        for prerequisite in CROSSCHECKS[:-1]:
            crosscheck(prerequisite, response, request, states, equal)
    return "fresh signed native observations agree with actual published SDK source/target reads for " + name


def consume(score, states, inbox: Path | None, wait_seconds: int, producer):
    try:
        request = request_for(score, states)
    except journeys.PrerequisiteMissing as exc:
        for name in CROSSCHECKS:
            score.block("crosscheck." + name, str(exc))
        return
    directory = score.capture_directory
    require(directory is not None, "native crosscheck requires a retained observation directory")
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / "native-reference-request.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n")
    print(f"native reference request ready: {request_path}", file=sys.stderr, flush=True)
    score.provenance["nativeRequestDigest"] = journeys.digest(request)
    if inbox is None:
        for name in CROSSCHECKS:
            score.block("crosscheck." + name, "missing authenticated native response to retained fresh request")
        return
    deadline = min(time.monotonic() + wait_seconds, getattr(producer, "deadline", time.monotonic() + wait_seconds))
    while not inbox.is_file() and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    if not inbox.is_file():
        for name in CROSSCHECKS:
            score.block("crosscheck." + name, "native response did not arrive within the explicit bounded wait")
        return
    try:
        response = authenticate(inbox, request)
    except (ReferenceError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        for name in CROSSCHECKS:
            score.record("crosscheck." + name, lambda exc=exc: (_ for _ in ()).throw(ReferenceError(str(exc))))
        return
    score.provenance["nativeAuthentication"] = response["authentication"]
    for name in CROSSCHECKS:
        score.record("crosscheck." + name, lambda name=name: crosscheck(name, response, request, states, producer.require_equal))
