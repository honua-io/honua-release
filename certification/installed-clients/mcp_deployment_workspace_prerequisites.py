"""Create the real owned GP workspace prerequisite through GPServer.

This is fixture setup, not a substitute for the primary installed MCP resource
read. It neither inserts workspace rows nor bypasses the canonical executor.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_deployment_workspace_state import identifier
from mcp_execute_plan_state import redis_command
from mcp_ingest_publish_state import database_query
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate

ORIGIN = "http://127.0.0.1:8080"


def polygon():
    return base64.b64encode(struct.pack("<BIII10d", 1, 3, 1, 5, 0, 0, 3, 0, 3, 4, 0, 4, 0, 0)).decode()


def request(path, credential, *, form=None, open=urlopen):
    if (not isinstance(path, str) or not path.startswith("/rest/services/") or ".." in path or "#" in path
            or not isinstance(credential, str) or not credential or "\r" in credential or "\n" in credential):
        raise ValueError("workspace fixture target/credential refused")
    data = urlencode(form).encode() if form is not None else None
    incoming = Request(ORIGIN + path, data=data, headers={"X-API-Key": credential, "Accept": "application/json"},
                       method="POST" if data is not None else "GET")
    # urllib's default redirect handling must not forward the private key.
    from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("workspace fixture redirect refused")
    if open is urlopen:
        open = build_opener(ProxyHandler({}), NoRedirect()).open
    with open(incoming, timeout=10) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024 or not response.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ValueError("actual GP workspace HTTP response refused")
    value = strict_json(raw)
    if not isinstance(value, dict) or "error" in value:
        raise ValueError("canonical GP workspace prerequisite failed")
    return value, {"path": path, "method": incoming.method, "requestBodyBase64": base64.b64encode(data or b"").decode(),
                   "requestBodySha256": "sha256:" + hashlib.sha256(data or b"").hexdigest(), "credentialPresent": True,
                   "responseStatus": 200, "responseBodyBase64": base64.b64encode(raw).decode(),
                   "responseBodySha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def create(service_id, label, *, credential, password, http=request, redis=redis_command, sql=database_query, sleep=time.sleep):
    identifier(service_id); identifier(label)
    base = "/rest/services/" + quote(service_id, safe="") + "/GPServer/geometry.area"
    form = {"f": "json", "wkb": polygon(), "srid": "3857", "env:workspace": label, "env:overwriteOutput": "false"}
    submitted, submitted_wire = http(base + "/submitJob", credential, form=form)
    job_id = identifier(submitted.get("jobId"))
    polls = []
    for _ in range(30):
        status, wire = http(base + "/jobs/" + job_id + "?f=json", credential)
        polls.append(wire)
        if status.get("jobId") != job_id:
            raise ValueError("actual GP job identity changed")
        state = status.get("jobStatus")
        if state == "esriJobSucceeded":
            break
        if state in ("esriJobFailed", "esriJobCancelled", "esriJobTimedOut"):
            raise ValueError("actual GP workspace job did not succeed")
        if state not in ("esriJobSubmitted", "esriJobWaiting", "esriJobExecuting", "esriJobNew"):
            raise ValueError("actual GP workspace job state unknown")
        sleep(2)
    else:
        raise ValueError("actual GP workspace job observation exhausted")
    output, output_wire = http(base + "/jobs/" + job_id + "/results/outputScalar?f=json", credential)
    raw = redis(["GET", "controlplane:job:" + job_id])
    if not isinstance(raw, bytes):
        raise ValueError("real GP job was not durably observed")
    job = strict_json(raw)
    audit = job.get("audit", {}) if isinstance(job, dict) else {}
    security = audit.get("submitterSecurityContext") or {}
    owner, scope = security.get("workspaceOwnerId") or audit.get("requestedBy"), security.get("tenantId")
    if job.get("operationId") != job_id or type(job.get("status")) is not int or job["status"] != 3 or not isinstance(owner, str) or not owner:
        raise ValueError("actual GP job owner/lifecycle missing")
    # Only the canonical dispatcher creates this unique label. Read its actual
    # durable identity instead of treating the caller's label as a workspace ID.
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    query = "SET TRANSACTION READ ONLY; SELECT COALESCE(json_agg(row_to_json(w)), '[]'::json)::text FROM (SELECT workspace_id,owner_id,scope_id FROM honua.gp_workspaces WHERE label=" + literal(label) + ") w;"
    rows = strict_json(sql(query, password))
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("owner_id") != owner or rows[0].get("scope_id") != scope:
        raise ValueError("actual workspace owner/scope/label join ambiguous")
    workspace_id = identifier(rows[0].get("workspace_id"))
    return {"schema": "honua.mcp-gp-workspace-prerequisite/v1", "serviceId": service_id, "label": label,
            "workspaceId": workspace_id, "jobId": job_id, "ownerId": owner, "scopeId": scope,
            "submitted": submitted_wire, "polls": polls, "output": output_wire,
            "durableJobSha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source-sha", required=True); parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--service-id", required=True); parser.add_argument("--label", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        before = inspect_candidate(args.candidate_source_sha, args.candidate_image)
        value = create(args.service_id, args.label, credential=os.environ.get("HONUA_MCP_API_KEY"), password=os.environ.get("HONUA_CERT_DB_PASSWORD"))
        if inspect_candidate(args.candidate_source_sha, args.candidate_image) != before:
            raise ValueError("candidate changed during real workspace setup")
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.SubprocessError):
        print("real canonical workspace prerequisite refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
