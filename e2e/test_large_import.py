"""S10 large-import (honua-release#548): fixture pins, S3 staging, the scenario against a fake admin API.

No cloud: `aws` is a recorded fake, and the cell admin API is a loopback HTTP server that answers the
import, job, publish and count routes the way honua-server does.

Run: python -m pytest e2e/test_large_import.py
"""
from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import cloud_journey  # noqa: E402
import cost_meter  # noqa: E402
import large_import as s10  # noqa: E402
import run_cloud  # noqa: E402
from fixtures import large_import as fx  # noqa: E402
from targets import terraform_target as tt  # noqa: E402

CELL_ON, CELL_OFF = "aws-ecs/redis-on", "aws-ecs/redis-off"
BUCKET, REGION = "honua-release-cells", "us-east-1"
ADMIN_KEY = "Honua-Gate-Aa1!cell-admin-key-value"
SIGNATURE = "c0ffee" * 10 + "abcd"
TOKEN = "IQoJb3JpZ2luX2VjEXAMPLESESSIONTOKEN"
MIB = 1024 * 1024


def _presigned(key="release-cells/aws-ecs-redis-on/s10/42-1/s10-large-import.geojson"):
    return (f"https://{BUCKET}.s3.amazonaws.com/{key}?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential=ASIAEXAMPLEKEYID0000%2F20261011%2Fus-east-1%2Fs3%2Faws4_request"
            f"&X-Amz-Date=20261011T000000Z&X-Amz-Expires=1800&X-Amz-SignedHeaders=host"
            f"&X-Amz-Security-Token={TOKEN}&X-Amz-Signature={SIGNATURE}")


URL = _presigned()


def _url_leaked(text: str) -> bool:
    parts = urllib.parse.urlsplit(URL)
    return any(value in text for value in (URL, parts.query, SIGNATURE, TOKEN, "X-Amz-Signature",
                                           "X-Amz-Credential", ADMIN_KEY))


# ---- fixtures: deterministic, pinned, verified -----------------------------------------------------------
def test_generator_writes_the_same_bytes_twice_and_the_committed_small_fixture():
    with tempfile.TemporaryDirectory() as scratch:
        first = fx.generate(Path(scratch) / "a.geojson", 2_500)
        second = fx.generate(Path(scratch) / "b.geojson", 2_500)
        assert first == second and first["features"] == 2_500
        assert (Path(scratch) / "a.geojson").read_bytes() == (Path(scratch) / "b.geojson").read_bytes()
        document = json.loads((Path(scratch) / "a.geojson").read_bytes())
        assert document["type"] == "FeatureCollection" and len(document["features"]) == 2_500
        assert [f["properties"]["s10_id"] for f in document["features"][:3]] == [1, 2, 3]
        small = fx.generate(Path(scratch) / "small.geojson", fx.SMALL.features)
        assert (Path(scratch) / "small.geojson").read_bytes() == fx.SMALL_PATH.read_bytes()
        assert (small["bytes"], small["sha256"]) == (fx.SMALL.bytes, fx.SMALL.sha256)
    assert fx.verify(fx.SMALL_PATH, fx.SMALL)["features"] == fx.SMALL.features


def test_the_large_pin_is_what_the_generator_produces():
    digest, size = hashlib.sha256(), 0
    for chunk in fx.chunks(fx.LARGE.features):
        digest.update(chunk)
        size += len(chunk)
    assert (size, digest.hexdigest()) == (fx.LARGE.bytes, fx.LARGE.sha256)


def test_the_large_fixture_sits_inside_the_cells_background_window():
    threshold = int(tt.IMPORT_BACKGROUND_THRESHOLD_VAR.split("=", 1)[1])
    default_memory = 100 * MIB  # ImportLimits.MaxMemoryBytes default
    assert threshold < fx.LARGE.bytes <= max(threshold, default_memory)
    assert fx.SMALL.bytes < threshold


def test_verify_refuses_a_fixture_whose_digest_differs():
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / fx.SMALL.name
        path.write_bytes(fx.SMALL_PATH.read_bytes().replace(b"s10_id", b"s10_ID"))
        with pytest.raises(fx.FixtureMismatch, match="fixture-sha-mismatch"):
            fx.verify(path, fx.SMALL)


# ---- staging, presign, delete (credentialed jobs; `aws` is a recorded fake) ------------------------------
class _Aws:
    def __init__(self, fail=None, presigned=URL):
        self.calls, self.fail, self.presigned = [], fail, presigned

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail and self.fail in argv:
            raise subprocess.CalledProcessError(254, argv, "", "An error occurred (AccessDenied) when calling "
                                                "the PutObject operation: secret detail arn:aws:iam::1:role/x")
        if argv[:3] == ["aws", "s3", "presign"]:
            return subprocess.CompletedProcess(argv, 0, self.presigned + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


def _stage(cell, aws, monkeypatch=None, *, bucket=BUCKET, scratch):
    if monkeypatch is not None and cell.endswith("/redis-on"):
        # Generating 82 MiB is covered by the pin test; stage a tiny stand-in that "verifies".
        def materialize(pin, workdir):
            path = Path(workdir) / pin.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")
            return path
        monkeypatch.setattr(s10.fixtures, "materialize", materialize)
    return s10.stage(cell, run_id="42", run_attempt="1", bucket=bucket, region=REGION,
                     workdir=Path(scratch), run=aws)


def test_stage_puts_the_pinned_fixture_under_the_cell_prefix_and_records_only_a_key_digest(monkeypatch):
    aws = _Aws()
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_ON, aws, monkeypatch, scratch=scratch)
        assert not list(Path(scratch).iterdir())  # the local copy is removed after the upload
    key = "release-cells/aws-ecs-redis-on/s10/42-1/s10-large-import.geojson"
    assert record["staged"] is True and record["variant"] == "positive"
    assert (record["bytes"], record["fixtureSha256"], record["features"]) == (
        fx.LARGE.bytes, fx.LARGE.sha256, fx.LARGE.features)
    assert record["keySha256"] == hashlib.sha256(key.encode()).hexdigest() and key not in json.dumps(record)
    (argv,) = aws.calls
    assert argv[:3] == ["aws", "s3api", "put-object"] and argv[argv.index("--key") + 1] == key
    assert argv[argv.index("--bucket") + 1] == BUCKET and argv[argv.index("--region") + 1] == REGION
    assert argv[argv.index("--content-type") + 1] == "application/geo+json"


def test_stage_on_a_redis_off_cell_uses_the_committed_small_fixture():
    aws = _Aws()
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_OFF, aws, scratch=scratch)
    assert record["staged"] and record["variant"] == "redis-off" and record["fixture"] == fx.SMALL.name
    assert aws.calls[0][aws.calls[0].index("--key") + 1].startswith("release-cells/aws-ecs-redis-off/s10/42-1/")


def test_stage_without_a_bucket_or_with_a_mismatched_fixture_stages_nothing_and_says_why(monkeypatch):
    aws = _Aws()
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_OFF, aws, bucket="", scratch=scratch)
        assert not record["staged"] and "HONUA_AWS_CELL_FILE_STORAGE_BUCKET" in record["why"]
        monkeypatch.setattr(fx, "SMALL_PATH", Path(scratch) / "tampered.geojson")
        (Path(scratch) / "tampered.geojson").write_bytes(b'{"type":"FeatureCollection","features":[]}')
        record = _stage(CELL_OFF, aws, scratch=scratch)
        assert not record["staged"] and "fixture-sha-mismatch" in record["why"]
    assert aws.calls == []


def test_a_refused_upload_names_the_aws_error_code_and_nothing_else():
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_OFF, _Aws(fail="put-object"), scratch=scratch)
    assert not record["staged"] and "(AccessDenied)" in record["why"] and "arn:aws" not in record["why"]


def test_presign_and_delete_are_bound_to_the_recorded_cell_and_run():
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_OFF, _Aws(), scratch=scratch)
    aws = _Aws()
    assert s10.presign(record, CELL_OFF, run=aws) == URL
    argv = aws.calls[0]
    assert argv[:3] == ["aws", "s3", "presign"] and argv[3].startswith(f"s3://{BUCKET}/release-cells/aws-ecs-redis-off/")
    assert argv[argv.index("--expires-in") + 1] == str(s10.PRESIGN_TTL_SECONDS) == "1800"
    with pytest.raises(ValueError, match="does not match"):
        s10.presign(record, CELL_ON, run=aws)
    assert s10.delete_staged(record, CELL_OFF, run=aws)["status"] == "deleted"
    assert aws.calls[-1][:3] == ["aws", "s3api", "delete-object"]
    assert s10.delete_staged(record, CELL_OFF, run=_Aws(fail="delete-object"))["status"] == "failed"
    assert s10.delete_staged({"staged": False}, CELL_OFF, run=aws) == {"status": "not-staged"}


# ---- the sealed handoff carries the URL; nothing else does -----------------------------------------------
def test_the_presigned_url_travels_only_inside_the_sealed_handoff():
    datasource = {"host": "db", "port": 5432, "databaseName": "honua", "username": "u", "password": "p"}
    for plaintext in (ADMIN_KEY, cloud_journey.pack_handoff(ADMIN_KEY, datasource)):
        extended = cloud_journey.attach_source_url(plaintext, URL)
        assert cloud_journey.handoff_source_url(extended) == URL
        key, ds = cloud_journey.unpack_handoff(extended)
        assert key == ADMIN_KEY and ds == (datasource if plaintext.startswith("{") else None)
    assert cloud_journey.handoff_source_url(ADMIN_KEY) is None
    with pytest.raises(ValueError):
        cloud_journey.attach_source_url(ADMIN_KEY, "http://insecure.example/x")
    with pytest.raises(ValueError):
        cloud_journey.unpack_handoff(json.dumps({"schema": cloud_journey.HANDOFF_SCHEMA, "appKey": "k",
                                                 cloud_journey.SOURCE_URL_FIELD: "ftp://x"}))


def test_the_admit_job_seals_the_url_with_the_key_and_a_presign_failure_never_blocks_delivery(capsys):
    with tempfile.TemporaryDirectory() as scratch:
        record = _stage(CELL_OFF, _Aws(), scratch=scratch)
    extend, note = run_cloud.s10_handoff_extension({"s10": record}, CELL_OFF, run=_Aws())
    assert cloud_journey.handoff_source_url(extend(ADMIN_KEY)) == URL and URL not in note
    extend, note = run_cloud.s10_handoff_extension({"s10": record}, CELL_OFF, run=_Aws(fail="presign"))
    assert extend is None and "(AccessDenied)" in note
    assert run_cloud.s10_handoff_extension({}, CELL_OFF)[0] is None


# ---- the scenario against a fake admin API ---------------------------------------------------------------
class _Cell:
    """A loopback honua admin API for the S10 routes. `script` shapes each answer."""

    def __init__(self, *, threshold=64 * MIB, memory=100 * MIB, upload=None, job_states=("Queued", "Processing",
                 "Completed"), job_error=None, features=None, layer=7, count=None, publish_status=201,
                 envelope=False):
        self.threshold, self.memory = threshold, memory
        self.upload = upload
        self.job_states, self.job_error = list(job_states), job_error
        self.features, self.layer, self.count, self.publish_status = features, layer, count, publish_status
        self.requests, self.reads, self.envelope = [], 0, envelope

    def answer(self, method, path, body, key):
        status, document = self._answer(method, path, body, key)
        if self.envelope and status == 200 and "data" not in document:
            document = {"success": True, "data": document}
        return status, document

    def _answer(self, method, path, body, key):
        self.requests.append((method, path, body))
        if key != ADMIN_KEY:
            return 401, {"title": "Unauthorized"}
        if path == s10.LIMITS_PATH:
            return 200, {"batchSize": 1000, "maxMemoryBytes": self.memory,
                         "backgroundJobThresholdBytes": self.threshold}
        if path == s10.UPLOAD_URL_PATH:
            if self.upload is not None:
                return self.upload
            return 202, {"jobId": "job-123", "message": "File queued for background processing",
                         "statusUrl": "/api/v1/admin/import/jobs/job-123"}
        if path.startswith("/api/v1/admin/import/jobs/"):
            state = self.job_states[min(self.reads, len(self.job_states) - 1)]
            self.reads += 1
            doc = {"jobId": "job-123", "status": state, "tableName": s10.TABLE, "sourceKind": "url",
                   "sourceUrl": URL, "startedAt": "2026-10-11T00:00:00+00:00", "format": "GeoJson"}
            if state in ("Completed", "Failed"):
                doc.update(completedAt="2026-10-11T00:02:30.5+00:00",
                           featuresProcessed=self.features if self.features is not None else fx.LARGE.features)
            if self.job_error:
                doc["errorMessage"] = self.job_error
            return 200, doc
        if path.endswith("/layers") and method == "POST":
            return self.publish_status, {"data": {"layerId": self.layer}}
        if "/FeatureServer/" in path:
            return 200, {"count": self.count if self.count is not None else fx.LARGE.features}
        return 404, {"title": "Not Found"}


def _serve(cell):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, document = cell.answer(self.command, self.path, body, self.headers.get("X-API-Key"))
            raw = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Honua-Instance", "task-a" if cell.reads % 2 == 0 else "task-b")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _scenario(cell, *, redis="redis-on", staged=True, source_url=URL, timeout=60.0, seed=True):
    pin = fx.LARGE if redis == "redis-on" else fx.SMALL
    record = {"scenario": s10.SCENARIO, "variant": "positive" if redis == "redis-on" else "redis-off",
              "fixture": pin.name, "features": pin.features, "bytes": pin.bytes, "fixtureSha256": pin.sha256,
              "staged": staged, "bucket": BUCKET, "region": REGION, "keySha256": "ab" * 32,
              **({} if staged else {"why": "no S3 bucket to stage the S10 fixture in (honua-release#548)"})}
    server = _serve(cell)
    clock = {"t": 0.0}
    try:
        with tempfile.TemporaryDirectory() as scratch:
            manifest = Path(scratch) / "seed-manifest.json"
            if seed:
                manifest.write_text(json.dumps({"connectionId": "3f2b8c1e-5d4a-4e7b-9c0d-1a2b3c4d5e6f"}))
            row = s10.run_scenario(
                {"redis": redis, "s10": record}, f"http://127.0.0.1:{server.server_address[1]}", ADMIN_KEY,
                source_url=source_url, seed_manifest=manifest, clock=lambda: clock["t"],
                sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds), job_timeout=timeout,
                poll_seconds=5.0)
    finally:
        server.shutdown()
        server.server_close()
    (line,) = row.evidence["scenarios"]
    assert row.name == "large-import" and line["scenario"] == "S10-large-import-object-url"
    rendered = json.dumps(run_cloud._check_dicts([row]))
    assert not _url_leaked(rendered), rendered
    return row, line["evidence"]


def test_a_redis_on_cell_follows_the_background_job_to_success_and_counts_the_pinned_rows():
    cell = _Cell()
    row, receipt = _scenario(cell)
    assert row.status == "pass", row.why
    assert receipt["jobId"] == "job-123" and receipt["rowCount"] == fx.LARGE.features
    assert receipt["bytes"] == fx.LARGE.bytes and receipt["fixtureSha256"] == fx.LARGE.sha256
    assert (receipt["thresholdBytes"], receipt["maxMemoryBytes"], receipt["stagingCapBytes"]) == (
        64 * MIB, 100 * MIB, 100 * MIB)
    assert receipt["queuedAt"] == "2026-10-11T00:00:00Z" and receipt["completedAt"] == "2026-10-11T00:02:30.500000Z"
    assert receipt["durationSeconds"] == 150.5 and receipt["durationMs"] == 10_000
    assert [t["status"] for t in receipt["transitions"]] == ["queued", "processing", "completed"]
    assert receipt["executor"] == "in-process" and receipt["topology"] == "redis-on"
    assert receipt["sourceUrlHost"] == f"{BUCKET}.s3.amazonaws.com"
    assert receipt["source"] == f"s3://{BUCKET}/sha256:{'ab' * 32}"
    assert set(receipt["instances"]) <= {"task-a", "task-b"} and receipt["layerId"] == 7
    assert set(receipt) <= set(s10.RECEIPT_FIELDS)
    upload = next(body for method, path, body in cell.requests if path == s10.UPLOAD_URL_PATH)
    assert upload["sourceUrl"] == URL and upload["forceBackground"] is False
    assert upload["fileName"] == fx.LARGE.name and upload["targetSchema"] == "honua_data"
    publish = next(body for method, path, body in cell.requests if path.endswith("/layers"))
    assert publish["table"] == "imported_s10_large_import" and publish["geometryColumn"] == "geometry"


def test_enveloped_admin_responses_are_unwrapped():
    # Review on #554: the job read may answer {"data": {...}}, as stage 3's executor already handles.
    row, receipt = _scenario(_Cell(envelope=True))
    assert row.status == "pass", row.why
    assert receipt["jobStatus"] == "completed" and receipt["thresholdBytes"] == 64 * MIB


def test_a_failed_job_surfaces_the_servers_error_and_fails():
    row, receipt = _scenario(_Cell(job_states=("Queued", "Failed"),
                                   job_error=f"Import failed. source {URL} unreadable"))
    assert row.status == "fail" and "job job-123 ended failed: Import failed. source [redacted]" in row.why
    assert receipt["jobStatus"] == "failed"


def test_a_job_that_never_ends_fails_with_its_transitions_never_blocks():
    row, receipt = _scenario(_Cell(job_states=("Queued", "Processing")), timeout=30.0)
    assert row.status == "fail"
    assert row.why.endswith("job-timeout: job job-123 was still processing after 30s "
                            "(transitions: queued -> processing)")


@pytest.mark.parametrize("upload,expected", [
    ((200, {"success": True, "featureCount": 2}), "imported synchronously (jobId null)"),
    ((202, {"jobId": None, "message": "queued"}), "202 without a usable jobId"),
    ((502, {"title": "Bad Gateway", "detail": "Failed to fetch source object: 403 Forbidden"}),
     "upload-url answered 502: Failed to fetch source object: 403 Forbidden"),
])
def test_a_missing_job_is_a_failure_whatever_the_upload_answered(upload, expected):
    row, _ = _scenario(_Cell(upload=upload))
    assert row.status == "fail" and expected in row.why


def test_no_background_window_fails_naming_both_limits_before_anything_is_uploaded():
    cell = _Cell(threshold=100 * MIB, memory=100 * MIB)
    row, receipt = _scenario(cell)
    assert row.status == "fail" and "no-background-window" in row.why
    assert "BackgroundJobThresholdBytes=104857600" in row.why and "MaxMemoryBytes=104857600" in row.why
    assert receipt["stagingCapBytes"] == 100 * MIB
    assert [path for _, path, _ in cell.requests] == [s10.LIMITS_PATH]


def test_a_fixture_outside_the_cells_window_fails_and_never_falls_back_to_force_background():
    cell = _Cell(threshold=90 * MIB, memory=200 * MIB)
    row, _ = _scenario(cell)
    assert row.status == "fail" and "fixture-outside-window" in row.why
    assert [path for _, path, _ in cell.requests] == [s10.LIMITS_PATH]


def test_a_published_layer_with_the_wrong_row_count_fails():
    row, receipt = _scenario(_Cell(count=fx.LARGE.features - 1))
    assert row.status == "fail" and receipt["rowCount"] == fx.LARGE.features - 1


def test_redis_off_passes_on_the_typed_503_with_force_background():
    cell = _Cell(upload=(503, {"type": "https://tools.ietf.org/html/rfc9110#section-15.6.4",
                               "title": "Service Unavailable", "status": 503, "detail": s10.REDIS_OFF_REFUSAL}))
    row, receipt = _scenario(cell, redis="redis-off")
    assert row.status == "pass" and receipt["topology"] == "redis-off"
    assert receipt["refusal"] == s10.REDIS_OFF_REFUSAL and receipt["fixtureSha256"] == fx.SMALL.sha256
    upload = next(body for method, path, body in cell.requests if path == s10.UPLOAD_URL_PATH)
    assert upload["forceBackground"] is True and upload["fileName"] == fx.SMALL.name


@pytest.mark.parametrize("upload,expected", [
    ((202, {"jobId": "job-9", "message": "File queued for background processing"}), "queued background import job job-9"),
    ((503, {"detail": "Background import requires shared file storage."}), "upload-url answered 503"),
    ((400, {"detail": "SourceUrl must point to a supported public S3, Azure Blob, or Azure File host."}),
     "upload-url answered 400"),
])
def test_redis_off_fails_on_anything_but_the_typed_refusal(upload, expected):
    row, _ = _scenario(_Cell(upload=upload), redis="redis-off")
    assert row.status == "fail" and expected in row.why


def test_an_unstaged_fixture_or_a_missing_url_is_a_named_blocked_precondition():
    row, receipt = _scenario(_Cell(), staged=False)
    assert row.status == "blocked" and "honua-release#548" in row.why and "source" not in receipt
    row, _ = _scenario(_Cell(), source_url=None)
    assert row.status == "blocked" and "presigned source URL" in row.why
    assert s10.run_scenario({"redis": "redis-on"}, "http://x", ADMIN_KEY, source_url=None) is None


def test_a_completed_job_without_a_seed_connection_cannot_pass():
    row, _ = _scenario(_Cell(), seed=False)
    assert row.status == "fail" and "no connection" in row.why


def test_receipts_refuse_a_signature_parameter_even_in_an_allowlisted_field():
    with pytest.raises(ValueError):
        s10.receipt({"source": "s3://b/k?X-Amz-Signature=abc"})
    assert s10.receipt({"jobId": "j", "sourceUrl": URL, "presignedUrl": URL}) == {"jobId": "j"}


# ---- the journey phase runs S10 first and keeps its row; require_real fails a blocked one -------------
def test_the_journey_phase_runs_s10_before_the_drivers_and_teardown_deletes_the_object(monkeypatch):
    order = []
    monkeypatch.setattr(run_cloud, "run_drivers", lambda *a, **k: order.append("drivers") or [])
    monkeypatch.setattr(run_cloud.large_import, "run_scenario", lambda state, endpoint, key, **k: (
        order.append(("s10", k["source_url"])) or s10._row("pass", "ok", {"topology": "redis-on"})))
    monkeypatch.setattr(run_cloud.cloud_journey, "attempt", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("journey not under test")))
    state = {"cell": CELL_ON, "redis": "redis-on", "endpoint": "https://cell.example", "ready": False,
             "runId": run_cloud._run_identity()[0], "runAttempt": run_cloud._run_identity()[1],
             "candidateDigest": cloud_journey.candidate_digest(), "report": {}, "s10": {"staged": True}}
    journey = run_cloud.journey_phase(state, admin_key=ADMIN_KEY, s10_source_url=URL)
    assert order == [("s10", URL), "drivers"]
    assert [row["name"] for row in journey["scenarioCoverage"]] == ["large-import"]


def test_cost_meter_prices_the_staged_fixture_by_run():
    record = {"staged": True, "bucket": BUCKET, "keySha256": "ab" * 32, "bytes": fx.LARGE.bytes}
    monthly, usage = s10.cost_lines(record)
    assert monthly == [(f"harness:s3://{BUCKET}/sha256:{'ab' * 32}", "s3-standard-gb", "0.079804")]
    assert {line[1] for line in usage} == {"s3-put-request", "s3-get-request"}
    t0 = datetime(2026, 10, 11, tzinfo=timezone.utc)
    state = {"startedAt": t0.isoformat(), "s10": record}
    cost = cost_meter.cell_cost(type("T", (), {"_iac_root": lambda self: Path(".")})(), state, "20",
                                run_id="42", run_attempt="1", aws=lambda args: {},
                                read_state=lambda target: {"values": {"root_module": {"resources": []}}},
                                clock=lambda: t0 + timedelta(hours=1))
    assert cost["status"] == "pass", cost.get("why")
    classes = {line["class"] for line in cost["estimateBasis"]["lines"]}
    assert {"s3-standard-gb", "s3-put-request", "s3-get-request"} <= classes
    assert s10.cost_lines({"staged": False}) == ([], [])


def test_redis_on_cells_pass_the_background_threshold_only_when_the_root_declares_it(monkeypatch):
    def values(declared, redis):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "infrastructure" / "terraform" / "examples" / "aws-serverless"
            root.mkdir(parents=True)
            (root / "variables.tf").write_text("".join(f'variable "{n}" {{}}\n' for n in ("region", *declared)))
            monkeypatch.setenv("HONUA_IAC_DIR", base)
            argv = tt.serverless(run_id="r1")._vars(redis)
        return {a[len("-var="):].split("=", 1)[0]: a.split("=", 2)[2] for a in argv if a.startswith("-var=")}
    monkeypatch.setenv("HONUA_LAMBDA_IMAGE_URI", "img")
    monkeypatch.setenv("HONUA_AWS_DB_INGRESS_CIDR", "192.0.2.10/32")
    monkeypatch.setenv("HONUA_LAMBDA_ARCHITECTURE", "x86_64")
    monkeypatch.setenv("HONUA_ADMIN_PASSWORD", ADMIN_KEY)
    name = "import_background_job_threshold_bytes"
    assert name not in values((), True)
    assert values((name,), True)[name] == "67108864"
    assert name not in values((name,), False)
    assert tt.IMPORT_BACKGROUND_THRESHOLD_VAR in tt.ECS_SPEC.redis_on_declared_vars
