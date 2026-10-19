"""Fixed owned-candidate console workflow setup for canonical publish_result.

Only public supported authoring/run endpoints create work. Redis/SQL access is
read-only independent observation. Never inserts a workflow, job or result.
"""
from __future__ import annotations
import argparse
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import strict_json, same_json
from mcp_publish_result_validation import (scenario, workflow_output, package_output, version_output,
    validation_output, publication_record, publication_inventory, definition_output, safe_response)
from mcp_publish_result_state import workflow_state, before_run as observe_before_run, connection_observation
from mcp_render_validation import inspect_candidate


def execute_setup(authored, credential, password, salt, receipt, persist):
    if (not isinstance(credential, str) or not credential or len(credential) > 8192 or any(ord(c) < 32 for c in credential)
            or not isinstance(password, str) or not password or not re.fullmatch(r"[a-f0-9]{32}", salt)):
        raise ValueError("workflow setup requires private credential environment")
    fingerprint = "scrypt:"+salt+":"+hashlib.scrypt(credential.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()
    receipt.update(schema="honua.mcp-publish-result-setup/v1", phase="preflight", steps=[], workflow=None,
                   connection=None, beforeRun=None, runtimeBefore=None, runtimeAfter=None)
    receipt["runtimeBefore"] = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
    if not same_json({**receipt["runtimeBefore"], "startedAt": authored["runtimeBinding"]["startedAt"]}, authored["runtimeBinding"]):
        raise ValueError("workflow setup candidate changed since bootstrap")
    receipt["connection"] = connection_observation(authored, password)
    persist()
    def request(identifier, method, path, arguments, expected_status):
        if not path.startswith("/api/v1/console/") or not path.isascii() or re.search(r"[^A-Za-z0-9_./:-]", path):
            raise ValueError("workflow setup path outside fixed supported surface")
        receipt["phase"] = identifier; persist()
        payload = json.dumps(arguments, allow_nan=False).encode() if arguments is not None else None
        connection = HTTPConnection("127.0.0.1", 8080, timeout=30)
        try:
            connection.request(method, path, body=payload, headers={"X-API-Key": credential, "Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read(262145)
            media = response.getheader("Content-Type", "").split(";",1)[0].strip().lower()
            if len(raw) > 262144 or media not in ("application/json", "application/problem+json"):
                receipt["refusedResponse"] = {"phase": identifier, "status": response.status, "truncated": len(raw) > 262144,
                                               "observedPrefixSha256": "sha256:"+hashlib.sha256(raw).hexdigest()}; persist()
                raise ValueError("workflow setup response is oversized or has wrong media")
            text = raw.decode("utf-8")
            try:
                body = safe_response(text, (credential, password))
            except (ValueError, UnicodeError):
                receipt["refusedResponse"] = {"phase": identifier, "status": response.status,
                                               "responseSha256": "sha256:"+hashlib.sha256(raw).hexdigest()}; persist()
                raise ValueError("workflow setup response refused before unsafe retention") from None
            receipt["steps"].append({"id": identifier, "method": method, "path": path, "arguments": arguments,
                "authenticated": True, "credentialFingerprint": fingerprint, "status": response.status, "responseMediaType": media,
                "responseText": text, "responseSha256": "sha256:"+hashlib.sha256(raw).hexdigest()})
            persist()
            if response.status != expected_status or not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), dict):
                raise ValueError("actual workflow setup request refused")
            return body["data"]
        finally:
            connection.close()
    root = "/api/v1/console"
    package = request("package", "POST", root+"/workflow-packages", authored["packageArguments"], 201)
    expected_hash = package_output(package, authored)
    base = root+"/workflow-packages/"+authored["packageId"]+"/versions"
    version = request("version", "POST", base, None, 201)
    version_output(version, authored, expected_hash)
    validation_output(request("validate", "POST", base+"/1/validate", None, 200), expected_hash)
    publication = request("publication", "POST", base+"/1/publish", authored["publicationArguments"], 200)
    pubid, definitionid = publication_record(publication, authored, expected_hash)
    publication_inventory(request("publication-read", "GET", root+"/workflow-publications", None, 200), publication)
    receipt["phase"] = "before-run"
    receipt["beforeRun"] = observe_before_run(definitionid)
    definition_output(receipt["beforeRun"]["definition"], authored, definitionid)
    persist()
    run = request("run", "POST", root+"/workflow-publications/"+pubid+"/runs", {}, 201)
    runid = run.get("workflowRunId")
    if not isinstance(runid, str) or not re.fullmatch(r"wf-[a-f0-9]{32}", runid):
        raise ValueError("actual workflow run identity malformed")
    receipt["phase"] = "worker"
    for _ in range(241):
        receipt["workflow"] = workflow_state(runid, definitionid)
        persist()
        current = receipt["workflow"]["run"].get("status")
        if current in (2, 3, 4):
            break
        time.sleep(0.5)
    else:
        raise ValueError("actual workflow did not reach bounded terminal status")
    receipt["runtimeAfter"] = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
    receipt["phase"] = "complete"
    persist()
    # Judge product responses and independent committed records before returning
    # any job/artifact identity to the actual canonical-client producer.
    return workflow_output(receipt, authored)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True); parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args(); receipt = {}
    def persist():
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, allow_nan=False)+"\n")
    try:
        observed = strict_json(args.observations.read_bytes())
        authored = scenario(args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes()), observed["nonce"], observed["canonicalClient"])
        if observed.get("candidateSourceSha") != authored["candidateSourceSha"] or observed.get("candidateImage") != authored["candidateImage"]:
            raise ValueError("workflow setup caller belongs to another candidate")
        result = execute_setup(authored, os.environ.get("HONUA_MCP_API_KEY"), os.environ.get("HONUA_CERT_DB_PASSWORD"),
                               os.environ.get("HONUA_MCP_CREDENTIAL_SALT", ""), receipt, persist)
        print(json.dumps(result, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, UnicodeError, HTTPException, subprocess.SubprocessError, RecursionError):
        print("owned public workflow setup refused", file=sys.stderr); return 1
    finally:
        persist()


if __name__ == "__main__":
    raise SystemExit(main())
