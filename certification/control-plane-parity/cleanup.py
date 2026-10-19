"""Idempotent cleanup for one owned replay; also invoked in an always-run CI step."""
from pathlib import Path
import json
import os
import re
import subprocess

import boto3

PROJECT = "honua-sdk1424-fixed"


def validate_owned_object(owned: dict, bucket: str) -> None:
    if owned.get("bucket") != bucket or not re.fullmatch(r"sdk1424/\d{8}/[a-f0-9-]{36}/fixture\.geojson", owned.get("key", "")):
        raise ValueError("Refusing cleanup of an object outside this fixture ownership contract")


def cleanup(run: Path, root: Path, bucket: str) -> dict:
    receipt = {"status": "passed", "project": PROJECT, "workerStopped": False}
    if not (run / "ownership.json").exists():
        return {**receipt, "status": "not-started"}
    owned_run = json.loads((run / "ownership.json").read_text())
    if owned_run != {"project": PROJECT, "runDirectory": str(run), "bucket": bucket}:
        raise ValueError("Run ownership record does not match the requested cleanup")
    env = {**os.environ, **dict(line.split("=", 1) for line in (run / "run.env").read_text().splitlines() if "=" in line)}
    compose = ["docker", "compose", "-p", PROJECT, "--env-file", str(run / "run.env"), "-f", str(root / "e2e/harness/compose.console-s4.yml"), "-f", str(run / "production.yml")]
    # Before the overlay is written no containers have been started.
    if (run / "production.yml").exists():
        stopped = subprocess.run([*compose, "down", "-v", "--remove-orphans"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        receipt["workerStopped"] = stopped.returncode == 0
        if not receipt["workerStopped"]:
            receipt["status"] = "failed"
    else:
        receipt["workerStopped"] = True
    if (run / "owned-import-object.json").exists():
        owned = json.loads((run / "owned-import-object.json").read_text())
        validate_owned_object(owned, bucket)
        # Do not remove a source while a surviving worker might still read it.
        if not receipt["workerStopped"]:
            receipt["objectCleanup"] = "deferred: owned worker did not stop"
            return receipt
        s3 = boto3.client("s3", region_name="us-east-1")
        response = s3.delete_object(Bucket=bucket, Key=owned["key"])
        try:
            s3.head_object(Bucket=bucket, Key=owned["key"])
        except s3.exceptions.ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        else:
            raise RuntimeError("Owned source still exists after deletion")
        receipt["objectCleanup"] = {"bucket": bucket, "key": owned["key"], "deleteStatus": response["ResponseMetadata"]["HTTPStatusCode"], "headStatus": 404}
    return receipt


if __name__ == "__main__":
    run = Path(os.environ["HONUA_PARITY_RUN"]).resolve()
    out = Path(os.environ["HONUA_PARITY_OUT"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        receipt = cleanup(run, Path(__file__).resolve().parents[2], os.environ["HONUA_PARITY_BUCKET"])
    except Exception as exc:
        # Exception text can contain a signed URL; retain the type only here.
        receipt = {"status": "failed", "errorType": type(exc).__name__}
    (out / "always-run-cleanup.json").write_text(json.dumps(receipt, indent=2))
    if not (out / "report.json").exists():
        (out / "report.json").write_text(json.dumps({"status": "failed", "failure": "Replay did not finalize; see workflow step and cleanup verdict"}))
    raise SystemExit(1 if receipt["status"] == "failed" else 0)
