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
# Owner amendment 2026-10-16 supersedes decisions 12/18: EKS is informational Preview.
# Founder re-tier 2026-10-25: AWS ECS is the only GA cloud target; Lambda (aws-serverless) is Preview.
GA_CELLS = tuple(f"aws-ecs/redis-{redis}" for redis in ("off", "on"))
# aws-mixed is out for 2026.1 rc.3 (no examples/aws-mixed root); restore it when honua-iac#209 lands.
PREVIEW_TARGETS: tuple[str, ...] = ("aws-serverless", "aws-eks")


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


def pack_handoff(admin_key, datasource=None, *, tenant_signing_key=None, installed_clients=None):
    """The sealed journey handoff: the bare application key, or a JSON object with the datasource."""
    if not datasource and tenant_signing_key is None and installed_clients is None:
        return admin_key
    document = {"schema": HANDOFF_SCHEMA, "appKey": admin_key}
    if datasource:
        document["datasource"] = {name: datasource[name] for name in DATASOURCE_FIELDS}
    if tenant_signing_key is not None:
        _validate_tenant_signing_key(tenant_signing_key)
        document["tenantSigningKey"] = tenant_signing_key
    if installed_clients is not None:
        document["installedClients"] = installed_clients
    return json.dumps(document, separators=(",", ":"))


def _validate_tenant_signing_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid sealed tenant fixture key")
    return value


def handoff_tenant_signing_key(value):
    if not value.startswith("{"):
        return None
    document = json.loads(value)
    if "tenantSigningKey" not in document:
        return None
    if document.get("schema") != HANDOFF_SCHEMA:
        raise ValueError("invalid sealed tenant fixture schema")
    return _validate_tenant_signing_key(document["tenantSigningKey"])


# S10 large-import (honua-release#548): the admit job adds the presigned GET URL of the staged
# fixture. It is a bearer credential for that object, so it travels only inside the sealed handoff.
SOURCE_URL_FIELD = "s10SourceUrl"
MAX_SOURCE_URL_CHARS = 8192


def _source_url_ok(value):
    return (isinstance(value, str) and value.startswith("https://") and len(value) <= MAX_SOURCE_URL_CHARS
            and not any(c.isspace() for c in value))


def attach_source_url(value, url):
    """The sealed-handoff plaintext `value` with the S10 presigned source URL added."""
    if not _source_url_ok(url):
        raise ValueError("S10 source URL must be one https URL")
    document = json.loads(value) if value.startswith("{") else {"schema": HANDOFF_SCHEMA, "appKey": value}
    document[SOURCE_URL_FIELD] = url
    return json.dumps(document, separators=(",", ":"))


def handoff_source_url(value):
    """The S10 presigned source URL a sealed handoff carries, or None."""
    if not value or not value.startswith("{"):
        return None
    try:
        url = json.loads(value).get(SOURCE_URL_FIELD)
    except (ValueError, AttributeError):
        return None
    return url if _source_url_ok(url) else None


def unpack_handoff(value):
    """(application key, datasource or None) from a sealed handoff; a bare key is accepted.

    A JSON handoff carries the datasource, the S10 source URL, or both."""
    if not value.startswith("{"):
        return value, None
    document = json.loads(value)
    datasource = document.get("datasource") if isinstance(document, dict) else None
    if isinstance(document, dict) and datasource is None and (SOURCE_URL_FIELD in document or "tenantSigningKey" in document):
        if (document.get("schema") != HANDOFF_SCHEMA or not isinstance(document.get("appKey"), str)
                or (SOURCE_URL_FIELD in document and not _source_url_ok(document[SOURCE_URL_FIELD]))):
            raise ValueError("malformed cloud journey handoff")
        handoff_tenant_signing_key(value)
        return document["appKey"], None
    if (not isinstance(document, dict) or document.get("schema") != HANDOFF_SCHEMA
            or not isinstance(document.get("appKey"), str) or not isinstance(datasource, dict)
            or set(datasource) != set(DATASOURCE_FIELDS)
            or not all(isinstance(datasource[name], str) and datasource[name] for name in DATASOURCE_FIELDS
                       if name != "port")
            or type(datasource["port"]) is not int):
        raise ValueError("malformed cloud journey handoff")
    handoff_tenant_signing_key(value)
    return document["appKey"], datasource


@contextmanager
def tenant_application_key(value):
    """Expose only the sealed application fixture key after stripping cloud/CI credentials."""
    name = "HONUA_CANARY_TENANT_SIGNING_KEY"
    previous = os.environ.get(name)
    try:
        if value is not None:
            _validate_tenant_signing_key(value)
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


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
def external_image(driver, image_ref, image_detail=None):
    # run_live reports no image for an external URL; it cannot see a cloud deployment. Supply
    # the image read back from the cloud control plane to the owned observe(), unchanged otherwise,
    # and the control plane's account of it (which task or digest disagreed) for 1.1's detail.
    original = driver.observe

    def observe(target, base_url, workspace, bindir, _image_ref, expected_revision):
        observation = original(target, base_url, workspace, bindir, image_ref, expected_revision)
        if image_detail and hasattr(observation, "image_detail"):
            observation.image_detail = str(image_detail)[:600]
        return observation

    driver.observe = observe
    try:
        yield
    finally:
        driver.observe = original


def _image_repository(reference):
    """`registry/name` of an image reference, without its tag or digest."""
    name = reference.split("@", 1)[0]
    head, _, tail = name.rpartition("/")
    return (f"{head}/{tail.split(':', 1)[0]}" if head else tail.split(":", 1)[0]).lower()


# A task listed with desired status RUNNING that has not reached RUNNING yet (a second MultiNode
# task still starting) is waited for, up to ECS_IMAGE_POLLS reads ECS_IMAGE_POLL_SECONDS apart.
ECS_STARTING = {"PROVISIONING", "PENDING", "ACTIVATING"}
ECS_IMAGE_POLLS = 12
ECS_IMAGE_POLL_SECONDS = 10.0


def _short(value, keep=19):
    value = str(value or "")
    return value if len(value) <= keep else value[:keep] + "..."


def observe_ecs_image(target, pinned, *, run=subprocess.run, sleep=None):
    """(image, detail): the pinned server image if every RUNNING task of the deployed ECS service
    runs it, else None, with a detail naming the task, container and digest that disagree.

    Read from ECS DescribeTasks, never inferred from the apply inputs.

    The cell deploys `repository@digest` with no tag (e2e-cloud-aws.yml: honua-iac refuses a tag
    next to the digest), so a container is matched by repository, never by the manifest's tagged
    `image`. Matching the tag made every ECS cell report no running image, so stage 1's
    1.1-candidate-image stayed blocked (run 38110106995). The digest ECS reports must be the
    manifest's index digest or its child for the cell architecture (platformDigests, amd64 for
    x86_64); the value returned is the manifest candidate (`candidate_image`), as on Lambda.

    The Redis-on cell runs two MultiNode tasks (#535). A task that is still starting is waited
    for, and once the wait is over only RUNNING tasks count; on run 38119871941 the Redis-on
    cell reported no running image while the single-task Redis-off cell matched.
    """
    import time
    sleep = sleep or time.sleep
    server = pinned["components"]["honua-server"]
    repository = _image_repository(server["image"])
    arch = {"x86_64": "amd64", "arm64": "arm64"}.get(str(server.get("awsEcsArchitecture") or "x86_64"))
    accepted = {server["digest"], str((server.get("platformDigests") or {}).get(arch) or "")} - {""}
    root = target._workdir
    if root is None:
        return None, "the ECS cell's Terraform working directory is unavailable"
    cluster, service = (target._tf(root, "output", "-raw", name).stdout.strip()
                        for name in ("ecs_cluster_name", "ecs_service_name"))
    if not cluster or not service:
        return None, "the root reported no ecs_cluster_name or ecs_service_name"

    def aws(*args):
        return json.loads(run(["aws", "ecs", *args, "--output", "json"], text=True,
                              capture_output=True, check=True).stdout)

    for poll in range(ECS_IMAGE_POLLS):
        arns = aws("list-tasks", "--cluster", cluster, "--service-name", service,
                   "--desired-status", "RUNNING")["taskArns"]
        described = aws("describe-tasks", "--cluster", cluster, "--tasks", *arns) if arns else {}
        tasks, failures = described.get("tasks") or [], described.get("failures") or []
        starting = [t for t in tasks if t.get("lastStatus") in ECS_STARTING]
        if not starting or poll == ECS_IMAGE_POLLS - 1:
            break
        sleep(ECS_IMAGE_POLL_SECONDS)
    task_id = lambda task: str(task.get("taskArn", "")).rsplit("/", 1)[-1] or "unknown"
    # Every listed task must be observed: a DescribeTasks failure or a missing record is never a pass.
    if failures or len(tasks) != len(arns):
        missing = [str(f.get("arn", "")).rsplit("/", 1)[-1] + f" ({f.get('reason')})" for f in failures]
        return None, (f"DescribeTasks described {len(tasks)} of {len(arns)} listed tasks of {service}"
                      + (f"; failures: {', '.join(missing)}" if missing else ""))
    running = [t for t in tasks if t.get("lastStatus") == "RUNNING"]
    others = [f"{task_id(t)} {t.get('lastStatus')}" for t in tasks if t.get("lastStatus") != "RUNNING"]
    if not running:
        return None, (f"service {service} has no RUNNING task"
                      + (f" ({', '.join(others)})" if others else ""))
    disagreements = []
    for task in running:
        servers = [c for c in task.get("containers", [])
                   if _image_repository(str(c.get("image", ""))) == repository]
        if not servers:
            names = ", ".join(f"{c.get('name')}={c.get('image')}" for c in task.get("containers", []))
            disagreements.append(f"task {task_id(task)} runs no {repository} container ({names or 'none'})")
        for container in servers:
            if container.get("imageDigest") not in accepted:
                disagreements.append(f"task {task_id(task)} container {container.get('name')} reports "
                                     f"{container.get('imageDigest') or 'no digest'}")
            elif container.get("lastStatus") != "RUNNING":
                disagreements.append(f"task {task_id(task)} container {container.get('name')} is "
                                     f"{container.get('lastStatus')}")
    if disagreements:
        return None, ("; ".join(disagreements) + f" (candidate {server['digest']}"
                      + (f" or its {arch} child {(server.get('platformDigests') or {}).get(arch)}"
                         if (server.get("platformDigests") or {}).get(arch) else "") + ")")
    detail = f"{len(running)} RUNNING task(s) of {service} report the candidate digest"
    if others:
        detail += f"; not counted: {', '.join(others)}"
    return f"{server['image']}@{server['digest']}", detail


def observed_ecs_image(target, pinned, *, run=subprocess.run, sleep=None):
    """The pinned server image only if every RUNNING task of the ECS service runs it, else None."""
    return observe_ecs_image(target, pinned, run=run, sleep=sleep)[0]


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
    roster = receipt.get("roster") if isinstance(receipt.get("roster"), dict) else {}
    if roster.get("status") == "fail":
        return []
    if roster.get("status") == "blocked":
        # The candidate roster exports are a tracked upstream limitation, never a pass.
        named = roster.get("blockedBy")
        named = [named] if isinstance(named, str) else list(named or [])
        named = [str(b) for b in named if str(b).strip()]
        if not named:
            return []
        blockers.update(named)
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


ATTEMPT_SUFFIX = "-attempt"


def attempt_datasource_name(base, number):
    """Attempt 1 keeps the authored datasource name; a retry gets its own. Attempt 1's connection
    usually cannot be deleted (the service it published still references it: 409), so a retry that
    re-used the name failed at CreateConnectionAsync before any stage could recover (runs
    39705901768, 39746615094, 39772292511)."""
    base = str(base or "")
    return base if number <= 1 or not base else f"{base}{ATTEMPT_SUFFIX}{number}"


def _base_datasource_name(name):
    head, sep, tail = str(name or "").rpartition(ATTEMPT_SUFFIX)
    return head if sep and tail.isdigit() and head else str(name or "")


def reset_prior_attempt(endpoint, admin_key, execution, number, *, request=None):
    """Delete attempt N-1's fixed-name journey objects before attempt N; returns the receipt notice.

    Attempt 1 has nothing to reset and gets no notice. Every outcome is reported, including a
    deletion the cell refused, so the notice says exactly what the retry starts from.
    """
    if number <= 1:
        return []
    request = request or admin_request
    base = (endpoint or "").rstrip("/")
    name = _base_datasource_name(((execution or {}).get("datasource") or {}).get("name"))
    style = str((execution or {}).get("styleId") or "")
    steps = []
    if name:
        status, document = request("GET", base + CONNECTIONS_PATH, admin_key)
        rows = document.get("data", document) if isinstance(document, dict) else document
        if status != 200 or not isinstance(rows, list):
            steps.append(f"listing connections answered {status or 'nothing'}, so no {name} connection was deleted")
        else:
            # Every earlier attempt's connection: the authored name and its per-attempt variants.
            ids = [str(row.get("connectionId")) for row in rows
                   if isinstance(row, dict) and (row.get("name") == name
                       or _base_datasource_name(row.get("name")) == name != row.get("name"))]
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


DISCOVERY_SIDECAR = "setup-discovery.json"
MAX_SIDECAR_BYTES = 4 * 1024 * 1024


def retain_discovery_sidecar(directory, workdir, number, secrets):
    """Copy the attempt's setup-discovery sidecar next to its receipt, credential-scrubbed.

    The sidecar holds the HTTP and installed-proxy tools/list results the stage 1 parity checks
    compared (run 38099175519 failed them with no evidence retained). The work directory is
    deleted at cleanup and never uploaded, because it can hold the application key, so the copy
    has every known credential value replaced and the minted journey keys scrubbed as well.
    Returns the retained path, or None when there is no sidecar.
    """
    source = Path(workdir) / DISCOVERY_SIDECAR
    if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_SIDECAR_BYTES:
        return None
    text = source.read_text(encoding="utf-8", errors="replace")
    values = [s for s in secrets if s]
    try:
        minted = json.loads((Path(workdir) / "private-principals.json").read_text(encoding="utf-8"))
        values.extend(v for v in (minted.get("keys") or {}).values() if isinstance(v, str) and v)
        if isinstance(minted.get("signingKey"), str):
            values.append(minted["signingKey"])
    except (OSError, ValueError, AttributeError):
        pass
    for value in sorted(set(values), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    target = Path(directory) / f"setup-discovery-{number}.json"
    target.write_text(text, encoding="utf-8")
    return target


def attempt(cell, number, endpoint, admin_key, running_image=None, *, image_detail=None, tenant_signing_key=None):
    driver, adapter = drivers()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    evidence_uri = bound_evidence_uri(cell, run_id, run_attempt)
    directory = cell_dir(cell)
    directory.mkdir(parents=True, exist_ok=True)
    workdir = directory / f"work-{number}"
    contract = driver.load(HERE / "journey.v1.json")
    pinned = manifest()
    template = driver.load(adapter.DEFAULT_TARGET)
    targets = cloud_targets()
    kind = targets.kind_of(cell)
    if kind is not None and endpoint is not None:
        # honua-release#377: the cell's own target document. It names references only; the
        # driver mints the journey principals through the cell admin API and revokes them.
        target = targets.build(template, cell=cell, endpoint=endpoint, tenant_fixture=tenant_signing_key is not None)
    else:
        target = template
        target.update(id=cell, kind=kind or "none")
        target["adminPassword"] = {"env": "HONUA_CLOUD_JOURNEY_ADMIN", "default": ""}
        target["compose"]["notes"] = "Externally provisioned cloud cell; the cloud harness owns teardown."
    datasource = (target.get("execution") or {}).get("datasource")
    if kind is not None and isinstance(datasource, dict) and datasource.get("name"):
        datasource["name"] = attempt_datasource_name(datasource["name"], number)
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
        sys.modules["discovery"].reset_calls()
    try:
        if endpoint is not None:
            # pins.CANDIDATE_ENV_ALLOWLIST is the only environment run_live keeps.
            # That drops AWS_*, ACTIONS_ID_TOKEN_REQUEST_*, HONUA_AWS_*, GITHUB_TOKEN
            # and GH_TOKEN before npm install and the candidate CLIs start.
            cell_host = http_cell_host(endpoint)
            with (admin_credential(admin_key), external_image(driver, running_image, image_detail),
                  driver.probes.allow_http_cell(cell_host)):
                transport = driver.probes.credential_transport(endpoint.rstrip("/") + "/")
                notices.append(f"Candidate transport: {transport or 'refused'} ({urllib.parse.urlsplit(endpoint).hostname})")
                with driver.pins.candidate_sandbox():
                    workspace, results, observed_notices, _ = driver.run_live(
                        target, pinned, contract, workdir, endpoint, True,
                        **({"tenant_signing_key": tenant_signing_key} if tenant_signing_key is not None else {}))
            notices.extend(observed_notices)
    except Exception as error:
        # Driver exceptions must still produce an attempt receipt, without retaining credentials
        # or raw tool output. The outer finally still checks cost and destroys infrastructure.
        failure = driver_failure_notices(error, sys.modules.get("discovery"), (
            admin_key, tenant_signing_key, *(os.environ.get(env) for field, env in cloud_targets().DATASOURCE_ENV.items()
                         if field != "port")))
        notices.extend(failure)
        print(f"{cell} journey attempt {number}: " + " | ".join(failure), file=sys.stderr, flush=True)
        attribution = "infrastructure"
    sidecar = retain_discovery_sidecar(directory, workdir, number, (
        admin_key, tenant_signing_key, *(os.environ.get(env) for field, env in cloud_targets().DATASOURCE_ENV.items()
                     if field != "port")))
    if sidecar is not None:
        notices.append(f"Setup discovery sidecar retained as {sidecar.name}")
    unsupported = kind is None
    if unsupported:
        notices.append("Owned receipt schema lacks this cloud kind and live evidence source; "
                       "this contract receipt cannot qualify the cell (honua-release#377).")
    build_only = results is None or unsupported
    if build_only:
        target["kind"] = "none"
    receipt = driver.build_receipt(
        manifest=pinned, journey=contract, roster=driver.roster_verdict(
            driver.load(Path(os.environ["HONUA_CLOUD_REST_ROSTER"])) if os.environ.get("HONUA_CLOUD_REST_ROSTER") else None,
            driver.load(Path(os.environ["HONUA_CLOUD_MCP_ROSTER"])) if os.environ.get("HONUA_CLOUD_MCP_ROSTER") else None,
            sys.modules["discovery"].calls() if "discovery" in sys.modules else [],
            source=driver.roster_source(
                Path(os.environ["HONUA_CLOUD_REST_ROSTER"]) if os.environ.get("HONUA_CLOUD_REST_ROSTER") else None,
                Path(os.environ["HONUA_CLOUD_MCP_ROSTER"]) if os.environ.get("HONUA_CLOUD_MCP_ROSTER") else None,
                os.environ.get("HONUA_CLOUD_ROSTER_SOURCE_SHA"), pinned)),
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
    record = {"number": number, "runId": run_id, "runAttempt": run_attempt, "cell": cell,
            "candidateDigest": candidate_digest(), "receipt": str(path.relative_to(ROOT / "e2e")),
            "receiptSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "failureAttribution": attribution or ("infrastructure" if receipt["status"] != "pass" else None),
            "observedServer": identity}
    if cell in ("aws-ecs/redis-off", "aws-serverless/redis-off") and endpoint is not None:
        import deterministic_refusal_profile as profile
        try:
            with (admin_credential(admin_key), driver.probes.allow_http_cell(http_cell_host(endpoint)),
                  driver.pins.candidate_sandbox(), tenant_application_key(tenant_signing_key)):
                proof = profile.produce(record, path, target_path, adapter=adapter,
                    pinned=pinned, digest=candidate_digest(), cell=cell)
        except Exception as exc:
            proof = {"status": "fail", "why": "profile prerequisite failed: " + type(exc).__name__}
        sidecar = directory / f"serving-refusal-{number}.json"
        sidecar.write_text(json.dumps(proof, indent=2) + "\n")
        record["servingRefusal"] = {"file": sidecar.name, "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}
        if proof.get("status") != "pass" and record["failureAttribution"] is None:
            record["failureAttribution"] = "infrastructure"
    return record


def profile_blocked(record, raw_path):
    """A Redis-off attempt whose serving/refusal profile is a documented block (no admissible
    prerequisite), as opposed to a profile that ran and failed."""
    import deterministic_refusal_profile as profile
    return profile.blocked_record(record, Path(raw_path))


def qualify_attempt(record, receipt, cell, *, raw_path, run_id, run_attempt,
                    at=None, pinned_manifest=None, pinned_manifest_digest=None, require_observation=True):
    """Preserve raw full-workflow verdict; admit only the explicit bounded off profile."""
    passed = validate_attempt(record, receipt, cell, run_id=run_id, run_attempt=run_attempt,
        at=at, pinned_manifest=pinned_manifest, pinned_manifest_digest=pinned_manifest_digest)
    if cell not in ("aws-ecs/redis-off", "aws-serverless/redis-off"):
        return passed
    import deterministic_refusal_profile as profile
    if profile.failed_record(record, receipt, Path(raw_path)):
        return False
    profile.validate_record(record, receipt, Path(raw_path), cell=cell,
        pinned=manifest() if pinned_manifest is None else pinned_manifest,
        digest=candidate_digest() if pinned_manifest_digest is None else pinned_manifest_digest,
        at=at, require_observation=require_observation)
    return True


def validate_attempt(record, receipt, cell, *, run_id, run_attempt, at=None,
                     pinned_manifest=None, pinned_manifest_digest=None):
    if (pinned_manifest is None) != (pinned_manifest_digest is None):
        raise ValueError("captured manifest and digest must be supplied together")
    if pinned_manifest_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", pinned_manifest_digest):
        raise ValueError("captured manifest digest must be SHA256")
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
    pinned = manifest() if pinned_manifest is None else pinned_manifest
    pinned_digest = candidate_digest() if pinned_manifest_digest is None else pinned_manifest_digest
    server = pinned["components"]["honua-server"]
    if (record.get("cell") != cell or receipt["target"]["id"] != cell
            or record.get("runId") != run_id or record.get("runAttempt") != run_attempt
            or record.get("candidateDigest") != pinned_digest
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


def cleanup_failure(report):
    """Cleanup is a safety obligation even when the journey is informational Preview."""
    if not isinstance(report, dict):
        return "cleanup report missing"
    if report.get("cleanupFailures") or report.get("s10Cleanup", {}).get("status") == "failed":
        return "run-owned resource cleanup failed"
    teardown = report.get("teardown", {})
    if report.get("provisionAttempted") is False:
        return "inconsistent cleanup obligation" if teardown.get("attempted") is True else None
    if report.get("provisionAttempted") is not True:
        return "provisioning cleanup obligation unknown"
    if teardown.get("attempted") is not True or teardown.get("completed") is not True:
        return "provisioned infrastructure cleanup incomplete"
    return None


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
        if preview and (cleanup_problem := cleanup_failure(report)):
            failures.append(f"{cell}: {cleanup_problem}")
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
                passed = qualify_attempt(record, receipt, cell, raw_path=path, run_id=run_id, run_attempt=run_attempt)
                attempt_blockers.append(documented_blockers(receipt))
            recorded_profiles = {(Path(report["artifactDirectory"]) / record["receipt"]).parent
                / f"serving-refusal-{record['number']}.json" for record in attempts if record.get("servingRefusal")}
            if any(path.resolve() not in {p.resolve() for p in recorded_profiles}
                   for path in Path(report["artifactDirectory"]).rglob("serving-refusal-*.json")):
                raise ValueError("produced serving/refusal sidecar omitted from attempt history")
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
