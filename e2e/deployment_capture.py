"""Before-actuation EKS inputs for the prepared deployment qualification producer.

Only the prepared workflow opts in. Every stage binds the exact arguments consumed by
that subprocess; Helm outputs are captured after Terraform, before Helm executes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def enabled():
    return os.environ.get("HONUA_DEPLOYMENT_PREPARED") == "true"


def source(path):
    sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("deployment checkout has no immutable source")
    return sha


SECRET_ARN = r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+"


def secret_versions(arguments, *, run=subprocess.run):
    """Read immutable identities, honoring any version already pinned in a reference."""
    references = sorted(set(re.findall(SECRET_ARN + r"(?:\?versionId=[A-Za-z0-9-]+)?",
                                      json.dumps(arguments))))
    versions = []
    for reference in references:
        arn, _, query = reference.partition("?")
        expected = urllib.parse.parse_qs(query).get("versionId", [None])[0]
        command = ["aws", "secretsmanager", "get-secret-value", "--secret-id", arn]
        if expected is not None:
            command += ["--version-id", expected]
        result = run([*command, "--query", "VersionId", "--output", "text"],
                     capture_output=True, text=True, check=True)
        version = result.stdout.strip()
        if (not re.fullmatch(r"[A-Za-z0-9-]{32,64}", version)
                or (expected is not None and version != expected)):
            raise ValueError("deployment secret has no matching immutable version")
        versions.append({"arn": arn, "versionId": version})
    return versions


def pin_secret_references(environment, *, run=subprocess.run):
    """Resolve floating server references once, then consume explicit version references."""
    result = dict(environment)
    for name, value in environment.items():
        if not isinstance(value, str) or not value.startswith("aws:secretsmanager:"):
            continue
        if not re.fullmatch("aws:secretsmanager:" + SECRET_ARN + r"(?:\?versionId=[A-Za-z0-9-]+)?", value):
            raise ValueError("prepared EKS secret reference must use an exact ARN and optional versionId")
        identity, = secret_versions([value], run=run)
        result[name] = "aws:secretsmanager:" + identity["arn"] + "?versionId=" + identity["versionId"]
    return result


def capture(target, stage, configuration, redis_enabled):
    if not enabled():
        return
    import cloud_journey
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.deployment_qualification import execution_inputs

    if stage not in ("terraform", "helm"):
        raise ValueError("unsupported deployment stage")
    cell = f"aws-eks/redis-{'on' if redis_enabled else 'off'}"
    directory = cloud_journey.cell_dir(cell)
    previous = getattr(target, "_deployment_terraform_capture", None)
    if stage == "helm" and previous is None:
        raise ValueError("Helm inputs require the original Terraform capture")
    entry = {"schema": "honua.deployment-stage-inputs/v1", "stage": stage,
             "capturedAt": now(), "configuration": configuration,
             "previousStageDigest": digest(previous) if stage == "helm" else None}
    write(directory / f"deployment-{stage}-inputs.json", entry)
    if stage == "terraform":
        (directory / "executed-manifest.yaml").write_bytes((ROOT / "platform-manifest.yaml").read_bytes())
        target._deployment_terraform_capture = entry
    else:
        revision = source(ROOT)
        if revision != os.environ.get("GITHUB_SHA"):
            raise ValueError("deployment producer source differs from its Actions source")
        workflow = os.environ.get("GITHUB_WORKFLOW_REF", "")
        prefix = "honua-io/honua-release/"
        if not workflow.startswith(prefix) or "@" not in workflow:
            raise ValueError("deployment workflow identity is unavailable")
        workflow = workflow[len(prefix):].split("@", 1)[0]
        inputs = execution_inputs(cloud_journey.manifest(), {
            "target": "awsEks", "component": "honua-helm", "architecture": "x86_64",
            "producer": {"repository": "honua-io/honua-release", "sourceRevision": revision,
                         "workflow": workflow},
            "operatingEnvelope": {"path": "docs/2026.1-operating-envelope.md", "sha256": "sha256:" + hashlib.sha256(
                (ROOT / "docs/2026.1-operating-envelope.md").read_bytes()).hexdigest()},
            "configuration": {"redisMode": "on" if redis_enabled else "off",
                              "stages": {"terraform": previous, "helm": entry}}})
        target.deployment_execution_inputs = inputs
        write(directory / "deployment-execution-inputs.json", inputs)


def actuating(target, stage, redis_enabled):
    if not enabled():
        return
    import cloud_journey
    milestones = getattr(target, "deployment_actuation", {})
    milestones[f"{stage}StartedAt"] = now()
    target.deployment_actuation = milestones
    write(cloud_journey.cell_dir(f"aws-eks/redis-{'on' if redis_enabled else 'off'}")
          / "deployment-actuation.json", milestones)
