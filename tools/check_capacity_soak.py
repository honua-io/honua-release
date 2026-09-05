#!/usr/bin/env python3
"""Fail-closed evaluator for sourced, exact-candidate capacity/SLO evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from capacity_observations import validate_sources


APPROVED_PRODUCER_REPOSITORY = "honua-io/honua-server"
APPROVED_PRODUCER_WORKFLOW = ".github/workflows/load-soak-nightly.yml"
APPROVED_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_URI_PATTERN = re.compile(r"^/honua-io/honua-server/actions/runs/[1-9][0-9]*/artifacts/[1-9][0-9]*$")
POPULATION_KINDS = {"ratio", "distribution", "gauge", "duration"}


class ContractError(ValueError):
    """Raised when a timestamp or top-level input cannot be interpreted."""


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _positive_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _immutable_https_url(value: object) -> bool:
    if not _nonempty(value):
        return False
    parsed = urlparse(str(value))
    return parsed.scheme == "https" and bool(parsed.netloc) and "latest" not in parsed.path.lower()


def _artifact_url(value: object) -> bool:
    if not _immutable_https_url(value):
        return False
    parsed = urlparse(str(value))
    return parsed.netloc == "github.com" and bool(ARTIFACT_URI_PATTERN.fullmatch(parsed.path))


def _refs(value: object, known: set[str]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item in known for item in value)
        and len(value) == len(set(value))
    )


def _contains_per_tenant_slo(value: object) -> bool:
    forbidden = {"tenantslo", "tenantslos", "pertenantslo", "pertenantslos"}
    if isinstance(value, dict):
        return any(str(key).replace("_", "").lower() in forbidden for key in value) or any(
            _contains_per_tenant_slo(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_per_tenant_slo(child) for child in value)
    return False


def lock_digest(path: Path) -> str:
    """Return the SHA-256 digest of the frozen capacity lock."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_raw_artifacts(
    receipt: dict[str, Any], artifact_root: Path, failures: list[str]
) -> set[str]:
    raw = receipt.get("rawArtifacts")
    if not isinstance(raw, list) or not raw:
        failures.append("raw observation artifacts are missing")
        return set()

    root = artifact_root.resolve()
    known: set[str] = set()
    for artifact in raw:
        item = _mapping(artifact)
        artifact_id = item.get("id")
        label = str(artifact_id) if _nonempty(artifact_id) else "<missing>"
        if not _nonempty(artifact_id) or artifact_id in known:
            failures.append(f"{label}: raw artifact id is missing or duplicated")
            continue
        known.add(str(artifact_id))
        if not _nonempty(item.get("kind")):
            failures.append(f"{label}: raw artifact kind is missing")
        if not _artifact_url(item.get("uri")):
            failures.append(f"{label}: raw artifact URI is not an immutable Actions artifact URL")
        elif urlparse(item["uri"]).path.split("/")[5] != str(_mapping(receipt.get("producer")).get("runId")):
            failures.append(f"{label}: raw artifact comes from a different producer run")
        if not HASH_PATTERN.fullmatch(str(item.get("sha256", ""))):
            failures.append(f"{label}: raw artifact SHA-256 is invalid")
        if not _positive_count(item.get("observationCount")):
            failures.append(f"{label}: raw observation population is missing")

        relative = item.get("path")
        if not _nonempty(relative):
            failures.append(f"{label}: raw artifact path is missing")
            continue
        candidate = (root / str(relative)).resolve()
        if candidate.parent != root or not candidate.is_file():
            failures.append(f"{label}: raw artifact is absent from the evidence bundle")
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != item.get("sha256"):
            failures.append(f"{label}: raw artifact hash mismatch")
    return known


def _validate_topology(receipt: dict[str, Any], image_digest: str, failures: list[str]) -> None:
    topology = _mapping(receipt.get("topology"))
    replicas = topology.get("replicas")
    if not isinstance(replicas, list) or len(replicas) < 2:
        failures.append("distributed topology must contain at least two replicas")
        replicas = []
    ids: set[str] = set()
    failure_domains: set[str] = set()
    for replica in replicas:
        item = _mapping(replica)
        replica_id = item.get("id")
        domain = item.get("failureDomain")
        if not _nonempty(replica_id) or replica_id in ids:
            failures.append("topology replica ids must be non-empty and unique")
        else:
            ids.add(str(replica_id))
        if not _nonempty(domain):
            failures.append("every topology replica must name a failure domain")
        else:
            failure_domains.add(str(domain))
        if item.get("imageDigest") != image_digest:
            failures.append(f"{replica_id or '<missing>'}: replica image digest differs from candidate")
    if replicas and len(failure_domains) < 2:
        failures.append("candidate replicas must span at least two failure domains")
    for dependency in ("database", "redis"):
        item = _mapping(topology.get(dependency))
        if not _nonempty(item.get("kind")) or not _nonempty(item.get("failureDomain")):
            failures.append(f"topology {dependency} identity/failure domain is missing")
    if topology.get("gpWorkers") != receipt.get("envelope", {}).get("gpWorkers"):
        failures.append("topology GP worker count differs from the exercised envelope")


def _validate_producer(receipt: dict[str, Any], expected_revision: str, failures: list[str]) -> None:
    producer = _mapping(receipt.get("producer"))
    expected_ref = f"{APPROVED_PRODUCER_REPOSITORY}/{APPROVED_PRODUCER_WORKFLOW}@{expected_revision}"
    if (
        producer.get("repository") != APPROVED_PRODUCER_REPOSITORY
        or producer.get("workflowPath") != APPROVED_PRODUCER_WORKFLOW
        or producer.get("workflowRef") != expected_ref
        or producer.get("sourceRevision") != expected_revision
    ):
        failures.append("receipt was not emitted by the approved soak producer at the candidate revision")
    if not _positive_count(producer.get("runId")) or not _positive_count(producer.get("runAttempt")):
        failures.append("producer workflow run identity is missing")
    if producer.get("predicateType") != APPROVED_PREDICATE_TYPE:
        failures.append("producer attestation predicate type is not approved")


def _validate_workloads(
    lock: dict[str, Any], receipt: dict[str, Any], artifacts: set[str], failures: list[str]
) -> set[str]:
    expected = _mapping(lock.get("supportedEnvelope"))
    workloads = receipt.get("workloads")
    if not isinstance(workloads, dict):
        workloads = {}
        failures.append("workload evidence object is missing")
    if set(workloads) != set(expected):
        for name in expected:
            if name not in workloads:
                failures.append(f"{name}: workload evidence is missing")
        for name in set(workloads) - set(expected):
            failures.append(f"{name}: workload is outside the frozen envelope")

    for name, target in expected.items():
        workload = _mapping(workloads.get(name))
        if workload.get("query") != _mapping(lock.get("workloadQueries")).get(name) or not workload.get("query"):
            failures.append(f"{name}: workload query differs from the frozen query")
        if workload.get("status") != "exercised":
            failures.append(f"{name}: workload was not exercised")
        if workload.get("proxy") is not False or workload.get("executionMode") != "candidate-topology":
            failures.append(f"{name}: Preview/proxy evidence is not admissible for the GA envelope")
        if workload.get("target") != target or workload.get("observed") != target:
            failures.append(f"{name}: workload did not exercise the exact frozen target")
        if not _refs(workload.get("rawArtifactIds"), artifacts):
            failures.append(f"{name}: workload raw artifact references are missing or invalid")
    return set(expected)


def _validate_population(name: str, signal: dict[str, Any], failures: list[str]) -> None:
    population = _mapping(signal.get("observationPopulation"))
    kind = population.get("kind")
    if kind not in POPULATION_KINDS or not _positive_count(population.get("sampleCount")):
        failures.append(f"{name}: raw observation population is missing or invalid")
        return
    if kind == "ratio":
        numerator = population.get("numerator")
        denominator = population.get("denominator")
        if not _finite(numerator) or not _finite(denominator) or numerator < 0 or denominator <= 0:
            failures.append(f"{name}: ratio numerator/denominator is missing or invalid")
            return
        if _finite(signal.get("value")):
            computed = numerator / denominator
            if not math.isclose(float(signal["value"]), computed, rel_tol=1e-9, abs_tol=1e-12):
                failures.append(f"{name}: value does not equal its numerator/denominator")


def _validate_recovery(signal: dict[str, Any], artifacts: set[str], window: dict[str, Any], failures: list[str]) -> None:
    evidence = _mapping(signal.get("recoveryEvidence"))
    events = evidence.get("events")
    if isinstance(events, list) and events:
        for event in events:
            event_signal = dict(signal, recoveryEvidence=dict(event, rawArtifactIds=evidence.get("rawArtifactIds")))
            event_signal.pop("value", None)
            _validate_recovery(event_signal, artifacts, window, failures)
        return
    if not _nonempty(evidence.get("failure")) or not _refs(evidence.get("rawArtifactIds"), artifacts):
        failures.append("recoveryTimeSeconds: recovery evidence and raw artifact are required")
        return
    try:
        injected = _time(evidence.get("injectedAt"), "recovery injectedAt")
        detected = _time(evidence.get("detectedAt"), "recovery detectedAt")
        recovered = _time(evidence.get("recoveredAt"), "recovery recoveredAt")
        started = _time(window.get("startedAt"), "window.startedAt")
        ended = _time(window.get("endedAt"), "window.endedAt")
        if not started <= injected <= detected <= recovered <= ended:
            failures.append("recoveryTimeSeconds: recovery timeline is outside the observation window or unordered")
        if _finite(signal.get("value")) and not math.isclose(
            float(signal["value"]), (recovered - injected).total_seconds(), rel_tol=0, abs_tol=1e-9
        ):
            failures.append("recoveryTimeSeconds: value does not equal the injected-to-recovered timeline")
    except ContractError as exc:
        failures.append(f"recoveryTimeSeconds: {exc}")


def _validate_saturation(signal: dict[str, Any], artifacts: set[str], failures: list[str]) -> None:
    components = _mapping(signal.get("saturationComponents"))
    values: list[float] = []
    for name in ("worker", "database", "redis"):
        component = _mapping(components.get(name))
        if not _finite(component.get("value")) or not _positive_count(component.get("sampleCount")):
            failures.append(f"saturationRatio: {name} saturation observation is missing")
            continue
        if not _refs(component.get("rawArtifactIds"), artifacts):
            failures.append(f"saturationRatio: {name} raw artifact reference is missing")
        values.append(float(component["value"]))
    if len(values) == 3 and _finite(signal.get("value")) and not math.isclose(
        float(signal["value"]), max(values), rel_tol=1e-9, abs_tol=1e-12
    ):
        failures.append("saturationRatio: gate value must equal the maximum worker/database/Redis observation")


def _validate_signals(
    lock: dict[str, Any],
    receipt: dict[str, Any],
    candidate: dict[str, Any],
    artifacts: set[str],
    workload_names: set[str],
    failures: list[str],
) -> None:
    signals = receipt.get("signals")
    if not isinstance(signals, dict):
        signals = {}
        failures.append("signals object is missing")
    required = lock.get("soak", {}).get("requiredSignals", [])
    thresholds = _mapping(lock.get("thresholds"))
    window = _mapping(receipt.get("window"))

    for name in required:
        signal = signals.get(name)
        if not isinstance(signal, dict):
            failures.append(f"{name}: missing structured signal evidence; bare numbers are inadmissible")
            continue
        if signal.get("status") != "observed":
            failures.append(f"{name}: missing, skipped, or unobserved")
        if signal.get("candidateIdentity") != candidate:
            failures.append(f"{name}: candidate identity differs from the receipt")

        query = _mapping(signal.get("query"))
        if query != _mapping(lock.get("queries")).get(name):
            failures.append(f"{name}: query differs from the frozen query in the lock")
        expression = query.get("expression")
        if not all(_nonempty(query.get(field)) for field in ("language", "expression", "version")):
            failures.append(f"{name}: query language/expression/version is missing")
        elif query.get("sha256") != hashlib.sha256(str(expression).encode()).hexdigest():
            failures.append(f"{name}: query hash does not bind the frozen expression")
        for field in ("owner", "alert", "runbook"):
            if not _nonempty(signal.get(field)):
                failures.append(f"{name}: {field} is missing")
        for field in ("alert", "runbook"):
            if _nonempty(signal.get(field)) and not _immutable_https_url(signal.get(field)):
                failures.append(f"{name}: {field} reference is not immutable HTTPS")
        if signal.get("window") != window:
            failures.append(f"{name}: observation window differs from the receipt")

        _validate_population(name, signal, failures)
        if not _refs(signal.get("rawArtifactIds"), artifacts):
            failures.append(f"{name}: raw artifact references are missing or invalid")
        dimensions = signal.get("workloadDimensions")
        if not isinstance(dimensions, list) or not dimensions or not set(dimensions).issubset(workload_names):
            failures.append(f"{name}: workload references are missing or invalid")

        value = signal.get("value")
        if not _finite(value):
            failures.append(f"{name}: value is absent or non-finite")
            continue
        threshold = _mapping(thresholds.get(name))
        operator, limit = threshold.get("operator"), threshold.get("value")
        passed = operator == "<=" and value <= limit or operator == ">=" and value >= limit
        verdict = _mapping(signal.get("thresholdVerdict"))
        if verdict != {"operator": operator, "limit": limit, "passed": passed}:
            failures.append(f"{name}: threshold verdict does not match the frozen lock and computed value")
        if not passed:
            failures.append(f"{name}: {value} violates frozen requirement {operator} {limit}")

        if name == "saturationRatio":
            _validate_saturation(signal, artifacts, failures)
        elif name == "recoveryTimeSeconds":
            _validate_recovery(signal, artifacts, window, failures)


def evaluate(
    lock: dict[str, Any],
    receipt: dict[str, Any],
    digest: str,
    expected_revision: str,
    artifact_root: Path,
    expected_image_digest: str,
) -> list[str]:
    """Return every contract failure; an empty list is the only green result."""
    failures: list[str] = []
    if not isinstance(lock, dict) or not isinstance(receipt, dict):
        return ["lock and receipt must be objects"]
    expected_ga = {"tenants", "services", "layersPerService", "featuresPerLayer", "maximumFeaturePayloadBytes", "concurrentVirtualUsers", "gpWorkers", "gpQueueDepth"}
    if set(_mapping(lock.get("supportedEnvelope"))) != expected_ga:
        failures.append("frozen envelope must cover all eight GA dimensions")
    if lock.get("excludedPreviewDimensions") != {name: {"status": "preview", "requiredForGa": False} for name in ("activeSubscriptions", "alertEvaluationsPerSecond")}:
        failures.append("both excluded Preview dimensions must be accounted for")
    if set(_mapping(lock.get("soak")).get("requiredSignals", [])) != {"availability", "errorRate", "p95LatencyMs", "p99LatencyMs", "throughputRps", "queueAgeSeconds", "saturationRatio", "recoveryTimeSeconds"}:
        failures.append("frozen contract must retain all eight SLIs")
    receipt_contract = _mapping(lock.get("receiptContract"))
    approved = _mapping(receipt_contract.get("approvedProducer"))
    if (
        receipt_contract.get("schemaVersion") != 2
        or receipt_contract.get("evidenceScope") != "single-tenant-ga"
        or receipt_contract.get("requiredRawEvidence") is not True
        or receipt_contract.get("requiredWorkloadStatus") != "exercised"
        or receipt_contract.get("previewProxyEvidenceAllowed") is not False
        or approved.get("repository") != APPROVED_PRODUCER_REPOSITORY
        or approved.get("workflowPath") != APPROVED_PRODUCER_WORKFLOW
        or approved.get("predicateType") != APPROVED_PREDICATE_TYPE
        or approved.get("runner") != "github-hosted"
    ):
        failures.append("frozen receipt contract is missing or weaker than the sourced-evidence policy")
    if receipt.get("schemaVersion") != receipt_contract.get("schemaVersion"):
        failures.append("capacity receipt schemaVersion does not match the frozen contract")
    if not expected_revision or not SHA_PATTERN.fullmatch(expected_revision):
        failures.append("expected manifest-pinned honua-server SHA is missing or invalid")
    if receipt.get("status") != "completed":
        failures.append("soak status must be completed (skipped/partial signals are failures)")
    if receipt.get("evidenceScope") != receipt_contract.get("evidenceScope"):
        failures.append("evidence scope must be single-tenant GA, not a demo-environment SLA")
    if _contains_per_tenant_slo(receipt):
        failures.append("per-tenant SLO evidence is outside this single-tenant GA contract")

    candidate = _mapping(receipt.get("candidateIdentity"))
    image_digest = str(candidate.get("imageDigest", ""))
    if candidate.get("serverRevision") != expected_revision or receipt.get("observedRevision") != expected_revision:
        failures.append("candidate revision does not match the manifest-pinned honua-server SHA")
    if not IMAGE_DIGEST_PATTERN.fullmatch(image_digest):
        failures.append("candidate image digest is missing or invalid; source-built evidence is inadmissible")
    if not IMAGE_DIGEST_PATTERN.fullmatch(expected_image_digest) or image_digest != expected_image_digest:
        failures.append("candidate image digest does not match the manifest-pinned image")
    if receipt.get("lockSha256") != digest:
        failures.append("receipt does not bind the exact committed threshold lock")
    if receipt.get("profile") != lock.get("soak", {}).get("profile"):
        failures.append("soak profile does not match the lock")
    if receipt.get("envelope") != lock.get("supportedEnvelope"):
        failures.append("tested capacity envelope does not exactly match the supported envelope")
    if receipt.get("envelope", {}).get("tenants") != 1:
        failures.append("capacity evidence must retain the single-tenant denominator")

    window = _mapping(receipt.get("window"))
    try:
        started = _time(window.get("startedAt"), "window.startedAt")
        ended = _time(window.get("endedAt"), "window.endedAt")
        frozen = max(_time(lock.get("frozenAt"), "frozenAt"), _time(receipt_contract.get("frozenAt"), "receiptContract.frozenAt"))
        if started <= frozen:
            failures.append("soak did not start after the threshold freeze")
        duration = (ended - started).total_seconds()
        if duration <= 0:
            failures.append("soak observation window is empty or reversed")
        if receipt.get("steadyStateSeconds") != duration:
            failures.append("steady-state seconds do not equal the exact UTC observation window")
        if duration < lock.get("soak", {}).get("minimumSteadyStateSeconds", 0):
            failures.append("steady-state duration is below the locked minimum")
    except ContractError as exc:
        failures.append(str(exc))

    _validate_topology(receipt, image_digest, failures)
    _validate_producer(receipt, expected_revision, failures)
    artifacts = _validate_raw_artifacts(receipt, artifact_root, failures)
    workload_names = _validate_workloads(lock, receipt, artifacts, failures)
    _validate_signals(lock, receipt, candidate, artifacts, workload_names, failures)
    failures.extend(validate_sources(lock, receipt, artifact_root))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-image-digest", required=True)
    args = parser.parse_args()
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        failures = evaluate(
            lock, receipt, lock_digest(args.lock), args.expected_revision, args.artifact_root, args.expected_image_digest
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        failures = [str(exc)]
    if failures:
        print("capacity-soak: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("capacity-soak: PASS — exact candidate, 8/8 GA workloads, 2 Preview exclusions, 8/8 sourced SLIs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
