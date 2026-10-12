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

Redis-off cells run the negative variant: the small committed fixture with `forceBackground: true`
must be refused with the typed 503 `Background import service not available...`
(ImportEndpoints.cs:680), recorded `pass` with `topology: redis-off` (owner ruling R3(a), decision 3).
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
SERVICE = "s10-large-import"

PRESIGN_TTL_SECONDS = 1800
JOB_TIMEOUT_SECONDS = 1200
POLL_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 120
TERMINAL = {"completed", "failed", "cancelled"}
# ImportStatus serializes as its name or its ordinal, depending on the serializer options.
_STATUS_ORDINALS = {0: "queued", 1: "processing", 2: "completed", 3: "failed", 4: "cancelled"}

REDIS_OFF_REFUSAL = "Background import service not available. File is too large for synchronous import."
# honua-server registers UniversalImportJobService for file-import background jobs on every host
# (src/Honua.Db/Postgres/ServiceCollectionExtensions.cs, IImportJobService): the job runs in the
# process that accepted the upload. ImportProgress carries no executor field, so this is the basis.
EXECUTOR = "in-process"
EXECUTOR_BASIS = ("honua-server runs file-import background jobs in UniversalImportJobService inside "
                  "the API host; the job document names no executor")

LIMITS_PATH = "/api/v1/admin/import/limits"
UPLOAD_URL_PATH = "/api/v1/admin/import/upload-url"
JOB_PATH = "/api/v1/admin/import/jobs/{job}"
LAYERS_PATH = "/api/v1/admin/connections/{connection}/layers"
COUNT_PATH = "/rest/services/{service}/FeatureServer/{layer}/query?where=1%3D1&returnCountOnly=true&f=json"
INSTANCE_HEADER = "Honua-Instance"

# Everything a receipt may carry. Anything else the server returns (including the job document's
# sourceUrl, which is the presigned URL) is dropped.
RECEIPT_FIELDS = ("scenario", "topology", "variant", "fixture", "fixtureSha256", "features", "bytes",
                  "thresholdBytes", "maxMemoryBytes", "stagingCapBytes", "source", "sourceUrlHost",
                  "uploadStatus", "jobId", "queuedAt", "completedAt", "durationSeconds", "durationMs",
                  "transitions", "jobStatus", "featuresProcessed", "layerId", "service", "rowCount",
                  "executor", "executorBasis", "instances", "refusal")

_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_CELL = re.compile(r"(aws-ecs|aws-serverless|aws-eks|stub)/redis-(on|off)\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_INSTANCE = re.compile(r"[\x21-\x7e]{1,128}\Z")
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


def run_scenario(state: dict, endpoint: str, admin_key: str, *, source_url: str | None,
                 seed_manifest: Path | None = None, request=admin_request, clock=time.monotonic,
                 sleep=time.sleep, job_timeout: float = JOB_TIMEOUT_SECONDS,
                 poll_seconds: float = POLL_SECONDS) -> CheckResult | None:
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

    def call(method, path, body=None):
        status, document, instance = request(method, base + path, admin_key, body)
        if instance and instance not in fields["instances"]:
            fields["instances"].append(instance)
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

    body = {"sourceUrl": source_url, "fileName": record["fixture"],
            "tableName": TABLE if redis_on else REDIS_OFF_TABLE, "targetSchema": SCHEMA, "targetSrid": 4326,
            "overwriteExisting": True, "forceBackground": not redis_on, "trackProgress": True}
    started, queued_at = clock(), now()
    status, document = call("POST", UPLOAD_URL_PATH, body)
    fields["uploadStatus"] = status
    message = scrub(server_message(document), secrets)
    data = document.get("data", document) if isinstance(document, dict) else {}
    data = data if isinstance(data, dict) else {}
    job = data.get("jobId")

    if not redis_on:
        if status == 503 and REDIS_OFF_REFUSAL in server_messages(document):
            fields["refusal"] = REDIS_OFF_REFUSAL
            return _row("pass", f"redis-off: forceBackground upload-url refused with the typed 503 "
                                f"'{REDIS_OFF_REFUSAL}' (topology redis-off, decision 3)", fields)
        if status == 202 and isinstance(job, str) and _JOB_ID.match(job):
            fields["jobId"] = job
            return _row("fail", f"redis-off: the cell queued background import job {job} instead of the typed "
                                f"503 '{REDIS_OFF_REFUSAL}'", fields)
        return _row("fail", f"redis-off: upload-url answered {status or 'nothing'}"
                            + (f" ({message})" if message else "")
                            + f", not the typed 503 '{REDIS_OFF_REFUSAL}'", fields)

    if status != 202:
        hint = (" (an expired or refused presigned URL reads as the cell's 502 'Failed to fetch source "
                "object: 403')" if status == 502 else "")
        why = f"upload-url answered {status or 'nothing'}" + (f": {message}" if message else "") + hint
        if status == 200:
            why = "upload-url imported synchronously (jobId null): the fixture never reached the background path"
        return _row("fail", why, fields)
    if not isinstance(job, str) or not _JOB_ID.match(job):
        return _row("fail", "upload-url answered 202 without a usable jobId", fields)
    fields.update(jobId=job, queuedAt=queued_at)

    transitions, last, progress = [], None, {}
    deadline = started + job_timeout
    while True:
        status, progress = call("GET", JOB_PATH.format(job=urllib.parse.quote(job, safe="")))
        progress = unwrap(progress)
        state_name = _status(progress) if status == 200 else f"http-{status or 'none'}"
        if state_name != last:
            transitions.append({"status": state_name, "at": now()})
            last = state_name
        if state_name in TERMINAL or clock() >= deadline:
            break
        sleep(poll_seconds)
    fields.update(transitions=transitions, jobStatus=last, durationMs=int((clock() - started) * 1000))
    progress = progress if isinstance(progress, dict) else {}
    if last not in TERMINAL:
        return _row("fail", f"job-timeout: job {job} was still {last} after {int(job_timeout)}s "
                            f"(transitions: {' -> '.join(t['status'] for t in transitions)})", fields)
    server_started, completed = _iso(progress.get("startedAt")), _iso(progress.get("completedAt"))
    if server_started:
        fields["queuedAt"] = server_started
    if completed:
        fields["completedAt"] = completed
    if server_started and completed:
        fields["durationSeconds"] = round((datetime.fromisoformat(completed.replace("Z", "+00:00"))
                                           - datetime.fromisoformat(server_started.replace("Z", "+00:00"))
                                           ).total_seconds(), 3)
    fields.update(executor=EXECUTOR, executorBasis=EXECUTOR_BASIS,
                  featuresProcessed=_int(progress.get("featuresProcessed")))
    if last != "completed":
        error = scrub(progress.get("errorMessage") or "", secrets)
        return _row("fail", f"job {job} ended {last}" + (f": {error}" if error else ""), fields)

    connection = _seed_connection(seed_manifest)
    if not connection:
        return _row("fail", "the job completed but the seed recorded no connection to publish the imported "
                            "table through", fields)
    status, published = call("POST", LAYERS_PATH.format(connection=urllib.parse.quote(connection, safe="")), {
        "schema": SCHEMA, "table": PHYSICAL_TABLE, "layerName": SERVICE, "serviceName": SERVICE,
        "geometryColumn": "geometry", "geometryType": "Point", "primaryKey": "id", "srid": 4326,
        "enabled": True})
    layer = ((published or {}).get("data") or {}).get("layerId") if isinstance(published, dict) else None
    if status not in (200, 201) or _int(layer) is None:
        detail = scrub(server_message(published), secrets)
        return _row("fail", f"publishing {SCHEMA}.{PHYSICAL_TABLE} answered {status or 'nothing'}"
                            + (f": {detail}" if detail else ""), fields)
    fields.update(layerId=layer, service=SERVICE)
    status, counted = call("GET", COUNT_PATH.format(service=SERVICE, layer=layer))
    counted = unwrap(counted)
    rows = _int((counted or {}).get("count")) if isinstance(counted, dict) else None
    fields["rowCount"] = rows
    if status != 200 or rows is None:
        return _row("fail", f"the count query on {SERVICE}/{layer} answered {status or 'nothing'} without a count",
                    fields)
    if rows != int(record["features"]):
        return _row("fail", f"the published layer holds {rows} rows, not the pinned {record['features']}", fields)
    return _row("pass", f"background job {job} imported {record['bytes']} bytes from {fields.get('sourceUrlHost')} "
                        f"({last} in {fields.get('durationSeconds', 'unknown')}s); {SERVICE}/{layer} holds the "
                        f"pinned {rows} rows", fields)
