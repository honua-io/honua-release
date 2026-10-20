"""Independent bounded Redis job/result readback for direct cancellation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_state import redis_command, snapshot
from mcp_execute_plan_validation import snapshot_output
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate


def observe(plan_id, job_id, source, image, *, read=redis_command, census=snapshot,
            inspect=inspect_candidate, clock=lambda: datetime.now(timezone.utc)):
    """Retain selected public provenance, never the job's raw parameter/secret bytes."""
    if (not isinstance(plan_id, str) or not re.fullmatch(r"mcp-direct-cancel-[a-f0-9]{24}", plan_id)
            or job_id is not None and (not isinstance(job_id, str)
                                    or not re.fullmatch(r"gp-[a-f0-9]{24,32}", job_id))):
        raise ValueError("direct cancel observation identity is malformed")
    before = inspect(source, image)
    if (not isinstance(before, dict) or before.get("running") is not True
            or before.get("candidateSourceSha") != source or before.get("imageReference") != image):
        raise ValueError("direct cancel store belongs to another candidate")
    started = clock().isoformat()
    value = census(plan_id)
    snapshot_output(value)
    job = None
    if job_id is not None:
        raw = read(["GET", "controlplane:job:" + job_id])
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 8 * 1024 * 1024:
            raise ValueError("owned durable job record is absent or unbounded")
        record = strict_json(raw)
        rows = [row for row in value["jobs"] if row["jobId"] == job_id]
        if (len(rows) != 1 or rows[0]["sha256"] != "sha256:" + hashlib.sha256(raw).hexdigest()
                or not isinstance(record, dict) or record.get("operationId") != job_id
                or type(record.get("status")) is not int or record["status"] not in range(6)
                or not isinstance(record.get("audit"), dict)
                or not isinstance(record.get("spec"), dict)):
            raise ValueError("owned durable job differs from atomic census")
        audit, spec = record["audit"], record["spec"]
        params = spec.get("parameters")
        if (not isinstance(params, dict) or params.get("honua.geoprocessing.plan_id") != plan_id
                or not isinstance(audit.get("requestedBy"), str)
                or not audit["requestedBy"].startswith("ApiKey:api-key:")
                or not isinstance(audit.get("idempotencyKey"), str)):
            raise ValueError("durable job lacks authored plan or canonical actor")
        job = {"jobId": job_id, "recordSha256": rows[0]["sha256"],
               "status": record["status"], "createdAt": record.get("createdAt"),
               "updatedAt": record.get("updatedAt"), "completedAt": record.get("completedAt"),
               "requestedBy": audit["requestedBy"], "tenantId": audit.get("tenantId"),
               "idempotencyKey": audit["idempotencyKey"],
               "processDefinitions": params.get("honua.geoprocessing.process_definitions"),
               "planId": params["honua.geoprocessing.plan_id"],
               "artifactReferences": record.get("artifactReferences"),
               "committedEffectReferences": record.get("committedEffectReferences")}
        if (not isinstance(job["artifactReferences"], list)
                or not isinstance(job["committedEffectReferences"], list)):
            raise ValueError("durable artifact references are malformed")
    completed = clock().isoformat()
    if inspect(source, image) != before:
        raise ValueError("direct cancel runtime changed during store read")
    return {"schema": "honua.mcp-direct-cancel-store/v1", "runtimeBinding": before,
            "startedAt": started, "completedAt": completed, "value": value, "job": job}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(observe(args.plan_id, args.job_id, args.candidate_source_sha,
                                 args.candidate_image), allow_nan=False, sort_keys=True))
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        print("owned direct cancel state refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
