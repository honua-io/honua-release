#!/usr/bin/env python3
"""Orchestrator for the cross-client interop scenarios (``interop-*``).

One booted candidate, several installed clients, one piece of work handed between them. The
orchestrator never makes an HTTP request itself:

* the Python, JS and .NET SDKs each run as one long-lived runner process (``drivers/<sdk>/``)
  that calls only the installed SDK's client classes. A runner answers one JSON request per line
  on stdin with one JSON reply per line on stdout, and keeps its client instances between
  requests, so a revocation is observed on the very client instance that used the key;
* the ``honua`` command of the installed ``@honua/sdk-js`` runs through its npm ``.bin`` shim;
* the installed ``honua-mcp-proxy`` is spoken to over stdio only.

It prints one JSON observation per contract step on stdout; ``judge.py`` evaluates them. Credentials
travel only in child environments (and, for the minted identity key, in a private file the CLI
writes); command output with secret material is never logged.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "certification" / "terminal-journey"))
import probes  # noqa: E402

# The suite's oracles (installed-clients/oracles.py) shadow the journey's module of the same name.
sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
import judge  # noqa: E402

PLAN = json.loads(open(os.environ["SDKREG_PLAN"], encoding="utf-8").read())
BASE = PLAN["baseUrl"]
COMMAND_TIMEOUT = 180
RUNNER_TIMEOUT = 300
TERMINAL_PROPOSAL_STATES = {"Succeeded", "Failed", "Rejected", "RolledBack", "Cancelled"}
HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")

# The exact API each step goes through; a self-test checks it equals the scenario contract.
API = {
    "interop-publish-query-edit": {
        "publish": "IHonuaAdminClient.PublishLayerAsync",
        "query": "honua_sdk.GeoServicesFeatureServerClient.query",
        "ids": "honua_sdk.GeoServicesFeatureServerClient.query(extra_params returnIdsOnly)",
        "count": "honua_sdk.HonuaClient.query(return_count_only=True)",
        "edit": "HonuaFeatureLayer.applyEdits(updates)",
        "read-back": "IHonuaFeatureServerClient.QueryAsync",
    },
    "interop-import-render-buffer": {
        "import": "honua admin import uploadImportFile --yes",
        "publish": "honua admin publish publishLayer --yes",
        "read": "honua-mcp-proxy tools/call honua_query_features",
        "render": "honua-mcp-proxy tools/call honua_render_map",
        "buffer-submit": "honua-mcp-proxy tools/call honua_execute_plan (geometry.buffer)",
        "job-status": "honua_sdk.HonuaGeoprocessing.job",
        "job-result": "honua_sdk.HonuaGeoprocessing.results",
    },
    "interop-proposal-approval": {
        "create-draft": "HonuaStudioLifecycleClient.drafts.create",
        "save-version": "HonuaStudioLifecycleClient.drafts.createContentVersion",
        "request-publication": "HonuaStudioLifecycleClient.publicationRequests.create",
        "self-approval-refused": "honua admin operate approveOperationProposal --profile proposer --yes",
        "approve": "honua admin operate approveOperationProposal --profile approver --yes",
        "proposal-resolved": "honua admin operate getOperationProposal --profile approver (until terminal)",
        "publication-url": "HonuaStudioLifecycleClient.publicationRequests.poll",
        "published-pointer": "IHonuaStudioPackageClient.GetContentItemPointersAsync",
        "published-content": "IHonuaStudioPackageClient.GetVersionAsync",
        "published-url": "(no published Studio route reader in Honua.Sdk)",
    },
    "interop-api-key-revocation": {
        "mint": "honua admin secure createAdminApiKey --secret-output --yes",
        "python-use": "honua_sdk.HonuaClient(api_key).feature_server(...).query",
        "js-use": "HonuaClient({apiKey}).featureLayer().queryFeatures",
        "dotnet-use": "IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.ApiKey)",
        "mcp-use": "honua-mcp-proxy tools/call honua_query_features (HONUA_API_KEY)",
        "revoke": "honua admin secure revokeAdminApiKey --yes",
        "python-revoked": "honua_sdk.HonuaClient(api_key).feature_server(...).query",
        "js-revoked": "HonuaClient({apiKey}).featureLayer().queryFeatures",
        "dotnet-revoked": "IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.ApiKey)",
        "mcp-revoked": "honua-mcp-proxy tools/call honua_query_features (HONUA_API_KEY)",
    },
}


class Unsupported(Exception):
    pass


class StepError(Exception):
    """A client refused or failed a call. Only its type and status reach the observation."""

    def __init__(self, error: dict[str, Any]):
        super().__init__(f"{error.get('type')} (status {error.get('status')})")
        self.error = error


class CommandFailed(StepError):
    def __init__(self, code: int, status: int | None):
        super().__init__({"type": f"CommandFailed (exit {code})", "status": status})
        self.code, self.status = code, status


class RunnerError(Exception):
    pass


def log(message: str) -> None:
    print(f"[interop] {message}", file=sys.stderr, flush=True)


def emit(scenario: str, name: str, **payload: Any) -> None:
    print(json.dumps({"scenario": scenario, "step": name, "api": API[scenario][name], **payload}, default=str), flush=True)


def step(scenario: str, name: str, action: Callable[[], dict[str, Any]], state: dict[str, Any], needs: tuple[str, ...] = ()) -> None:
    missing = [dependency for dependency in needs if dependency not in state]
    if missing:
        emit(scenario, name, skipped=f"depends on {missing}, which did not complete")
        return
    try:
        emit(scenario, name, observed=action())
    except Unsupported as exc:
        emit(scenario, name, unsupported=str(exc))
    except StepError as exc:
        emit(scenario, name, error=exc.error)
    except Exception as exc:  # noqa: BLE001 - every client failure is an observation
        traceback.print_exc(file=sys.stderr)
        emit(scenario, name, error={"type": type(exc).__name__, "status": None})


# ── clients ──────────────────────────────────────────────────────────────────────────────────


# Runtime locations the .NET installer sets. Not credentials, and not inherited by the other runners.
_DOTNET_RUNTIME = ("DOTNET_ROOT", "DOTNET_CLI_HOME", "DOTNET_CLI_TELEMETRY_OPTOUT", "DOTNET_NOLOGO",
                   "DOTNET_MULTILEVEL_LOOKUP", "DOTNET_ROLL_FORWARD", "NUGET_PACKAGES", "NUGET_HTTP_CACHE_PATH")
# Each runner reads only these keys. The bearer, database password and approver key stay in the orchestrator.
_RUNNER_CREDENTIALS = {
    "python-runner": ("SDKREG_API_KEY",),
    "js-runner": ("SDKREG_API_KEY", "SDKREG_PROPOSER_KEY"),
    "dotnet-runner": ("SDKREG_API_KEY",),
}


def runner_env(name: str) -> dict[str, str]:
    """Child environment for one SDK runner: startup variables, its plan, and only the keys it reads."""
    env = {key: os.environ[key] for key in probes.PROXY_INHERITED_ENV if key in os.environ}
    if name == "dotnet-runner":
        env.update({key: os.environ[key] for key in _DOTNET_RUNTIME if key in os.environ})
    if "SDKREG_PLAN" in os.environ:
        env["SDKREG_PLAN"] = os.environ["SDKREG_PLAN"]
    for key in _RUNNER_CREDENTIALS.get(name, ()):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


class Runner:
    """A long-lived SDK runner: one JSON request per line on stdin, one JSON reply per line on stdout."""

    def __init__(self, name: str, argv: list[str], cwd: str | None = None):
        self.name = name
        self.process = subprocess.Popen(argv, cwd=cwd, env=runner_env(name), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self._next_id = 0
        self._buffer = bytearray()
        self._replies: dict[int, dict[str, Any]] = {}
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            sys.stderr.write(f"[{self.name}] {line.decode('utf-8', errors='replace')}")

    def send(self, op: str, **args: Any) -> int:
        self._next_id += 1
        try:
            assert self.process.stdin is not None
            self.process.stdin.write((json.dumps({"id": self._next_id, "op": op, "args": args}) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RunnerError(f"{self.name} runner is not accepting requests ({type(exc).__name__})") from None
        return self._next_id

    def receive(self, request_id: int, timeout: float = RUNNER_TIMEOUT) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while request_id not in self._replies:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._buffer[:newline]).strip()
                    del self._buffer[:newline + 1]
                    try:
                        reply = json.loads(line) if line.startswith(b"{") else None
                    except json.JSONDecodeError:
                        reply = None
                    if isinstance(reply, dict) and type(reply.get("id")) is int:
                        self._replies[reply["id"]] = reply
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RunnerError(f"{self.name} runner did not answer within {timeout:.0f}s")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RunnerError(f"{self.name} runner exited (code {self.process.poll()})")
                self._buffer.extend(chunk)
        return self._replies.pop(request_id)

    def call(self, op: str, timeout: float = RUNNER_TIMEOUT, **args: Any) -> dict[str, Any]:
        return self.receive(self.send(op, **args), timeout)

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            self.process.kill()


def outcome(reply: dict[str, Any]) -> dict[str, Any]:
    """A runner reply as an observed value, or the refusal/unsupported it reported."""
    if "unsupported" in reply:
        raise Unsupported(str(reply["unsupported"]))
    if "error" in reply:
        error = reply["error"] if isinstance(reply["error"], dict) else {}
        raise StepError({"type": str(error.get("type") or "Error"), "status": error.get("status")})
    observed = reply.get("observed")
    if not isinstance(observed, dict):
        raise RunnerError("runner reply carries no observation")
    return observed


class Cli:
    """The installed ``honua`` command, each run with only the credentials it needs."""

    def __init__(self, honua: str, workdir: Path):
        self.honua, self.workdir = honua, workdir
        self.profiles = workdir / "honua-config"

    def run(self, args: list[str], credentials: dict[str, str], *, quiet: bool = False) -> str:
        # Startup variables only; no SDKREG_*, E2E_* or HONUA_* value beyond this command's own.
        env = {key: os.environ[key] for key in probes.PROXY_INHERITED_ENV if key in os.environ}
        env.update({"HONUA_BASE_URL": BASE, "HONUA_CONFIG_HOME": str(self.profiles), **credentials})
        log(f"$ honua {' '.join(args[:3])} ...")
        proc = subprocess.run([self.honua, *args], env=env, cwd=self.workdir, text=True, capture_output=True,
                              timeout=COMMAND_TIMEOUT, check=False)
        # Commands that return secret material (or its prefix) are never echoed into the job log.
        log(f"exit {proc.returncode}" + ("" if quiet else f"\n{proc.stdout[-1500:]}{proc.stderr[-1500:]}"))
        if proc.returncode:
            match = HTTP_STATUS.search(proc.stdout + proc.stderr)
            raise CommandFailed(proc.returncode, int(match.group(1)) if match else None)
        return proc.stdout

    def json(self, args: list[str], credentials: dict[str, str], *, quiet: bool = False) -> Any:
        return json.loads(self.run(args, credentials, quiet=quiet))

    def write_profiles(self) -> None:
        """Named proposer/approver profiles (owner-only, as the CLI requires); keys are supplied per command."""
        self.profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.profiles, 0o700)
        config = self.profiles / "config.json"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"profiles": {name: {"baseUrl": BASE} for name in ("proposer", "approver")}}, stream)

    def private_file(self, name: str, content: str) -> Path:
        path = self.workdir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        return path


def unwrap(document: Any) -> Any:
    return document.get("data", document) if isinstance(document, dict) else document


def mcp_session(proxy: str, key: str) -> probes.McpProxySession:
    # The proxy starts from a scrubbed environment carrying this one credential.
    return probes.McpProxySession([proxy], f"{BASE}/mcp", env={"HONUA_API_KEY": key})


def mcp_tool(session: probes.McpProxySession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = session.request("tools/call", {"name": name, "arguments": arguments})
    if "error" in response:
        error = response["error"] if isinstance(response["error"], dict) else {}
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        raise StepError({"type": str(data.get("code") or error.get("code")), "status": None})
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    content = result.get("structuredContent")
    if result.get("isError") or not isinstance(content, dict):
        code = content.get("code") if isinstance(content, dict) else None
        raise StepError({"type": f"tool-error:{code or 'unstructured'}", "status": None})
    return content


def imported_attributes(raw: Any) -> dict[str, Any] | None:
    """``gid`` and ``name`` from one queried feature.

    A file import stores the GeoJSON properties in a single JSON column named
    ``properties`` (a string or an object). A table that already has those
    columns returns them as attributes. Either shape is the imported data.
    """
    if not isinstance(raw, dict):
        return None
    props = raw.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except json.JSONDecodeError:
            props = None
    source = props if isinstance(props, dict) else raw
    return {key: source.get(key) for key in ("gid", "name")}


def revocation_wait_seconds(revocation: dict[str, Any]) -> float:
    """How long to wait for one client's revocation reply.

    The first call must be refused within ``observationSeconds``. Each confirmation is one
    more call on that same instance, so the wait is one period per call. It is not
    ``observationSeconds + 120``: that grace let a refusal at 60–149s come back.
    """
    return float(revocation["observationSeconds"]) * (1 + int(revocation["confirmations"]))


# ── scenarios ────────────────────────────────────────────────────────────────────────────────


class Interop:
    def __init__(self, runners: dict[str, Runner], cli: Cli, proxy: str):
        self.python, self.js, self.dotnet = runners["python"], runners["js"], runners["dotnet"]
        self.cli, self.proxy = cli, proxy
        self.root = os.environ["SDKREG_API_KEY"]
        self.proposer = os.environ.get("SDKREG_PROPOSER_KEY", "")
        self.approver = os.environ.get("SDKREG_APPROVER_KEY", "")

    # (a) .NET publishes -> Python queries -> JS edits -> .NET reads the edit back
    def publish_query_edit(self) -> None:
        scenario, handoff, state = "interop-publish-query-edit", PLAN["handoff"], {}

        def publish() -> dict[str, Any]:
            observed = outcome(self.dotnet.call(
                "publish-layer", connectionId=PLAN["connectionId"], schema=handoff["schema"], table=handoff["table"],
                layerName=handoff["layerName"], service=handoff["service"], geometryType=handoff["geometryType"]))
            if type(observed.get("layerId")) is int:
                state["layer"] = observed["layerId"]
            return observed
        step(scenario, "publish", publish, state)

        def query() -> dict[str, Any]:
            observed = outcome(self.python.call("query-features", service=handoff["service"], layerId=state["layer"],
                                                fields=handoff["fields"]))
            rows = observed.get("features") or []
            state["oidField"] = observed.get("objectIdField")
            state["oids"] = {row.get("attributes", {}).get("gid"): row.get("objectId") for row in rows}
            return {"features": [{key: row.get(key) for key in ("attributes", "x", "y")} for row in rows]}
        step(scenario, "query", query, state, ("layer",))
        step(scenario, "ids", lambda: outcome(self.python.call("query-ids", service=handoff["service"], layerId=state["layer"])),
             state, ("layer",))
        step(scenario, "count", lambda: outcome(self.python.call("query-count", service=handoff["service"], layerId=state["layer"])),
             state, ("layer",))

        def edit() -> dict[str, Any]:
            target = handoff["edit"]
            object_id = state["oids"].get(target["gid"])
            if object_id is None or not state.get("oidField"):
                raise StepError({"type": "HandoffMissing", "status": None})
            return outcome(self.js.call("apply-edit", service=handoff["service"], layerId=state["layer"],
                                        objectIdField=state["oidField"], objectId=object_id,
                                        attributes=target["attributes"], x=target["x"], y=target["y"]))
        step(scenario, "edit", edit, state, ("layer", "oids"))
        step(scenario, "read-back", lambda: outcome(self.dotnet.call(
            "query-features", service=handoff["service"], layerId=state["layer"], fields=handoff["fields"])), state, ("layer",))

    # (b) honua CLI imports and publishes -> MCP proxy reads, renders and buffers -> Python reads the job
    def import_render_buffer(self) -> None:
        scenario, area, state = "interop-import-render-buffer", PLAN["area"], {}
        admin = {"HONUA_ADMIN_KEY": self.root}

        def imported() -> dict[str, Any]:
            document = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"gid": feature["gid"], "name": feature["name"]},
                 "geometry": {"type": "Polygon", "coordinates": [[list(point) for point in
                                                                   [*judge.envelope_ring(feature["envelope"]),
                                                                    judge.envelope_ring(feature["envelope"])[0]]]]}}
                for feature in area["features"]]}
            path = self.cli.workdir / area["fileName"]
            path.write_text(json.dumps(document))
            result = unwrap(self.cli.json(["admin", "import", "uploadImportFile", "--body",
                                           json.dumps({"file": f"@{path}", "TableName": area["tableName"]}), "--yes", "--json"], admin))
            if isinstance(result.get("physicalTableName"), str) and isinstance(result.get("schema"), str):
                state["table"] = (result["schema"], result["physicalTableName"])
            return {key: result.get(key) for key in ("success", "featureCount", "physicalTableName", "schema")}
        step(scenario, "import", imported, state)

        def publish() -> dict[str, Any]:
            schema, table = state["table"]
            listing = unwrap(self.cli.json(["admin", "connect", "getConnectionTables", "--path", f"id={PLAN['connectionId']}",
                                            "--json"], admin))
            tables = listing.get("tables") if isinstance(listing, dict) else listing
            match = [row for row in tables or [] if isinstance(row, dict) and (row.get("schema"), row.get("table")) == (schema, table)]
            if len(match) != 1:
                raise StepError({"type": "ImportedTableNotDiscovered", "status": None})
            keys = [column.get("name") for column in match[0].get("columns") or [] if column.get("isPrimaryKey")]
            body = self.cli.private_file("area-layer.json", json.dumps({
                "schema": schema, "table": table, "layerName": area["layerName"], "serviceName": area["service"],
                "geometryColumn": match[0].get("geometryColumn"), "geometryType": area["geometryType"],
                "primaryKey": keys[0] if len(keys) == 1 else None, "srid": 4326, "enabled": True}))
            published = unwrap(self.cli.json(["admin", "publish", "publishLayer", "--path", f"id={PLAN['connectionId']}",
                                              "--body", f"@{body}", "--yes", "--json"], admin))
            if type(published.get("layerId")) is int:
                state["layer"] = published["layerId"]
            return {key: published.get(key) for key in ("layerId", "layerName", "serviceName", "enabled", "geometryType")}
        step(scenario, "publish", publish, state, ("table",))

        with mcp_session(self.proxy, self.root) as session:
            def initialized() -> None:
                if "session" not in state:
                    session.initialize()
                    state["session"] = True

            def read() -> dict[str, Any]:
                initialized()
                content = mcp_tool(session, "honua_query_features", {"serviceId": area["service"], "layerId": state["layer"],
                                                                     "where": "1=1", "limit": 100})
                features = content.get("features") if isinstance(content.get("features"), list) else []
                rings: list[Any] = []
                attributes: list[Any] = []
                for feature in features:
                    if not isinstance(feature, dict):
                        rings.append(None)
                        attributes.append(None)
                        continue
                    geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
                    coordinates = geometry.get("coordinates") if isinstance(geometry.get("coordinates"), list) else []
                    rings.append(coordinates[0] if coordinates else None)
                    raw = feature.get("attributes")
                    if not isinstance(raw, dict):
                        raw = feature.get("properties")
                    attributes.append(imported_attributes(raw))
                if len(rings) == 1 and isinstance(rings[0], list) and isinstance(features[0].get("geometry"), dict):
                    state["polygon"] = features[0]["geometry"].get("coordinates")
                return {"rings": rings, "attributes": attributes}
            step(scenario, "read", read, state, ("layer",))

            def render() -> dict[str, Any]:
                initialized()
                spec = area["render"]
                content = mcp_tool(session, "honua_render_map", {
                    "layers": [{"serviceId": area["service"], "layerId": state["layer"]}], "bbox": spec["bbox"], "bboxSrid": 4326,
                    "width": spec["width"], "height": spec["height"], "transparent": True, "maxInlineBytes": 1024 * 1024})
                image = content.get("image") or {}
                return {"png": image.get("base64"), "mimeType": image.get("format")}
            step(scenario, "render", render, state, ("layer",))

            def submit() -> dict[str, Any]:
                initialized()
                buffer = area["buffer"]
                plan = {"planId": f"sdkreg-interop-buffer-{PLAN['tag']}", "intentId": f"sdkreg-interop-buffer-{PLAN['tag']}",
                        "outputs": ["FeatureLayer"],
                        "steps": [{"stepId": "buffer", "kind": "Geoprocess", "processId": buffer["processId"], "inputs": {
                            "wkb": judge.polygon_wkb(state["polygon"]), "srid": "4326", "distance": str(buffer["distance"]),
                            "geodesic": "false"}}]}
                content = mcp_tool(session, "honua_execute_plan", {"plan": plan, "idempotencyKey": f"sdkreg-interop-{time.time_ns()}"})
                if content.get("jobId"):
                    state["job"] = content["jobId"]
                return {"jobId": content.get("jobId"), "status": content.get("status")}
            step(scenario, "buffer-submit", submit, state, ("polygon",))

        step(scenario, "job-status", lambda: outcome(self.python.call(
            "job-status", timeout=area["buffer"]["pollTimeoutSeconds"] + 60, jobId=state["job"],
            timeoutSeconds=area["buffer"]["pollTimeoutSeconds"])), state, ("job",))
        step(scenario, "job-result", lambda: outcome(self.python.call("job-result", jobId=state["job"])), state, ("job",))

    # (c) JS SDK proposes -> honua admin CLI approves as a separate principal -> .NET verifies
    def proposal_approval(self) -> None:
        scenario, proposal, state = "interop-proposal-approval", PLAN["proposal"], {}
        self.cli.write_profiles()

        def draft() -> dict[str, Any]:
            observed = outcome(self.js.call("studio-create-draft", packageKey=proposal["packageKey"], envelope=proposal["envelope"]))
            if observed.get("draftId"):
                state["draft"] = observed["draftId"]
            return observed
        step(scenario, "create-draft", draft, state)

        def save() -> dict[str, Any]:
            observed = outcome(self.js.call("studio-save-version", draftId=state["draft"]))
            if observed.get("versionId") and observed.get("itemId"):
                state["version"] = observed
            return observed
        step(scenario, "save-version", save, state, ("draft",))

        def request() -> dict[str, Any]:
            version = state["version"]
            observed = outcome(self.js.call("studio-request-publication", itemId=version["itemId"], versionId=version["versionId"],
                                            contentHash=version["contentHash"], route=proposal["route"],
                                            visibility=proposal["visibility"]))
            state["request"] = observed
            if observed.get("proposalId"):
                state["proposal"] = observed["proposalId"]
            return observed
        step(scenario, "request-publication", request, state, ("version",))

        def read_proposal() -> dict[str, Any]:
            return unwrap(self.cli.json(["admin", "operate", "getOperationProposal", "--path", f"id={state['proposal']}",
                                         "--profile", "approver", "--json"], {"HONUA_ADMIN_KEY": self.approver}))

        if "proposal" in state:
            try:
                self.cli.run(["admin", "operate", "approveOperationProposal", "--path", f"id={state['proposal']}",
                              "--profile", "proposer", "--yes", "--json"], {"HONUA_ADMIN_KEY": self.proposer})
                emit(scenario, "self-approval-refused", observed={"status": read_proposal().get("status")})
            except CommandFailed as exc:
                try:
                    emit(scenario, "self-approval-refused", error=exc.error, observed={"status": read_proposal().get("status")})
                except Exception:  # noqa: BLE001 - the follow-up read failed
                    traceback.print_exc(file=sys.stderr)
                    emit(scenario, "self-approval-refused", error=exc.error, observed={"status": None})
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
                emit(scenario, "self-approval-refused", error={"type": type(exc).__name__, "status": None})
        else:
            emit(scenario, "self-approval-refused", skipped="depends on ['proposal'], which did not complete")

        def approve() -> dict[str, Any]:
            approved = unwrap(self.cli.json(["admin", "operate", "approveOperationProposal", "--path", f"id={state['proposal']}",
                                             "--profile", "approver", "--yes", "--json"], {"HONUA_ADMIN_KEY": self.approver}))
            state["approved"] = True
            return {"status": approved.get("status")}
        step(scenario, "approve", approve, state, ("proposal",))

        def resolved() -> dict[str, Any]:
            deadline = time.monotonic() + proposal["proposalPollSeconds"]
            record = read_proposal()
            while record.get("status") not in TERMINAL_PROPOSAL_STATES and time.monotonic() < deadline:
                time.sleep(1)
                record = read_proposal()
            if record.get("status") == "Succeeded":
                state["resolved"] = True
            return {key: record.get(key) for key in ("status", "kind", "requestedBy", "resolvedBy")}
        step(scenario, "proposal-resolved", resolved, state, ("approved",))

        def publication_url() -> dict[str, Any]:
            request_id = state["request"].get("requestId")
            if not request_id:
                # The create response named no publication request, so there is nothing to poll.
                return {"requestId": None}
            version = state["version"]
            observed = outcome(self.js.call("studio-publication-url", timeout=proposal["publicationPollSeconds"] + 60,
                                            itemId=version["itemId"], versionId=version["versionId"], requestId=request_id,
                                            timeoutMs=proposal["publicationPollSeconds"] * 1000))
            if observed.get("state") == "Active" and isinstance(observed.get("publicationUrl"), str):
                # The handoff: the .NET reader consumes this exact URL, never one rebuilt from the fixture route.
                state["publicationUrl"] = observed["publicationUrl"]
            return observed
        step(scenario, "publication-url", publication_url, state, ("request", "resolved"))

        def pointer() -> dict[str, Any]:
            observed = outcome(self.dotnet.call("studio-pointers", itemId=state["version"]["itemId"]))
            if observed.get("publishedVersionId"):
                state["published"] = observed["publishedVersionId"]
            return observed
        step(scenario, "published-pointer", pointer, state, ("version", "resolved"))
        step(scenario, "published-content", lambda: outcome(self.dotnet.call(
            "studio-version", itemId=state["version"]["itemId"], versionId=state["published"])), state, ("published",))
        # No dependency gate: the missing .NET reader (honua-sdk-dotnet#411) surfaces whatever the upstream steps did.
        # A reader handed no polled URL refuses, and the judge compares the URL it read with the one the JS SDK polled.
        step(scenario, "published-url", lambda: outcome(self.dotnet.call(
            "studio-published-url", url=state.get("publicationUrl"))), state)

    # (d) one API key minted by the admin CLI, used by every client, revoked, refused by every client
    def api_key_revocation(self) -> None:
        scenario, identity, sites, state = "interop-api-key-revocation", PLAN["identity"], PLAN["sites"], {}
        admin = {"HONUA_ADMIN_KEY": self.root}
        secret = self.cli.workdir / "identity-key.secret"
        secret.unlink(missing_ok=True)

        def mint() -> dict[str, Any]:
            body = {"name": identity["keyName"], "permissions": identity["permissions"], "expiresAt": None}
            created = unwrap(self.cli.json(["admin", "secure", "createAdminApiKey", "--body", json.dumps(body),
                                            "--secret-output", str(secret), "--yes", "--json"], admin, quiet=True))
            resource = created.get("resource") if isinstance(created.get("resource"), dict) else {}
            private = secret.is_file() and stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0
            if resource.get("id") and secret.is_file():
                state["key"] = resource["id"]
            return {"keyId": resource.get("id"), "status": resource.get("status"), "permissions": resource.get("permissions"),
                    "secretWritten": created.get("secretWritten"), "secretPrivate": private}
        step(scenario, "mint", mint, state)

        use = {"service": sites["service"], "layerId": sites["layerId"], "secretFile": str(secret)}
        for runner, name in ((self.python, "python-use"), (self.js, "js-use"), (self.dotnet, "dotnet-use")):
            def used(runner: Runner = runner, name: str = name) -> dict[str, Any]:
                observed = outcome(runner.call("identity-use", **use))
                state[name] = True
                return observed
            step(scenario, name, used, state, ("key",))

        with contextlib.ExitStack() as stack:
            stack.callback(secret.unlink, missing_ok=True)
            # One proxy session for the whole scenario: the revocation is probed on the session that used the key.
            session = stack.enter_context(mcp_session(self.proxy, secret.read_text().strip())) if "key" in state else None

            def mcp_count() -> dict[str, Any]:
                session.initialize()
                content = mcp_tool(session, "honua_query_features", {"serviceId": sites["service"], "layerId": sites["layerId"],
                                                                     "where": "1=1", "limit": 1000})
                state["mcp-use"] = True
                return {"count": len(content.get("features") or [])}
            step(scenario, "mcp-use", mcp_count, state, ("key",))

            def revoke() -> dict[str, Any]:
                revoked = unwrap(self.cli.json(["admin", "secure", "revokeAdminApiKey", "--path", f"id={state['key']}",
                                                "--yes", "--json"], admin, quiet=True))
                resource = revoked.get("resource") if isinstance(revoked.get("resource"), dict) else revoked
                state["revokedAt"] = time.time()
                return {"status": resource.get("status"), "revokedAt": resource.get("revokedAt")}
            step(scenario, "revoke", revoke, state, ("key",))
            self._observe_revocation(scenario, session, state)

    def _observe_revocation(self, scenario: str, session: probes.McpProxySession | None, state: dict[str, Any]) -> None:
        """Every client probes concurrently, on the client instance (and MCP session) that used the key."""
        revocation, sites = PLAN["identity"]["revocation"], PLAN["sites"]
        probe = {"service": sites["service"], "layerId": sites["layerId"], "revokedAt": state.get("revokedAt"),
                 "observationSeconds": revocation["observationSeconds"], "confirmations": revocation["confirmations"]}
        pending: dict[str, tuple[Runner, int]] = {}
        for runner, name, used in ((self.python, "python-revoked", "python-use"), (self.js, "js-revoked", "js-use"),
                                   (self.dotnet, "dotnet-revoked", "dotnet-use")):
            if "revokedAt" in state and used in state:
                try:
                    pending[name] = (runner, runner.send("identity-revoked", **probe))
                except RunnerError as exc:
                    emit(scenario, name, error={"type": type(exc).__name__, "status": None})
            else:
                missing = [key for key in ("revokedAt", used) if key not in state]
                emit(scenario, name, skipped=f"depends on {missing}, which did not complete")
        mcp_result: dict[str, Any] = {}
        thread = None
        if session is not None and "revokedAt" in state and "mcp-use" in state:
            def probe_mcp() -> None:
                try:
                    mcp_result.update(self._mcp_revoked(session, probe))
                except Exception as exc:  # noqa: BLE001 - a probe crash is an error, never the blocked timeout
                    traceback.print_exc(file=sys.stderr)
                    mcp_result["crash"] = {"type": type(exc).__name__, "status": None}
            thread = threading.Thread(target=probe_mcp, daemon=True)
            thread.start()
        wait = revocation_wait_seconds(revocation)
        for name, (runner, request_id) in pending.items():
            try:
                emit(scenario, name, observed=outcome(runner.receive(request_id, wait)))
            except StepError as exc:
                emit(scenario, name, error=exc.error)
            except Exception as exc:  # noqa: BLE001
                emit(scenario, name, error={"type": type(exc).__name__, "status": None})
        if thread is None:
            missing = [key for key in ("revokedAt", "mcp-use") if key not in state]
            emit(scenario, "mcp-revoked", skipped=f"depends on {missing}, which did not complete")
            return
        thread.join(revocation["observationSeconds"] * (revocation["confirmations"] + 2) + 60)
        if "crash" in mcp_result:
            emit(scenario, "mcp-revoked", error=mcp_result["crash"])
        elif mcp_result:
            emit(scenario, "mcp-revoked", observed=mcp_result)
        elif thread.is_alive():
            # The worker is still blocked in a call past the join bound: a hang, not a crash.
            emit(scenario, "mcp-revoked", observed={"timedOut": True, "timeoutSeconds": revocation["observationSeconds"]})
        else:
            emit(scenario, "mcp-revoked", error={"type": "probe exited without an observation", "status": None})

    def _mcp_revoked(self, session: probes.McpProxySession, probe: dict[str, Any]) -> dict[str, Any]:
        """Probe the same proxy session; the per-call deadline is the observation bound."""
        bound = probe["observationSeconds"]
        arguments = {"serviceId": probe["service"], "layerId": probe["layerId"], "where": "1=1", "limit": 1}
        previous, probes.MCP_TIMEOUT_SECONDS = probes.MCP_TIMEOUT_SECONDS, bound
        try:
            def attempt() -> Any:
                """The call's refusal status, or None when the call succeeded. A timeout is not a status."""
                try:
                    mcp_tool(session, "honua_query_features", arguments)
                    return None
                except StepError as exc:
                    kind = str(exc.error.get("type") or "")
                    # An error with no code is still not a successful read.
                    return kind.removeprefix("tool-error:") or "error"
            successes, started = 0, time.time()
            while True:
                try:
                    status = attempt()
                except probes.McpError:
                    return {"timedOut": True, "timeoutSeconds": bound, "succeededAfterRevocation": successes}
                if status is not None:
                    after = round(time.time() - (probe["revokedAt"] or started), 3)
                    confirmations = []
                    for _ in range(probe["confirmations"]):
                        try:
                            confirmations.append(attempt())
                        except probes.McpError:
                            confirmations.append(None)
                    return {"refused": True, "status": status, "succeededAfterRevocation": successes,
                            "refusedAfterSeconds": after, "confirmations": confirmations, "observationSeconds": bound}
                successes += 1
                if time.time() - started > bound:
                    return {"refused": False, "succeededAfterRevocation": successes, "observationSeconds": bound}
                time.sleep(0.25)
        finally:
            probes.MCP_TIMEOUT_SECONDS = previous


RUNNERS = {
    "interop-publish-query-edit": Interop.publish_query_edit,
    "interop-import-render-buffer": Interop.import_render_buffer,
    "interop-proposal-approval": Interop.proposal_approval,
    "interop-api-key-revocation": Interop.api_key_revocation,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-runner", required=True, help="JSON argv of the Python SDK runner")
    parser.add_argument("--js-runner", required=True, help="JSON argv of the JS SDK runner")
    parser.add_argument("--js-cwd", required=True, help="the npm consumer the JS runner resolves @honua/sdk-js from")
    parser.add_argument("--dotnet-runner", required=True, help="JSON argv of the .NET SDK runner")
    parser.add_argument("--honua", required=True, help="the installed honua executable")
    parser.add_argument("--proxy", required=True, help="the installed honua-mcp-proxy executable")
    args = parser.parse_args()
    workdir = Path(os.environ["SDKREG_PLAN"]).parent
    runners = {"python": Runner("python-runner", json.loads(args.python_runner)),
               "js": Runner("js-runner", json.loads(args.js_runner), cwd=args.js_cwd),
               "dotnet": Runner("dotnet-runner", json.loads(args.dotnet_runner))}
    interop = Interop(runners, Cli(args.honua, workdir), args.proxy)
    started = time.monotonic()
    try:
        for scenario in PLAN["scenarios"]:
            try:
                RUNNERS[scenario["id"]](interop)
            except Exception:  # noqa: BLE001 - a crashed scenario leaves its steps unobserved (a fail)
                traceback.print_exc(file=sys.stderr)
    finally:
        for runner in runners.values():
            runner.close()
    log(f"interop scenarios finished in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
