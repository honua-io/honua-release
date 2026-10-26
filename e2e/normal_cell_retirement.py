"""Use the reviewed complete-cell retirement path during ordinary Lambda cleanup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

from retire_cloud_cell import compute_absence, private_retirement_directory, require

# serverless_cell_retirement.py at honua-iac eeca0567 (#299); moves with the e2e-cloud-aws-cell.yml pin.
HELPER_SHA256 = "6d0ab71fcaac87b5fa3088f7d60f6a5a807de768ffbbdc32ea76affce1efb0ce"


def load_helper(path):
    require(path.is_file() and not path.is_symlink() and
            hashlib.sha256(path.read_bytes()).hexdigest() == HELPER_SHA256,
            "ordinary cleanup requires the exact reviewed retirement helper")
    spec = importlib.util.spec_from_file_location("serverless_cell_retirement", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def retire(target, root, variables, redis_enabled, *, loader=load_helper, session=None, sealer=None):
    import boto3
    import run_cloud

    run_id, attempt = str(target.run_id), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(os.environ.get("GITHUB_REF") == "refs/heads/trunk" and
            os.environ.get("GITHUB_REPOSITORY") == "honua-io/honua-release" and
            os.environ.get("GITHUB_RUN_ID") == run_id and run_id.isdecimal() and attempt.isdecimal(),
            "ordinary retirement requires the original protected workflow run")
    cell = "aws-serverless/redis-" + ("on" if redis_enabled else "off")
    original_key = run_cloud.state_secret_name(cell)
    recovery_key = original_key + "-retirement-" + run_id + "-" + attempt
    directory = run_cloud.cloud_journey.cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "honua.ordinary-cell-retirement/v1", "runId": run_id, "runAttempt": attempt,
               "cell": cell, "terraformRetired": False, "costReconciled": False,
               "originalStateKeyRetained": True, "recoveryStateSealed": False,
               "helperSha256": HELPER_SHA256}
    target.retirement_receipt = receipt
    try:
        iac = Path(os.environ["HONUA_IAC_DIR"]).resolve()
        require(root.resolve() == iac / "infrastructure/terraform/examples/aws-serverless",
                "retirement root differs from the original cell")
        require((root / "terraform.tfstate").is_file(), "original state is required for retirement")
        helper = loader(Path(os.environ["HONUA_SERVERLESS_RETIREMENT_HELPER"]))
        session = session or boto3.Session(region_name=target.region)
        account = session.client("sts").get_caller_identity()["Account"]
        require(account == "585192672263" and target.region == "us-east-1",
                "retirement requires the reviewed release account and region")
        private_retirement_directory(root, iac)
        result = helper.retire(root, iac / "infrastructure/terraform/modules/aws-serverless",
            run_id=run_id, cell=cell, account_id=account, region=target.region, variables=variables,
            lambda_client=session.client("lambda"), sts_client=session.client("sts"))
        require(result["terraformRetired"] is True, "complete Terraform retirement was not observed")
        receipt.update(terraformRetired=True, destroyCount=result["destroyCount"], planSha256=result["planSha256"])
        receipt["nativeAbsence"] = compute_absence(result, session)
        target._cell_dns_leftovers(redis_enabled)
        receipt["status"] = "compute-retired-cost-and-recovery-pending"
    except Exception as error:
        receipt.update(status="incomplete", failureType=type(error).__name__)
        raise
    finally:
        try:
            require((root / "terraform.tfstate").is_file(), "state must remain available for encrypted recovery")
            receipt["recoveryStateSha256"] = (sealer or run_cloud.seal_state)(
                root, directory / "state-retirement.enc", recovery_key)
            receipt.update(recoveryStateKey=recovery_key, recoveryStateSealed=True)
        except Exception as error:
            receipt.update(status="incomplete", recoveryFailureType=type(error).__name__)
            raise
        finally:
            (directory / "cell-retirement.json").write_text(json.dumps(receipt, sort_keys=True)+"\n")
