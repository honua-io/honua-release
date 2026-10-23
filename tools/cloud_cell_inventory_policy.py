#!/usr/bin/env python3
"""Import and review one fixed inventory-policy update through Terraform only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "certification/cloud-cell-inventory"
ROLE = "honua-release-cicd"
POLICY = "release-cicd-guardrails"
ADDRESS = "aws_iam_role_policy.inventory"
READ_ACTION = "lambda:ListVersionsByFunction"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def desired_policy(before):
    after = copy.deepcopy(before)
    rows = [row for row in after["Statement"] if row["Sid"] == "DenyStandingTaggedResources"]
    require(len(rows) == 1 and rows[0] == {
        "Sid": "DenyStandingTaggedResources", "Effect": "Deny", "Action": "*", "Resource": "*",
        "Condition": {"StringEquals": {"aws:ResourceTag/Environment": ["cert", "demo"]}},
    }, "standing policy snapshot differs")
    del rows[0]["Action"]
    rows[0]["NotAction"] = [READ_ACTION]
    return after


def validate_plan(plan, original, desired, observed):
    require(observed in (original, desired), "unreviewed live policy drift")
    changes = plan.get("resource_changes", [])
    require(len(changes) == 1, "plan must contain exactly the fixed inline policy")
    row = changes[0]
    require(row.get("address") == ADDRESS and row.get("mode") == "managed"
            and row.get("type") == "aws_iam_role_policy", "wrong resource identity")
    change = row["change"]
    require(change.get("actions") in (["update"], ["no-op"]), "only in-place update or unchanged import is allowed")
    require(change.get("importing", {}).get("id") == ROLE + ":" + POLICY, "fixed import identity required")
    for name, expected in (("before", observed), ("after", desired)):
        values = change.get(name) or {}
        require(values.get("name") == POLICY and values.get("role") == ROLE, "role or policy identity changed")
        require(json.loads(values.get("policy", "null")) == expected, "planned policy differs from reviewed bytes")
    require(not change.get("replace_paths"), "replacement is forbidden")
    require(not plan.get("deferred_changes"), "deferred changes are forbidden")
    require(not plan.get("resource_drift"), "unexpected resource drift")
    return {"importId": ROLE + ":" + POLICY, "actions": change["actions"],
            "beforeSha256": digest(observed), "afterSha256": digest(desired)}


def run(mode):
    require(mode in ("plan", "deploy"), "unknown deployment mode")
    require(os.environ.get("GITHUB_REPOSITORY") == "honua-io/honua-iac"
            and os.environ.get("GITHUB_REF") == "refs/heads/trunk"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_WORKFLOW_REF") ==
            "honua-io/honua-iac/.github/workflows/release-cell-inventory-policy.yml@refs/heads/trunk",
            "only the checked protected-trunk deployment workflow may execute")
    import boto3
    require(boto3.client("sts").get_caller_identity()["Account"] == "585192672263", "wrong account")
    iam = boto3.client("iam", region_name="us-east-1")
    original = json.loads((SOURCE / "guardrails.before.json").read_text())
    desired = desired_policy(original)

    def observe():
        return iam.get_role_policy(RoleName=ROLE, PolicyName=POLICY)["PolicyDocument"]

    observed = observe()
    require(observed in (original, desired), "live policy changed; reconcile source before deployment")
    private = Path(os.environ["RUNNER_TEMP"]) / "cell-inventory-private"
    private.mkdir(mode=0o700, exist_ok=False)
    work = private / "root"
    shutil.copytree(SOURCE, work)
    log = private / "terraform.log"

    def terraform(*args, output=None):
        with log.open("ab") as errors:
            if output is None:
                subprocess.run(["terraform", "-chdir=" + str(work), *args], check=True, stdout=errors, stderr=errors)
            else:
                with output.open("wb") as stream:
                    subprocess.run(["terraform", "-chdir=" + str(work), *args], check=True, stdout=stream, stderr=errors)

    terraform("init", "-backend=false", "-input=false", "-lockfile=readonly", "-no-color")
    terraform("validate", "-no-color")
    terraform("plan", "-input=false", "-out=reviewed.tfplan", "-no-color")
    plan_path = private / "plan.json"
    terraform("show", "-json", "reviewed.tfplan", output=plan_path)
    review = validate_plan(json.loads(plan_path.read_text()), original, desired, observed)
    saved = work / "reviewed.tfplan"
    plan_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
    review.update({"schema": "honua.cloud-cell-inventory-policy/v1", "mode": mode,
                   "sourceSha": os.environ["RELEASE_ROOT_SHA"], "runId": os.environ["GITHUB_RUN_ID"],
                   "savedPlanSha256": plan_hash, "applyAttempted": False,
                   "applied": False, "observedDesired": observed == desired})
    receipt = Path(os.environ["RUNNER_TEMP"]) / "cell-inventory-review.json"
    receipt.write_text(json.dumps(review, indent=2) + "\n")
    if mode == "deploy":
        require(observe() == observed, "live policy changed after planning")
        require(hashlib.sha256(saved.read_bytes()).hexdigest() == plan_hash, "saved plan changed")
        review.update({"applyAttempted": True, "applied": None, "observedDesired": None})
        receipt.write_text(json.dumps(review, indent=2) + "\n")
        terraform("apply", "-input=false", "-no-color", "reviewed.tfplan")
        review["applied"] = True
        review["observedDesired"] = observe() == desired
        receipt.write_text(json.dumps(review, indent=2) + "\n")
        require(review["observedDesired"], "live policy readback differs")
    print("Reviewed exactly one fixed inline policy; metadata-only receipt retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "deploy"))
    run(parser.parse_args().mode)
