"""Credential-safe live transports; responses stay private to the executor."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import discovery

MAX_BYTES = 4 * 1024 * 1024


class ExecutionError(RuntimeError):
    def __init__(self, command, reason, *, blocked=False):
        self.command, self.reason, self.blocked = command, reason, blocked
        super().__init__(f"{command}: {reason}")


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
            headers["X-API-Key"] = self.credentials[principal]
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        label = f"{method} {urllib.parse.urlsplit(url).path}"
        try:
            with self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                raw, status = response.read(MAX_BYTES + 1), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(MAX_BYTES + 1), exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError(label, "candidate request failed") from exc
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

    def tool(self, name, arguments, view):
        """Re-negotiate the bounded view on the actual installed proxy session."""
        import probes

        if not self.proxy or not self.proxy.is_file():
            raise ExecutionError(name, "verified installed proxy is unavailable", blocked=True)
        with probes.McpProxySession(str(self.proxy), self.base_url + "/mcp",
                                    {"HONUA_API_KEY": self.credentials["proposer"],
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
                raise ExecutionError(name, "candidate tool call refused")
            return result

    def cli_approve(self, proposal_id, principal):
        if not self.honua or not self.honua.is_file():
            raise ExecutionError("approveOperationProposal", "verified installed honua CLI is unavailable", blocked=True)
        # No shell or caller-supplied flags; credentials travel only in the child environment.
        args = [str(self.honua), "admin", "operate", "approveOperationProposal",
                "--base-url", self.base_url, "--json", "--path", f"id={proposal_id}", "--profile", principal, "--yes"]
        profiles = self.workdir / "profiles"
        profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
        profiles.chmod(0o700)
        config = profiles / "config.json"
        config.touch(mode=0o600, exist_ok=True)
        config.chmod(0o600)
        config.write_text(json.dumps({"profiles": {name: {"baseUrl": self.base_url}
                                                  for name in ("proposer", "approver")}}))
        env = {"PATH": os.environ.get("PATH", ""), "HONUA_ADMIN_KEY": self.credentials[principal],
               "HONUA_CONFIG_HOME": str(self.workdir / "profiles")}
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError("approveOperationProposal", "typed CLI could not execute") from exc
        # Never retain stdout/stderr in a receipt, including on failure.
        if len(result.stdout.encode()) > MAX_BYTES:
            raise ExecutionError("approveOperationProposal", "CLI response exceeds byte bound")
        try:
            document = json.loads(result.stdout)
        except (ValueError, TypeError):
            document = None
        return result.returncode, document
