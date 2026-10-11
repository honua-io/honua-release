"""Cloud bindings for the owned terminal journey; no stage implementations live here."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "certification/terminal-journey"
EVIDENCE = ROOT / "e2e/cloud-evidence"
# aws-eks is GA for 2026.1 (honua-release#203; owner decisions 12/18 of 2026-10-10).
GA_CELLS = tuple(f"{target}/redis-{redis}" for target in ("aws-ecs", "aws-serverless", "aws-eks")
                 for redis in ("off", "on"))
# aws-mixed is out for 2026.1 rc.3 (no examples/aws-mixed root); restore it when honua-iac#209 lands.
PREVIEW_TARGETS: tuple[str, ...] = ()


def drivers():
    # The owned modules use sibling imports. Load the runner under its own name, as the live
    # adapter imports it too; never copy stage logic or invoke a subprocess driver.
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    if "run" not in sys.modules or Path(sys.modules["run"].__file__) != HERE / "run.py":
        spec = importlib.util.spec_from_file_location("run", HERE / "run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["run"] = module
        spec.loader.exec_module(module)
    if "live_driver" not in sys.modules or Path(sys.modules["live_driver"].__file__) != HERE / "live_driver.py":
        spec = importlib.util.spec_from_file_location("live_driver", HERE / "live_driver.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["live_driver"] = module
        spec.loader.exec_module(module)
    return sys.modules["run"], sys.modules["live_driver"]


def cloud_targets():
    """The owned cloud target module (certification/terminal-journey/cloud_target.py)."""
    drivers()
    import cloud_target  # noqa: PLC0415 (a sibling of the owned driver, on its path once loaded)
    return cloud_target


def manifest():
    return yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())


def candidate_digest():
    return hashlib.sha256((ROOT / "platform-manifest.yaml").read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def cell_dir(cell):
    target, redis = cell.split("/")
    if target not in (*PREVIEW_TARGETS, "aws-ecs", "aws-serverless", "aws-eks", "stub") or redis not in ("redis-on", "redis-off"):
        raise ValueError("invalid cloud cell")
    return EVIDENCE / os.environ.get("GITHUB_RUN_ID", "local") / os.environ.get("GITHUB_RUN_ATTEMPT", "1") / target / redis


@contextmanager
def admin_credential(value):
    previous = os.environ.get("HONUA_CLOUD_JOURNEY_ADMIN")
    os.environ["HONUA_CLOUD_JOURNEY_ADMIN"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HONUA_CLOUD_JOURNEY_ADMIN", None)
        else:
            os.environ["HONUA_CLOUD_JOURNEY_ADMIN"] = previous


# The provision job hands the credential-free journey job one sealed value. With a seeded cell it
# also carries the connection the fixture seed used, so the journey's stage 3 datasource is the
# cell's own RDS database. The value never reaches a handoff file, a target document or a receipt.
HANDOFF_SCHEMA = "honua-cloud-journey-handoff-v1"
DATASOURCE_FIELDS = ("host", "port", "databaseName", "username", "password")


def pack_handoff(admin_key, datasource=None):
    """The sealed journey handoff: the bare application key, or a JSON object with the datasource."""
    if not datasource:
        return admin_key
    fields = {name: datasource[name] for name in DATASOURCE_FIELDS}
    return json.dumps({"schema": HANDOFF_SCHEMA, "appKey": admin_key, "datasource": fields},
                      separators=(",", ":"))


def unpack_handoff(value):
    """(application key, datasource or None) from a sealed handoff; a bare key is accepted."""
    if not value.startswith("{"):
        return value, None
    document = json.loads(value)
    datasource = document.get("datasource") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or document.get("schema") != HANDOFF_SCHEMA
            or not isinstance(document.get("appKey"), str) or not isinstance(datasource, dict)
            or set(datasource) != set(DATASOURCE_FIELDS)
            or not all(isinstance(datasource[name], str) and datasource[name] for name in DATASOURCE_FIELDS
                       if name != "port")
            or type(datasource["port"]) is not int):
        raise ValueError("malformed cloud journey handoff")
    return document["appKey"], datasource


@contextmanager
def cell_datasource(datasource):
    """Expose the cell's datasource to the imported driver as its environment references.

    The cloud target names these variables (cloud_target.DATASOURCE_ENV); pins keeps exactly them
    in the candidate sandbox. Without a datasource nothing is set and stage 3 is blocked on it.
    """
    names = cloud_targets().DATASOURCE_ENV if datasource else {}
    previous = {env: os.environ.get(env) for env in names.values()}
    try:
        for field, env in names.items():
            os.environ[env] = str(datasource[field])
        yield
    finally:
        for env, value in previous.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value


_RUN_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def run_binding(run_id, run_attempt):
    """Identity suffix stored in every stage evidence URI. The owned receipt schema has no run field."""
    if not _RUN_TOKEN.fullmatch(run_id or "") or not _RUN_TOKEN.fullmatch(run_attempt or ""):
        raise ValueError("invalid run identity")
    return f"#honua-run={run_id}/{run_attempt}"


def bound_evidence_uri(cell, run_id, run_attempt):
    base = os.environ.get("HONUA_RUN_URL") or f"urn:honua:cloud:{cell}"
    return base.split("#", 1)[0] + run_binding(run_id, run_attempt)


@contextmanager
def external_image(driver, image_ref):
    # run_live reports no image for an external URL; it cannot see a cloud deployment. Supply
    # the image read back from the cloud control plane to the owned observe(), unchanged otherwise.
    original = driver.observe

    def observe(target, base_url, workspace, bindir, _image_ref, expected_revision):
        return original(target, base_url, workspace, bindir, image_ref, expected_revision)

    driver.observe = observe
    try:
        yield
    finally:
        driver.observe = original


def observed_ecs_image(target, pinned, *, run=subprocess.run):
    """The pinned server image only if every RUNNING task of the deployed ECS service runs it.

    Read from ECS DescribeTasks, never inferred from the apply inputs. Anything else is None.
    """
    server = pinned["components"]["honua-server"]
    root = target._workdir
    if root is None:
        return None
    cluster, service = (target._tf(root, "output", "-raw", name).stdout.strip()
                        for name in ("ecs_cluster_name", "ecs_service_name"))
    if not cluster or not service:
        return None

    def aws(*args):
        return json.loads(run(["aws", "ecs", *args, "--output", "json"], text=True,
                              capture_output=True, check=True).stdout)

    arns = aws("list-tasks", "--cluster", cluster, "--service-name", service,
               "--desired-status", "RUNNING")["taskArns"]
    if not arns:
        return None
    tasks = aws("describe-tasks", "--cluster", cluster, "--tasks", *arns)["tasks"]
    for task in tasks:
        servers = [c for c in task.get("containers", [])
                   if str(c.get("image", "")).split("@", 1)[0] == server["image"]]
        if (task.get("lastStatus") != "RUNNING" or not servers
                or any(c.get("imageDigest") != server["digest"] or c.get("lastStatus") != "RUNNING"
                       for c in servers)):
            return None
    return f"{server['image']}@{server['digest']}" if len(tasks) == len(arns) else None


def candidate_image(cell, pinned=None):
    """The server image a cell must be observed running: the Lambda pin on aws-serverless."""
    server = (pinned or manifest())["components"]["honua-server"]
    if cell.startswith("aws-serverless/") and server.get("awsLambdaImage") and server.get("awsLambdaDigest"):
        return f"{server['awsLambdaImage'].split('@', 1)[0]}@{server['awsLambdaDigest']}"
    return f"{server['image']}@{server['digest']}"


def observed_lambda_image(target, pinned, *, run=subprocess.run):
    """The pinned Lambda image only if the cell's API function runs its mirrored ECR digest.

    Read from Lambda GetFunction (read-only), never inferred from the apply inputs. The function
    runs the same-region ECR mirror of awsLambdaImage, whose digest the manifest records as
    awsLambdaEcrDigest (honua-release#99). Anything else is None.
    """
    server = pinned["components"]["honua-server"]
    expected = str(server.get("awsLambdaEcrDigest") or "")
    root = target._workdir
    if root is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        return None
    name = target._tf(root, "output", "-raw", "lambda_function_name").stdout.strip()
    if not name:
        return None
    function = json.loads(run(["aws", "lambda", "get-function", "--function-name", name, "--output", "json"],
                              text=True, capture_output=True, check=True).stdout)
    code, configuration = function.get("Code") or {}, function.get("Configuration") or {}
    resolved = str(code.get("ResolvedImageUri") or "")
    if (code.get("RepositoryType") != "ECR" or configuration.get("PackageType") != "Image"
            or "@" not in resolved or resolved.rsplit("@", 1)[1] != expected):
        return None
    return candidate_image("aws-serverless/", pinned)


UNOBSERVED_SHA = "0" * 40

# Notices attempt() writes into a receipt. A driver exception is a failure of this run, never a
# documented limitation; an unsupported cloud kind is the tracked honua-release#377 gap.
DRIVER_ERROR_NOTICE = "Imported journey driver raised "
UNSUPPORTED_KIND_NOTICE = "Owned receipt schema lacks this cloud kind"
UNSUPPORTED_KIND_ISSUE = "https://github.com/honua-io/honua-release/issues/377"


MAX_ERROR_CHARS = 400


def _scrub(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "...(truncated)"


def driver_failure_notices(error, trace_module, secrets):
    """Receipt notices naming what the imported driver raised, where, and its last HTTP exchange.

    The first notice keeps the DRIVER_ERROR_NOTICE prefix that marks the attempt failed. The
    message, step and redacted HTTP summary come from harness-owned diagnostics; any known
    credential value is scrubbed again before it can reach a receipt or the job log.
    """
    message = _scrub(str(error), secrets)
    notices = [f"{DRIVER_ERROR_NOTICE}{type(error).__name__}" + (f": {message}" if message else "")]
    lines = trace_module.describe_trace() if trace_module is not None else ["step: unknown (no driver trace)"]
    notices.extend(f"Imported journey driver failure {_scrub(line, secrets)}" for line in lines)
    return notices


CELL_HTTP_HOST_SUFFIX = ".elb.amazonaws.com"


def http_cell_host(endpoint):
    """The cell host the imported driver may reach over plain HTTP, or None.

    Only the harness-provisioned AWS load balancer of the cell under test qualifies (owner ruling
    canary-http-cell-2026-10-08). HTTPS endpoints need no exemption; any other HTTP host stays
    refused by the driver's credential transport policy.
    """
    parsed = urllib.parse.urlsplit(endpoint or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme == "http" and host.endswith(CELL_HTTP_HOST_SUFFIX) and len(host) > len(CELL_HTTP_HOST_SUFFIX):
        return host
    return None


def documented_blockers(receipt):
    """The tracked limitations a BLOCKED journey receipt names, or [] when it is not one.

    A receipt is blocked by documented limitations only when nothing in it failed, the imported
    driver did not raise, and every blocked stage names the issue that blocks it (or the cell kind
    is the tracked #377 gap). Anything else is a failed attempt.
    """
    if not isinstance(receipt, dict) or receipt.get("status") != "blocked":
        return []
    notices = [str(notice) for notice in receipt.get("notices") or []]
    if any(notice.startswith(DRIVER_ERROR_NOTICE) for notice in notices):
        return []
    blockers = set()
    if any(notice.startswith(UNSUPPORTED_KIND_NOTICE) for notice in notices):
        blockers.add(UNSUPPORTED_KIND_ISSUE)
    for stage in receipt.get("stages") or []:
        if stage.get("status") == "fail":
            return []
        if stage.get("status") == "blocked":
            named = [str(b) for b in stage.get("blockedBy") or [] if str(b).strip()]
            if not named:
                return []
            blockers.update(named)
    return sorted(blockers)


CAPABILITY_MANIFEST = "/api/v1/capabilities/manifest"


def parse_server_identity(body):
    """deploymentRevision and its source from an anonymous capability manifest body, or None."""
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        return None
    server = (document.get("server") or document.get("Server") or {}) if isinstance(document, dict) else {}
    if not isinstance(server, dict):
        return None
    revision = server.get("deploymentRevision") or server.get("DeploymentRevision")
    source = server.get("deploymentRevisionSource") or server.get("DeploymentRevisionSource")
    return {"revision": revision, "source": source} if isinstance(revision, str) else None


def server_identity(endpoint, *, opener=urllib.request.urlopen):
    """The revision the live deployment advertises on its anonymous capability manifest, or None."""
    try:
        with opener(endpoint.rstrip("/") + CAPABILITY_MANIFEST, timeout=15) as response:
            return parse_server_identity(response.read().decode("utf-8"))
    except Exception:
        return None


def observed_server(identity, running_image):
    """Receipt `server` pins from observations only: never copied from the manifest.

    sourceSha is the commit the endpoint advertises; image is what the control plane reports
    running. Anything unobserved stays visibly unobserved, so validate_attempt cannot match it.
    """
    revision = (identity or {}).get("revision") or ""
    source = (identity or {}).get("source")
    commit = source in ("commit-sha", "assembly-metadata") and re.fullmatch(r"[0-9a-f]{40}", revision)
    image = running_image or "unobserved"
    if source == "image-digest" and running_image and running_image.rsplit("@", 1)[-1] != revision:
        image = "unobserved"
    return {"sourceSha": revision if commit else UNOBSERVED_SHA, "image": image}


# Journey attempts are idempotent (owner decision 7 of 2026-10-10). The journey's stage 3 and 4
# objects carry fixed authored names: the secure connection (`execution.datasource.name`,
# journey_source) and the standalone style (`execution.styleId`). Attempt 1 leaves them on the cell,
# so attempt 2 failed at CreateConnectionAsync on the existing connection and never added evidence.
# Strategy: delete-and-recreate. Before attempt N > 1 the harness deletes what attempt N-1 left,
# through the cell admin REST API with the cell's admin key, and attempt N re-creates every object
# through the same SDK and CLI calls attempt 1 used, so the retry proves creation again rather than
# reusing (and never re-proving) a prior attempt's object. The uploaded table needs nothing: the
# upload passes OverwriteExisting=true. Studio objects are keyed by the attempt's own workspace id.
RETRY_STRATEGY_NOTICE = "Journey retry strategy: delete-and-recreate. "
CONNECTIONS_PATH = "/api/v1/admin/connections"
STYLES_PATH = "/ogc/styles"
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_STYLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class _HoldRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never carry the admin key to another location


def admin_request(method, url, admin_key, *, timeout=30):
    """(status, JSON document or None) for one cell admin call; 0 when the cell did not answer.
    The key travels only in the X-API-Key header, is never re-sent on a redirect, and never
    appears in what is returned."""
    request = urllib.request.Request(url, method=method,
                                     headers={"X-API-Key": admin_key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_HoldRedirect).open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, (error.read() if error.fp else b"")
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        return status, None


def _deleted(status):
    return {200: "deleted", 204: "deleted", 404: "already absent",
            409: "refused 409 (in use by a service the earlier attempt published; this attempt will "
                 "meet the existing object)"}.get(status, f"answered {status or 'nothing'}")


def reset_prior_attempt(endpoint, admin_key, execution, number, *, request=None):
    """Delete attempt N-1's fixed-name journey objects before attempt N; returns the receipt notice.

    Attempt 1 has nothing to reset and gets no notice. Every outcome is reported, including a
    deletion the cell refused, so the notice says exactly what the retry starts from.
    """
    if number <= 1:
        return []
    request = request or admin_request
    base = (endpoint or "").rstrip("/")
    name = str(((execution or {}).get("datasource") or {}).get("name") or "")
    style = str((execution or {}).get("styleId") or "")
    steps = []
    if name:
        status, document = request("GET", base + CONNECTIONS_PATH, admin_key)
        rows = document.get("data", document) if isinstance(document, dict) else document
        if status != 200 or not isinstance(rows, list):
            steps.append(f"listing connections answered {status or 'nothing'}, so no {name} connection was deleted")
        else:
            ids = [str(row.get("connectionId")) for row in rows
                   if isinstance(row, dict) and row.get("name") == name]
            if not ids:
                steps.append(f"no {name} connection was left")
            for connection_id in ids:
                if not _GUID.match(connection_id):
                    steps.append(f"{name} connection with a malformed id was not deleted")
                    continue
                status, _ = request("DELETE", f"{base}{CONNECTIONS_PATH}/{connection_id}", admin_key)
                steps.append(f"{name} connection {connection_id} {_deleted(status)}")
    if style and _STYLE_ID.match(style):
        status, _ = request("DELETE", f"{base}{STYLES_PATH}/{urllib.parse.quote(style, safe='')}", admin_key)
        steps.append(f"style {style} {_deleted(status)}")
    return [f"{RETRY_STRATEGY_NOTICE}Before attempt {number} the harness removed attempt {number - 1}'s "
            "fixed-name objects through the cell admin API, and this attempt re-creates them through "
            "the journey's own SDK and CLI calls: " + ("; ".join(steps) or "nothing to remove") + "."]


def attempt(cell, number, endpoint, admin_key, running_image=None):
    driver, adapter = drivers()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    evidence_uri = bound_evidence_uri(cell, run_id, run_attempt)
    directory = cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    workdir = directory / f"work-{number}"
    contract = driver.load(HERE / "journey.v1.json")
    policy = driver.load(HERE / "control-plane-roster.v1.json")
    pinned = manifest()
    template = driver.load(adapter.DEFAULT_TARGET)
    targets = cloud_targets()
    kind = targets.kind_of(cell)
    if kind is not None and endpoint is not None:
        # honua-release#377: the cell's own target document. It names references only; the
        # driver mints the journey principals through the cell admin API and revokes them.
        target = targets.build(template, cell=cell, endpoint=endpoint)
    else:
        target = template
        target.update(id=cell, kind=kind or "none")
        target["adminPassword"] = {"env": "HONUA_CLOUD_JOURNEY_ADMIN", "default": ""}
        target["compose"]["notes"] = "Externally provisioned cloud cell; the cloud harness owns teardown."
    target_path = directory / f"target-{number}.json"
    target_path.write_text(json.dumps(target) + "\n")
    notices = [f"Imported adapter protocol: {adapter.PROTOCOL}"]
    if endpoint is not None and kind is not None and number > 1:
        # The admin key goes only where the driver's own transport rule would send a credential:
        # an https endpoint, or the provisioned cell load balancer over plain HTTP.
        if urllib.parse.urlsplit(endpoint).scheme == "https" or http_cell_host(endpoint):
            try:
                notices.extend(reset_prior_attempt(endpoint, admin_key, target.get("execution"), number))
            except Exception as error:  # never let the reset stop the attempt from being recorded
                notices.append(f"{RETRY_STRATEGY_NOTICE}Resetting attempt {number - 1}'s objects raised "
                               f"{type(error).__name__}; attempt {number} meets whatever it left.")
        else:
            notices.append(f"{RETRY_STRATEGY_NOTICE}Not applied before attempt {number}: the endpoint is "
                           "refused for credentials, so attempt {0}'s objects were not removed.".format(number - 1))
    results = None
    workspace = driver.pins.ClientWorkspace(status="blocked", root=None,
                                            reason="cloud cell did not reach the driver")
    attribution = None
    if "discovery" in sys.modules:
        sys.modules["discovery"].reset_trace()
    try:
        if endpoint is not None:
            # pins.CANDIDATE_ENV_ALLOWLIST is the only environment run_live keeps.
            # That drops AWS_*, ACTIONS_ID_TOKEN_REQUEST_*, HONUA_AWS_*, GITHUB_TOKEN
            # and GH_TOKEN before npm install and the candidate CLIs start.
            cell_host = http_cell_host(endpoint)
            with (admin_credential(admin_key), external_image(driver, running_image),
                  driver.probes.allow_http_cell(cell_host)):
                transport = driver.probes.credential_transport(endpoint.rstrip("/") + "/")
                notices.append(f"Candidate transport: {transport or 'refused'} ({urllib.parse.urlsplit(endpoint).hostname})")
                with driver.pins.candidate_sandbox():
                    workspace, results, observed_notices, _ = driver.run_live(
                        target, pinned, contract, workdir, endpoint, True)
            notices.extend(observed_notices)
    except Exception as error:
        # Driver exceptions must still produce an attempt receipt, without retaining credentials
        # or raw tool output. The outer finally still checks cost and destroys infrastructure.
        failure = driver_failure_notices(error, sys.modules.get("discovery"), (
            admin_key, *(os.environ.get(env) for field, env in cloud_targets().DATASOURCE_ENV.items()
                         if field != "port")))
        notices.extend(failure)
        print(f"{cell} journey attempt {number}: " + " | ".join(failure), file=sys.stderr, flush=True)
        attribution = "infrastructure"
    unsupported = kind is None
    if unsupported:
        notices.append("Owned receipt schema lacks this cloud kind and live evidence source; "
                       "this contract receipt cannot qualify the cell (honua-release#377).")
    build_only = results is None or unsupported
    if build_only:
        target["kind"] = "none"
    receipt = driver.build_receipt(
        manifest=pinned, journey=contract, roster=driver.roster_verdict(policy,
            driver.load(Path(os.environ["HONUA_CLOUD_REST_ROSTER"])) if os.environ.get("HONUA_CLOUD_REST_ROSTER") else None,
            driver.load(Path(os.environ["HONUA_CLOUD_MCP_ROSTER"])) if os.environ.get("HONUA_CLOUD_MCP_ROSTER") else None),
        evidence_uri=evidence_uri,
        mode="build" if build_only else "live", target=target,
        target_path=target_path, target_base_url=endpoint, workspace=workspace,
        stage_results=None if build_only else results, notices=notices)
    # build_receipt attributes live observations to the target kind (live-aws-ecs or
    # live-aws-serverless); unsupported cloud kinds retain a blocked build receipt above.
    if not build_only:
        for stage in receipt["stages"]:
            stage["evidence"]["source"] = "live-" + kind
    receipt["target"]["composeProject"] = None
    identity = None
    if endpoint is not None:
        # honua-release#381: a receipt for a real cell states what that cell reported, not what the
        # manifest says it should be, even when the driver failed before any stage ran;
        # validate_attempt then compares the two.
        identity = server_identity(endpoint)
        receipt["server"] = observed_server(identity, running_image)
    driver.validate_receipt(receipt, HERE / "receipt.schema.json")
    path = directory / f"receipt-{number}.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    return {"number": number, "runId": run_id, "runAttempt": run_attempt, "cell": cell,
            "candidateDigest": candidate_digest(), "receipt": str(path.relative_to(ROOT / "e2e")),
            "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "failureAttribution": attribution or ("infrastructure" if receipt["status"] != "pass" else None),
            "observedServer": identity}


def validate_attempt(record, receipt, cell, *, run_id, run_attempt, at=None):
    driver, _ = drivers()
    driver.validate_receipt(receipt, HERE / "receipt.schema.json")
    # A receipt speaks only for its own cell kind: a build receipt ("none"), or live evidence of
    # exactly this cell's kind. Neither the kind nor any stage source may name another cell kind.
    cell_kind = cell.split("/")[0]
    if (receipt["target"]["kind"] not in ("none", cell_kind)
            or not {stage["evidence"]["source"] for stage in receipt["stages"]}
            <= {"harness-build", "live-" + cell_kind}):
        raise ValueError("cell receipt kind or evidence source is not this cell's")
    contract = driver.load(HERE / "journey.v1.json")
    expected_stages = [(stage["number"], stage["id"], stage["command"]) for stage in contract["stages"]]
    actual_stages = [(stage["number"], stage["stage"], stage["command"]) for stage in receipt["stages"]]
    if actual_stages != expected_stages or receipt["evidenceKey"] != contract["evidenceKey"]:
        raise ValueError("cell receipt does not cover the pinned journey contract")
    if receipt["status"] == "pass" and receipt["roster"]["status"] != "pass":
        raise ValueError("passing receipt lacks authoritative candidate roster evidence")
    pinned = manifest()
    server = pinned["components"]["honua-server"]
    if (record.get("cell") != cell or receipt["target"]["id"] != cell
            or record.get("runId") != run_id or record.get("runAttempt") != run_attempt
            or record.get("candidateDigest") != candidate_digest()
            or receipt["release"] != pinned["platformRelease"]
            or receipt["clientArtifacts"] != driver.pins.receipt_pins(pinned)):
        raise ValueError("cell receipt is bound to the wrong candidate, run or cell")
    # A cell receipt's server pins are what the cell advertised and its control plane reported
    # (honua-release#381). No attempt, failed or not, may show a server other than the candidate.
    # ECS (DescribeTasks) and serverless (Lambda GetFunction, the Lambda pin) report their running
    # image; a failed attempt may leave a value visibly unobserved and is still recorded as that
    # cell's failure; a passing one must show the candidate.
    candidate = {"sourceSha": server["sha"], "image": candidate_image(cell, pinned)}
    if receipt["server"] != candidate:
        if (receipt["server"]["sourceSha"] not in (candidate["sourceSha"], UNOBSERVED_SHA)
                or receipt["server"]["image"] not in (candidate["image"], "unobserved")):
            raise ValueError("cell receipt is bound to the wrong candidate, run or cell")
        if receipt["status"] == "pass":
            raise ValueError("cell receipt server identity was not observed on the live cell")
    suffix = run_binding(run_id, run_attempt)
    uris = [stage["evidence"]["uri"] for stage in receipt["stages"]]
    if len(set(uris)) != 1 or uris[0].count("#") != 1 or not uris[0].endswith(suffix):
        raise ValueError("cell receipt is bound to the wrong run")
    generated = datetime.fromisoformat(receipt["generatedAt"].replace("Z", "+00:00"))
    age = ((at or datetime.now(timezone.utc)) - generated).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError("stale cell receipt")
    if receipt["status"] != "pass":
        if record.get("failureAttribution") not in ("model", "infrastructure"):
            raise ValueError("failed attempt lacks failure attribution")
        return False
    if receipt["mode"] != "live" or receipt["target"]["kind"] != cell.split("/")[0]:
        raise ValueError("passing cell receipt lacks live cloud evidence")
    for stage in receipt["stages"]:
        evidence = stage["evidence"]
        if (evidence["source"] != "live-" + cell.split("/")[0]
                or evidence["freshness"] != "verified-current" or evidence["completeness"] != "complete"):
            raise ValueError("passing cell receipt lacks current complete cloud evidence")
        observed = datetime.fromisoformat(evidence["observedAt"].replace("Z", "+00:00"))
        if not 0 <= (generated - observed).total_seconds() <= 86400:
            raise ValueError("stale stage evidence")
    return True


def check_cost(path, ceiling, *, started_at):
    # A current run-scoped meter must supply cumulative USD before infrastructure is destroyed.
    # AWS Cost Explorer's delayed account totals cannot honestly serve as this meter; the reading is
    # e2e/cost_meter.py's live estimate (owner decision 9), and Cost Explorer's lagged actuals for
    # earlier runs are enforced by the next run (aggregate's prior_runs).
    ceiling = Decimal(str(ceiling))
    if not ceiling.is_finite() or ceiling <= 0:
        raise ValueError("cost ceiling must be finite and positive")
    cost = json.loads(Path(path).read_text())
    if (cost.get("runId") != os.environ.get("GITHUB_RUN_ID", "local")
            or cost.get("runAttempt") != os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            or cost.get("currency") != "USD" or cost.get("scope") != "run"):
        raise ValueError("cost meter is not bound to this run")
    measured = datetime.fromisoformat(cost["measuredAt"].replace("Z", "+00:00"))
    if not started_at <= measured <= datetime.now(timezone.utc):
        raise ValueError("stale cost meter")
    # The meter's own fields (estimate basis, the lagged actual slot) travel with the verdict.
    extra = {key: cost[key] for key in ("meter", "estimateUsd", "estimateBasis", "actualUsd", "actualAsOf",
                                        "currency") if key in cost}
    if cost.get("status") == "unavailable":
        # The meter ran and could not price the whole run: named, never pass.
        return {**extra, "status": "unavailable", "why": cost.get("why", "run cost meter unavailable"),
                "ceilingUsd": str(ceiling), "measuredAt": cost["measuredAt"], "scope": "run",
                "runId": cost["runId"], "runAttempt": cost["runAttempt"]}
    amount = Decimal(str(cost["amount"]))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid cost amount")
    return {**extra, "status": "fail" if amount > ceiling else "pass", "amountUsd": str(amount),
            "ceilingUsd": str(ceiling), "measuredAt": cost["measuredAt"], "scope": "run",
            "runId": cost["runId"], "runAttempt": cost["runAttempt"],
            "candidateDigest": candidate_digest()}


def cleanup(cell):
    # run_live with an external URL never starts Compose or a second AWS deployment. Its local
    # registry/install workspace is the only additional resource it creates. Destroying the cell
    # removes its server/database contents as well. Backstop cleanup runs even without a receipt.
    directory = cell_dir(cell)
    for workdir in directory.glob("work-*"):
        if workdir.is_symlink():
            workdir.unlink()
        else:
            shutil.rmtree(workdir)


def prior_run_failures(prior_runs):
    """Earlier runs whose SETTLED Cost Explorer spend exceeded their own ceiling (decision 9: the
    ceiling is enforceable at the next run). Settled spend only ever grows, so an over-ceiling
    settled figure fails even while later days are pending or the history is incomplete. The
    current run's own row (a rerun's earlier attempts) is judged by prior_run_gate instead."""
    failures = []
    for prior in (prior_runs or {}).get("runs", []):
        if prior.get("currentRun"):
            continue
        try:
            settled = Decimal(str(prior.get("settledUsd", prior["actualUsd"] if prior.get("status") == "measured"
                                            else "0")))
            ceiling = Decimal(str(prior["ceilingUsd"]))
        except Exception:
            failures.append(f"prior run {prior.get('runId')} cost reading is malformed")
            continue
        if settled > ceiling:
            failures.append(f"prior run {prior['runId']} cost ${settled} (Cost Explorer, settled spend "
                            f"through {prior.get('actualAsOf')}) exceeded its ${ceiling} ceiling")
    return failures


def prior_run_gate(prior_runs, *, run_id, run_attempt, final_cost):
    """(failures, gaps) the Cost Explorer reading adds to a full-scope run. A gap (no usable reading,
    or a rerun whose earlier attempts have not settled) blocks a lenient run and fails a strict one."""
    failures, gaps = prior_run_failures(prior_runs), []
    if prior_runs.get("status") != "measured":
        gaps.append(f"prior-run cost actuals {prior_runs.get('status', 'unavailable')}: "
                    f"{prior_runs.get('why', 'no Cost Explorer reading')}")
    elif str(run_attempt) != "1":
        # A rerun shares its run id with the earlier attempts, whose cells the live estimate does
        # not cover. Their spend must be settled in Cost Explorer and fit the ceiling with this one.
        own = next((r for r in prior_runs.get("runs", []) if r.get("runId") == str(run_id)), None)
        if own is None or own.get("status") != "measured":
            gaps.append(f"rerun attempt {run_attempt}: earlier attempts' spend is "
                        f"{'not yet in Cost Explorer' if own is None else own.get('status')}")
        else:
            estimate = Decimal(str((final_cost or {}).get("amountUsd") or "0"))
            total, ceiling = estimate + Decimal(str(own["settledUsd"])), Decimal(str(own["ceilingUsd"]))
            if total > ceiling:
                failures.append(f"run {run_id} cost ${total} (this attempt's estimate + earlier attempts' "
                                f"settled Cost Explorer spend) exceeds its ${ceiling} ceiling")
    return failures, gaps


def aggregate(reports, reports_root, *, require_real, full_scope, run_id, run_attempt,
              final_cost=None, prior_runs=None):
    cells = []
    failures = []
    blocked = []
    # A certifying (full-scope) run fails on an earlier run's settled over-ceiling actual, naming it,
    # and cannot pass without a usable Cost Explorer reading (decision 9).
    if full_scope and prior_runs is not None:
        prior_failures, prior_gaps = prior_run_gate(prior_runs, run_id=run_id, run_attempt=run_attempt,
                                                    final_cost=final_cost)
        failures.extend(prior_failures)
        (failures if require_real else blocked).extend(prior_gaps)
    # Per-cell readings predate later cells, teardowns and iac-live. Only a reading taken after
    # every cloud job finished bounds the whole run. A meter that produced no reading at all blocks
    # a non-strict run (named, never pass); require_real fails on it.
    if final_cost is not None and final_cost.get("status") == "unavailable" and not require_real:
        blocked.append("final run cost: " + final_cost.get("why", "no run cost meter reading"))
    elif final_cost is not None and final_cost.get("status") != "pass":
        failures.append("final run cost: " + final_cost.get("why", "run cost ceiling exceeded"))
    elif final_cost is None and full_scope:
        failures.append("final run cost evidence missing after all cloud jobs")
    by_cell = {}
    for report in reports:
        cell = report.get("cell", report.get("gate", "unknown"))
        preview = cell not in GA_CELLS
        row = {**report, "evidenceTier": "Preview" if preview else "GA"}
        cells.append(row)
        by_cell.setdefault(cell, []).append(row)
        # Preview journey failures are informational. The run-wide spend ceiling remains
        # independent of topology maturity, including a later snapshot from a Preview cell.
        cost = report.get("cost", {})
        if (preview and cost.get("scope") == "run" and cost.get("runId") == run_id
                and cost.get("runAttempt") == run_attempt and cost.get("status") == "fail"):
            failures.append(f"run cost ceiling exceeded during {cell}")
    if full_scope:
        failures.extend(f"full-scope cloud reports missing required cells: {cell}"
                        for cell in GA_CELLS if cell not in by_cell)
    for cell in GA_CELLS:
        rows = by_cell.get(cell, [])
        if not rows:
            continue
        if len(rows) != 1:
            failures.append(f"duplicate cell report: {cell}")
            continue
        report = rows[0]
        try:
            attempts = report.get("journeyAttempts", [])
            if not attempts or [a["number"] for a in attempts] != list(range(1, len(attempts) + 1)) or len(attempts) > 2:
                raise ValueError("missing or invalid attempt history")
            passed = False
            attempt_blockers = []
            for record in attempts:
                # Uploaded files are loaded relative to the matching report's artifact directory.
                relative = Path(record["receipt"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe receipt path")
                path = Path(report["artifactDirectory"]) / relative
                if not path.resolve().is_relative_to(Path(reports_root).resolve()):
                    raise ValueError("receipt escapes artifact directory")
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != record["receiptSha256"]:
                    raise ValueError("receipt digest mismatch")
                if passed:
                    raise ValueError("attempt recorded after a passing journey")
                receipt = json.loads(data)
                passed = validate_attempt(record, receipt, cell, run_id=run_id, run_attempt=run_attempt)
                attempt_blockers.append(documented_blockers(receipt))
            cost = report.get("cost", {})
            # Outside require_real a cell that is BLOCKED only by tracked, documented limitations
            # (journey receipts, scenarios, a missing run cost meter) is reported blocked, never
            # pass. A failed attempt, an over-ceiling or unbound cost reading, or require_real
            # leaves it a failure.
            if (not require_real and report["status"] == "blocked"
                    and (passed or all(attempt_blockers)) and cost.get("status") in ("pass", "unavailable")):
                blocked.append(f"{cell}: {report.get('why') or 'blocked'}")
                continue
            if not passed or report["status"] != "pass":
                raise ValueError("full-scope cloud reports did not all pass: journey/cell failed")
            if (cost.get("status") != "pass" or cost.get("scope") != "run"
                    or cost.get("runId") != run_id or cost.get("runAttempt") != run_attempt
                    or cost.get("candidateDigest") != candidate_digest()):
                raise ValueError("missing passing run cost ceiling evidence")
            amount, ceiling = Decimal(cost["amountUsd"]), Decimal(cost["ceilingUsd"])
            if not amount.is_finite() or not ceiling.is_finite() or not 0 <= amount <= ceiling or ceiling <= 0:
                raise ValueError("invalid or over-ceiling run cost evidence")
            measured = datetime.fromisoformat(cost["measuredAt"].replace("Z", "+00:00"))
            if not 0 <= (datetime.now(timezone.utc) - measured).total_seconds() <= 86400:
                raise ValueError("stale run cost evidence")
        except Exception as error:
            failures.append(f"{cell}: {type(error).__name__}: {error}")
    status = "fail" if failures else "blocked" if blocked else "pass"
    if not full_scope and status == "pass":
        status = "blocked"
    return {"gate": "cloud-parity", "status": status, "why": "; ".join(failures) if failures else
            ("blocked by tracked limitations: " + "; ".join(blocked)) if blocked else
            ("GA cloud journeys passed" if full_scope else "focused dispatch is diagnostic only"),
            "cells": cells, "canaryProbes": [probe for row in cells for probe in row.get("canaryProbes", [])],
            "certifying": full_scope and require_real and status == "pass",
            "certifyingScope": full_scope, "lambdaGaQualification": "pending",
            "finalCost": final_cost, "priorRunCosts": prior_runs, "generatedAt": now()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate cloud artifacts and assemble the GA gate")
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--final-cost", type=Path,
                        help="run-cost.json refreshed after every cloud job completed")
    parser.add_argument("--final-cost-after",
                        help="ISO time the aggregation began; the final reading must be later")
    parser.add_argument("--cost-ceiling-usd", default=os.environ.get("HONUA_CLOUD_COST_CEILING_USD", "20"))
    parser.add_argument("--prior-run-costs", type=Path,
                        help="cost_meter.py prior-runs output: Cost Explorer actuals of earlier runs")
    args = parser.parse_args()
    prior_runs = None
    if args.prior_run_costs is not None:
        try:
            prior_runs = json.loads(args.prior_run_costs.read_text())
        except FileNotFoundError:
            prior_runs = {"status": "unavailable", "why": f"no reading at {args.prior_run_costs}", "runs": []}
        except ValueError as error:
            prior_runs = {"status": "unavailable", "why": f"unreadable: {type(error).__name__}", "runs": []}
    final_cost = None
    if args.final_cost is not None:
        try:
            after = datetime.fromisoformat(args.final_cost_after.replace("Z", "+00:00"))
            final_cost = check_cost(args.final_cost, args.cost_ceiling_usd, started_at=after)
        except FileNotFoundError:
            final_cost = {"status": "unavailable", "why": f"unavailable: no reading at {args.final_cost}"}
        except Exception as error:
            final_cost = {"status": "fail", "why": f"unavailable: {type(error).__name__}"}
    reports = []
    for path in sorted(args.reports.rglob("gate-report-cloud.json")):
        try:
            report = json.loads(path.read_text())
        except (ValueError, OSError):
            report = {"gate": "invalid-artifact", "status": "fail", "why": "unreadable cell report"}
        report["artifactDirectory"] = str(path.parent)
        reports.append(report)
    report = aggregate(reports, args.reports,
        require_real=os.environ.get("REQUIRE_REAL") == "true",
        full_scope=os.environ.get("FULL_SCOPE") == "true",
        run_id=os.environ.get("GITHUB_RUN_ID", "local"),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"), final_cost=final_cost,
        prior_runs=prior_runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
