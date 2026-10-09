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
import re
import selectors
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Fixed, deterministic budgets. No exponential backoff, no jitter.
HTTP_TIMEOUT_SECONDS = 30
READINESS_POLL_SECONDS = 3
MCP_TIMEOUT_SECONDS = 120
MCP_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


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
        if self.status == "blocked" or (self.status == "fail" and self.blocked_by):
            # A failed check carries blockedBy only when the cause is a tracked issue.
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


# The only parent variables a proxy inherits: what Node needs to start and locate its temp
# directory. No credential, token or base URL crosses unless the session names it.
PROXY_INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT", "PATHEXT")


def proxy_environment(env: dict[str, str], remote_url: str) -> dict[str, str]:
    """A scrubbed proxy environment: the startup variables, the session's own non-empty values, the remote URL."""
    inherited = {key: os.environ[key] for key in PROXY_INHERITED_ENV if key in os.environ}
    return {**inherited, **{key: value for key, value in env.items() if value}, "HONUA_MCP_REMOTE_URL": remote_url}


class McpProxySession:
    """Speak MCP JSON-RPC through the pinned `honua-mcp-proxy` over stdio.

    Using the pinned proxy rather than a direct HTTP call is deliberate: stage 1
    must prove *proxy connectivity* from the exact client artifact, not merely that
    the server answers. The proxy starts from a scrubbed environment (see
    ``proxy_environment``): a session with no ``env`` is anonymous whatever the
    caller's environment holds.
    """

    def __init__(self, argv: list[str] | str, remote_url: str, env: dict[str, str] | None = None):
        self.argv = [argv] if isinstance(argv, str) else list(argv)
        self.remote_url = remote_url
        self.env = env or {}
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self._stdout_buffer = bytearray()

    def __enter__(self) -> McpProxySession:
        self._process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=proxy_environment(self.env, self.remote_url),
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
            self._process.wait(timeout=5)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise McpError("proxy session is not started")
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"proxy closed the connection during {method}: {exc}") from exc

        deadline = time.monotonic() + MCP_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while True:
                newline = self._stdout_buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._stdout_buffer[:newline])
                    del self._stdout_buffer[:newline + 1]
                    payload = parse_mcp_response(line)
                    if type(payload.get("id")) is int and payload["id"] == self._next_id:
                        return payload
                    continue
                if len(self._stdout_buffer) > MCP_MAX_RESPONSE_BYTES:
                    raise McpError("proxy response exceeds the byte bound")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise McpError(f"proxy did not answer {method} within {MCP_TIMEOUT_SECONDS}s")
                chunk = os.read(self._process.stdout.fileno(), min(65536, MCP_MAX_RESPONSE_BYTES + 1 - len(self._stdout_buffer)))
                if not chunk:
                    raise McpError(f"proxy exited during {method}")
                self._stdout_buffer.extend(chunk)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._process is None or self._process.stdin is None:
            raise McpError("proxy session is not started")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self._process.stdin.flush()

    def initialize(self, *, workflow_view: str | None = None) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "honua-terminal-journey-driver", "version": "1"},
                **({"_meta": {"honua.io/workflow-view": workflow_view}} if workflow_view is not None else {}),
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


def parse_mcp_response(line: bytes) -> dict[str, Any]:
    """Strict original proxy wire parsing, before any lossy reserialization."""
    if len(line) > MCP_MAX_RESPONSE_BYTES:
        raise McpError("proxy response exceeds the byte bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise McpError("proxy response contains duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise McpError("proxy response contains a nonstandard JSON constant")

    try:
        payload = json.loads(line.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise McpError("proxy response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise McpError("proxy response is not a JSON-RPC object")
    if "id" in payload:
        if type(payload["id"]) not in (int, str) or ("result" in payload) == ("error" in payload):
            raise McpError("proxy response has an invalid identity or result/error envelope")
        if not isinstance(payload.get("result", payload.get("error")), dict):
            raise McpError("proxy response result/error must be an object")
    elif not isinstance(payload.get("method"), str):
        raise McpError("proxy notification has no method")
    return payload


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


# ---------------------------------------------------------------------------
# Stage 2 credential preflight. Read-only discovery is not this probe.
# ---------------------------------------------------------------------------
_PREFLIGHT_NAME = "honua-terminal-journey-preflight"
_PREFLIGHT_GRANTS = ["admin:read"]
_GUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_SECRET_FIELDS = {"key", "secret", "password", "adminKey", "apiKey", "token", "accessToken"}
_CREDENTIAL_INVOCATION = (
    "HONUA_ADMIN_KEY=<env> honua admin secure "
    "createAdminApiKey --base-url <loopback> --json --yes --body "
    '{"name":"honua-terminal-journey-preflight","permissions":["admin:read"]} '
    "--secret-output <private-sink>; "
    "getAdminApiKeyEffectivePermissions --path id=<id>; "
    "listAdminApiKeys; "
    "revokeAdminApiKey --yes --path id=<id>"
)
_CLI_TIMEOUT_SECONDS = 60


@dataclass
class CredentialProbe:
    """Secret-free outcome of the reversible admin credential preflight."""

    status: str  # pass | fail | blocked
    detail: str
    invocation: str = _CREDENTIAL_INVOCATION
    blocked_by: list[str] = field(default_factory=list)
    key_id: str | None = None


def _redact(text: str, secrets: list[str]) -> tuple[str, bool]:
    leaked = False
    redacted = text
    for secret in secrets:
        if secret and secret in redacted:
            leaked = True
            redacted = redacted.replace(secret, "[redacted]")
    return redacted, leaked


def _admin_target_refusal(base_url: str) -> str | None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "admin base URL must not include credentials, a query, or a fragment"
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return None
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return None
    return "admin credential is not sent to a non-loopback HTTP endpoint"


def _has_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key in _SECRET_FIELDS or _has_secret_field(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_has_secret_field(child) for child in value)
    return False


def _load_json(stdout: str) -> tuple[Any | None, str | None]:
    text = stdout.strip()
    if not text:
        return None, "command produced no JSON"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "command produced non-JSON output"


def _envelope_data(document: Any) -> Any:
    if not isinstance(document, dict) or document.get("success") is not True:
        return None
    return document.get("data")


def _run_honua(
    honua: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str] | str:
    try:
        return subprocess.run(
            [str(honua), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__


def run_credential_preflight(
    *, honua: Path, base_url: str, admin_key: str, workdir: Path
) -> CredentialProbe:
    """Mint, inspect, and revoke one temporary admin:read key.

    The root credential is passed only as ``HONUA_ADMIN_KEY`` in the child
    environment. The one-time key is written by the CLI to a private sink and
    deleted here. Neither value is returned.
    """
    refusal = _admin_target_refusal(base_url)
    if refusal is not None:
        return CredentialProbe("fail", refusal)
    if len(admin_key) < 8:
        return CredentialProbe("blocked", "no usable admin credential is configured for this target")

    workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o700)
    config_home = workdir / "config"
    config_home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(config_home, 0o700)
    secret_path = workdir / "one-time-secret"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "HONUA_CONFIG_HOME": str(config_home),
        "HONUA_ADMIN_KEY": admin_key,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    body = json.dumps({"name": _PREFLIGHT_NAME, "permissions": list(_PREFLIGHT_GRANTS)}, separators=(",", ":"))
    outputs: list[str] = []
    issued_secret = ""
    key_id: str | None = None
    revoked = False
    problems: list[str] = []

    def capture(completed: subprocess.CompletedProcess[str] | str, label: str) -> Any | None:
        if isinstance(completed, str):
            problems.append(f"{label} {completed}")
            return None
        outputs.append(completed.stdout)
        outputs.append(completed.stderr)
        if completed.returncode != 0:
            problems.append(f"{label} exited {completed.returncode}")
            return None
        document, error = _load_json(completed.stdout)
        if error is not None:
            problems.append(f"{label} {error}")
            return None
        if _has_secret_field(document):
            problems.append(f"{label} JSON contained secret material")
        return document

    try:
        created = capture(
            _run_honua(
                honua,
                [
                    "admin",
                    "secure",
                    "createAdminApiKey",
                    "--base-url",
                    base_url,
                    "--json",
                    "--yes",
                    "--body",
                    body,
                    "--secret-output",
                    str(secret_path),
                ],
                env,
            ),
            "createAdminApiKey",
        )
        resource = created.get("resource") if isinstance(created, dict) else None
        if not isinstance(created, dict) or created.get("operationId") != "createAdminApiKey":
            problems.append("createAdminApiKey did not return the private-sink receipt")
        elif created.get("secretWritten") is not True or not isinstance(resource, dict):
            problems.append("createAdminApiKey did not write a private sink")
        else:
            candidate_id = resource.get("id")
            if not isinstance(candidate_id, str) or _GUID.fullmatch(candidate_id) is None:
                problems.append("createAdminApiKey did not return a key id")
            elif resource.get("name") != _PREFLIGHT_NAME or resource.get("permissions") != _PREFLIGHT_GRANTS:
                problems.append("createAdminApiKey did not grant exactly admin:read")
            elif resource.get("status") not in (None, "active"):
                problems.append("createAdminApiKey did not return an active key")
            else:
                key_id = candidate_id
        if secret_path.is_symlink():
            problems.append("private sink is a symlink")
        elif secret_path.is_file():
            file_mode = stat.S_IMODE(secret_path.stat().st_mode)
            if file_mode != 0o600:
                problems.append(f"private sink mode is {file_mode:04o}, expected 0600")
            issued_secret = secret_path.read_text(encoding="utf-8")
            if not issued_secret or issued_secret == admin_key:
                problems.append("private sink did not contain a distinct one-time key")
        elif key_id is not None:
            problems.append("private sink is missing")

        if key_id is not None:
            effective = capture(
                _run_honua(
                    honua,
                    [
                        "admin",
                        "secure",
                        "getAdminApiKeyEffectivePermissions",
                        "--base-url",
                        base_url,
                        "--json",
                        "--path",
                        f"id={key_id}",
                    ],
                    env,
                ),
                "getAdminApiKeyEffectivePermissions",
            )
            effective_data = _envelope_data(effective)
            if not isinstance(effective_data, dict):
                problems.append("effective permissions response was not a success envelope")
            elif (
                effective_data.get("id") != key_id
                or effective_data.get("status") != "active"
                or effective_data.get("canAuthenticate") is not True
                or effective_data.get("permissions") != _PREFLIGHT_GRANTS
            ):
                problems.append("effective permissions were not exactly active admin:read")

            listed = capture(
                _run_honua(
                    honua,
                    ["admin", "secure", "listAdminApiKeys", "--base-url", base_url, "--json"],
                    env,
                ),
                "listAdminApiKeys",
            )
            rows = _envelope_data(listed)
            if not isinstance(rows, list):
                problems.append("listAdminApiKeys response was not a success envelope")
            else:
                matches = [row for row in rows if isinstance(row, dict) and row.get("id") == key_id]
                if len(matches) != 1 or matches[0].get("status") != "active":
                    problems.append("listAdminApiKeys did not show the temporary key as active")
                elif matches[0].get("permissions") != _PREFLIGHT_GRANTS:
                    problems.append("listed grants were not exactly admin:read")

            revoked_doc = capture(
                _run_honua(
                    honua,
                    [
                        "admin",
                        "secure",
                        "revokeAdminApiKey",
                        "--base-url",
                        base_url,
                        "--json",
                        "--yes",
                        "--path",
                        f"id={key_id}",
                    ],
                    env,
                ),
                "revokeAdminApiKey",
            )
            revoked_data = _envelope_data(revoked_doc)
            if not isinstance(revoked_data, dict) or revoked_data.get("id") != key_id or revoked_data.get("status") != "revoked":
                problems.append("revokeAdminApiKey did not revoke the temporary key")
            else:
                confirmed = capture(
                    _run_honua(
                        honua,
                        ["admin", "secure", "listAdminApiKeys", "--base-url", base_url, "--json"],
                        env,
                    ),
                    "listAdminApiKeys after revoke",
                )
                remaining = _envelope_data(confirmed)
                if not isinstance(remaining, list):
                    problems.append("post-revoke list was not a success envelope")
                else:
                    still_active = [
                        row
                        for row in remaining
                        if isinstance(row, dict) and row.get("id") == key_id and row.get("status") == "active"
                    ]
                    if still_active:
                        problems.append("temporary key was still active after revoke")
                    else:
                        revoked = True
    finally:
        if key_id is not None and not revoked:
            capture(
                _run_honua(
                    honua,
                    [
                        "admin",
                        "secure",
                        "revokeAdminApiKey",
                        "--base-url",
                        base_url,
                        "--json",
                        "--yes",
                        "--path",
                        f"id={key_id}",
                    ],
                    env,
                ),
                "revokeAdminApiKey cleanup",
            )
        if secret_path.exists():
            try:
                secret_path.unlink()
            except OSError:
                problems.append("private sink could not be deleted")
        if secret_path.exists():
            problems.append("private sink still exists")

    _, leaked = _redact("\n".join(outputs), [admin_key, issued_secret])
    if leaked:
        problems.append("command output contained credential material and was redacted")
    if not problems and key_id is None:
        problems.append("credential preflight did not observe a key id")
    detail, detail_leaked = _redact("; ".join(dict.fromkeys(problems)), [admin_key, issued_secret])
    if detail_leaked:
        detail = "credential material was removed from the preflight detail"
    if problems:
        return CredentialProbe("fail", detail or "credential preflight failed", key_id=key_id)
    return CredentialProbe(
        "pass",
        f"temporary admin:read key {key_id} reported canAuthenticate true; "
        "list omitted key material; revoke removed the active key; private sink deleted",
        key_id=key_id,
    )
