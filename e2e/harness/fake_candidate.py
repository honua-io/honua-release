"""A fake candidate for the approval-lane tests: plain HTTP for the drivers, TLS for the human session.

It answers only what S3 and lib/human_approval.py ask, with the server's contract for approval:

* an API key on ``POST /api/v1/admin/proposals/{id}/approve`` gets the 403 the server sends for any
  approval that is not a person's (``key_approval`` selects a regression instead);
* the TLS listener mints a password session behind the antiforgery token, re-binds the token to the
  session, issues a review capability and accepts an approval only with cookie, token and capability,
  and refuses any of those requests that carries an explicit credential.

Every request is recorded so a test can assert what the clients sent.
"""
from __future__ import annotations

import json
import secrets
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PASSWORD = "fake-admin-password"
HUMAN_REFUSAL = ("Human approval requires a live interactive browser session and a valid review capability. "
                 "Open the proposal in Console; MCP tokens and API keys cannot approve it.")
FAMILIES = [
    {"family": "query", "format": "honua.query.v1", "currentSchemaVersion": "1",
     "supportedOperations": ["publish-request.create"], "publishSupported": True},
    {"family": "analysis", "format": "honua.analysis-content.v1", "currentSchemaVersion": "1",
     "supportedOperations": [], "publishSupported": False, "limitations": ["analysis stops at a content version"]},
    {"family": "map", "format": "honua_map_package.v1", "currentSchemaVersion": "1",
     "supportedOperations": ["publish-request.create"], "publishSupported": True},
]
# key_approval: "human-only" is the server's contract; "accepted" and "other-403" are regressions.
KEY_APPROVAL_ANSWERS = {
    "human-only": (403, {"status": 403, "title": "Forbidden", "detail": HUMAN_REFUSAL}),
    "other-403": (403, {"status": 403, "title": "Forbidden", "detail": "The caller lacks admin:approve."}),
}


class State:
    def __init__(self, key_approval: str, session_works: bool):
        self.key_approval, self.session_works = key_approval, session_works
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.proposals: dict[str, dict[str, Any]] = {}
        self.sessions: set[str] = set()
        self.echo_refused_password = False   # a careless server that quotes the rejected body back

    def new_proposal(self) -> tuple[str, str]:
        with self.lock:
            number = len(self.proposals) + 1
            proposal, handle = f"proposal-{number}", f"handle-{number}"
            self.proposals[proposal] = {"handle": handle, "status": "AwaitingApproval", "capability": None}
            return proposal, handle

    def by_handle(self, handle: str) -> dict[str, Any] | None:
        return next((p for p in self.proposals.values() if p["handle"] == handle), None)


class Handler(BaseHTTPRequestHandler):
    state: State
    tls = False
    body = ""

    def log_message(self, *args: Any) -> None:
        return

    def reply(self, status: int, body: Any = None, cookies: tuple[str, ...] = ()) -> None:
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for cookie in cookies:
            self.send_header("Set-Cookie", f"{cookie}; path=/; secure; httponly")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def cookies(self) -> dict[str, str]:
        pairs = (item.strip().split("=", 1) for item in self.headers.get("Cookie", "").split(";") if "=" in item)
        return {name: value for name, value in pairs}

    def record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length).decode() if length else ""
        self.state.requests.append({"tls": self.tls, "method": self.command, "path": self.path,
                                    "headers": dict(self.headers.items()), "body": self.body})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.record()
        (tls_get if self.tls else http_get)(self)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self.record()
        (tls_post if self.tls else http_post)(self)


def http_get(h: Handler) -> None:
    path = h.path.split("?")[0]
    if path.startswith("/api/v1/operations/handles/"):
        proposal = h.state.by_handle(path.rsplit("/", 1)[1]) or {}
        done = proposal.get("status") == "Succeeded"
        result = {"details": {"payload": {"status": "accepted", "requestId": "request-1"}}}
        h.reply(200, {"data": {"status": "Completed", "result": result} if done else {"status": "AwaitingApproval"}})
        return
    answers = {
        "/healthz/ready": {"status": "ready"},
        "/api/v1/admin/license": {"data": {"edition": "Enterprise", "mode": "disabled"}},
        "/api/v1/studio/package-families": {"data": {"families": FAMILIES}},
        "/api/v1/capabilities/manifest": {"server": {"deploymentEnvironment": "Development"},
                                          "capabilities": [{"id": "operations.proposals", "available": True}]},
    }
    h.reply(200, answers[path]) if path in answers else h.reply(404, {"detail": f"fake has no {path}"})


def http_post(h: Handler) -> None:
    path = h.path
    if path.endswith("/approve"):
        http_key_approve(h, path.split("/")[-2])
    elif path.endswith("/publish-requests"):
        proposal, handle = h.state.new_proposal()
        h.reply(202, {"data": {"handleId": handle, "proposalId": proposal}})
    elif path == "/api/v1/studio/package-drafts":
        h.reply(201, {"data": {"draftId": "draft-1"}})
    elif path.endswith("/validate"):
        h.reply(201, {"data": {"validation": {"status": "valid"}}})
    elif path.endswith("/preview-plan"):
        h.reply(200, {"data": {"plan": []}})
    elif path.endswith("/content-versions"):
        h.reply(201, {"data": {"itemId": "item-1", "versionId": "version-1"}})
    elif path.startswith("/api/v1/admin/api-keys"):
        h.reply(201, {"data": {"key": "minted-approver-key"}})
    else:
        h.reply(404, {"detail": f"fake has no POST {path}"})


def http_key_approve(h: Handler, proposal_id: str) -> None:
    proposal = h.state.proposals.get(proposal_id)
    if proposal is None:
        h.reply(404)
        return
    if h.state.key_approval in KEY_APPROVAL_ANSWERS:
        h.reply(*KEY_APPROVAL_ANSWERS[h.state.key_approval])
        return
    proposal["status"] = "Succeeded"
    h.reply(200, {"status": "Succeeded", "approvalChannel": None})


def csrf_token(h: Handler) -> str:
    session = h.cookies().get("honua_admin_session")
    return f"token-for-{session}" if session in h.state.sessions else "token-anonymous"


def explicit_credential(h: Handler) -> bool:
    return any(name in h.headers for name in ("X-API-Key", "Authorization"))


def tls_get(h: Handler) -> None:
    path = h.path.split("?")[0]
    if path == "/healthz/ready":
        h.reply(200, {"status": "ready"})
    elif path == "/api/v1/admin/auth/csrf":
        h.reply(200, {"requestToken": csrf_token(h)}, cookies=("honua_admin_csrf=cookie-token",))
    elif path.endswith("/approval-review"):
        tls_review(h, path.split("/")[-2])
    else:
        h.reply(404)


def tls_review(h: Handler, proposal_id: str) -> None:
    proposal = h.state.proposals.get(proposal_id)
    if explicit_credential(h) or h.cookies().get("honua_admin_session") not in h.state.sessions:
        h.reply(403, {"status": 403, "detail": HUMAN_REFUSAL})
        return
    if proposal is None:
        h.reply(404)
        return
    proposal["capability"] = secrets.token_hex(8)
    h.reply(200, {"proposal": {"proposalId": proposal_id}, "review": {"capability": proposal["capability"]}})


def tls_post(h: Handler) -> None:
    if h.cookies().get("honua_admin_csrf") != "cookie-token" or h.headers.get("X-Honua-CSRF") != csrf_token(h):
        h.reply(400)
        return
    if h.path == "/api/v1/admin/auth/session":
        tls_sign_in(h)
    elif h.path.endswith("/approve"):
        tls_approve(h, h.path.split("/")[-2])
    else:
        h.reply(404)


def tls_sign_in(h: Handler) -> None:
    body = json.loads(h.body or "{}")
    if not h.state.session_works or body.get("password") != PASSWORD:
        h.reply(401, {"detail": f"password {body.get('password')} rejected"} if h.state.echo_refused_password else None)
        return
    session = secrets.token_hex(8)
    h.state.sessions.add(session)
    h.reply(204, cookies=(f"honua_admin_session={session}",))


def tls_approve(h: Handler, proposal_id: str) -> None:
    proposal = h.state.proposals.get(proposal_id) or {}
    session = h.cookies().get("honua_admin_session")
    granted = proposal.get("capability") and h.headers.get("X-Honua-Approval-Capability") == proposal["capability"]
    if explicit_credential(h) or session not in h.state.sessions or not granted:
        h.reply(403, {"status": 403, "detail": HUMAN_REFUSAL})
        return
    proposal["status"] = "Succeeded"
    h.reply(200, {"proposalId": proposal_id, "status": "Succeeded", "approvalChannel": "console-interactive-password",
                  "requestedBy": "00000000-0000-0000-0000-000000000002",
                  "resolvedBy": "adminsession:subject:-:00000000-0000-0000-0000-000000000002"})


class FakeCandidate:
    """Context manager: ``http_base``, ``https_base``, ``tls_dir`` and the shared ``state``."""

    def __init__(self, key_approval: str = "human-only", session_works: bool = True):
        self.state = State(key_approval, session_works)
        self._dir = tempfile.TemporaryDirectory()
        self.tls_dir = self._dir.name
        self._servers: list[ThreadingHTTPServer] = []

    def _serve(self, tls: bool) -> str:
        handler = type("BoundHandler", (Handler,), {"state": self.state, "tls": tls})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(f"{self.tls_dir}/tls.crt", f"{self.tls_dir}/tls.key")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._servers.append(server)
        return f"{'https' if tls else 'http'}://{'localhost' if tls else '127.0.0.1'}:{server.server_address[1]}"

    def __enter__(self) -> "FakeCandidate":
        subprocess.run(["bash", str(HERE / "lib" / "tls-cert.sh"), self.tls_dir], check=True, capture_output=True)
        self.http_base, self.https_base = self._serve(tls=False), self._serve(tls=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._dir.cleanup()
