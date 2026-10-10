"""Credential-safe live transports; responses stay private to the executor."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import discovery

MAX_BYTES = 4 * 1024 * 1024


class ExecutionError(RuntimeError):
    def __init__(self, command, reason, *, blocked=False, blocked_by=None, problem=None):
        # blocked_by names the tracked dependency of a blocked step when it is more precise than
        # the journey driver itself (for example a capability the target topology does not offer).
        # problem holds the masked typed fields of a candidate tool error (tool_error), if any.
        self.command, self.reason, self.blocked = command, reason, blocked
        self.blocked_by = list(blocked_by or [])
        self.problem = dict(problem or {})
        super().__init__(f"{command}: {reason}")


# The typed fields of an MCP tool error (honua-server McpToolHelpers.ErrorResult mirrors the problem
# envelope in structuredContent) that a receipt may name. Only these scalar members are kept; the
# message is masked and bounded. Nothing else of the tool output reaches a receipt.
TOOL_ERROR_FIELDS = ("type", "title", "code", "reasonCode", "missingDependency", "capability", "kind",
                     "retryable", "approvalRequired")
TOOL_ERROR_MESSAGE_CHARS = 200


def _bounded_token(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^ -~]", "?", value).strip()
    return text[:120] if text and not discovery._SENSITIVE.search(text) else None


def credential_values(credentials):
    """Credential values a candidate message must never carry into a receipt."""
    if not isinstance(credentials, dict):
        return []
    return [value.removeprefix("Bearer ") for value in credentials.values() if isinstance(value, str)]


def _scrub(text, secrets):
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return text


def masked_fields(values, secrets=()):
    """Bounded scalar evidence for a receipt: tokens kept, the message masked and bounded.

    The candidate's error envelope is untrusted, so the run's own credentials are scrubbed from every
    retained field, not only the message.
    """
    fields = {}
    for name, value in values.items():
        if name == "message":
            continue
        token = _bounded_token(_scrub(value, secrets) if isinstance(value, str) else value)
        if token is not None:
            fields[name] = token
    message = values.get("message")
    if isinstance(message, str) and message.strip():
        masked = discovery.excerpt(_scrub(message, secrets).encode("utf-8"))
        fields["message"] = (masked if len(masked) <= TOOL_ERROR_MESSAGE_CHARS
                             else masked[:TOOL_ERROR_MESSAGE_CHARS] + "...(truncated)")
    return fields


def describe_fields(fields):
    return "; ".join(f"{name}={json.dumps(value) if name == 'message' else value}" for name, value in fields.items())


def tool_error(result, secrets=()):
    """The typed problem a candidate tool error carries, masked; never raw tool output."""
    content = result.get("structuredContent")
    content = content if isinstance(content, dict) else {}
    nested = content.get("error") if isinstance(content.get("error"), dict) else {}
    values = {name: content.get(name, nested.get(name)) for name in TOOL_ERROR_FIELDS}
    message = content.get("message", content.get("detail", nested.get("message")))
    if not isinstance(message, str):
        texts = [block.get("text") for block in result.get("content") or []
                 if isinstance(block, dict) and isinstance(block.get("text"), str)]
        message = texts[0] if texts and not texts[0].lstrip().startswith(("{", "[")) else None
    return masked_fields({**values, "message": message}, secrets)


def describe_tool_error(fields):
    if not fields:
        return "candidate returned an MCP tool error without a typed problem"
    return f"candidate returned an MCP tool error ({describe_fields(fields)})"


def safe_url(base, path):
    """Forbid credentials, query strings, fragments and cross-origin artifacts."""
    wanted = urllib.parse.urlsplit(urllib.parse.urljoin(base.rstrip("/") + "/", path))
    origin = urllib.parse.urlsplit(base)
    if (wanted.scheme, wanted.netloc) != (origin.scheme, origin.netloc) or (
            wanted.username or wanted.password or wanted.query or wanted.fragment):
        raise ExecutionError("artifact read", "URL is outside the candidate origin or contains private parameters")
    # Reuse discovery's HTTPS/loopback policy before any credential is sent.
    discovery.HttpSession(urllib.parse.urlunsplit(wanted), "")
    return urllib.parse.urlunsplit(wanted)


class Transport:
    def __init__(self, base_url, proxy, honua, workdir, credentials):
        safe_url(base_url, "/")
        self.base_url, self.proxy, self.honua = base_url, proxy, honua
        self.workdir, self.credentials = Path(workdir), credentials
        self.opener = urllib.request.build_opener(discovery._NoRedirect())

    def http(self, method, path, *, principal="proposer", body=None, expected=(200,), extra_headers=None):
        url = safe_url(self.base_url, path)
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        headers.update(extra_headers or {})
        if principal is not None:
            credential = self.credentials[principal]
            if callable(credential):
                credential = credential()
            if credential.startswith("Bearer "):
                headers["Authorization"] = credential
            else:
                headers["X-API-Key"] = credential
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        label = f"{method} {urllib.parse.urlsplit(url).path}"
        discovery.record_http(method, url, None, None)
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                raw, status = response.read(MAX_BYTES + 1), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(MAX_BYTES + 1), exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError(label, "candidate request failed") from exc
        discovery.record_http(method, url, status, raw)
        if status not in expected:
            raise ExecutionError(label, f"HTTP {status}")
        if len(raw) > MAX_BYTES:
            raise ExecutionError(label, "response exceeds byte bound")
        return raw, status

    def get_json(self, path, **kwargs):
        raw, _ = self.http("GET", path, **kwargs)
        try:
            return discovery.parse(raw)
        except discovery.DiscoveryError as exc:
            raise ExecutionError("GET " + path, "candidate did not return valid bounded JSON") from exc

    def tool(self, name, arguments, view, principal="proposer"):
        """Re-negotiate the bounded view on the actual installed proxy session."""
        import probes

        if not self.proxy or not self.proxy.is_file():
            raise ExecutionError(name, "verified installed proxy is unavailable", blocked=True)
        with probes.McpProxySession(str(self.proxy), self.base_url + "/mcp",
                                    {"HONUA_API_KEY": self.credentials[principal],
                                     "HONUA_ADMIN_KEY": "", "HONUA_MCP_AUTH_TOKEN": ""}) as session:
            initialized = session.initialize(workflow_view="setup")
            if "error" in initialized:
                raise ExecutionError(name, "proxy initialize refused")
            current = session.request("tools/list", {"view": "setup"})
            result = current.get("result", {})
            if (result.get("_meta") != view["metadata"] or result.get("tools") != view["tools"]):
                raise ExecutionError(name, "bounded authority changed since observation")
            if name not in {tool["name"] for tool in result["tools"]}:
                raise ExecutionError(name, "tool is outside the initialize-bound view")
            response = session.request("tools/call", {"name": name, "arguments": arguments})
            result = response.get("result")
            if "error" in response or not isinstance(result, dict):
                # A JSON-RPC error carries the same typed problem in error.data; name it (masked).
                error = response.get("error") if isinstance(response.get("error"), dict) else {}
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                fields = tool_error({"structuredContent": {"message": error.get("message"), **data}},
                                    self.secret_values())
                raise ExecutionError(name, "candidate tool call refused"
                                     + (f" ({describe_fields(fields)})" if fields else ""), problem=fields)
            return result

    def secret_values(self):
        return credential_values(self.credentials)

    def cli_approve(self, proposal_id, principal):
        return self.cli_admin("operate", "approveOperationProposal", principal,
                              path={"id": proposal_id}, yes=True, label="approveOperationProposal")

    def cli_admin(self, group, operation, principal, *, path=None, body=None, content_type=None,
                  yes=False, label=None):
        """Run one fixed `honua admin <group> <operation>` from the verified installed CLI.

        Arguments are built here from harness-owned values; nothing is shell-parsed and no
        model argv reaches the child. Credentials travel only in the child environment.
        """
        label = label or f"honua admin {group} {operation}"
        if not self.honua or not self.honua.is_file():
            raise ExecutionError(label, "verified installed honua CLI is unavailable", blocked=True)
        args = [str(self.honua), "admin", group, operation, "--base-url", self.base_url, "--json"]
        for name, value in (path or {}).items():
            args += ["--path", f"{name}={value}"]
        if body is not None:
            args += ["--body", json.dumps(body, allow_nan=False, separators=(",", ":"))]
        if content_type:
            args += ["--content-type", content_type]
        args += ["--profile", principal]
        if yes:
            args.append("--yes")
        profiles = self.workdir / "profiles"
        profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        profiles.chmod(0o700)
        config = profiles / "config.json"
        config.touch(mode=0o600, exist_ok=True)
        config.chmod(0o600)
        config.write_text(json.dumps({"profiles": {name: {"baseUrl": self.base_url}
                                                  for name in ("operator", "proposer", "approver")}}))
        env = {"PATH": os.environ.get("PATH", ""), "HONUA_ADMIN_KEY": self.credentials[principal],
               "HONUA_CONFIG_HOME": str(self.workdir / "profiles")}
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError(label, "typed CLI could not execute") from exc
        # Never retain stdout/stderr in a receipt, including on failure.
        if len(result.stdout.encode()) > MAX_BYTES:
            raise ExecutionError(label, "CLI response exceeds byte bound")
        try:
            document = json.loads(result.stdout)
        except (ValueError, TypeError):
            document = None
        return result.returncode, document
