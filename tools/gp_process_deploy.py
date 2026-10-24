"""Execute a release-bound GP smoke against the authenticated production HTTP contract.

Infrastructure is provisioned by IaC first. This client verifies an existing immutable
Batch definition; it never manufactures a replacement definition with lost settings.
Credentials come from the runner environment and never enter receipts or CLI arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import gp_process_release as gate

BASE = "/api/v1/admin/gp/process-releases"
MAX_RESPONSE = 4 * 1024 * 1024
ARN = re.compile(r"^arn:aws(?:-[a-z]+)*:batch:[a-z0-9-]+:[0-9]{12}:job-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$")


class DeploymentError(Exception):
    """Safe error text: never includes a remote body, token or signed URL."""


class ServerRefusal(DeploymentError):
    """A definite HTTP/protocol refusal cannot be recovered as our activation."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ServerRefusal("Server redirected an authenticated request; refusing credential forwarding")


class Client:
    def __init__(self, base_url: str, api_key: str, *, allow_loopback_http=False):
        parsed = urlsplit(base_url)
        local = allow_loopback_http and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if ((parsed.scheme != "https" and not (local and parsed.scheme == "http"))
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise DeploymentError("Server URL must be HTTPS without credentials, query or fragment")
        if not api_key or any(c in api_key for c in "\r\n"):
            raise DeploymentError("HONUA_GP_ADMIN_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, document=None, *, idempotency_key=None):
        payload = self.read(method, path, document, idempotency_key=idempotency_key)
        try:
            result = json.loads(payload)
        except ValueError:
            raise DeploymentError("Server returned invalid JSON") from None
        if not isinstance(result, dict):
            raise DeploymentError("Server response must be a JSON object")
        return result

    def read(self, method, path, document=None, *, idempotency_key=None, maximum=MAX_RESPONSE):
        if not path.startswith("/") or path.startswith("//"):
            raise DeploymentError("API path must be relative to the configured server")
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        body = None
        if document is not None:
            body = json.dumps(document, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with self.opener.open(Request(self.base_url + path, body, headers, method=method), timeout=30) as response:
                payload = response.read(maximum + 1)
            if len(payload) > maximum:
                raise DeploymentError("Server response exceeds the deployment response limit")
            return payload
        except HTTPError as exc:
            # A timeout/gateway/server error can follow acceptance. Ordinary
            # client refusals, including the binding API's explicit CAS 409,
            # cannot become this run's activation through another actor's GET.
            error = ServerRefusal if 400 <= exc.code < 500 and exc.code != 408 else DeploymentError
            raise error(f"Server refused {method} request (HTTP {exc.code})") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DeploymentError(f"Server {method} request failed or returned invalid JSON") from None


def write(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def promotion_binding_version(state, process, previous, *, client=None):
    """Capture the live version without changing it or weakening deployment CAS.

    A reference environment has no remote binding. A live promotion must name
    the exact currently pinned predecessor before it records a new version.
    Concurrent changes after this GET remain deployment-time refusals.
    """
    if state.get("deploymentTooling", {}).get("kind") == "reference":
        return state["bindingVersion"]
    if state.get("deploymentTooling", {}).get("kind") != "aws-batch":
        raise DeploymentError("Live promotion requires the supported IaC Batch deployment contract")
    client = client or Client(os.environ.get("HONUA_GP_SERVER_URL", ""), os.environ.get("HONUA_GP_ADMIN_API_KEY", ""))
    observed = client.request("GET", f"{BASE}/bindings/{quote(process, safe='')}")
    expected_lock = previous["lockDigest"] if previous else None
    if (observed.get("process") != process or type(observed.get("bindingVersion")) is not int
            or observed["bindingVersion"] < 0 or observed.get("lockDigest") != expected_lock):
        raise DeploymentError("Live promotion binding identity or reviewed predecessor differs; reconcile before promotion")
    return observed["bindingVersion"]



def promotion_predecessor(state, process, current, *, client=None):
    """Capture a reviewed forward or rollback predecessor for the next pin PR.

    A successful rollback leaves the forward pin unchanged. Only its exact
    previous lock, with native reverse lineage to that forward release, can be
    reconciled here. Unknown native locks still require separate review.
    """
    if state.get("deploymentTooling", {}).get("kind") == "reference":
        return (current["release"] if current else None), state["bindingVersion"]
    if state.get("deploymentTooling", {}).get("kind") != "aws-batch":
        raise DeploymentError("Live promotion requires the supported IaC Batch deployment contract")
    client = client or Client(os.environ.get("HONUA_GP_SERVER_URL", ""), os.environ.get("HONUA_GP_ADMIN_API_KEY", ""))
    observed = client.request("GET", f"{BASE}/bindings/{quote(process, safe='')}")
    if (observed.get("process") != process or type(observed.get("bindingVersion")) is not int
            or observed["bindingVersion"] < 0):
        raise DeploymentError("Live promotion binding identity or version differs")
    forward = current["release"] if current else None
    previous = current.get("previous") if current else None
    if observed.get("lockDigest") == (forward["lockDigest"] if forward else None):
        return forward, observed["bindingVersion"]
    if (forward and previous and observed.get("lockDigest") == previous["lockDigest"]
            and observed.get("previousLockDigest") == forward["lockDigest"]):
        return previous, observed["bindingVersion"]
    raise DeploymentError("Live promotion binding is outside reviewed forward/rollback lineage")


def live_preflight(client, lock, pin, state, index, operation):
    """Use the current binding, never a checked-in version as live state."""
    process = lock["process"]["name"]
    live = client.request("GET", f"{BASE}/bindings/{quote(process, safe='')}")
    if live.get("process") != process or type(live.get("bindingVersion")) is not int:
        raise DeploymentError("Binding response identity/version is invalid")
    expected = pin.get("previous")
    lineage_error = None
    if operation == "rollback":
        if (live.get("lockDigest") != pin["release"]["lockDigest"]
                or expected is None or live.get("previousLockDigest") != expected["lockDigest"]):
            lineage_error = "ROLLBACK_TARGET_MISMATCH"
        pin = dict(pin, expectedBindingVersion=live["bindingVersion"])
    elif live.get("lockDigest") != (expected["lockDigest"] if expected else None):
        lineage_error = "LIVE_PREDECESSOR_MISMATCH"
    observed = dict(state, bindingVersion=live["bindingVersion"],
                    serverContractVersion=live.get("serverContractVersion"), targets=live.get("supportedTargets"))
    gate.validate_document(gate.STATE_SCHEMA, observed, "live environment")
    decision = gate.preflight(lock, pin, observed, index, lock if operation == "rollback" else None)
    if lineage_error:
        decision["admissible"] = False
        decision["refusalReasons"].append(lineage_error)
        check = "rollbackTarget" if operation == "rollback" else "bindingVersion"
        decision["checks"][check] = {"status": "fail", "detail": "Live binding lineage differs from the reviewed pin"}
    return decision, live


def registration_for(lock, state):
    if state["deploymentTooling"]["kind"] != "aws-batch" or {t["kind"] for t in lock["targets"]} != {"aws-batch"}:
        raise DeploymentError("Live custom-code releases require an IaC-provisioned AWS Batch target (ADR-0063)")
    registration = state["deploymentTooling"].get("releases", {}).get(gate.digest(lock))
    if not isinstance(registration, dict) or not ARN.fullmatch(registration.get("jobDefinitionArn", "")):
        raise DeploymentError("Environment must name the exact IaC-provisioned Batch revision for this lock digest")
    return registration


def aws_json(arguments):
    try:
        result = subprocess.run(["aws", *arguments, "--output", "json"], capture_output=True, timeout=60, check=False)
        if result.returncode or len(result.stdout) > MAX_RESPONSE:
            raise DeploymentError("AWS verification failed")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise DeploymentError("AWS verification failed") from None


def verify_definition(lock, registration, *, aws=aws_json):
    arn = registration["jobDefinitionArn"]
    definitions = aws(["batch", "describe-job-definitions", "--job-definitions", arn]).get("jobDefinitions", [])
    target = lock["targets"][0]
    image = lock["runtimeImage"]["reference"].split("@")[0] + "@" + target["workerImageDigest"]
    if (len(definitions) != 1 or definitions[0].get("jobDefinitionArn") != arn
            or definitions[0].get("status") != "ACTIVE"
            or definitions[0].get("containerProperties", {}).get("image") != image
            or (target.get("jobDefinitionName") and definitions[0].get("jobDefinitionName") != target["jobDefinitionName"])):
        raise DeploymentError("IaC Batch definition is unavailable or does not match the release image/identity")
    definition = definitions[0]
    container = definition.get("containerProperties", {})
    if definition.get("platformCapabilities") != ["FARGATE"] or container.get("jobRoleArn"):
        raise DeploymentError("Custom workers require Fargate without a task IAM role")
    if container.get("environmentFiles") or container.get("environment") or container.get("secrets"):
        raise DeploymentError("Custom worker definitions cannot inject environment values, files or secrets")
    return {"kind": "aws-batch", "jobDefinitionArn": arn}


def activate(client, lock, live, target, operation, state_path):
    process = lock["process"]["name"]
    lock_digest = gate.digest(lock)
    published = client.request("POST", BASE, lock)
    if published.get("lockDigest") != lock_digest or published.get("process") != process:
        raise DeploymentError("Published release identity disagrees with the submitted lock")
    persisted = client.request("GET", BASE + "/" + quote(lock_digest, safe=""))
    if gate.digest(persisted) != lock_digest:
        raise DeploymentError("Published lock readback differs from the submitted bytes")
    # Persist uncertainty BEFORE mutation. A lost response must never become 'unchanged'.
    write(state_path, {"status": "unknown", "detail": "Activation requested; readback not yet verified"})
    request = {"lockDigest": lock_digest, "expectedBindingVersion": live["bindingVersion"],
               "operation": operation, "targets": [target]}
    path = f"{BASE}/bindings/{quote(process, safe='')}"
    try:
        client.request("POST", path + "/activate", request)
    except ServerRefusal:
        write(state_path, {"status": "unknown", "detail": "Server explicitly refused this activation; do not attribute a concurrent binding to this run"})
        raise
    except DeploymentError:
        # Recover an ambiguous transport response by observing the exact desired version.
        # Never retry mutation with a newly observed version.
        pass
    observed = client.request("GET", path)
    expected_version = live["bindingVersion"] + 1
    if (observed.get("process") != process or observed.get("lockDigest") != lock_digest
            or observed.get("bindingVersion") != expected_version
            or not any(t.get("jobDefinitionArn") == target["jobDefinitionArn"] for t in observed.get("targets", []))):
        raise DeploymentError("Activation was refused, raced or could not be verified; inspect live binding before retry")
    write(state_path, {"status": "activated", "detail": "Exact lock, provider revision and incremented version read back",
                       "bindingVersion": expected_version, "lockDigest": lock_digest})
    return observed


def load_smoke(path):
    fixture = gate.load_json(path)
    if set(fixture) != {"params", "declaredScope", "expectedArtifacts"} or not isinstance(fixture["params"], dict):
        raise DeploymentError("Smoke fixture requires params, declaredScope and expectedArtifacts")
    if not isinstance(fixture["declaredScope"], list) or not fixture["expectedArtifacts"]:
        raise DeploymentError("Smoke must declare scope and at least one independently expected output")
    for artifact in fixture["expectedArtifacts"]:
        if (set(artifact) != {"label", "sha256", "maxBytes"} or not artifact["label"]
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["sha256"])
                or type(artifact["maxBytes"]) is not int or not 0 < artifact["maxBytes"] <= MAX_RESPONSE):
            raise DeploymentError("Smoke artifact expectation is invalid or exceeds 4 MiB")
    return fixture


def artifact_digest(client, job_id, artifact, maximum):
    """Fetch only the same-origin, owner-authorized custom output content route."""
    link = artifact.get("providerLink") or ""
    parsed, base = urlsplit(link), urlsplit(client.base_url)
    path = parsed.path
    if parsed.fragment or parsed.username or parsed.password:
        raise DeploymentError("Artifact content link is invalid")
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise DeploymentError("Refusing to forward credentials to another artifact origin")
        if not path.startswith(base.path.rstrip("/") + "/"):
            raise DeploymentError("Artifact content link is outside the configured server")
        path = path[len(base.path.rstrip("/")):]
    prefix = "/api/geoprocessing/jobs/" + job_id + "/custom-artifacts/"
    if not path.startswith(prefix) or any(x in path for x in ("..", "\\", "%2e", "%2E", "%2f", "%2F")):
        raise DeploymentError("Artifact link is not an authorized output of the smoke job")
    if parsed.query:
        path += "?" + parsed.query
    payload = client.read("GET", path, maximum=maximum)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def smoke(client, lock, binding, fixture, key, *, timeout=600, sleep=time.sleep, clock=time.monotonic,
          read_artifact=artifact_digest, record_job=None):
    process = lock["process"]["name"]
    submitted = client.request("POST", f"{BASE}/bindings/{quote(process, safe='')}/jobs",
                               {"params": fixture["params"], "declaredScope": fixture["declaredScope"]}, idempotency_key=key)
    job_id = submitted.get("jobId")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job_id):
        raise DeploymentError("Submission did not return a valid job identity")
    if record_job is not None:
        record_job(job_id)
    path = "/api/v1/admin/jobs/" + job_id
    deadline = clock() + timeout
    while True:
        job = client.request("GET", path)
        if job.get("jobId") != job_id:
            raise DeploymentError("Polled job identity changed")
        status = str(job.get("status", "")).lower()
        if status in {"completed", "succeeded", "successful"}:
            break
        if status in {"failed", "cancelled", "canceled", "dismissed"}:
            raise DeploymentError("Smoke job terminated without success; inspect authorized job diagnostics")
        if clock() >= deadline:
            raise DeploymentError("Smoke deadline exceeded; job may still be running and requires inspection")
        sleep(2)
    diagnostics = client.request("GET", path + "/diagnostics")
    release = diagnostics.get("release", {})
    if (diagnostics.get("jobId") != job_id or release.get("lockDigest") != gate.digest(lock)
            or release.get("sourceRef") != lock["source"]["sha"]
            or release.get("processRelease") != process
            or str(release.get("bindingVersion")) != str(binding["bindingVersion"])):
        raise DeploymentError("Smoke job did not execute the activated release/source/version")
    page = client.request("GET", path + "/artifacts?limit=100")
    if page.get("jobId") != job_id or page.get("nextCursor"):
        raise DeploymentError("Smoke artifact list is incomplete or belongs to another job")
    checked = []
    for expected in fixture["expectedArtifacts"]:
        matches = [a for a in page.get("items", []) if a.get("label") == expected["label"]]
        if len(matches) != 1 or str(matches[0].get("availability", "")).lower() != "available":
            raise DeploymentError("Expected smoke artifact is missing, ambiguous or unavailable")
        actual = read_artifact(client, job_id, matches[0], expected["maxBytes"])
        if actual != expected["sha256"]:
            raise DeploymentError("Smoke output bytes do not match the independently committed expectation")
        checked.append({"label": expected["label"], "sha256": actual})
    return {"status": "pass", "jobId": job_id, "lockDigest": gate.digest(lock),
            "bindingVersion": binding["bindingVersion"], "artifacts": checked}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--pin", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--artifact-index", required=True)
    parser.add_argument("--operation", choices=["deploy", "rollback"], default="deploy")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    write(out / "binding.json", {"status": "unchanged", "detail": "Activation not attempted"})
    write(out / "smoke.json", {"status": "skipped", "evidence": "Smoke not reached"})
    try:
        lock, pin, state, index = (gate.load_json(p) for p in (args.lock, args.pin, args.environment, args.artifact_index))
        gate.validate_lock(lock)
        gate.validate_pin(pin, Path.cwd())
        gate.validate_document(gate.STATE_SCHEMA, state, "environment")
        # A validated input gets a structured refusal even when auth/provider/network checks
        # prevent reading live state. This placeholder can never authorize activation.
        initial = gate.preflight(lock, pin, state, index, lock if args.operation == "rollback" else None)
        initial["admissible"] = False
        initial["refusalReasons"].append("LIVE_PREFLIGHT_NOT_COMPLETED")
        for name in ("bindingVersion", "compatibility", "targets"):
            initial["checks"][name] = {"status": "fail", "detail": "Live server state has not been verified"}
        write(out / "decision.json", initial)
        registration = registration_for(lock, state)
        fixture = load_smoke(registration["smokeFixture"])
        client = Client(os.environ.get("HONUA_GP_SERVER_URL", ""), os.environ.get("HONUA_GP_ADMIN_API_KEY", ""))
        decision, live = live_preflight(client, lock, pin, state, index, args.operation)
        write(out / "decision.json", decision)
        if not decision["admissible"]:
            raise DeploymentError("Live preflight refused activation; see deployment decision")
        target = verify_definition(lock, registration)
        binding = activate(client, lock, live, target, args.operation, out / "binding.json")
        write(out / "smoke.json", {"status": "fail", "evidence": "Smoke started; completion not verified"})
        key = "gp-deploy-" + hashlib.sha256((os.environ.get("GITHUB_RUN_ID", "") + ":" +
                   os.environ.get("GITHUB_RUN_ATTEMPT", "") + ":" + str(binding["bindingVersion"]) + ":" + gate.digest(lock)).encode()).hexdigest()
        result = smoke(client, lock, binding, fixture, key, record_job=lambda job_id: write(
            out / "smoke.json", {"status": "fail", "jobId": job_id, "evidence": "Job submitted; output proof not yet verified"}))
        result["evidence"] = "Actual admitted release and bounded output bytes verified"
        write(out / "smoke.json", result)
        return 0
    except (DeploymentError, gate.GateError, KeyError, OSError, ValueError) as exc:
        # Only our explicitly safe exceptions are printable; files/remote data may contain secrets.
        detail = str(exc) if isinstance(exc, DeploymentError) else "Deployment input validation failed"
        write(out / "error.json", {"error": detail})
        print("::error::" + detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
