"""gp_batch_native admits only a native GDAL-worker OGC job.

A managed geometry job, a busybox success, and a Redis-off refusal are not evidence.
These tests call the shipped judge. They do not apply Terraform or start a cloud run.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scenarios.gp_batch_native.scenario import SCENARIO_ID, evaluate

E2E = Path(__file__).resolve().parent


def test_busybox_success_is_not_native_evidence():
    row = evaluate({
        "status": "successful",
        "workerKind": "gdal-native",
        "engine": "gdal",
        "api": "ogc-processes",
        "image": "busybox:1.36",
        "processId": "conversion.gdal",
    })
    assert row["scenario"] == SCENARIO_ID
    assert row["status"] == "fail"
    assert "busybox" in row["why"]


def test_managed_geometry_job_is_not_native_evidence():
    row = evaluate({
        "status": "successful",
        "jobType": "managed-geometry",
        "api": "ogc-processes",
        "image": "public.ecr.aws/honua/honua-server@sha256:abc",
        "processId": "geometry.buffer",
    })
    assert row["status"] == "fail"
    assert "managed geometry" in row["why"]


def test_redis_off_refusal_is_not_native_evidence():
    row = evaluate({"status": "refused", "reason": "redis-off", "api": "ogc-processes"})
    assert row["status"] == "fail"
    assert "Redis-off" in row["why"]


def test_native_gdal_worker_ogc_job_is_the_only_pass():
    row = evaluate({
        "status": "successful",
        "workerKind": "gdal-native",
        "engine": "gdal",
        "api": "ogc-processes",
        "image": "public.ecr.aws/honua/honua-worker-gdal@sha256:def",
        "processId": "conversion.gdal",
        "artifact": {"mediaType": "image/tiff", "byteLength": 128, "sha256": "a" * 64},
    })
    assert row["status"] == "pass"
    assert row["evidence"]["processId"] == "conversion.gdal"


def test_missing_receipt_stays_blocked_and_does_not_start_a_run():
    row = evaluate(None)
    assert row["status"] == "blocked"
    assert "Terraform" in row["why"]
    assert "--require-real" in row["why"]


def test_cli_scenario_flag_reports_blocked_without_provisioning():
    completed = subprocess.run(
        [sys.executable, "run_cloud.py", "--scenario", "gp_batch_native", "--target", "aws-serverless"],
        cwd=E2E,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    row = json.loads(completed.stdout)
    assert row["scenario"] == "gp_batch_native"
    assert row["status"] == "blocked"
    assert "terraform" not in completed.stderr.lower()
