"""Recover an owned failed cloud-cell cleanup through a protected workflow.

Public receipts contain bindings and outcomes only. Original state, credentials,
Terraform plans and raw diagnostics remain private and are re-sealed for recovery.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

REPOSITORY = "honua-io/honua-release"
WORKFLOW = ".github/workflows/e2e-cloud-aws.yml"


class RecoveryRefused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryRefused(message)


def original_request(metadata, manifest_bytes, *, run_id, attempt, redis):
    require(re.fullmatch(r"[1-9][0-9]*", run_id or "") and
            re.fullmatch(r"[1-9][0-9]*", attempt or "") and redis in ("off", "on"),
            "invalid original run/attempt/cell")
    require(metadata.get("id") == int(run_id) and metadata.get("run_attempt") == int(attempt) and
            metadata.get("repository", {}).get("full_name") == REPOSITORY and
            metadata.get("path") == WORKFLOW and metadata.get("head_branch") == "trunk" and
            metadata.get("event") in ("workflow_dispatch", "schedule", "push") and
            metadata.get("status") == "completed",
            "original run must be a completed trunk AWS qualification run")
    head = metadata.get("head_sha", "")
    require(re.fullmatch(r"[0-9a-f]{40}", head), "original source SHA unavailable")
    manifest = yaml.safe_load(manifest_bytes)
    iac = manifest["components"]["honua-iac"]
    require(iac.get("repository") == "https://github.com/honua-io/honua-iac" and
            re.fullmatch(r"[0-9a-f]{40}", iac.get("sha", "")), "original IaC source is not immutable")
    return {"schemaVersion": "honua.cloud-cell-retirement-request/v1", "runId": run_id,
            "runAttempt": attempt, "cell": "aws-serverless/redis-" + redis,
            "sourceHead": head, "iacSource": iac["sha"],
            "candidateDigest": hashlib.sha256(manifest_bytes).hexdigest(),
            "artifactSuffix": "aws-serverless-redis-" + redis + "-" + attempt}


def verify_handoff(request, handoff):
    require(all(handoff.get(k) == request[k] for k in ("runId", "runAttempt", "cell", "candidateDigest")) and
            handoff.get("target") == "aws-serverless" and handoff.get("provisionAttempted") is True and
            isinstance(handoff.get("startedAt"), str),
            "provision handoff does not bind the original deployed run and manifest")


def preflight(args, *, run=subprocess.run):
    require(os.environ.get("GITHUB_REF") == "refs/heads/trunk", "retirement workflow must execute on trunk")
    require(re.fullmatch(r"[1-9][0-9]*", args.original_run) and
            re.fullmatch(r"[1-9][0-9]*", args.original_attempt), "invalid original run identity")

    def github(path):
        return json.loads(run(["gh", "api", "repos/" + REPOSITORY + "/" + path],
                              capture_output=True, text=True, check=True).stdout)

    # Read the latest run, not an older completed attempt while a rerun is live.
    metadata = github("actions/runs/" + args.original_run)
    head = metadata.get("head_sha", "")
    require(re.fullmatch(r"[0-9a-f]{40}", head), "original source SHA unavailable")
    file = github("contents/platform-manifest.yaml?ref=" + head)
    require(file.get("encoding") == "base64", "original manifest bytes unavailable")
    manifest = base64.b64decode(file["content"], validate=False)
    request = original_request(metadata, manifest, run_id=args.original_run, attempt=args.original_attempt, redis=args.redis)
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "original-platform-manifest.yaml").write_bytes(manifest)
    (args.directory / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key in ("sourceHead", "iacSource", "artifactSuffix"):
            output.write(key + "=" + request[key] + "\n")
    return request


def compute_absence(record, session):
    """Independent reads of the original billable compute/network identities.

    Secret recovery windows are reported explicitly; an inactive secret pending
    deletion is never described as absent. Terraform's receipt covers its full
    resource inventory, while these native observations name their narrower scope.
    """
    from botocore.exceptions import ClientError
    checks = {
        "aws_lambda_function": ("lambda", "get_function", "FunctionName", None),
        "aws_db_instance": ("rds", "describe_db_instances", "DBInstanceIdentifier", "DBInstances"),
        "aws_elasticache_replication_group": ("elasticache", "describe_replication_groups", "ReplicationGroupId", "ReplicationGroups"),
        "aws_batch_compute_environment": ("batch", "describe_compute_environments", "computeEnvironments", "computeEnvironments"),
        "aws_batch_job_queue": ("batch", "describe_job_queues", "jobQueues", "jobQueues"),
        "aws_vpc": ("ec2", "describe_vpcs", "VpcIds", "Vpcs"),
        "aws_eip": ("ec2", "describe_addresses", "AllocationIds", "Addresses"),
        "aws_nat_gateway": ("ec2", "describe_nat_gateways", "NatGatewayIds", "NatGateways"),
    }
    missing = {"ResourceNotFoundException", "DBInstanceNotFound", "ReplicationGroupNotFoundFault",
               "InvalidVpcID.NotFound", "InvalidAllocationID.NotFound", "NatGatewayNotFound"}
    observed, pending = [], []
    for resource in record["resources"]:
        kind, identity = resource["type"], resource["id"]
        if kind == "aws_secretsmanager_secret":
            try:
                secret = session.client("secretsmanager").describe_secret(SecretId=identity)
                require(secret.get("DeletedDate"), "original secret is still active after Terraform retirement")
                pending.append({"arn": identity, "deletedDate": secret["DeletedDate"].isoformat()})
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
            continue
        if kind not in checks:
            continue
        service, method, argument, collection = checks[kind]
        value = [identity] if argument in ("computeEnvironments", "jobQueues", "VpcIds", "AllocationIds", "NatGatewayIds") else identity
        try:
            response = getattr(session.client(service), method)(**{argument: value})
            rows = response.get(collection, []) if collection else [response]
            if kind == "aws_nat_gateway":
                rows = [row for row in rows if row.get("State") != "deleted"]
            require(not rows, "original compute/network resource remains: " + resource["address"])
        except ClientError as error:
            if error.response["Error"]["Code"] not in missing:
                raise
        observed.append({"address": resource["address"], "type": kind, "absent": True})
    require(observed, "no independent compute/network identities were inspected")
    return {"scope": sorted(checks), "resources": observed, "pendingSecretDeletion": pending}


def private_retirement_directory(root, iac):
    # Recovery uses the exact old source, whose ignore rules may predate the
    # helper. Protect private evidence locally without changing tracked IaC.
    exclude = Path(subprocess.run(["git", "-C", str(iac), "rev-parse", "--path-format=absolute",
                                  "--git-path", "info/exclude"],
                                 capture_output=True, text=True, check=True).stdout.strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as stream:
        stream.write("\n**/.release-cell-retirement/\n**/release_cell_retirement_override.tf.json\n")
    private = root / ".release-cell-retirement"
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    return private


def perform(args):
    import boto3
    import cost_meter
    import run_cloud
    from targets.terraform_target import serverless

    request = json.loads((args.directory / "request.json").read_text())
    require((args.original_run, args.original_attempt, "aws-serverless/redis-" + args.redis) ==
            (request["runId"], request["runAttempt"], request["cell"]), "retirement inputs changed after preflight")
    handoff = json.loads((args.handoff / "provision.json").read_text())
    verify_handoff(request, handoff)
    require(os.environ.get("GITHUB_REF") == "refs/heads/trunk", "retirement workflow must execute on trunk")
    current_run, current_attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    require(current_run != request["runId"], "recovery must have its own workflow identity")
    latest = json.loads(subprocess.run(["gh", "api", "repos/" + REPOSITORY + "/actions/runs/" + request["runId"]],
                                      capture_output=True, text=True, check=True).stdout)
    require(original_request(latest, (args.directory / "original-platform-manifest.yaml").read_bytes(),
                             run_id=args.original_run, attempt=args.original_attempt, redis=args.redis) == request,
            "original run changed after preflight")
    session = boto3.Session(region_name="us-east-1")
    account = session.client("sts").get_caller_identity()["Account"]
    require(account == args.account_id, "retirement AWS account differs from the reviewed account")
    os.environ.update(GITHUB_RUN_ID=request["runId"], GITHUB_RUN_ATTEMPT=request["runAttempt"],
                      AWS_DEFAULT_REGION="us-east-1", HONUA_IAC_DIR=str(args.iac.resolve()))
    target = serverless(run_id=request["runId"], region="us-east-1")
    root = target._iac_root()
    require(root is not None, "original IaC root unavailable")
    expected = subprocess.run(["git", "-C", str(args.iac), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    require(expected == request["iacSource"], "Terraform resources must use the original immutable IaC source")
    spec = importlib.util.spec_from_file_location("serverless_cell_retirement", args.helper)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    key_name = run_cloud.state_secret_name(request["cell"])
    recovery_name = key_name + "-retirement-" + current_run + "-" + current_attempt
    receipt = {**request, "recoveryRunId": current_run, "recoveryRunAttempt": current_attempt,
               "status": "incomplete", "terraformRetired": False, "costReconciled": False,
               "originalStateKeyRetained": True, "recoveryStateSealed": False}
    try:
        run_cloud.open_state(root, args.bundle, key_name)
        private = private_retirement_directory(root, args.iac)
        state = json.loads((root / "terraform.tfstate").read_text())
        rows = [{**instance["attributes"], "resourceType": group["type"], "resourceName": group["name"]}
                for group in state["resources"] if group["mode"] == "managed" for instance in group["instances"]]
        function = next(row for row in rows if row["resourceType"] == "aws_lambda_function" and row["resourceName"] == "this")
        job = next(row for row in rows if row["resourceType"] == "aws_batch_job_definition" and row["resourceName"] == "gp")
        os.environ["HONUA_LAMBDA_IMAGE_URI"] = function["image_uri"]
        os.environ["HONUA_LAMBDA_ARCHITECTURE"] = function["architectures"][0]
        os.environ["HONUA_GP_BATCH_IMAGE"] = json.loads(job["container_properties"])["image"]
        initialized = subprocess.run(["terraform", f"-chdir={root}", "init", "-input=false", "-no-color"],
                                     text=True, capture_output=True)
        with os.fdopen(os.open(private / "terraform-init.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as log:
            log.write(initialized.stdout + initialized.stderr)
        require(initialized.returncode == 0, "original Terraform initialization failed; private diagnostics retained")
        target._workdir = root
        try:
            ceilings = {row.get("tags_all", {}).get("honua-release:cost-ceiling-usd") for row in rows if row.get("tags_all")}
            require(len(ceilings) == 1 and None not in ceilings, "original cell cost ceiling is unavailable or inconsistent")
            ceiling = ceilings.pop()
            receipt["originalCellCostCeilingUsd"] = ceiling
            receipt["costBeforeRetirement"] = cost_meter.cell_cost(target, handoff, ceiling, run_id=request["runId"],
                                                                 run_attempt=request["runAttempt"])
        except Exception as error:
            # Metering failure never prevents cleanup and never becomes zero.
            receipt["costBeforeRetirement"] = {"status": "unavailable", "failureType": type(error).__name__}
        variables = target._read_transition_vars(root, args.redis == "on") or target._vars(args.redis == "on", destroy=True)
        record = helper.retire(root, args.iac / "infrastructure/terraform/modules/aws-serverless",
                               run_id=request["runId"], cell=request["cell"], account_id=account, region="us-east-1",
                               variables=variables, lambda_client=session.client("lambda"), sts_client=session.client("sts"))
        receipt["terraformRetired"] = record["terraformRetired"]
        receipt["destroyCount"] = record["destroyCount"]
        receipt["planSha256"] = record["planSha256"]
        receipt["nativeAbsence"] = compute_absence(record, session)
        target._cell_dns_leftovers(args.redis == "on")
        receipt["status"] = "compute-retired-cost-and-recovery-pending"
    except Exception as error:
        receipt["failureType"] = type(error).__name__
        raise
    finally:
        if (root / "terraform.tfstate").exists():
            try:
                # The original key is never overwritten or deleted. Preserve the
                # new state under this recovery attempt, even after partial apply.
                receipt["recoveryStateSha256"] = run_cloud.seal_state(root, args.directory / "state-recovery.enc", recovery_name)
                receipt["recoveryStateKey"] = recovery_name
                receipt["recoveryStateSealed"] = True
            except Exception as error:
                receipt.update(status="incomplete", recoverySealFailure=type(error).__name__)
        (args.directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        os.environ.update(GITHUB_RUN_ID=current_run, GITHUB_RUN_ATTEMPT=current_attempt)
    require(receipt["recoveryStateSealed"], "updated state could not be sealed; original cleanup obligation remains open")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "retire"))
    parser.add_argument("--original-run", required=True)
    parser.add_argument("--original-attempt", required=True)
    parser.add_argument("--redis", choices=("off", "on"), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--iac", type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--account-id")
    args = parser.parse_args()
    if args.phase == "preflight":
        preflight(args)
    else:
        require(all((args.iac, args.helper, args.bundle, args.handoff, args.account_id)), "retirement inputs are incomplete")
        perform(args)


if __name__ == "__main__":
    main()
