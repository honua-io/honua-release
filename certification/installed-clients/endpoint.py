"""Trusted fixture preparation and application-only execution against an existing cloud cell.

The secret context is handed between jobs only inside the existing encrypted application handoff.
It is never an artifact. The receipt contains only its nonsecret deployment binding.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
SCHEMA = "honua.installed-client-endpoint/v1"
CELLS = {"aws-ecs/redis-on", "aws-serverless/redis-on"}
ENVIRONMENT = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "DOTNET_ROOT",
               "DOTNET_CLI_HOME", "DOTNET_CLI_TELEMETRY_OPTOUT", "DOTNET_NOLOGO", "NUGET_PACKAGES"}
CLOUD_CREDENTIALS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                     "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                     "AWS_CONTAINER_CREDENTIALS_FULL_URI", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"}


class PrerequisiteUnavailable(ValueError):
    """A recognized cell prerequisite is absent; no installed-client execution is claimed."""


def cloud_policy():
    name = "installed_endpoint_cloud_policy"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE.parents[1] / "e2e/cloud_journey.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def runner():
    name = "installed_client_endpoint_runner"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@contextmanager
def module_scope():
    """Keep legacy sibling imports from replacing the terminal journey's independent oracles."""
    previous_path = list(sys.path)
    previous = {name: sys.modules.get(name) for name in ("oracles", "judge")}
    try:
        sys.path[:0] = [str(HERE), str(HERE / "interop")]
        for name, path in (("oracles", HERE / "oracles.py"), ("judge", HERE / "interop/judge.py")):
            private = "installed_endpoint_" + name
            if private not in sys.modules:
                spec = importlib.util.spec_from_file_location(private, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[private] = module
                spec.loader.exec_module(module)
            sys.modules[name] = sys.modules[private]
        yield
    finally:
        sys.path[:] = previous_path
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@module_scope()
def prepare(target, state, manifest, manifest_path, *, admin_key, signing_key):
    """Only authored SQL/admin fixture setup; never install or execute a client with cloud authority."""
    if not signing_key:
        raise ValueError("installed-client endpoint requires the run-owned bearer issuer")
    rx = runner()._regression()
    interop = runner()._interop()
    fixture = rx.load_fixture()
    slugs = [slug for driver, slug in rx.SUITE_DRIVERS.items() if rx.DRIVER_CLIENTS[driver][0] == "sdk"]
    workflows = [slug for driver, slug in rx.SUITE_DRIVERS.items() if rx.DRIVER_CLIENTS[driver][0] == "cli"]
    tag = secrets.token_hex(4)
    sql = rx.seed_sql(fixture, slugs, workflow_slugs=workflows) + interop.engine.seed_sql(rx, interop.fixture, tag)
    connection = target.seed_database(sql)
    database = {key: connection[key] for key in ("host", "port", "databaseName", "username")}
    database.update(provider="PostGIS", sslRequired=connection.get("sslRequired", True),
                    sslMode=connection.get("sslMode", "Require"))
    fixture["lifecycle"]["database"] = database
    api = rx.AdminApi(state["endpoint"], admin_key)
    published = rx.publish_harness(api, fixture, slugs, connection["password"])
    created = api.request("POST", "/api/v1/admin/connections/", {
        "name": interop.fixture["connection"]["namePrefix"] + tag, **database, "password": connection["password"]})
    cid = created.get("connectionId") or created.get("id")
    if not cid:
        raise ValueError("installed-client endpoint fixture connection has no identity")
    binding = {"endpoint": state["endpoint"], "cell": state["cell"], "runId": state["runId"],
               "runAttempt": state["runAttempt"], "candidateDigest": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
               "server": {"sourceSha": (state.get("observedServer") or {}).get("revision"), "image": state.get("runningImage")},
               "fixtureRevision": json.loads((HERE / "matrix.json").read_text())["fixtureRevision"],
               "matrixDigest": hashlib.sha256(json.dumps(json.loads((HERE / "matrix.json").read_text()), sort_keys=True).encode()).hexdigest(),
               "suiteFixtureDigest": hashlib.sha256((HERE / "fixture.v1.json").read_bytes()).hexdigest(),
               "interopFixtureDigest": hashlib.sha256((HERE / "interop/fixture.v1.json").read_bytes()).hexdigest(),
               "preparedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return {"schema": SCHEMA, "binding": binding, "database": database, "dbPassword": connection["password"],
            "published": published, "principals": rx.mint_principals(api, tag),
            "interopContext": {"tag": tag, "connectionId": cid},
            "interopPrincipals": rx.mint_principals(api, "interop-" + tag),
            "bearer": rx.mint_bearer(signing_key, issuer="https://terminal-journey.invalid", audience="terminal-journey")}


def read_context(path):
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("endpoint context must be readable only by its owner")
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError("endpoint context is not readable JSON") from None


def validate_context(context, manifest, matrix, manifest_path):
    """Reject another deployment/run, stale setup, unobserved image, or changed fixture before install."""
    try:
        binding = context["binding"]
        parsed = urllib.parse.urlsplit(binding["endpoint"])
        server = manifest["components"]["honua-server"]
        expected = {"sourceSha": server["sha"], "image": cloud_policy().candidate_image(binding["cell"], manifest)}
        prepared = datetime.fromisoformat(binding["preparedAt"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - prepared
        valid = (context["schema"] == SCHEMA and parsed.hostname
                 and (parsed.scheme == "https" or cloud_policy().http_cell_host(binding["endpoint"]) is not None)
                 and set(binding) == {"endpoint", "cell", "runId", "runAttempt", "candidateDigest", "server",
                                      "fixtureRevision", "matrixDigest", "suiteFixtureDigest", "interopFixtureDigest", "preparedAt"}
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path in ("", "/") and binding["cell"] in CELLS
                 and binding["runId"] == os.environ.get("GITHUB_RUN_ID")
                 and binding["runAttempt"] == os.environ.get("GITHUB_RUN_ATTEMPT", "1")
                 and binding["server"] == expected
                 and binding["candidateDigest"] == hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
                 and binding["fixtureRevision"] == matrix["fixtureRevision"]
                 and binding["matrixDigest"] == hashlib.sha256(json.dumps(matrix, sort_keys=True).encode()).hexdigest()
                 and binding["suiteFixtureDigest"] == hashlib.sha256((HERE / "fixture.v1.json").read_bytes()).hexdigest()
                 and binding["interopFixtureDigest"] == hashlib.sha256((HERE / "interop/fixture.v1.json").read_bytes()).hexdigest()
                 and timedelta(0) <= age <= timedelta(hours=1)
                 and all(context[name] for name in ("database", "dbPassword", "published", "bearer", "principals",
                                                    "interopContext", "interopPrincipals")))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("endpoint context identity, freshness, fixture or credential contract differs")


def verify_live_identity(context, manifest):
    """The current endpoint must independently advertise the pinned source, before and after clients."""
    try:
        url = context["binding"]["endpoint"].rstrip("/") + "/api/v1/capabilities/manifest"
        with urllib.request.urlopen(url, timeout=30) as response:
            server = json.load(response)["server"]
        observed = server.get("deploymentRevision")
        valid = (observed == manifest["components"]["honua-server"]["sha"]
                 and server.get("deploymentRevisionSource") in ("commit-sha", "assembly-metadata"))
    except Exception:
        valid = False
    if not valid:
        raise ValueError("endpoint did not independently advertise the exact candidate source")


def redact_receipt(receipt, context, admin_key):
    values = [admin_key, context["bearer"], context["dbPassword"]]
    values.extend(principal["key"] for group in ("principals", "interopPrincipals")
                  for principal in context[group].values())
    raw = json.dumps(receipt)
    for secret in sorted(set(values), key=len, reverse=True):
        if secret:
            raw = raw.replace(json.dumps(secret)[1:-1], "[redacted]")
    return json.loads(raw)


def redact_log(text, context, admin_key):
    return redact_receipt({"text": text}, context, admin_key)["text"]


@module_scope()
def run_cloud(state, context, admin_key, output):
    """Application-only half; all matrix cells and their original oracles execute unchanged."""
    run = runner()
    manifest_path = run.ROOT / "platform-manifest.yaml"
    manifest, matrix = run.load_inputs(manifest_path, run.DEFAULT_MATRIX)
    run.validate_release_inputs(manifest, matrix)
    validate_context(context, manifest, matrix, manifest_path)
    if any(context["binding"].get(name) != state.get(name) for name in ("endpoint", "cell", "runId", "runAttempt", "candidateDigest")):
        raise ValueError("installed-client fixture belongs to a different provisioned deployment")
    require_client_transport(context)
    with client_environment(context, admin_key):
        verify_live_identity(context, manifest)
        receipt = run.execute(manifest, matrix,
            f"https://github.com/honua-io/honua-release/actions/runs/{state['runId']}", endpoint_context=context)
        verify_live_identity(context, manifest)
    receipt = redact_receipt(receipt, context, admin_key)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    if run.verify_receipt(matrix, receipt):
        raise ValueError("installed-client endpoint receipt violates the complete matrix")
    return receipt


def require_client_transport(context):
    if cloud_policy().http_cell_host(context["binding"]["endpoint"]):
        # The approved ALB exception belongs to the harness. Published CLI/MCP clients retain
        # their independent HTTPS policy (terminal-journey/probes.py CLIENT_HTTPS_REFUSAL).
        raise PrerequisiteUnavailable("approved ALB HTTP cell needs HTTPS for the published-client workflow; no clients executed")


@contextmanager
def client_environment(context, admin_key=None):
    """No cloud role, OIDC exchange token or inherited build/repository token reaches an installer."""
    if any(os.environ.get(key) for key in CLOUD_CREDENTIALS):
        raise ValueError("endpoint clients must execute in a separate job without cloud credentials")
    previous = dict(os.environ)
    key = admin_key or previous.get("E2E_API_KEY")
    if not key:
        raise ValueError("endpoint application key is missing")
    safe = {name: value for name, value in previous.items() if name in ENVIRONMENT}
    safe.update(HONUA_SERVER_URL=context["binding"]["endpoint"], E2E_API_KEY=key)
    with tempfile.TemporaryDirectory(prefix="honua-endpoint-home-") as home:
        safe.update(HOME=home, DOTNET_CLI_HOME=home)
        try:
            os.environ.clear()
            os.environ.update(safe)
            yield
        finally:
            os.environ.clear()
            os.environ.update(previous)
