#!/usr/bin/env python3
"""Verify the native ECS qualification envelope against the frozen train and GitHub run."""
import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from image_platforms import image_platform_digests
from check_journey_receipts import GA_CELLS, evaluate, load_json
from model_journey_report import build as build_model_report

CELL, = GA_CELLS
PRODUCER = ".github/workflows/native-ecs-qualification.yml"
REPORT = "gate-report-cloud.json"


@dataclass(frozen=True)
class ProducerRun:
    repository: str
    run_id: str
    attempt: str


@dataclass(frozen=True)
class FrozenCandidate:
    manifest: Path
    digest: str
    server_image: str
    lock_digest: str


@dataclass(frozen=True)
class Qualification:
    producer: ProducerRun
    candidate: FrozenCandidate
    require_model: bool


def verify_run(run, producer):
    expected = {"id": int(producer.run_id), "run_attempt": int(producer.attempt), "head_branch": "trunk",
                "path": PRODUCER, "event": "workflow_dispatch", "status": "completed",
                "conclusion": "success"}
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")) or any(run.get(key) != value for key, value in expected.items()) or any(
        run.get(key, {}).get("full_name") != producer.repository for key in ("repository", "head_repository")
    ):
        raise ValueError("native ECS producer identity or conclusion mismatch")


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo != timezone.utc:
        raise ValueError("qualification timestamps must be UTC")
    return stamp


def verify_envelope(root, qualification, server):
    producer = qualification.producer
    candidate = qualification.candidate
    envelope = load_json(root / "qualification.json")
    expected = {
        "schemaVersion": 1, "status": "pass", "cell": CELL, "deployment": "cloudformation-ecs-fargate",
        "runId": producer.run_id, "runAttempt": producer.attempt, "candidateDigest": candidate.digest,
        "serverImage": candidate.server_image, "lockDigest": candidate.lock_digest,
        "serverSha": server["sha"], "imageDigest": server["digest"],
        "journeyReportSha256": hashlib.sha256((root / REPORT).read_bytes()).hexdigest(),
    }
    if (not re.fullmatch(r"[0-9a-f]{64}", candidate.digest)
        or candidate.server_image != server["image"].split("@", 1)[0] + "@" + server["digest"]
        or not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", candidate.server_image)
        or any(envelope.get(key) != value for key, value in expected.items())):
        raise ValueError("native ECS qualification does not match the frozen candidate and journey report")
    return envelope


def verify_installation(envelope, run, server):
    stack = envelope["stackId"]
    if not isinstance(stack, str) or not re.fullmatch(
        r"arn:aws:cloudformation:[a-z0-9-]+:[0-9]{12}:stack/honua-gate-[A-Za-z0-9-]+/[0-9a-f-]+", stack
    ):
        raise ValueError("qualification did not identify a disposable stack")
    cleanup = envelope["cleanup"]
    if (cleanup["stackId"] != stack or cleanup["stackStatus"] != "DELETE_COMPLETE"
        or cleanup["remainingResources"] != []):
        raise ValueError("cleanup did not verify deletion of the qualified stack and its resources")
    start, end = timestamp(run["run_started_at"]), timestamp(run["updated_at"])
    installed, deleted = timestamp(envelope["installedAt"]), timestamp(cleanup["observedAt"])
    if not start <= installed <= deleted <= end:
        raise ValueError("installation and cleanup fall outside the authenticated producer attempt")
    observation = envelope["installation"]
    architecture = {"X86_64": "amd64", "ARM64": "arm64"}.get(observation.get("cpuArchitecture"))
    platform_digests = image_platform_digests(
        server.get("platformDigests"), index_digest=server["digest"], architectures=server.get("architectures")
    )
    expected_digest = platform_digests.get(architecture)
    server_image = server["image"].split("@", 1)[0] + "@" + server["digest"]
    if (observation["stackName"] != stack.split("/")[1]
        or observation["declaredImage"] != server_image
        or expected_digest is None
        or observation["resolvedPlatformImageDigest"] != expected_digest
        or observation["observedPlatformImageDigest"] != observation["resolvedPlatformImageDigest"]
        or type(observation["healthyTaskCount"]) is not int or observation["healthyTaskCount"] < 1
        or timestamp(observation["completedAt"]) != installed
        or not start <= timestamp(observation["startedAt"]) <= installed):
        raise ValueError("native ECS installation observation does not match the frozen candidate stack")
    return installed, deleted


def verify(root, run, qualification, *, now=None):
    producer = qualification.producer
    candidate = qualification.candidate
    verify_run(run, producer)
    if timestamp(run["updated_at"]) > (now or datetime.now(timezone.utc)):
        raise ValueError("producer attempt ends in the future")
    server = yaml.safe_load(candidate.manifest.read_bytes())["components"]["honua-server"]
    envelope = verify_envelope(root, qualification, server)
    installed, deleted = verify_installation(envelope, run, server)
    if load_json(root / REPORT).get("cell") != CELL:
        raise ValueError("top-level journey report is outside the GA cell")
    journey = evaluate(root, candidate.manifest, candidate.digest, producer.run_id, producer.attempt, at=now)
    if journey["status"] != "pass":
        raise ValueError(journey["why"])
    attempts = next(row["attempts"] for row in journey["cells"] if row["cell"] == CELL)
    for record in attempts:
        receipt = load_json(root / record["receipt"])
        observations = [stage["evidence"]["observedAt"] for stage in receipt["stages"]
                        if stage["evidence"].get("observedAt")]
        if any(not installed <= timestamp(value) <= deleted for value in [
            receipt["generatedAt"], record["completedAt"], *observations
        ]):
            raise ValueError("installed journey observations fall outside the qualified stack lifetime")
    evidence = InstalledJourney(root, installed, deleted, attempts, now)
    model_report = None
    if qualification.require_model:
        model_report = verify_model(qualification, evidence)
    return journey, model_report


@dataclass(frozen=True)
class InstalledJourney:
    root: Path
    installed: datetime
    deleted: datetime
    attempts: list
    now: datetime | None


def verify_model(qualification, evidence):
    candidate = qualification.candidate
    producer = qualification.producer
    paths = sorted(evidence.root.glob("model-attempt-*.json"))
    try:
        report = build_model_report(
            paths, cell=CELL, lock_digest=candidate.lock_digest or None, candidate_digest=candidate.digest,
            run_id=producer.run_id, run_attempt=producer.attempt, now=evidence.now)
        if report["status"] == "pass":
            verify_model_links(paths, candidate, evidence)
        return report
    except (KeyError, AttributeError, TypeError, ValueError, OSError) as error:
        return {"status": "fail", "why": str(error), "cells": []}


def verify_model_links(paths, candidate, evidence):
    journey_hashes = {
        hashlib.sha256((evidence.root / record["receipt"]).read_bytes()).hexdigest()
        for record in evidence.attempts if record["status"] == "pass"
    }
    for path in paths:
        receipt = load_json(path)
        if (receipt.get("serverImage", {}).get("reference") != candidate.server_image
            or not evidence.installed <= timestamp(receipt["completedAt"]) <= evidence.deleted
            or receipt["linkedEvidence"]["deterministicReceipt"].get("sha256") not in journey_hashes):
            raise ValueError("mismatched genuine-model qualification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--server-image", required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--require-model", action="store_true")
    parser.add_argument("--train-run-id", required=True)
    parser.add_argument("--train-attempt", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        run = load_json(args.run)
        producer_run = ProducerRun(args.repository, args.run_id, args.attempt)
        candidate = FrozenCandidate(args.candidate, args.candidate_digest, args.server_image, args.lock_digest)
        journey, model = verify(args.root, run, Qualification(producer_run, candidate, args.require_model))
    except (KeyError, AttributeError, TypeError, ValueError, OSError, yaml.YAMLError) as error:
        parser.exit(1, f"Native ECS qualification rejected: {error}\n")
    # The train signs its verification decision. Preserve the original producer identity
    # beside it while binding the decision to the train that the nightly mint consumes.
    producer = {"repository": args.repository, "workflow": PRODUCER, "runId": args.run_id,
                "runAttempt": args.attempt, "headSha": run["head_sha"]}
    for name, report in (("deterministic", journey), ("model", model)):
        if report is None:
            continue
        report.update(runId=args.train_run_id, runAttempt=args.train_attempt, producer=producer)
        path = args.out / name / "gate-report-journey.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")
    if model is not None and model["status"] == "pass":
        for receipt in args.root.glob("model-attempt-*.json"):
            (args.out / "model" / receipt.name).write_bytes(receipt.read_bytes())
    (args.out / "qualification.json").write_bytes((args.root / "qualification.json").read_bytes())


if __name__ == "__main__":
    main()
