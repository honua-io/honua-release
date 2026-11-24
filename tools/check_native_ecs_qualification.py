#!/usr/bin/env python3
"""Verify the native ECS qualification envelope against the frozen train and GitHub run."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from check_journey_receipts import GA_CELLS, evaluate, load_json
from model_journey_report import build as build_model_report

CELL, = GA_CELLS
PRODUCER = ".github/workflows/native-ecs-qualification.yml"
REPORT = "gate-report-cloud.json"


def verify_run(run, repository, run_id, attempt):
    expected = {"id": int(run_id), "run_attempt": int(attempt), "head_branch": "trunk",
                "path": PRODUCER, "event": "workflow_dispatch", "status": "completed",
                "conclusion": "success"}
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")) or any(run.get(key) != value for key, value in expected.items()) or any(
        run.get(key, {}).get("full_name") != repository for key in ("repository", "head_repository")
    ):
        raise ValueError("native ECS producer identity or conclusion mismatch")


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo != timezone.utc:
        raise ValueError("qualification timestamps must be UTC")
    return stamp


def verify_envelope(root, run_id, attempt, candidate_digest, server_image, lock_digest):
    envelope = load_json(root / "qualification.json")
    expected = {
        "schemaVersion": 1, "cell": CELL, "deployment": "cloudformation-ecs-fargate",
        "runId": run_id, "runAttempt": attempt, "candidateDigest": candidate_digest,
        "serverImage": server_image, "lockDigest": lock_digest,
        "journeyReportSha256": hashlib.sha256((root / REPORT).read_bytes()).hexdigest(),
    }
    if (not re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
        or not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", server_image)
        or any(envelope.get(key) != value for key, value in expected.items())):
        raise ValueError("native ECS qualification does not match the frozen candidate and journey report")
    return envelope


def verify_installation(envelope, run, server_image, now):
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
    if not start <= installed <= deleted <= end <= (now or datetime.now(timezone.utc)):
        raise ValueError("installation and cleanup fall outside the authenticated producer attempt")
    observation = envelope["installation"]
    if (observation["stackName"] != stack.split("/")[1]
        or observation["declaredImage"] != server_image
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", observation["resolvedPlatformImageDigest"])
        or observation["observedPlatformImageDigest"] != observation["resolvedPlatformImageDigest"]
        or type(observation["healthyTaskCount"]) is not int or observation["healthyTaskCount"] < 1
        or timestamp(observation["completedAt"]) != installed
        or not start <= timestamp(observation["startedAt"]) <= installed):
        raise ValueError("native ECS installation observation does not match the frozen candidate stack")
    return installed, deleted


def verify(root, run, repository, run_id, attempt, candidate, candidate_digest, server_image, lock_digest,
           require_model, *, now=None):
    verify_run(run, repository, run_id, attempt)
    envelope = verify_envelope(root, run_id, attempt, candidate_digest, server_image, lock_digest)
    installed, deleted = verify_installation(envelope, run, server_image, now)
    if load_json(root / REPORT).get("cell") != CELL:
        raise ValueError("top-level journey report is outside the GA cell")
    journey = evaluate(root, candidate, candidate_digest, run_id, attempt, at=now)
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
    model_report = None
    if require_model:
        paths = sorted(root.glob("model-attempt-*.json"))
        try:
            model_report = build_model_report(
                paths, cell=CELL, lock_digest=lock_digest or None, candidate_digest=candidate_digest,
                run_id=run_id, run_attempt=attempt, now=now)
            journey_hashes = {
                hashlib.sha256((root / record["receipt"]).read_bytes()).hexdigest()
                for record in attempts if record["status"] == "pass"
            }
            receipts = [load_json(path) for path in paths]
            if model_report["status"] == "pass" and any(
                receipt.get("serverImage", {}).get("reference") != server_image
                or not installed <= timestamp(receipt["completedAt"]) <= deleted
                or receipt["linkedEvidence"]["deterministicReceipt"].get("sha256") not in journey_hashes
                for receipt in receipts
            ):
                raise ValueError("mismatched genuine-model qualification")
        except (KeyError, AttributeError, TypeError, ValueError, OSError) as error:
            model_report = {"status": "fail", "why": str(error), "cells": []}
    return journey, model_report


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
        journey, model = verify(args.root, run, args.repository, args.run_id, args.attempt,
                                args.candidate, args.candidate_digest, args.server_image,
                                args.lock_digest, args.require_model)
    except (KeyError, AttributeError, TypeError, ValueError, OSError) as error:
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
