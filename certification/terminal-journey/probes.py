#!/usr/bin/env python3
"""Deterministic probe primitives for the terminal journey driver.

Every probe is a fixed command, HTTP request or MCP tool call. There is no model
anywhere in this module and no adaptive retry on content: a probe either observes
the contract it names or reports why it could not. That is what makes a failure
identify a broken contract rather than a flaky run (honua-release#123).
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Fixed, deterministic budgets. No exponential backoff, no jitter.
HTTP_TIMEOUT_SECONDS = 30
READINESS_POLL_SECONDS = 3
MCP_TIMEOUT_SECONDS = 120


@dataclass
class Check:
    """One deterministic probe outcome."""

    id: str
    kind: str  # http | cli | mcp-tool | compose | artifact
    invocation: str
    status: str  # pass | fail | blocked
    detail: str = ""
    blocked_by: list[str] = field(default_factory=list)

    def as_receipt(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "invocation": self.invocation,
            "status": self.status,
            "detail": self.detail,
        }
        if self.status == "blocked":
            row["blockedBy"] = list(dict.fromkeys(self.blocked_by))
        return row


def blocked(check_id: str, kind: str, invocation: str, why: str, blockers: list[str]) -> Check:
    """A probe that cannot run yet. Never a pass, never silently skipped."""
    return Check(check_id, kind, invocation, "blocked", why, list(blockers))


@dataclass
class HttpResult:
    status: int
    body: bytes
    content_type: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


def http_get(url: str, *, headers: dict[str, str] | None = None) -> HttpResult:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return HttpResult(response.status, response.read(), response.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read(), exc.headers.get("Content-Type", "") if exc.headers else "")


def wait_for_ready(url: str, *, timeout_seconds: int) -> tuple[bool, str]:
    """Poll a readiness endpoint on a fixed interval until it reports ready."""
    deadline = time.monotonic() + timeout_seconds
    last = "no response"
    while time.monotonic() < deadline:
        try:
            result = http_get(url)
            last = f"HTTP {result.status}: {result.text().strip()[:120]}"
            body = result.text().strip()
            ready = body.lower() == "ready"
            if not ready and "json" in result.content_type.lower():
                try:
                    document = result.json()
                    ready = isinstance(document, dict) and str(document.get("status", "")).lower() == "ready"
                except json.JSONDecodeError:
                    ready = False
            if result.status == 200 and ready:
                return True, last
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = f"unreachable: {exc}"
        time.sleep(READINESS_POLL_SECONDS)
    return False, last


class ComposeError(RuntimeError):
    pass


def resolve_env_default(name: str, default: str) -> str:
    """Honor an explicitly configured value and fall back only when absent."""
    return os.environ.get(name, default)


@dataclass
class Compose:
    """Lifecycle for the pinned-candidate local Docker stack."""

    compose_file: str
    project: str
    env: dict[str, str]

    def _run(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, **self.env}
        return subprocess.run(
            ["docker", "compose", "-f", self.compose_file, "-p", self.project, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )

    def up(self) -> subprocess.CompletedProcess[str]:
        return self._run("up", "-d", "--wait", timeout=900)

    def down(self) -> subprocess.CompletedProcess[str]:
        return self._run("down", "-v", "--remove-orphans", timeout=300)

    def images(self) -> str:
        return self._run("images", "--format", "json", timeout=120).stdout


class McpError(RuntimeError):
    pass


class McpProxySession:
    """Speak MCP JSON-RPC through the pinned `honua-mcp-proxy` over stdio.

    Using the pinned proxy rather than a direct HTTP call is deliberate: stage 1
    must prove *proxy connectivity* from the exact client artifact, not merely that
    the server answers.
    """

    def __init__(self, argv: list[str] | str, remote_url: str, env: dict[str, str] | None = None):
        self.argv = [argv] if isinstance(argv, str) else list(argv)
        self.remote_url = remote_url
        self.env = env or {}
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 0

    def __enter__(self) -> McpProxySession:
        environment = {**os.environ, **self.env, "HONUA_MCP_REMOTE_URL": self.remote_url}
        self._process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            self._process.kill()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise McpError("proxy session is not started")
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"proxy closed the connection during {method}: {exc}") from exc

        deadline = time.monotonic() + MCP_TIMEOUT_SECONDS
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise McpError(f"proxy did not answer {method} within {MCP_TIMEOUT_SECONDS}s")
            line = self._process.stdout.readline()
            if not line:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise McpError(f"proxy exited during {method}: {stderr.strip()[:300]}")
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == self._next_id:
                return payload

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._process is None or self._process.stdin is None:
            raise McpError("proxy session is not started")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def initialize(self) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "honua-terminal-journey-driver", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        # Paginate independently of any expected count (#123 control-plane preflight).
        while True:
            params = {"cursor": cursor} if cursor else {}
            payload = self.request("tools/list", params)
            if "error" in payload:
                raise McpError(f"tools/list returned {payload['error']}")
            result = payload.get("result") or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools


def enumerate_tools(bin_path: Path, remote_url: str) -> tuple[tuple[str, ...], str | None, str | None]:
    """Enumerate the candidate tool surface through the pinned proxy.

    Two fixed attempts, in this order:

    1. the installed executable exactly as npm exposes it, which is the only
       invocation honua-release#136 permits a customer to make; then
    2. the same pinned bytes launched by their resolved module path.

    If (1) is silent and (2) works, that difference is itself evidence: the
    published proxy guards its entry point on ``process.argv[1] ===
    fileURLToPath(import.meta.url)``, which is false when Node is started through
    npm's ``node_modules/.bin`` symlink, so the installed executable exits doing
    nothing. The tool surface is still read from the pinned bytes; the packaging
    defect is reported alongside it rather than hidden.
    """
    attempts: list[tuple[list[str], str]] = [([str(bin_path)], "installed executable")]
    real = bin_path.resolve()
    if real != bin_path:
        attempts.append((["node", str(real)], "resolved module path"))

    first_error: str | None = None
    for index, (argv, label) in enumerate(attempts):
        names, error = _enumerate_with(argv, remote_url)
        if error is None:
            note = None
            if index > 0:
                note = (
                    "the pinned `honua-mcp-proxy` executable exits silently when launched "
                    "through npm's node_modules/.bin shim; its published entry point is "
                    "guarded on `process.argv[1] === fileURLToPath(import.meta.url)`, which "
                    "the shim path never satisfies. The same pinned bytes were launched by "
                    f"their resolved module path to read the tool surface ({label}). This is "
                    "a defect in the pinned client artifact, not in the candidate server."
                )
            if index > 0:
                return names, first_error or "installed executable failed", note
            return names, None, note
        if first_error is None:
            first_error = f"{label}: {error}"
    return (), first_error or "the pinned proxy could not be started", None


def _enumerate_with(argv: list[str], remote_url: str) -> tuple[tuple[str, ...], str | None]:
    try:
        with McpProxySession(argv, remote_url) as session:
            session.initialize()
            tools = session.list_tools()
        return tuple(sorted(str(tool.get("name", "")) for tool in tools)), None
    except (McpError, OSError) as exc:
        return (), str(exc)
