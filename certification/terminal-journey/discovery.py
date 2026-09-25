"""Measured setup discovery over actual HTTP sessions and the installed proxy.

Discovery is not call authority. This module never executes a tool, reconstructs
a catalog, substitutes package bytes, or qualifies later journey stages.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import probes

VIEW_KEY = "honua.io/workflow-view"
MAX_TOOLS = 48
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_VIEW_BYTES = 128 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_CATALOG_PAGES = 256


class DiscoveryError(ValueError):
    pass


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise DiscoveryError("nonstandard JSON numeric constant")


DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_invalid_constant)


def parse(raw: bytes) -> Any:
    if len(raw) > MAX_PAGE_BYTES:
        raise DiscoveryError("MCP response exceeds the page byte bound")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError("invalid UTF-8 JSON response") from exc


def _space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \r\n\t":
        position += 1
    return position


def member_span(text: str, start: int, wanted: str) -> tuple[int, int]:
    """Locate a parsed object's value without reserializing any JSON bytes."""
    position = _space(text, start)
    if text[position] != "{":
        raise DiscoveryError("expected a JSON object")
    position = _space(text, position + 1)
    while text[position] != "}":
        key, end = DECODER.raw_decode(text, position)
        position = _space(text, end)
        if text[position] != ":":
            raise DiscoveryError("invalid object separator")
        begin = _space(text, position + 1)
        _, end = DECODER.raw_decode(text, begin)
        if key == wanted:
            return begin, end
        position = _space(text, end)
        if text[position] == ",":
            position = _space(text, position + 1)
    raise DiscoveryError(f"missing JSON member {wanted}")


def wire_measurements(raw: bytes) -> dict[str, Any]:
    document = parse(raw)
    if not isinstance(document, dict) or not isinstance(document.get("result"), dict):
        raise DiscoveryError("missing MCP result")
    text = raw.decode("utf-8")
    result_start, _ = member_span(text, 0, "result")
    start, end = member_span(text, result_start, "tools")
    array = text[start:end]
    if not array.startswith("["):
        raise DiscoveryError("tools is not an array")
    sizes = []
    position = _space(array, 1)
    while array[position] != "]":
        _, finish = DECODER.raw_decode(array, position)
        sizes.append(len(array[position:finish].encode("utf-8")))
        position = _space(array, finish)
        if array[position] == ",":
            position = _space(array, position + 1)
    encoded = array.encode("utf-8")
    return {"descriptorBytes": len(encoded), "largestDescriptorBytes": max(sizes, default=0),
            "descriptorDigest": digest(encoded), "descriptorSizes": sizes}


def _names(tools: Any) -> list[str]:
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise DiscoveryError("tools must be complete descriptor objects")
    names = [tool.get("name") for tool in tools]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise DiscoveryError("missing or duplicate tool names")
    if any(not isinstance(tool.get("inputSchema"), dict) or tool["inputSchema"].get("type") != "object" for tool in tools):
        raise DiscoveryError("missing complete tool input schema")
    return names


def validate_view(raw: bytes, expected_view: str = "setup") -> dict[str, Any]:
    document = parse(raw)
    result = document.get("result") if isinstance(document, dict) else None
    if not isinstance(result, dict) or result.get("nextCursor") is not None:
        raise DiscoveryError("bounded view must be complete in one response")
    names = _names(result.get("tools"))
    meta = result.get("_meta")
    if not isinstance(meta, dict) or meta.get("view") != expected_view:
        raise DiscoveryError("requested server-authored view identity is missing")
    for field in ("revisionDigest", "membershipDigest", "descriptorDigest"):
        if not isinstance(meta.get(field), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", meta[field]):
            raise DiscoveryError(f"missing or malformed server {field}")
    if not isinstance(meta.get("revision"), str) or not meta["revision"] or meta.get("fullCatalogView") != "full":
        raise DiscoveryError("missing view revision or full catalog escape hatch")
    measured = wire_measurements(raw)
    if not 1 <= len(names) <= MAX_TOOLS or measured["descriptorBytes"] > MAX_VIEW_BYTES or measured["largestDescriptorBytes"] > MAX_DESCRIPTOR_BYTES:
        raise DiscoveryError("setup descriptor budget exceeded")
    for field, expected in (("toolCount", len(names)), ("descriptorBytes", measured["descriptorBytes"]),
                            ("estimatedTokens", measured["descriptorBytes"] // 4)):
        if type(meta.get(field)) is not int or meta[field] != expected:
            raise DiscoveryError(f"server {field} disagrees with measured wire bytes")
    if meta["descriptorDigest"] != measured["descriptorDigest"]:
        raise DiscoveryError("server descriptor digest disagrees with raw HTTP tools bytes")
    stages = meta.get("stages")
    if not isinstance(stages, list) or not stages:
        raise DiscoveryError("server-authored stages are missing")
    stage_by_tool = {}
    stage_ids = set()
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str) or not stage["id"] or stage["id"] in stage_ids:
            raise DiscoveryError("missing or duplicate server stage identity")
        stage_ids.add(stage["id"])
        members = stage.get("tools")
        if not isinstance(members, list) or not members:
            raise DiscoveryError("empty or missing required stage membership")
        for name in members:
            if not isinstance(name, str) or name not in names or name in stage_by_tool:
                raise DiscoveryError("stage membership does not partition the view")
            stage_by_tool[name] = stage["id"]
    if set(stage_by_tool) != set(names):
        raise DiscoveryError("stage membership omits a descriptor")
    membership = "".join(f"{stage_by_tool[name]}/{name}\n" for name in names).encode("utf-8")
    if digest(membership) != meta["membershipDigest"]:
        raise DiscoveryError("server membership digest disagrees with wire membership")
    return {"status": "pass", "tools": result["tools"], "metadata": meta, "measured": measured}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpSession:
    def __init__(self, url: str, credential: str):
        parsed = urllib.parse.urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in {"http", "https"}:
            raise DiscoveryError("MCP endpoint is not a credential-safe URL")
        if parsed.scheme == "http" and not loopback:
            raise DiscoveryError("credential-bearing HTTP must use loopback")
        self.url, self.credential, self.session = url, credential, None
        self.next_id = 0
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, params: dict | None = None) -> tuple[dict, bytes]:
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json", "X-API-Key": self.credential,
                   "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
                if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                    raise DiscoveryError("MCP HTTP response is not successful JSON")
                issued = response.headers.get("Mcp-Session-Id")
                if method == "initialize":
                    if not issued or len(issued) > 256:
                        raise DiscoveryError("HTTP initialize did not issue a session")
                    self.session = issued
                elif issued and issued != self.session:
                    raise DiscoveryError("HTTP response changed the bound session")
        except urllib.error.HTTPError as exc:
            raise DiscoveryError(f"MCP HTTP request refused with status {exc.code}") from exc
        document = parse(raw)
        if not isinstance(document, dict) or type(document.get("id")) is not int or document["id"] != self.next_id or document.get("jsonrpc") != "2.0" or "error" in document or not isinstance(document.get("result"), dict):
            raise DiscoveryError("MCP response identity/result validation failed")
        return document, raw

    def initialize(self) -> dict:
        document, _ = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "honua-terminal-discovery", "version": "1"}, "_meta": {VIEW_KEY: "setup"}})
        identity = document["result"].get("serverInfo")
        if document["result"].get("protocolVersion") != "2025-06-18" or not isinstance(identity, dict) or any(
                not isinstance(identity.get(field), str) or not identity[field] for field in ("name", "version")):
            raise DiscoveryError("HTTP initialize omitted a supported protocol or valid server identity")
        # Send the actual initialized notification on the server-issued session.
        request = urllib.request.Request(self.url, method="POST",
            data=b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            headers={"Content-Type": "application/json", "Accept": "application/json", "X-API-Key": self.credential,
                     "Mcp-Session-Id": self.session, "MCP-Protocol-Version": "2025-06-18"})
        with self.opener.open(request, timeout=30) as response:
            if response.status not in {200, 202, 204}:
                raise DiscoveryError("initialized notification was refused")
        return document["result"]

    def close(self) -> None:
        if self.session:
            request = urllib.request.Request(self.url, method="DELETE", headers={"X-API-Key": self.credential,
                "Mcp-Session-Id": self.session, "MCP-Protocol-Version": "2025-06-18"})
            try:
                self.opener.open(request, timeout=10).close()
            except (urllib.error.URLError, OSError):
                pass


def full_catalog(request) -> tuple[list[dict], int]:
    tools, cursors, total_bytes = [], set(), 0
    cursor = None
    for page in range(MAX_CATALOG_PAGES):
        params = {"view": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        document, raw = request("tools/list", params)
        total_bytes += len(raw)
        if total_bytes > MAX_CATALOG_BYTES:
            raise DiscoveryError("full catalog byte bound exceeded")
        result = document.get("result")
        if not isinstance(result, dict) or "error" in document:
            raise DiscoveryError("full catalog request failed")
        _names(result.get("tools"))
        tools.extend(result["tools"])
        _names(tools)
        cursor = result.get("nextCursor")
        if cursor is None:
            return tools, page + 1
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
            raise DiscoveryError("full catalog cursor is malformed or repeated")
        cursors.add(cursor)
    raise DiscoveryError("full catalog page bound exceeded")


def capture_setup_view(proxy: Path | None, url: str, credential: str) -> dict[str, Any]:
    receipt: dict[str, Any] = {"scope": "setup-discovery-only", "status": "fail", "qualification": False,
        "http": {"status": "blocked"}, "proxy": {"status": "blocked"}, "tools": []}
    session = None
    try:
        session = HttpSession(url, credential)
        identity = session.initialize()
        _, raw = session.request("tools/list")
        view = validate_view(raw)
        # Preserve original bytes as UTF-8 text; digest is checked again on readback.
        receipt["http"] = {**view, "status": "fail", "wireValidation": "pass", "serverInfo": identity.get("serverInfo"), "rawResponse": raw.decode("utf-8"),
                           "selection": "initialize-bound-session", "sessionIssued": True}
        complete, pages = full_catalog(session.request)
        canonical = {tool["name"]: tool for tool in complete}
        if any(canonical.get(tool["name"]) != tool for tool in view["tools"]):
            raise DiscoveryError("bounded descriptors differ from the complete canonical catalog")
        # The explicit full request must not mutate the negotiated session view.
        _, restored_raw = session.request("tools/list")
        restored = validate_view(restored_raw)
        if restored != view:
            raise DiscoveryError("request override changed the initialized session view")
        comparison_hash = digest(json.dumps(complete, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        receipt["http"].update({"status": "pass", "fullCatalogTools": len(complete), "fullCatalogPages": pages,
            "fullCatalogDescriptors": complete, "fullCatalogComparisonSha256": comparison_hash, "overrideRestored": True})
        if proxy is None or not proxy.is_file():
            raise DiscoveryError("verified installed proxy executable is unavailable")
        receipt["proxy"] = {"status": "fail", "installedExecutable": True}
        with probes.McpProxySession([str(proxy)], url, env={"HONUA_API_KEY": credential,
                "HONUA_ADMIN_KEY": "", "HONUA_MCP_AUTH_TOKEN": ""}) as stdio:
            initialized = stdio.initialize(workflow_view="setup")
            listed = stdio.request("tools/list")
            result = listed.get("result")
            if "error" in initialized or initialized.get("result", {}).get("protocolVersion") != identity.get("protocolVersion") or initialized.get("result", {}).get("serverInfo") != identity.get("serverInfo"):
                raise DiscoveryError("installed proxy initialized a different server identity")
            if result != parse(raw)["result"]:
                raise DiscoveryError("installed proxy session descriptors or metadata differ from HTTP")
            proxied_full, proxy_pages = full_catalog(lambda method, params: _proxy_request(stdio, method, params))
            if proxied_full != complete:
                raise DiscoveryError("installed proxy complete catalog differs from HTTP")
            if stdio.request("tools/list").get("result") != result:
                raise DiscoveryError("proxy request override changed its negotiated session")
        receipt["proxy"] = {"status": "pass", "selection": "initialize-bound-session", "installedExecutable": True,
                            "fullCatalogPages": proxy_pages, "overrideRestored": True, "fullCatalogComparisonSha256": comparison_hash}
        receipt.update({"status": "pass", "tools": view["tools"], "metadata": view["metadata"],
                        "catalogToolNames": [tool["name"] for tool in complete]})
    except (DiscoveryError, probes.McpError, OSError, ValueError, TypeError, KeyError) as exc:
        # Only our contract diagnostics are emitted; arbitrary proxy stderr or
        # upstream bodies may contain secrets and are deliberately not copied.
        receipt["error"] = str(exc) if isinstance(exc, DiscoveryError) else "setup transport or JSON contract could not be observed"
    finally:
        if session is not None:
            session.close()
    return receipt


def _proxy_request(session: probes.McpProxySession, method: str, params: dict) -> tuple[dict, bytes]:
    document = session.request(method, params)
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    # Proxy serialization is only a transport bound, never the server-wire digest oracle.
    parse(raw)
    return document, raw
