"""Write public raw EKS proof from persisted before-actuation inputs after owned teardown."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def bounded_text(path: Path, maximum=4 * 1024 * 1024) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("missing, symlinked or oversized deployment proof input")
    return path.read_text(encoding="utf-8")


def write_receipt(report: dict, directory: Path, e2e_dir: Path, *, environ=None) -> Path:
    """A raw receipt is not qualification: the consumer verifies original Actions artifacts."""
    env = os.environ if environ is None else environ
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    source = env.get("GITHUB_SHA", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    if (not run_id.isdigit() or int(run_id) < 1 or not attempt.isdigit() or int(attempt) < 1
            or not re.fullmatch(r"[0-9a-f]{40}", source)
            or repository != "honua-io/honua-release"):
        raise ValueError("actual release producer identity is required")
    inputs = json.loads(bounded_text(directory / "deployment-execution-inputs.json"))
    expected_producer = {"repository": repository, "sourceRevision": source,
                         "workflow": ".github/workflows/e2e-cloud-aws.yml"}
    if (inputs.get("schema") != "honua.deployment-execution-inputs/v1"
            or inputs.get("deployment", {}).get("producer") != expected_producer
            or inputs["deployment"].get("target") != "awsEks"):
        raise ValueError("persisted EKS capture belongs to another producer")
    mode = inputs["deployment"]["configuration"].get("redisMode")
    cell = f"aws-eks/redis-{mode}"
    expected_directory = e2e_dir / "cloud-evidence" / run_id / attempt / cell
    if mode not in {"off", "on"} or directory.resolve() != expected_directory.resolve() or report.get("cell") != cell:
        raise ValueError("persisted EKS capture belongs to another run or cell")
    documents = {}
    for record in report.get("journeyAttempts", []):
        number = record.get("number")
        if type(number) is not int or number not in (1, 2):
            raise ValueError("invalid journey attempt")
        path = directory / f"receipt-{number}.json"
        relative = path.relative_to(e2e_dir).as_posix()
        if record.get("receipt") != relative or relative in documents:
            raise ValueError("journey receipt is not owned by this cell")
        text = bounded_text(path)
        if hashlib.sha256(text.encode()).hexdigest() != record.get("receiptSha256"):
            raise ValueError("journey receipt bytes changed")
        documents[relative] = text
    producer = {**expected_producer, "runId": run_id, "runAttempt": int(attempt),
                "runUrl": f"https://github.com/{repository}/actions/runs/{run_id}"}
    result = {"schema": "honua.eks-deployment-cell/v1", "result": report.get("status", "fail"),
              "executionInputs": {"schema": inputs["schema"],
                                  "digest": "sha256:" + hashlib.sha256(canonical_bytes(inputs)).hexdigest()},
              "producer": producer, "runUrl": producer["runUrl"],
              "report": copy.deepcopy(report),
              "actuation": json.loads(bounded_text(directory / "deployment-actuation.json")),
              "executedManifestYaml": bounded_text(directory / "executed-manifest.yaml"),
              "journeyReceiptBytes": documents}
    path = directory / "deployment-cell-receipt.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return path
