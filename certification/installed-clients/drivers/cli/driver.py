#!/usr/bin/env python3
"""Command-line driver for the installed-client regression suite (cli-* scenarios).

``--client npm`` runs the ``honua`` command of the manifest-pinned ``@honua/sdk-js`` (including its
typed ``honua admin`` control plane) through the npm ``node_modules/.bin`` shim, exactly as a
customer types it. The proposal step goes through the co-installed ``honua-mcp-proxy``: in the
terminal journey governed proposals come from the agent surface and a separate operator approves
them with ``honua admin operate approveOperationProposal``.

``--client pypi`` runs the ``honua`` console script that the pinned ``honua-sdk`` wheel installs
(``honua-admin`` is installed with it and ships no command). Every step that script has no command
for is reported unsupported, never skipped silently.

The driver never makes an HTTP request itself. Credentials travel only in each child's
environment, and command output stays in the job log. Prints one JSON observation per step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[4]
# The shared stdio MCP session the installed-client MCP cells use.
sys.path.insert(0, str(ROOT / "certification" / "terminal-journey"))
import probes  # noqa: E402

PLAN = json.loads(open(os.environ["SDKREG_PLAN"], encoding="utf-8").read())
BASE = PLAN["baseUrl"]
SCENARIO = "cli-workflow"
COMMAND_TIMEOUT = 180
# An approved proposal executes asynchronously; it is read until one of these statuses or the deadline.
TERMINAL_PROPOSAL_STATES = {"Succeeded", "Failed", "Rejected", "RolledBack", "Cancelled"}
PROPOSAL_POLL_SECONDS = 120
HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")

API = {
    "npm": {
        "discover": "honua services --json",
        "create-datasource": "honua admin connect createConnection --yes",
        "test-datasource": "honua connection test --yes",
        "publish": "honua admin publish publishLayer --yes",
        "list": "honua admin publish getPublishedLayers",
        "query": "honua query --where --format geojson",
        "served": "honua query --count",
        "propose-publication": "honua-mcp-proxy tools/list {view: setup} + tools/call honua_publish_service (proposer)",
        "self-approval-refused": "honua admin operate approveOperationProposal --profile proposer --yes",
        "approve": "honua admin operate approveOperationProposal --profile approver --yes",
        "proposal-resolved": "honua admin operate getOperationProposal --profile approver (until terminal)",
        "approved-served": "honua query --format geojson",
        "unpublish": "honua admin publish setLayerEnabled --yes",
        "unpublished-refused": "honua query --format geojson",
    },
    "pypi": {
        "discover": "honua services --format json",
        "create-datasource": "(no datasource command in the PyPI honua CLI)",
        "test-datasource": "(no datasource command in the PyPI honua CLI)",
        "publish": "(no publish command in the PyPI honua CLI)",
        "list": "(no published-layer listing command in the PyPI honua CLI)",
        "query": "(no query command in the PyPI honua CLI)",
        "served": "(no query command in the PyPI honua CLI)",
        "propose-publication": "(no proposal command in the PyPI honua CLI)",
        "self-approval-refused": "(no approval command in the PyPI honua CLI)",
        "approve": "(no approval command in the PyPI honua CLI)",
        "proposal-resolved": "(no proposal command in the PyPI honua CLI)",
        "approved-served": "(no query command in the PyPI honua CLI)",
        "unpublish": "(no unpublish command in the PyPI honua CLI)",
        "unpublished-refused": "(no query command in the PyPI honua CLI)",
    },
}


class Unsupported(Exception):
    pass


class CommandFailed(Exception):
    """A command exited non-zero. Only the exit code and an HTTP status reach the observation."""

    def __init__(self, code: int, status: int | None):
        super().__init__(f"exit {code}" + (f" (HTTP {status})" if status else ""))
        self.code, self.status = code, status


class Cli:
    def __init__(self, client: str, honua: str, proxy: str | None, workdir: Path):
        self.client, self.honua, self.proxy, self.workdir = client, honua, proxy, workdir
        self.profiles = workdir / "honua-config"

    def run(self, args: list[str], credentials: dict[str, str]) -> str:
        """Run one command with only the credentials it needs; returns stdout, raises on a non-zero exit."""
        # Startup variables only (PYTHONPATH carries the isolated PyPI wheels); no SDKREG_*, E2E_* or HONUA_* value.
        env = {key: os.environ[key] for key in (*probes.PROXY_INHERITED_ENV, "PYTHONPATH") if key in os.environ}
        env.update({"HONUA_BASE_URL": BASE, "HONUA_CONFIG_HOME": str(self.profiles), **credentials})
        print(f"[cli-driver] $ honua {' '.join(args)}", file=sys.stderr)
        proc = subprocess.run([self.honua, *args], env=env, cwd=self.workdir, text=True, capture_output=True,
                              timeout=COMMAND_TIMEOUT, check=False)
        print(f"[cli-driver] exit {proc.returncode}\n{proc.stdout[-1500:]}{proc.stderr[-1500:]}", file=sys.stderr)
        if proc.returncode:
            match = HTTP_STATUS.search(proc.stdout + proc.stderr)
            raise CommandFailed(proc.returncode, int(match.group(1)) if match else None)
        return proc.stdout

    def json(self, args: list[str], credentials: dict[str, str]) -> Any:
        return json.loads(self.run(args, credentials))

    def write_profiles(self) -> None:
        """Named profiles for the two principals; each key is supplied per command, never stored."""
        self.profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = self.profiles / "config.json"
        config.touch(mode=0o600, exist_ok=True)
        config.write_text(json.dumps({"profiles": {name: {"baseUrl": BASE} for name in ("proposer", "approver")}}))

    def private_body(self, name: str, body: dict[str, Any]) -> str:
        """A request body file readable only by this user (it can hold the fixture DB password)."""
        path = self.workdir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(body, stream)
        return f"@{path}"


def emit(client: str, name: str, **payload: Any) -> None:
    print(json.dumps({"scenario": SCENARIO, "step": name, "api": API[client][name], **payload}, default=str), flush=True)


def error_of(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, CommandFailed):
        return {"type": f"CommandFailed (exit {exc.code})", "status": exc.status}
    print(f"[cli-driver] {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
    return {"type": type(exc).__name__, "status": None}


def step(client: str, name: str, action: Callable[[], dict[str, Any]], state: dict[str, Any], needs: tuple[str, ...] = ()) -> None:
    missing = [dependency for dependency in needs if dependency not in state]
    if missing:
        emit(client, name, skipped=f"depends on {missing}, which did not complete")
        return
    try:
        emit(client, name, observed=action())
    except Unsupported as exc:
        emit(client, name, unsupported=str(exc))
    except Exception as exc:  # noqa: BLE001 - every command failure is an observation
        if not isinstance(exc, CommandFailed):
            traceback.print_exc(file=sys.stderr)
        emit(client, name, error=error_of(exc))


def until_terminal(read: Callable[[], dict[str, Any]], timeout: float = PROPOSAL_POLL_SECONDS) -> dict[str, Any]:
    """Read the proposal until its status is terminal or the deadline passes; returns the last record."""
    deadline = time.monotonic() + timeout
    while True:
        record = read()
        if record.get("status") in TERMINAL_PROPOSAL_STATES or time.monotonic() >= deadline:
            return record
        time.sleep(1)


def geojson_rows(document: dict[str, Any]) -> dict[str, Any]:
    features = []
    for feature in document["features"]:
        x, y = feature["geometry"]["coordinates"][:2]
        features.append({"properties": feature["properties"], "x": x, "y": y})
    return {"features": features}


def run_npm(cli: Cli, state: dict[str, Any]) -> None:
    # The operator's root key reads data (HONUA_API_KEY) and runs admin operations (HONUA_ADMIN_KEY).
    root = os.environ["SDKREG_API_KEY"]
    proposer, approver = os.environ.get("SDKREG_PROPOSER_KEY", ""), os.environ.get("SDKREG_APPROVER_KEY", "")
    sites, life, proposal = PLAN["sites"], PLAN["lifecycle"], PLAN["proposal"]
    data = {"HONUA_API_KEY": root}
    admin = {"HONUA_ADMIN_KEY": root}
    cli.write_profiles()

    def discover() -> dict[str, Any]:
        listing = cli.json(["services", "--json"], data)
        return {"services": sorted({service["name"] for service in listing["services"]})}
    step("npm", "discover", discover, state)

    def create() -> dict[str, Any]:
        body = cli.private_body("datasource.json", {
            "name": life["connectionName"], **life["database"], "password": os.environ["SDKREG_DB_PASSWORD"],
            "sslRequired": False, "sslMode": "Disable"})
        try:
            created = cli.json(["admin", "connect", "createConnection", "--body", body, "--yes", "--json"], admin)
        finally:
            (cli.workdir / "datasource.json").unlink(missing_ok=True)
        created = created.get("data", created)
        state["connection"] = created.get("connectionId") or created.get("id")
        return {"connectionId": state["connection"]}
    step("npm", "create-datasource", create, state)

    def test() -> dict[str, Any]:
        receipt = cli.json(["connection", "test", state["connection"], "--yes", "--json"], data)
        output = receipt.get("output") or {}
        return {"success": receipt.get("status") == "ok" and (output.get("data") or {}).get("isHealthy") is True}
    step("npm", "test-datasource", test, state, ("connection",))

    def publish() -> dict[str, Any]:
        body = cli.private_body("layer.json", {
            "schema": "honua_data", "table": life["table"], "layerName": life["layerName"], "serviceName": life["service"],
            "geometryColumn": "geom", "geometryType": life["geometryType"], "primaryKey": "gid", "srid": 4326, "enabled": True})
        published = cli.json(["admin", "publish", "publishLayer", "--path", f"id={state['connection']}",
                              "--body", body, "--yes", "--json"], admin)
        published = published.get("data", published)
        state["layer"] = published["layerId"]
        return {key: published.get(key) for key in ("layerId", "layerName", "serviceName", "enabled")}
    step("npm", "publish", publish, state, ("connection",))

    def listed() -> dict[str, Any]:
        layers = cli.json(["admin", "publish", "getPublishedLayers", "--path", f"id={state['connection']}",
                           "--query", f"serviceName={life['service']}", "--json"], admin)
        layers = layers.get("data", layers) if isinstance(layers, dict) else layers
        return {"layers": [{"layerId": layer.get("layerId"), "enabled": layer.get("enabled")} for layer in layers or []]}
    step("npm", "list", listed, state, ("layer",))

    step("npm", "query", lambda: geojson_rows(cli.json(
        ["query", f"{sites['service']}/{sites['layerId']}", "--where", sites["where"], "--format", "geojson"], data)), state)

    def served() -> dict[str, Any]:
        counted = cli.json(["query", f"{life['service']}/{state['layer']}", "--count", "--json"], data)
        return {"count": counted.get("count")}
    step("npm", "served", served, state, ("layer",))

    def propose() -> dict[str, Any]:
        if not cli.proxy or not proposer:
            raise Unsupported("the installed honua-mcp-proxy or the proposer principal is unavailable")
        arguments = {"connectionId": state["connection"], "schema": proposal["schema"], "table": proposal["table"],
                     "layerName": proposal["layerName"], "serviceName": proposal["service"], "geometryColumn": "geom",
                     "geometryType": proposal["geometryType"], "srid": 4326, "primaryKey": "gid"}
        # Scrubbed proxy environment: the proposer's key is the only credential it can read. As in the
        # terminal journey, the agent discovers the publish tool in the setup view before calling it; the
        # default view does not list it. (The 0.1.12 proxy dropped the initialize selector, sdk-js#1875, so
        # the view is also named on tools/list.)
        with probes.McpProxySession([cli.proxy], f"{BASE}/mcp", env={"HONUA_API_KEY": proposer}) as session:
            session.initialize(workflow_view="setup")
            listed = session.request("tools/list", {"view": "setup"})
            names = {tool.get("name") for tool in (listed.get("result") or {}).get("tools") or []}
            if "error" in listed or "honua_publish_service" not in names:
                raise probes.McpError("honua_publish_service is not in the proposer's setup view")
            response = session.request("tools/call", {"name": "honua_publish_service", "arguments": arguments})
        if "error" in response:
            raise probes.McpError(f"tools/call refused: {response['error'].get('code')}")
        result = response.get("result") or {}
        content = result.get("structuredContent") or {}
        state["proposal"] = content.get("proposalId")
        if not state["proposal"]:
            state.pop("proposal")
        return {**{key: content.get(key) for key in ("status", "requiresApproval", "proposalId")},
                "servedBeforeApproval": served_before_approval()}

    def served_before_approval() -> bool:
        """The proposed service must not be served until the approver acts (unknown service: exit 1, 404)."""
        try:
            return bool(cli.json(["layers", proposal["service"], "--json"], data).get("layers"))
        except CommandFailed as exc:
            if exc.status == 404:
                return False
            raise
    step("npm", "propose-publication", propose, state, ("connection",))

    def read_proposal(key: str, profile: str) -> dict[str, Any]:
        return cli.json(["admin", "operate", "getOperationProposal", "--path", f"id={state['proposal']}",
                         "--profile", profile, "--json"], {"HONUA_ADMIN_KEY": key})

    def self_approval() -> None:
        try:
            cli.run(["admin", "operate", "approveOperationProposal", "--path", f"id={state['proposal']}",
                     "--profile", "proposer", "--yes", "--json"], {"HONUA_ADMIN_KEY": proposer})
        except CommandFailed as exc:
            after = read_proposal(approver, "approver")
            emit("npm", "self-approval-refused", error=error_of(exc), observed={"status": after.get("status")})
            return
        emit("npm", "self-approval-refused", observed={"status": read_proposal(approver, "approver").get("status")})
    if "proposal" in state:
        try:
            self_approval()
        except Exception as exc:  # noqa: BLE001 - the follow-up read failed
            traceback.print_exc(file=sys.stderr)
            emit("npm", "self-approval-refused", observed={"status": None, "readError": error_of(exc)})
    else:
        emit("npm", "self-approval-refused", skipped="depends on ['proposal'], which did not complete")

    def approve() -> dict[str, Any]:
        approved = cli.json(["admin", "operate", "approveOperationProposal", "--path", f"id={state['proposal']}",
                             "--profile", "approver", "--yes", "--json"], {"HONUA_ADMIN_KEY": approver})
        state["approved"] = True
        return {"status": approved.get("status")}
    step("npm", "approve", approve, state, ("proposal",))

    def resolved() -> dict[str, Any]:
        record = until_terminal(lambda: read_proposal(approver, "approver"))
        if record.get("status") == "Succeeded":
            state["resolved"] = True
        return {key: record.get(key) for key in ("status", "kind", "requestedBy", "resolvedBy")}
    step("npm", "proposal-resolved", resolved, state, ("approved",))

    def approved_served() -> dict[str, Any]:
        layers = cli.json(["layers", proposal["service"], "--json"], data).get("layers") or []
        if len(layers) != 1:
            raise RuntimeError(f"the approved service lists {len(layers)} layers")
        return geojson_rows(cli.json(["query", f"{proposal['service']}/{layers[0]['id']}", "--format", "geojson"], data))
    step("npm", "approved-served", approved_served, state, ("resolved",))

    def unpublish() -> dict[str, Any]:
        summary = cli.json(["admin", "publish", "setLayerEnabled", "--path", f"id={state['connection']}",
                            "--path", f"layerId={state['layer']}", "--query", f"serviceName={life['service']}",
                            "--body", json.dumps({"enabled": False}), "--yes", "--json"], admin)
        summary = summary.get("data", summary)
        state["unpublished"] = True
        return {"layerId": summary.get("layerId"), "enabled": summary.get("enabled")}
    step("npm", "unpublish", unpublish, state, ("layer",))

    def refused() -> dict[str, Any]:
        document = cli.json(["query", f"{life['service']}/{state['layer']}", "--format", "geojson"], data)
        return {"features": len(document.get("features") or [])}
    step("npm", "unpublished-refused", refused, state, ("unpublished",))


def run_pypi(cli: Cli, state: dict[str, Any]) -> None:
    def discover() -> dict[str, Any]:
        rows = cli.json(["services", "--format", "json", "--base-url", BASE], {"HONUA_API_KEY": os.environ["SDKREG_API_KEY"]})
        return {"services": sorted({row["name"] for row in rows})}
    step("pypi", "discover", discover, state)
    # honua-admin 0.1.9 ships no command; the honua console script has services, layers, style apply
    # and doctor only. Every other step of the workflow has no command to run.
    for name in API["pypi"]:
        if name != "discover":
            emit("pypi", name, unsupported=f"honua-admin ships no command and the honua console script has none for {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=sorted(API), required=True)
    parser.add_argument("--honua", required=True, help="the installed honua executable")
    parser.add_argument("--proxy", help="the installed honua-mcp-proxy executable (npm)")
    args = parser.parse_args()
    cli = Cli(args.client, args.honua, args.proxy, Path(os.environ["SDKREG_PLAN"]).parent)
    started = time.monotonic()
    (run_npm if args.client == "npm" else run_pypi)(cli, {})
    print(f"[cli-driver] {args.client} workflow finished in {time.monotonic() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
