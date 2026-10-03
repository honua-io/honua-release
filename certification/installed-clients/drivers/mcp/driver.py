#!/usr/bin/env python3
"""MCP driver for the installed-client regression suite (mcp-* scenarios).

Speaks JSON-RPC only through the stdio of the manifest-pinned ``honua-mcp-proxy``, launched through
the npm ``node_modules/.bin`` shim exactly as an MCP host launches it. Discovery runs anonymously;
the full-catalog override and the tool calls run with the operator key in the proxy's environment.
The driver never makes an HTTP request itself. Prints one JSON observation per step.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
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
SCENARIO = "mcp-workflow"
VIEW_SELECTOR = "honua.io/workflow-view"
TERMINAL_JOB_STATES = {"Succeeded", "Failed", "Cancelled"}
MAX_PAGES = 100

API = {
    "initialize-setup": "honua-mcp-proxy initialize (workflow-view setup)",
    "setup-tools-list": "honua-mcp-proxy tools/list (selector-free, setup session)",
    "default-tools-list": "honua-mcp-proxy initialize + tools/list (no selector)",
    "full-catalog-refused": "honua-mcp-proxy tools/list {view: full} (anonymous)",
    "full-catalog": "honua-mcp-proxy tools/list {view: full} (HONUA_API_KEY)",
    "read": "honua-mcp-proxy tools/call honua_query_features",
    "render": "honua-mcp-proxy tools/call honua_render_map",
    "buffer-submit": "honua-mcp-proxy tools/call honua_execute_plan (geometry.buffer)",
    "buffer-poll": "honua-mcp-proxy resources/read honua://jobs/{jobId}",
    "buffer-result": "honua-mcp-proxy resources/read honua://jobs/{jobId}/results",
}


class RpcError(Exception):
    """A JSON-RPC error or a tool result flagged isError. Only its code reaches the observation."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def emit(name: str, **payload: Any) -> None:
    print(json.dumps({"scenario": SCENARIO, "step": name, "api": API[name], **payload}, default=str), flush=True)


def error_of(exc: BaseException) -> dict[str, Any]:
    print(f"[mcp-driver] {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
    return {"type": exc.kind if isinstance(exc, RpcError) else type(exc).__name__, "status": None}


def step(name: str, action: Callable[[], dict[str, Any]], state: dict[str, Any], needs: tuple[str, ...] = ()) -> None:
    missing = [dependency for dependency in needs if dependency not in state]
    if missing:
        emit(name, skipped=f"depends on {missing}, which did not complete")
        return
    try:
        emit(name, observed=action())
    except Exception as exc:  # noqa: BLE001 - every proxy failure is an observation
        if not isinstance(exc, RpcError):
            traceback.print_exc(file=sys.stderr)
        emit(name, error=error_of(exc))


def session(proxy: str, key: str = "") -> probes.McpProxySession:
    # Only the credential the step needs; an inherited operator variable must not authenticate discovery.
    return probes.McpProxySession([proxy], f"{BASE}/mcp", env={"HONUA_API_KEY": key, "HONUA_ADMIN_KEY": "",
                                                               "HONUA_MCP_AUTH_TOKEN": "", "HONUA_BASE_URL": ""})


def result_of(response: dict[str, Any]) -> dict[str, Any]:
    if "error" in response:
        error = response["error"]
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        raise RpcError(str(data.get("code") or error.get("code")))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RpcError("malformed-result")
    return result


def tool(mcp: probes.McpProxySession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = result_of(mcp.request("tools/call", {"name": name, "arguments": arguments}))
    content = result.get("structuredContent")
    if result.get("isError") or not isinstance(content, dict):
        code = content.get("code") if isinstance(content, dict) else None
        raise RpcError(f"tool-error:{code or 'unstructured'}")
    return content


def resource(mcp: probes.McpProxySession, uri: str) -> dict[str, Any]:
    contents = result_of(mcp.request("resources/read", {"uri": uri})).get("contents") or []
    if len(contents) != 1 or not isinstance(contents[0].get("text"), str):
        raise RpcError("malformed-resource")
    return json.loads(contents[0]["text"])


def view_of(result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    tools = result.get("tools") if isinstance(result.get("tools"), list) else []
    return {"view": meta.get("view"), "revision": meta.get("revision"), "toolCount": meta.get("toolCount"),
            "names": [tool.get("name") for tool in tools], "nextCursor": result.get("nextCursor")}


def initialized(response: dict[str, Any]) -> dict[str, Any]:
    result = result_of(response)
    identity = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
    return {"protocolVersion": result.get("protocolVersion"), "serverName": identity.get("name"),
            "serverVersion": identity.get("version")}


def full_catalog(mcp: probes.McpProxySession) -> tuple[list[str], int]:
    names: list[str] = []
    cursor, pages = None, 0
    while pages < MAX_PAGES:
        result = result_of(mcp.request("tools/list", {"view": "full", **({"cursor": cursor} if cursor else {})}))
        pages += 1
        names += [tool.get("name") for tool in result.get("tools") or []]
        cursor = result.get("nextCursor")
        if not cursor:
            return names, pages
    raise RpcError("unbounded-pagination")


def point_wkb(x: float, y: float) -> str:
    return base64.b64encode(struct.pack("<BIdd", 1, 1, x, y)).decode()


def run(proxy: str) -> None:
    key = os.environ["SDKREG_API_KEY"]
    sites, area, processes, render_spec = PLAN["sites"], PLAN["area"], PLAN["processes"], PLAN["mcp"]["render"]
    state: dict[str, Any] = {}

    with session(proxy) as setup:
        def initialize_setup() -> dict[str, Any]:
            observed = initialized(setup.initialize(workflow_view="setup"))
            state["setup"] = True
            return observed
        step("initialize-setup", initialize_setup, state)
        step("setup-tools-list", lambda: view_of(result_of(setup.request("tools/list"))), state, ("setup",))

    with session(proxy) as default:
        def default_view() -> dict[str, Any]:
            initialized(default.initialize())
            return view_of(result_of(default.request("tools/list")))
        step("default-tools-list", default_view, state)

    with session(proxy) as anonymous:
        def refused() -> dict[str, Any]:
            initialized(anonymous.initialize())
            names, pages = full_catalog(anonymous)
            return {"names": names, "pages": pages}
        step("full-catalog-refused", refused, state)

    with session(proxy, key) as operator:
        def full() -> dict[str, Any]:
            initialized(operator.initialize())
            names, pages = full_catalog(operator)
            restored = view_of(result_of(operator.request("tools/list")))
            return {"names": names, "pages": pages, "restoredView": restored["view"]}
        step("full-catalog", full, state)

        def read() -> dict[str, Any]:
            content = tool(operator, "honua_query_features", {"serviceId": sites["service"], "layerId": sites["layerId"],
                                                              "where": sites["where"], "limit": 1000})
            return {"features": [{"attributes": feature["attributes"], "x": feature["geometry"]["coordinates"][0],
                                  "y": feature["geometry"]["coordinates"][1]} for feature in content["features"]]}
        step("read", read, state)

        def render() -> dict[str, Any]:
            content = tool(operator, "honua_render_map", {
                "layers": [{"serviceId": area["service"], "layerId": area["layerId"]}], "bbox": render_spec["bbox"],
                "bboxSrid": 4326, "width": render_spec["width"], "height": render_spec["height"], "transparent": True,
                "maxInlineBytes": 1024 * 1024})
            image = content.get("image") or {}
            return {"png": image.get("base64"), "mimeType": image.get("format")}
        step("render", render, state)

        def submit() -> dict[str, Any]:
            x, y = processes["point"]
            plan = {"planId": "sdkreg-mcp-buffer", "intentId": "sdkreg-mcp-buffer", "outputs": ["FeatureLayer"],
                    "steps": [{"stepId": "buffer", "kind": "Geoprocess", "processId": processes["processId"], "inputs": {
                        "wkb": point_wkb(x, y), "srid": str(processes["srid"]), "distance": str(processes["distance"]),
                        "geodesic": "false"}}]}
            content = tool(operator, "honua_execute_plan", {"plan": plan, "idempotencyKey": f"sdkreg-mcp-buffer-{time.time_ns()}"})
            if content.get("jobId"):
                state["job"] = content["jobId"]
            return {"jobId": content.get("jobId"), "status": content.get("status")}
        step("buffer-submit", submit, state)

        def poll() -> dict[str, Any]:
            deadline = time.monotonic() + processes["pollTimeoutSeconds"]
            while True:
                job = resource(operator, f"honua://jobs/{state['job']}")
                if job.get("status") in TERMINAL_JOB_STATES or time.monotonic() > deadline:
                    if job.get("status") == "Succeeded":
                        state["succeeded"] = True
                    return {"status": job.get("status")}
                time.sleep(1)
        step("buffer-poll", poll, state, ("job",))

        def result() -> dict[str, Any]:
            package = resource(operator, f"honua://jobs/{state['job']}/results")
            artifacts = [item for item in package.get("artifacts") or [] if item.get("kind") == "FeatureLayer"]
            if len(artifacts) != 1:
                raise RpcError(f"{len(artifacts)}-feature-layer-artifacts")
            uri = artifacts[0].get("uri") or ""
            prefix = "data:application/geo+json;base64,"
            if not uri.startswith(prefix):
                raise RpcError("artifact-not-inline-geojson")
            return {"geometry": json.loads(base64.b64decode(uri[len(prefix):], validate=True))}
        step("buffer-result", result, state, ("succeeded",))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True, help="the installed honua-mcp-proxy executable")
    args = parser.parse_args()
    started = time.monotonic()
    run(args.proxy)
    print(f"[mcp-driver] workflow finished in {time.monotonic() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
