"""Retain real candidate discovery responses; never project or edit expected-ga-manifest.json."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

import yaml


class CaptureError(ValueError):
    """Only fixed, credential-free diagnostics belong in the receipt."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_response(url: str, key: str | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def inspect_response(body: bytes, status: int, expected_revision: str) -> dict:
    if status != 200:
        raise CaptureError("capability discovery did not return HTTP 200")
    document = json.loads(body)
    if not isinstance(document, dict) or document.get("schemaVersion") != "honua.capability_manifest.v1":
        raise CaptureError("capability discovery schema is not recognized")
    server = document.get("server", {})
    if (not isinstance(server, dict) or server.get("deploymentRevision") != expected_revision
            or server.get("deploymentRevisionSource") != "commit-sha"):
        raise CaptureError("runtime deployment identity differs from the candidate source pin")
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities or not all(isinstance(c, dict) for c in capabilities):
        raise CaptureError("capability discovery returned no valid capabilities")
    ids = [entry.get("id") for entry in capabilities]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise CaptureError("capability IDs are missing or duplicated")
    for entry in capabilities:
        if type(entry.get("supported")) is not bool or type(entry.get("available")) is not bool:
            raise CaptureError("capability support/availability must be explicit booleans")
        if not isinstance(entry.get("lifecycle"), str) or not entry["lifecycle"]:
            raise CaptureError("capability lifecycle must be explicit")
    return {"schemaVersion": document.get("schemaVersion"),
            "deploymentRevision": server["deploymentRevision"],
            "deploymentRevisionSource": server["deploymentRevisionSource"],
            "capabilities": [{key: entry.get(key) for key in ("id", "lifecycle", "supported", "available")}
                             for entry in capabilities]}


def capture(manifest_path: Path, out: Path, base: str, key: str, fetch=read_response) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for filename in ("anonymous.json", "authenticated.json", "capture.json"):
        (out / filename).unlink(missing_ok=True)
    report = {"schemaVersion": "honua.capability-capture.v1", "status": "fail",
              "capturedAt": datetime.now(timezone.utc).isoformat(), "topology": "redis-" + os.getenv("E2E_REDIS", "on"),
              "workflowRun": os.getenv("GITHUB_RUN_ID"), "workflowAttempt": os.getenv("GITHUB_RUN_ATTEMPT"),
              "workflowRevision": os.getenv("GITHUB_SHA"), "responses": {}}
    try:
        raw_manifest = manifest_path.read_bytes()
        component = yaml.safe_load(raw_manifest)["components"]["honua-server"]
        revision, digest, image = component["sha"], component["digest"], component["image"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise CaptureError("candidate source and image digest must be immutable")
        expected_image = image if "@" in image else image + "@" + digest
        if not expected_image.endswith("@" + digest):
            raise CaptureError("candidate image and digest disagree")
        report.update(expectedRevision=revision, expectedImage=expected_image,
                      platformManifestSha256=hashlib.sha256(raw_manifest).hexdigest())
        boot = json.loads((out.parent / "boot.json").read_text())
        if boot.get("booted") is not True or boot.get("image") != expected_image:
            raise CaptureError("boot evidence does not match the manifest-pinned image")
        if not key:
            raise CaptureError("authenticated discovery requires the configured candidate admin key")
        url = base.rstrip("/") + "/api/v1/capabilities/manifest"
        for name, credential in (("anonymous", None), ("authenticated", key)):
            status, body = fetch(url, credential)
            evidence = {"httpStatus": status, "sha256": hashlib.sha256(body).hexdigest()}
            report["responses"][name] = evidence
            # Response bodies are public capability data, but refuse retention if a server echoes the key.
            if key.encode() in body:
                raise CaptureError("response contained a credential; raw response was not retained")
            filename = name + ".json"
            (out / filename).write_bytes(body)
            evidence["file"] = filename
        for evidence in report["responses"].values():
            evidence.update(inspect_response((out / evidence["file"]).read_bytes(), evidence["httpStatus"], revision))
        def structural(name):
            return sorted((row["id"], row["lifecycle"], row["supported"])
                          for row in report["responses"][name]["capabilities"])
        if structural("anonymous") != structural("authenticated"):
            raise CaptureError("authenticated and anonymous discovery disagree on capability identity/support")
        report["status"] = "pass"
    except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError) as error:
        # Do not put exception text, request headers or URLs into public evidence.
        report["failureType"] = type(error).__name__
        if isinstance(error, CaptureError):
            report["failureReason"] = str(error)
    (out / "capture.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = capture(args.manifest, args.out, os.environ["E2E_BASE"], os.environ.get("E2E_API_KEY", ""))
    print("raw discovery capture and exact candidate identity: " + report["status"])
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
