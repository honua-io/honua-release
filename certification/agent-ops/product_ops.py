"""Call the installed server's /mcp tools and approve as a different principal.

The harness is the approver. This module does not apply a deploy, restore a
database, or restart a workload.
"""
from __future__ import annotations

import json
import re
from typing import Callable
from urllib.parse import quote

TOOL_NAMES = (
    "honua_ops_health",
    "honua_ops_findings",
    "honua_alert_events",
    "honua_operate_events",
    "honua_propose_platform_release_convergence",
    "honua_propose_rollback",
    "honua_propose_deploy_operation",
    "honua_propose_finding",
)

_PROPOSAL_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_SECRET_KEY = re.compile(r"(?i)password|secret|token|credential|authorization|api[-_]?key|connection")
Transport = Callable[[dict], dict]


class ProductError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def scrub_model_env(env: dict) -> dict:
    """Return the environment a model process may see. AWS credentials stay behind."""
    if not isinstance(env, dict):
        raise ProductError("model-env")
    allowed = {"HOME", "PATH", "LANG", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}
    return {key: env[key] for key in allowed if key in env}


def redact(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                continue
            cleaned[str(key)] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000]
    return value


def proposal_ids(payload: dict) -> list[str]:
    found = []
    if not isinstance(payload, dict):
        return found
    direct = payload.get("proposalId") or payload.get("proposal_id")
    if isinstance(direct, str):
        found.append(direct)
    targets = payload.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict):
                identifier = target.get("proposalId") or target.get("proposal_id")
                if isinstance(identifier, str):
                    found.append(identifier)
    return found


class ProductClient:
    def __init__(self, endpoint: str, proposer_key: str, transport: Transport):
        if not endpoint.startswith("https://") or not proposer_key or proposer_key.strip() != proposer_key:
            raise ProductError("endpoint")
        self.endpoint = endpoint.rstrip("/")
        self._key = proposer_key
        self._transport = transport
        self._session = None
        self._next_id = 1

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in TOOL_NAMES:
            raise ProductError("unknown-tool", name)
        if not isinstance(arguments, dict):
            raise ProductError("arguments")
        self._ensure_session()
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._allocate(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        body = _json_body(response)
        if response.get("status") != 200 or not isinstance(body, dict):
            raise ProductError("mcp-http", str(response.get("status")))
        if body.get("error"):
            raise ProductError("mcp-error", _error_code(body.get("error")))
        result = body.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            raise ProductError("tool-failed", name)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return redact(structured)
        return redact(_text_payload(result))

    def _ensure_session(self) -> None:
        if self._session:
            return
        opened = self._post({
            "jsonrpc": "2.0",
            "id": self._allocate(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "honua-ops-harness", "version": "1"},
            },
        }, session=False)
        session = (opened.get("headers") or {}).get("Mcp-Session-Id")
        if opened.get("status") != 200 or not isinstance(session, str) or not session:
            raise ProductError("mcp-session")
        self._session = session
        noted = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=True)
        if noted.get("status") not in (200, 202):
            raise ProductError("mcp-session")

    def _allocate(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _post(self, payload: dict, session: bool = True) -> dict:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if session:
            if not self._session:
                raise ProductError("mcp-session")
            headers["Mcp-Session-Id"] = self._session
        return self._transport({
            "method": "POST",
            "url": self.endpoint + "/mcp",
            "headers": headers,
            "body": payload,
        })


class Approver:
    """Separate principal. Refuses to approve with the proposer's credential."""

    def __init__(self, endpoint: str, proposer_key: str, approver_key: str, transport: Transport):
        if not endpoint.startswith("https://") or not approver_key or approver_key == proposer_key:
            raise ProductError("approver")
        self.endpoint = endpoint.rstrip("/")
        self._key = approver_key
        self._transport = transport

    def approve(self, proposal_id: str) -> dict:
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
            raise ProductError("proposal-id")
        response = self._transport({
            "method": "POST",
            "url": self.endpoint + "/api/v1/admin/proposals/" + quote(proposal_id, safe="") + "/approve",
            "headers": {
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
            },
            "body": None,
        })
        body = _json_body(response)
        status = response.get("status")
        if status == 422 and isinstance(body, dict) and _problem_code(body) == "operation-executor-unavailable":
            raise ProductError("operation-executor-unavailable")
        if status == 409:
            raise ProductError("approval-conflict")
        if status != 200 or not isinstance(body, dict):
            raise ProductError("approval-http", str(status))
        execution = body.get("executionOperationId") or body.get("execution_operation_id")
        return redact({
            "proposalId": proposal_id,
            "status": body.get("status"),
            "executionOperationId": execution if isinstance(execution, str) and execution else None,
        })


def _json_body(response: dict):
    body = response.get("body")
    if isinstance(body, (dict, list)) or body is None:
        return body
    if isinstance(body, str) and body:
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    return None


def _text_payload(result: dict) -> dict:
    content = result.get("content")
    if not isinstance(content, list):
        return {}
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _error_code(error) -> str:
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:200]
    return "error"


def _problem_code(body: dict) -> str | None:
    code = body.get("code")
    if isinstance(code, str):
        return code
    extensions = body.get("extensions")
    if isinstance(extensions, dict) and isinstance(extensions.get("code"), str):
        return extensions["code"]
    return None
