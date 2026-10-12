"""Cloud scenario S10 large-import: a background import from the cell's S3 bucket (honua-release#548).

Owner decision 24 (2026-10-11); fix unit J19. The journey's stage 3 proves only the small synchronous
multipart upload. S10 proves the other path: a SHA-pinned fixture above the cell's background
threshold is staged in S3, imported through `POST /api/v1/admin/import/upload-url` from a short-lived
presigned GET URL, followed through `GET /api/v1/admin/import/jobs/{jobId}` to a terminal state,
published, and queried; the row count must equal the pinned feature count.

The work is split across the cell workflow's jobs, as everything else in it is (honua-release#381):

* provision (AWS credentials): `stage` writes the pinned fixture to
  `s3://<bucket>/release-cells/<cell>/s10/<run>-<attempt>/<fixture>` and records identifiers only
  (the key travels as its SHA-256; it is recomputed from the cell and run).
* admit (AWS credentials): `presign` mints a GET URL valid for PRESIGN_TTL_SECONDS. The URL is a
  bearer credential, so it reaches the journey only inside the RSA-sealed handoff, beside the key.
* journey (no AWS credential): `run_scenario` drives the cell's admin API. Receipts keep only the
  allowlisted RECEIPT_FIELDS; the URL, its signature and query never reach a receipt or a log.
* teardown (AWS credentials): `delete_staged` removes the object; the bucket lifecycle expiring
  `release-cells/` after a day is only the backstop.

Redis-off cells run the same path on the small committed fixture with `forceBackground: true`.
honua-server registers its file-import job service without Redis and runs the job in-process, so the
cell may answer 202 with a job (cloud run 38192851328): S10 then follows that job to completion,
publishes and counts, exactly as on Redis-on (`executor: in-process`, `topology: redis-off`). Only a
host without the import job service answers the typed 503 `Background import service not
available...` (ImportEndpoints.cs:643-647 at honua-server 93cad86ec); that refusal still passes with
`refusal` recorded (decision 3). Anything else fails with the server's scrubbed message.

A job that stops changing state fails `job-stalled` on either topology, naming the last transition,
the time since it and the instances that answered the status reads.

Publishing the imported table (fix unit J22). honua-server has no asynchronous publish: `service.publish`
(the operation behind stage 3's `honua_publish_service`) is ExecutionKind Synchronous and its executor
calls the same `ILayerPublishingService.PublishLayerAsync` inline (ServicePublishOperation.cs:39,
ServicePublishExecutor.cs:120,168 at 0e03bdd06), so routing S10 through it would add an operation
handle but not remove the wait. On the 330,000-row table the publish transaction (geometry-health scan,
extent scan, a one-time copy of every row into the canonical `features` snapshot, extent refresh) outlives
the ECS ALB's 60 s idle timeout and the cell answers 504 (run 38249563601). S10 therefore keeps the admin
publish and, when it answers 504 or nothing, reads the connection's layer list until the layer for the
imported table appears (PUBLISH_CONVERGE_SECONDS), recording `publishAnswered` and how long convergence
took. It never re-POSTs. The admin publish runs on the request's abort token
(LayerPublishingEndpoints.cs:228-231), so a gateway that drops the connection may roll the publish back;
a layer that never appears fails `publish-not-converged`, naming the answer, the wait and the last read.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from canonical_checks import CheckResult
from fixtures import large_import as fixtures

SCENARIO = "S10-large-import-object-url"
ROW = "large-import"
ISSUE = "honua-release#548"

# The release cells lower the threshold through this setting (terraform_target.
# IMPORT_BACKGROUND_THRESHOLD_VAR, aws_eks.IMPORT_THRESHOLD_ENV); the scenario reads the live limits.
LIMIT_ENV = "Import__Limits__BackgroundJobThresholdBytes"

TABLE, REDIS_OFF_TABLE = "s10_large_import", "s10_redis_off"
SCHEMA = "honua_data"
# ImportEndpoints.ResolvePhysicalImportedTableName: "imported_" + the sanitized lower-case name.
PHYSICAL_TABLE = "imported_" + TABLE
SERVICE, REDIS_OFF_SERVICE = "s10-large-import", "s10-redis-off"

PRESIGN_TTL_SECONDS = 1800
JOB_TIMEOUT_SECONDS = 1200
POLL_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 120
TERMINAL = {"completed", "failed", "cancelled"}
# The publish answers that leave its outcome unknown: a gateway timeout (the ECS ALB's 60 s idle timeout
# answers 504) or no answer within REQUEST_TIMEOUT_SECONDS. Anything else is the publish's own verdict.
PUBLISH_UNKNOWN = {0, 504}
# How long S10 reads the layer list after such an answer: the server's snapshot materialization budget
# (LayerPublishing:MaterializationTimeoutSeconds, default 300, LayerPublishingOptions.cs:13) plus a minute.
PUBLISH_CONVERGE_SECONDS = 360
PUBLISH_PATH = "admin-layers"
# The server change that would let a large publish answer inside a gateway's idle timeout.
PUBLISH_FOLLOW_UP = "server follow-up: asynchronous service.publish"
# ImportStatus serializes as its name or its ordinal, depending on the serializer options.
_STATUS_ORDINALS = {0: "queued", 1: "processing", 2: "completed", 3: "failed", 4: "cancelled"}

REDIS_OFF_REFUSAL = "Background import service not available. File is too large for synchronous import."
# honua-server registers UniversalImportJobService for file-import background jobs on every host
# (src/Honua.Db/Postgres/ServiceCollectionExtensions.cs, IImportJobService): the job runs in the
# process that accepted the upload. ImportProgress (src/Honua.Core/Features/Import/Domain/
# ImportProgress.cs at 93cad86ec) carries no runner field; when a later server adds one under any of
# JOB_RUNNER_FIELDS the receipt records it as `jobRunner`. Until then `executor` is derived (below).
EXECUTOR = "in-process"
JOB_RUNNER_FIELDS = ("jobRunner", "runner", "executor")
MANIFEST_PATH = "/api/v1/capabilities/manifest"
# The manifest's `jobs.runner` is the Redis-backed durable job runner (geoprocessing jobs and
# workflows). File imports do not run on it: run 38192851328's Redis-off cell reported it
# `dependency-unavailable` and still queued the import (202).
JOBS_RUNNER_CAPABILITY = "jobs.runner"

# A job that stops changing state is a server-side stall (an import left in the host that accepted it
# stops advancing, e.g. a Lambda invocation frozen after its 202). The server fix is honua-server#5916.
STALL_FOLLOW_UP = "honua-server#5916"

LIMITS_PATH = "/api/v1/admin/import/limits"
UPLOAD_URL_PATH = "/api/v1/admin/import/upload-url"
JOB_PATH = "/api/v1/admin/import/jobs/{job}"
LAYERS_PATH = "/api/v1/admin/connections/{connection}/layers"
LIST_LAYERS_PATH = LAYERS_PATH + "?serviceName={service}"
COUNT_PATH = "/rest/services/{service}/FeatureServer/{layer}/query?where=1%3D1&returnCountOnly=true&f=json"
INSTANCE_HEADER = "Honua-Instance"

# Everything a receipt may carry. Anything else the server returns (including the job document's
# sourceUrl, which is the presigned URL) is dropped.
RECEIPT_FIELDS = ("scenario", "topology", "variant", "fixture", "fixtureSha256", "features", "bytes",
                  "thresholdBytes", "maxMemoryBytes", "stagingCapBytes", "source", "sourceUrlHost",
                  "uploadStatus", "jobId", "queuedAt", "completedAt", "durationSeconds", "durationMs",
                  "transitions", "jobStatus", "featuresProcessed", "layerId", "service", "rowCount",
                  "executor", "executorBasis", "jobRunner", "jobsRunnerCapability", "instances",
                  "statusInstances", "stall", "refusal", "publishPath", "publishAnswered", "publishMs",
                  "publishConvergedSeconds", "publishReads")

_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_CELL = re.compile(r"(aws-ecs|aws-serverless|aws-eks|stub)/redis-(on|off)\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_INSTANCE = re.compile(r"[\x21-\x7e]{1,128}\Z")
_RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_AWS_ERROR = re.compile(r"\(([A-Za-z][A-Za-z0-9.]{2,63})\)")
_SIGNATURE = re.compile(r"(?i)x-amz-[a-z-]+=[^&\s\"']*")
MAX_DETAIL_CHARS = 300


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pin_for(cell: str) -> fixtures.FixturePin:
    return fixtures.LARGE if cell.endswith("/redis-on") else fixtures.SMALL


def object_key(cell: str, run_id: str, run_attempt: str) -> str:
    """The staged object's key: `release-cells/<cell>/s10/<run>-<attempt>/<fixture>`."""
    if not _CELL.fullmatch(cell) or not _RUN.fullmatch(run_id or "") or not _RUN.fullmatch(run_attempt or ""):
        raise ValueError("invalid cell or run identity for the S10 object key")
    return f"release-cells/{cell.replace('/', '-')}/s10/{run_id}-{run_attempt}/{pin_for(cell).name}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def aws_error(error: subprocess.CalledProcessError) -> str:
    """The AWS error code an `aws` failure names (AccessDenied, NoSuchBucket, ...), never its text."""
    code = _AWS_ERROR.search(str(error.stderr or ""))
    return f"aws exited {error.returncode}" + (f" ({code.group(1)})" if code else "")


# ---- provision (credentialed) ------------------------------------------------------------------------
def stage(cell: str, *, run_id: str, run_attempt: str, bucket: str | None, region: str | None,
          workdir: Path, run=subprocess.run) -> dict:
    """Write the cell's pinned fixture to its S3 prefix. Returns the handoff record (identifiers only).

    `staged: false` names why (no bucket configured, a fixture whose digest differs from its pin, or
    the AWS error code); the journey reports that as a blocked precondition, which require_real fails.
    """
    pin = pin_for(cell)
    record = {"scenario": SCENARIO, "variant": "positive" if pin is fixtures.LARGE else "redis-off",
              "fixture": pin.name, "features": pin.features, "bytes": pin.bytes,
              "fixtureSha256": pin.sha256, "staged": False}
    bucket = (bucket or "").strip()
    if not bucket:
        return {**record, "why": f"no S3 bucket to stage the S10 fixture in: HONUA_AWS_CELL_FILE_STORAGE_BUCKET "
                                 f"is unset ({ISSUE})"}
    if not _BUCKET.fullmatch(bucket) or not region:
        return {**record, "why": f"HONUA_AWS_CELL_FILE_STORAGE_BUCKET is not an S3 bucket name, or the cell "
                                 f"names no region ({ISSUE})"}
    key = object_key(cell, run_id, run_attempt)
    record.update(bucket=bucket, region=region, keySha256=_sha(key), runId=run_id, runAttempt=run_attempt)
    path = None
    try:
        path = fixtures.materialize(pin, Path(workdir))
        run(["aws", "s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path),
             "--content-type", "application/geo+json", "--region", region, "--output", "json"],
            text=True, capture_output=True, check=True)
    except fixtures.FixtureMismatch as error:
        return {**record, "why": f"{error} ({ISSUE})"}
    except subprocess.CalledProcessError as error:
        return {**record, "why": f"staging the S10 fixture failed: put-object {aws_error(error)} ({ISSUE})"}
    except OSError as error:
        return {**record, "why": f"staging the S10 fixture failed: {type(error).__name__} ({ISSUE})"}
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)
    return {**record, "staged": True, "stagedAt": now()}


def _bound_key(record: dict, cell: str) -> str:
    key = object_key(cell, str(record.get("runId")), str(record.get("runAttempt")))
    if record.get("keySha256") != _sha(key) or not _BUCKET.fullmatch(str(record.get("bucket", ""))):
        raise ValueError("S10 staging record does not match this cell and run")
    return key


# ---- admit (credentialed) ----------------------------------------------------------------------------
def presign(record: dict, cell: str, *, ttl: int = PRESIGN_TTL_SECONDS, run=subprocess.run) -> str:
    """A presigned GET URL for the staged object, valid `ttl` seconds. Never printed or written."""
    if not record.get("staged"):
        raise ValueError("nothing was staged")
    key = _bound_key(record, cell)
    result = run(["aws", "s3", "presign", f"s3://{record['bucket']}/{key}", "--expires-in", str(int(ttl)),
                  "--region", str(record["region"])], text=True, capture_output=True, check=True)
    url = (result.stdout or "").strip()
    if not url.startswith("https://") or len(url) > 8192 or any(c.isspace() for c in url):
        raise ValueError("aws s3 presign returned no https URL")
    return url


# ---- teardown (credentialed) -------------------------------------------------------------------------
def delete_staged(record: dict | None, cell: str, *, run=subprocess.run) -> dict:
    """Delete the staged object. `status` is deleted, not-staged or failed (with the AWS error code)."""
    if not record or not record.get("staged"):
        return {"status": "not-staged"}
    try:
        key = _bound_key(record, cell)
        run(["aws", "s3api", "delete-object", "--bucket", record["bucket"], "--key", key,
             "--region", str(record["region"]), "--output", "json"], text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        return {"status": "failed", "why": f"delete-object {aws_error(error)}"}
    except (ValueError, OSError) as error:
        return {"status": "failed", "why": f"{type(error).__name__}: {error}"}
    return {"status": "deleted", "source": source_label(record)}


def source_label(record: dict) -> str:
    """`s3://<bucket>/sha256:<key digest>`: where the fixture was staged, with the key hashed."""
    return f"s3://{record.get('bucket')}/sha256:{record.get('keySha256')}"


def cost_lines(record: dict | None) -> tuple[list, list]:
    """(monthly, usage) cost-meter lines for the staged fixture: S3 Standard storage, one PUT, one GET."""
    if not record or not record.get("staged"):
        return [], []
    address = f"harness:{source_label(record)}"
    gib = f"{record['bytes'] / (1024 ** 3):.6f}"
    return [(address, "s3-standard-gb", gib)], [(address, "s3-put-request", 1), (address, "s3-get-request", 1)]


# ---- journey (credential-free) -----------------------------------------------------------------------
class _HoldRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never carry the admin key to another location


def admin_request(method: str, url: str, admin_key: str, body=None, *, timeout=REQUEST_TIMEOUT_SECONDS):
    """(status, JSON document or None, answering instance) for one cell admin call; 0 = no answer."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-API-Key": admin_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_HoldRedirect).open(request, timeout=timeout) as response:
            status, raw, response_headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        status, raw, response_headers = error.code, (error.read(64 * 1024) if error.fp else b""), error.headers
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None, None
    instance = response_headers.get(INSTANCE_HEADER) if response_headers is not None else None
    instance = instance if isinstance(instance, str) and _INSTANCE.match(instance) else None
    try:
        return status, (json.loads(raw.decode("utf-8")) if raw else None), instance
    except (UnicodeDecodeError, ValueError):
        return status, None, instance


def scrub(text, secrets=()) -> str:
    """A bounded, single-line excerpt with every known secret, the presigned URL (whole, without its
    query, or its query alone) and any X-Amz-* parameter replaced."""
    text = str(text)
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = _SIGNATURE.sub("[redacted]", text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[:MAX_DETAIL_CHARS] + "...(truncated)"


def _url_secrets(url: str | None, admin_key: str) -> tuple:
    if not url:
        return (admin_key,)
    parts = urllib.parse.urlsplit(url)
    return (url, parts.query, urllib.parse.urlunsplit(parts._replace(query="")), admin_key)


def server_message(document) -> str:
    """What a problem/admin error body says (detail, title, message or error), never anything else."""
    if not isinstance(document, dict):
        return ""
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    for source in (document, data):
        for name in ("detail", "message", "error", "title"):
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def server_messages(document) -> list[str]:
    """Every message field a problem/admin error body carries, for matching a typed refusal."""
    if not isinstance(document, dict):
        return []
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    return [value for source in (document, data) for name in ("detail", "message", "error", "title")
            if isinstance(value := source.get(name), str) and value.strip()]


def unwrap(document):
    """The payload of an admin response: its `data` object when enveloped, else the document."""
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        return document["data"]
    return document


def _status(document) -> str:
    value = (document or {}).get("status") if isinstance(document, dict) else None
    if isinstance(value, int) and not isinstance(value, bool):
        return _STATUS_ORDINALS.get(value, f"unknown-{value}")
    return str(value or "").strip().lower()[:32] or "missing"


def _iso(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def receipt(fields: dict) -> dict:
    """The allowlisted receipt; a value that still holds an S3 signature parameter is refused."""
    kept = {name: fields[name] for name in RECEIPT_FIELDS if name in fields}
    if _SIGNATURE.search(json.dumps(kept)) or "?" in str(kept.get("source", "")):
        raise ValueError("S10 receipt would carry a presigned URL parameter")
    return kept


def _row(status: str, why: str, fields: dict) -> CheckResult:
    evidence = receipt({"scenario": SCENARIO, **fields})
    line = {"scenario": SCENARIO, "status": status, "why": why, "evidence": evidence}
    return CheckResult(ROW, status, f"{SCENARIO}: {why}", {"scenarios": [line]})


def _seed_connection(seed_manifest: Path | None) -> str | None:
    try:
        manifest = json.loads(Path(seed_manifest).read_text(encoding="utf-8")) if seed_manifest else {}
    except (OSError, ValueError):
        return None
    connection = manifest.get("connectionId") if isinstance(manifest, dict) else None
    return str(connection) if isinstance(connection, (str, int)) and str(connection) else None


def job_runner(progress) -> str | None:
    """Where the server says the job runs, when the job document names it (JOB_RUNNER_FIELDS)."""
    if not isinstance(progress, dict):
        return None
    for name in JOB_RUNNER_FIELDS:
        value = progress.get(name)
        if isinstance(value, str) and _RUNNER.fullmatch(value.strip()):
            return value.strip().lower()
    return None


def jobs_runner_capability(manifest) -> dict | None:
    """The manifest's `jobs.runner` entry reduced to {available, reasonCode}, or None."""
    capabilities = manifest.get("capabilities") if isinstance(manifest, dict) else None
    for entry in capabilities if isinstance(capabilities, list) else []:
        if isinstance(entry, dict) and entry.get("id") == JOBS_RUNNER_CAPABILITY:
            reason = entry.get("reasonCode")
            return {"available": entry.get("available") is True,
                    "reasonCode": reason[:64] if isinstance(reason, str) else None}
    return None


def executor_basis(capability: dict | None) -> str:
    """Why the receipt says `executor: in-process` when the job document names no runner."""
    if capability is None:
        manifest = "the capability manifest reported no jobs.runner entry"
    elif capability["available"]:
        manifest = ("the manifest's jobs.runner (the Redis-backed geoprocessing runner) is available, but "
                    "file imports do not run on it")
    else:
        manifest = (f"the manifest's jobs.runner is unavailable ({capability.get('reasonCode') or 'no reason'}), "
                    "so the accepted import can only run in the API host")
    return (f"derived: the job document names no runner; {manifest}; honua-server runs file-import jobs in "
            "UniversalImportJobService inside the host that accepted the upload")


def layer_list(document) -> list:
    """The layers a layer-list answer carries: its `data` array when enveloped, else the array itself."""
    if isinstance(document, dict):
        document = document.get("data")
    return document if isinstance(document, list) else []


def find_layer(listing, schema: str, table: str) -> int | None:
    """The id of the published layer whose source is schema.table in a layer-list answer, or None."""
    for entry in layer_list(listing):
        if (isinstance(entry, dict) and str(entry.get("schema") or "").lower() == schema.lower()
                and str(entry.get("table") or "").lower() == table.lower()
                and _int(entry.get("layerId")) is not None):
            return entry["layerId"]
    return None


def status_text(status) -> str:
    return str(status) if status else "nothing"


def publish_not_converged(target: str, answered: int, publish_ms: int, *, wait: float, reads: int,
                          last: str) -> str:
    """The `publish-not-converged` failure: the answer, how long it took, the wait and the last read."""
    return (f"publish-not-converged: publishing {target} answered {status_text(answered)} after "
            f"{publish_ms / 1000:.1f}s and no layer for that table appeared in {reads} layer-list reads over "
            f"{int(wait)}s (last read {last}); service.publish has no asynchronous mode and the admin publish "
            "runs on the request's abort token, so a gateway timeout can roll the publish transaction back "
            f"({PUBLISH_FOLLOW_UP})")


def stall_detail(job: str, transitions: list, *, timeout: float, since: float, instances: list) -> str:
    """The `job-stalled` failure: last transition, time since it, who answered, the server item."""
    last = transitions[-1] if transitions else {"status": "missing", "at": "never"}
    previous = transitions[-2]["status"] if len(transitions) > 1 else None
    change = f"{previous} -> {last['status']}" if previous else f"first read {last['status']}"
    answered = ", ".join(instances) if instances else f"no {INSTANCE_HEADER} header"
    return (f"job-stalled: job {job} was still {last['status']} after {int(timeout)}s; last transition "
            f"{change} at {last['at']}, {int(since)}s with no change since; status reads answered by "
            f"{answered} (transitions: {' -> '.join(t['status'] for t in transitions)}); the job runs in the "
            f"host that accepted the upload, which stopped advancing it ({STALL_FOLLOW_UP})")


def run_scenario(state: dict, endpoint: str, admin_key: str, *, source_url: str | None,
                 seed_manifest: Path | None = None, request=admin_request, clock=time.monotonic,
                 sleep=time.sleep, job_timeout: float = JOB_TIMEOUT_SECONDS,
                 poll_seconds: float = POLL_SECONDS,
                 publish_converge: float = PUBLISH_CONVERGE_SECONDS) -> CheckResult | None:
    """S10 on a live cell, from the journey job. None when provision recorded no S10 staging."""
    record = state.get("s10")
    if not isinstance(record, dict):
        return None
    redis_on = state.get("redis") == "redis-on"
    topology = "redis-on" if redis_on else "redis-off"
    secrets = _url_secrets(source_url, admin_key)
    base = endpoint.rstrip("/")
    fields: dict = {"topology": topology, "variant": record.get("variant"), "fixture": record.get("fixture"),
                    "fixtureSha256": record.get("fixtureSha256"), "features": record.get("features"),
                    "bytes": record.get("bytes"), "instances": []}
    if record.get("staged"):
        fields["source"] = source_label(record)
    if source_url:
        fields["sourceUrlHost"] = (urllib.parse.urlsplit(source_url).hostname or "").lower()
    if not record.get("staged"):
        return _row("blocked", scrub(record.get("why") or f"the S10 fixture was not staged ({ISSUE})", secrets),
                    fields)
    if not source_url:
        return _row("blocked", "the admit job delivered no presigned source URL for the staged fixture "
                               f"(presign failed or was not attempted; see the admit job log) ({ISSUE})", fields)

    def call(method, path, body=None, *, seen=None):
        status, document, instance = request(method, base + path, admin_key, body)
        for into in (fields["instances"], seen if seen is not None else []):
            if instance and instance not in into:
                into.append(instance)
        return status, document

    # Preflight: the cell's limits and the staging cap max(threshold, MaxMemoryBytes) both upload
    # paths enforce (ImportEndpoints.cs:326,532).
    status, limits = call("GET", LIMITS_PATH)
    limits = unwrap(limits)
    threshold = _int((limits or {}).get("backgroundJobThresholdBytes")) if isinstance(limits, dict) else None
    memory = _int((limits or {}).get("maxMemoryBytes")) if isinstance(limits, dict) else None
    if status != 200 or threshold is None or memory is None:
        return _row("fail", f"import limits preflight answered {status or 'nothing'} without "
                            "backgroundJobThresholdBytes and maxMemoryBytes", fields)
    cap = max(threshold, memory)
    fields.update(thresholdBytes=threshold, maxMemoryBytes=memory, stagingCapBytes=cap)
    pin_bytes = int(record["bytes"])
    if redis_on and cap <= threshold:
        return _row("fail", f"no-background-window: BackgroundJobThresholdBytes={threshold} and "
                            f"MaxMemoryBytes={memory} cap a staged file at {cap} bytes, which does not exceed "
                            f"the threshold, so no file can queue as a background import; the cell must "
                            f"set {LIMIT_ENV} below MaxMemoryBytes ({ISSUE})", fields)
    if pin_bytes > cap or (redis_on and pin_bytes <= threshold):
        window = f"({threshold}, {cap}]" if redis_on else f"[0, {cap}]"
        return _row("fail", f"fixture-outside-window: the {pin_bytes}-byte fixture is outside the cell's "
                            f"window {window} (BackgroundJobThresholdBytes={threshold}, "
                            f"MaxMemoryBytes={memory})", fields)

    table, service = (TABLE, SERVICE) if redis_on else (REDIS_OFF_TABLE, REDIS_OFF_SERVICE)
    label = "" if redis_on else "redis-off: "
    body = {"sourceUrl": source_url, "fileName": record["fixture"], "tableName": table, "targetSchema": SCHEMA,
            "targetSrid": 4326, "overwriteExisting": True, "forceBackground": not redis_on,
            "trackProgress": True}
    started, queued_at = clock(), now()
    status, document = call("POST", UPLOAD_URL_PATH, body)
    fields["uploadStatus"] = status
    message = scrub(server_message(document), secrets)
    data = document.get("data", document) if isinstance(document, dict) else {}
    data = data if isinstance(data, dict) else {}
    job = data.get("jobId")

    if not redis_on and status == 503 and REDIS_OFF_REFUSAL in server_messages(document):
        fields["refusal"] = REDIS_OFF_REFUSAL
        return _row("pass", f"redis-off: forceBackground upload-url refused with the typed 503 "
                            f"'{REDIS_OFF_REFUSAL}': this host runs no import job service (topology redis-off, "
                            f"decision 3)", fields)
    if status != 202:
        hint = (" (an expired or refused presigned URL reads as the cell's 502 'Failed to fetch source "
                "object: 403')" if status == 502 else "")
        why = f"upload-url answered {status or 'nothing'}" + (f": {message}" if message else "") + hint
        if status == 200:
            why = "upload-url imported synchronously (jobId null): the fixture never reached the background path"
        if not redis_on:
            why += f"; expected 202 with a background job or the typed 503 '{REDIS_OFF_REFUSAL}'"
        return _row("fail", label + why, fields)
    if not isinstance(job, str) or not _JOB_ID.match(job):
        return _row("fail", label + "upload-url answered 202 without a usable jobId", fields)
    fields.update(jobId=job, queuedAt=queued_at)

    transitions, last, progress, status_instances, changed = [], None, {}, [], clock()
    deadline = started + job_timeout
    while True:
        status, progress = call("GET", JOB_PATH.format(job=urllib.parse.quote(job, safe="")),
                                seen=status_instances)
        progress = unwrap(progress)
        state_name = _status(progress) if status == 200 else f"http-{status or 'none'}"
        if state_name != last:
            transitions.append({"status": state_name, "at": now()})
            last, changed = state_name, clock()
        if state_name in TERMINAL or clock() >= deadline:
            break
        sleep(poll_seconds)
    fields.update(transitions=transitions, jobStatus=last, durationMs=int((clock() - started) * 1000),
                  statusInstances=status_instances)
    progress = progress if isinstance(progress, dict) else {}

    runner = job_runner(progress)
    if runner:
        fields.update(jobRunner=runner, executor=runner, executorBasis="the job document names its runner")
    else:
        _, manifest = call("GET", MANIFEST_PATH)
        capability = jobs_runner_capability(unwrap(manifest))
        if capability is not None:
            fields["jobsRunnerCapability"] = capability
        fields.update(executor=EXECUTOR, executorBasis=executor_basis(capability))

    if last not in TERMINAL:
        since = clock() - changed
        fields["stall"] = {"lastStatus": last, "lastTransitionAt": transitions[-1]["at"] if transitions else None,
                           "secondsSinceLastTransition": int(since), "followUp": STALL_FOLLOW_UP}
        return _row("fail", label + stall_detail(job, transitions, timeout=job_timeout, since=since,
                                                 instances=status_instances), fields)
    server_started, completed = _iso(progress.get("startedAt")), _iso(progress.get("completedAt"))
    if server_started:
        fields["queuedAt"] = server_started
    if completed:
        fields["completedAt"] = completed
    if server_started and completed:
        fields["durationSeconds"] = round((datetime.fromisoformat(completed.replace("Z", "+00:00"))
                                           - datetime.fromisoformat(server_started.replace("Z", "+00:00"))
                                           ).total_seconds(), 3)
    fields["featuresProcessed"] = _int(progress.get("featuresProcessed"))
    if last != "completed":
        error = scrub(progress.get("errorMessage") or "", secrets)
        return _row("fail", label + f"job {job} ended {last}" + (f": {error}" if error else ""), fields)

    connection = _seed_connection(seed_manifest)
    if not connection:
        return _row("fail", label + "the job completed but the seed recorded no connection to publish the imported "
                            "table through", fields)
    quoted, target = urllib.parse.quote(connection, safe=""), f"{SCHEMA}.imported_{table}"
    publish_started = clock()
    status, published = call("POST", LAYERS_PATH.format(connection=quoted), {
        "schema": SCHEMA, "table": "imported_" + table, "layerName": service, "serviceName": service,
        "geometryColumn": "geometry", "geometryType": "Point", "primaryKey": "id", "srid": 4326,
        "enabled": True})
    publish_ms = int((clock() - publish_started) * 1000)
    fields.update(publishPath=PUBLISH_PATH, publishAnswered=status, publishMs=publish_ms)
    layer = ((published or {}).get("data") or {}).get("layerId") if isinstance(published, dict) else None
    converged = None
    if status in PUBLISH_UNKNOWN:
        # The publish may still be running (or have committed) behind the gateway: read, never re-POST.
        reads, last_read, deadline = 0, "none", clock() + publish_converge
        while True:
            list_status, listing = call("GET", LIST_LAYERS_PATH.format(
                connection=quoted, service=urllib.parse.quote(service, safe="")))
            reads += 1
            layer = find_layer(listing, SCHEMA, "imported_" + table) if list_status == 200 else None
            last_read = (f"answered 200 with {len(layer_list(listing))} layers in {service}"
                         if list_status == 200 else f"answered {status_text(list_status)}")
            if layer is not None or clock() >= deadline:
                break
            sleep(poll_seconds)
        fields["publishReads"] = reads
        if layer is None:
            return _row("fail", label + publish_not_converged(target, status, publish_ms, wait=publish_converge,
                                                              reads=reads, last=last_read), fields)
        converged = int(clock() - publish_started)
        fields["publishConvergedSeconds"] = converged
    elif status not in (200, 201) or _int(layer) is None:
        detail = scrub(server_message(published), secrets)
        return _row("fail", label + f"publishing {target} answered {status_text(status)}"
                            + (f": {detail}" if detail else ""), fields)
    fields.update(layerId=layer, service=service)
    status, counted = call("GET", COUNT_PATH.format(service=service, layer=layer))
    counted = unwrap(counted)
    rows = _int((counted or {}).get("count")) if isinstance(counted, dict) else None
    fields["rowCount"] = rows
    if status != 200 or rows is None:
        return _row("fail", label + f"the count query on {service}/{layer} answered {status or 'nothing'} without a count",
                    fields)
    if rows != int(record["features"]):
        return _row("fail", label + f"the published layer holds {rows} rows, not the pinned {record['features']}", fields)
    publish_note = (f"; publish answered {status_text(fields['publishAnswered'])}, converged after {converged}s"
                    if converged is not None else "")
    return _row("pass", label + f"background job {job} ran {fields['executor']} and imported {record['bytes']} "
                        f"bytes from {fields.get('sourceUrlHost')} ({last} in "
                        f"{fields.get('durationSeconds', 'unknown')}s); {service}/{layer} holds the pinned {rows} rows"
                        + publish_note, fields)
