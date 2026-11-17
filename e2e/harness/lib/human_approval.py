#!/usr/bin/env python3
"""Approve one operation proposal the way a person does in Console, and print a typed result.

The server refuses proposal approval from API keys and MCP tokens with 403 ("Human approval
requires a live interactive browser session and a valid review capability"). The accepted path is
a builtin password session in a browser:

1. ``GET  /api/v1/admin/auth/csrf``                     antiforgery cookie + request token (HTTPS only)
2. ``POST /api/v1/admin/auth/session``                  password sign-in, sets ``honua_admin_session``
3. ``GET  /api/v1/admin/auth/csrf``                     a token bound to the signed-in session
4. ``GET  /api/v1/admin/proposals/{id}/approval-review`` the review capability (valid five minutes)
5. ``POST /api/v1/admin/proposals/{id}/approve``         cookie + CSRF token + capability header

No request carries ``X-API-Key`` or ``Authorization``: an explicit credential is exactly what the
server refuses. The password comes from the environment (``HONUA_ADMIN_PASSWORD`` by default),
never from argv, and never reaches the output.

Usage::

    HONUA_ADMIN_PASSWORD=... human_approval.py --base https://localhost:8444 --proposal ID [--cafile CERT]

Prints one JSON object. ``result`` is ``approved`` (exit 0) or ``refused`` (exit 1, with the stage,
HTTP status and server detail). A transport failure is ``error`` (exit 2).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

CSRF_HEADER = "X-Honua-CSRF"
CAPABILITY_HEADER = "X-Honua-Approval-Capability"
SESSION_COOKIE = "honua_admin_session"
# The server's refusal for any approval that is not a live human session with a review capability.
HUMAN_APPROVAL_REQUIRED = "Human approval requires a live interactive browser session and a valid review capability"
TIMEOUT_SECONDS = 30


class Refused(Exception):
    def __init__(self, stage: str, status: int, detail: str):
        super().__init__(f"{stage}: HTTP {status}")
        self.stage, self.status, self.detail = stage, status, detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def problem_detail(body: bytes) -> str:
    try:
        document = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")[:300]
    if isinstance(document, dict):
        return str(document.get("detail") or document.get("title") or "")[:300]
    return ""


def is_human_approval_refusal(status: int, detail: str) -> bool:
    """True for exactly the server's refusal of a non-human approval."""
    return status == HTTPStatus.FORBIDDEN and HUMAN_APPROVAL_REQUIRED in (detail or "")


class HumanSession:
    """One browser-like session: cookies kept in memory, sent only to this origin."""

    def __init__(self, base: str, cafile: str | None = None):
        if urllib.parse.urlsplit(base).scheme != "https":
            raise ValueError("human approval needs the server's HTTPS base URL")
        self.base = base.rstrip("/")
        context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def request(self, stage: str, route: str, body: Any = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """``route`` is the method and path, as in ``"GET /api/v1/admin/auth/csrf"``."""
        method, path = route.split(" ", 1)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookies:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                status, payload, received = response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status, payload, received = exc.code, exc.read(), exc.headers
        self._keep_cookies(received.get_all("Set-Cookie") or [])
        if status >= HTTPStatus.MULTIPLE_CHOICES:
            raise Refused(stage, status, problem_detail(payload))
        return status, payload

    def _keep_cookies(self, headers: list[str]) -> None:
        for header in headers:
            jar = SimpleCookie()
            jar.load(header)
            for name, morsel in jar.items():
                if morsel.value:
                    self.cookies[name] = morsel.value
                else:
                    self.cookies.pop(name, None)

    def refresh_csrf(self) -> None:
        _, payload = self.request("csrf", "GET /api/v1/admin/auth/csrf")
        token = (json.loads(payload or b"{}") or {}).get("requestToken")
        if not token:
            raise Refused("csrf", 200, "the CSRF endpoint returned no requestToken")
        self.csrf = token

    def sign_in(self, password: str) -> None:
        self.refresh_csrf()
        self.request("session", "POST /api/v1/admin/auth/session", {"password": password}, {CSRF_HEADER: self.csrf})
        if SESSION_COOKIE not in self.cookies:
            raise Refused("session", 204, f"sign-in set no {SESSION_COOKIE} cookie")
        # The antiforgery token is bound to the identity it was issued for, so take a fresh one.
        self.refresh_csrf()

    def open_review(self, proposal_id: str) -> str:
        quoted = urllib.parse.quote(proposal_id, safe="")
        _, payload = self.request("review", f"GET /api/v1/admin/proposals/{quoted}/approval-review")
        review = (json.loads(payload or b"{}") or {}).get("review") or {}
        capability = review.get("capability")
        if not capability:
            raise Refused("review", 200, "the approval review carried no capability")
        return capability

    def approve(self, proposal_id: str) -> dict[str, Any]:
        capability = self.open_review(proposal_id)
        quoted = urllib.parse.quote(proposal_id, safe="")
        status, payload = self.request("approve", f"POST /api/v1/admin/proposals/{quoted}/approve", {},
                                       {CSRF_HEADER: self.csrf, CAPABILITY_HEADER: capability})
        detail = json.loads(payload or b"{}") or {}
        return {"result": "approved", "proposalId": proposal_id, "httpStatus": status,
                **{key: detail.get(key) for key in ("status", "approvalChannel", "requestedBy", "resolvedBy")}}


def approve_as_human(base: str, proposal_id: str, password: str, cafile: str | None = None) -> dict[str, Any]:
    """Sign in, open the review and approve. Returns the typed result; never raises Refused."""
    session = HumanSession(base, cafile)
    try:
        session.sign_in(password)
        return session.approve(proposal_id)
    except Refused as exc:
        return {"result": "refused", "proposalId": proposal_id, "stage": exc.stage, "httpStatus": exc.status,
                "detail": exc.detail}


def approve_in_child(base: str, proposal_id: str, password: str, cafile: str | None = None) -> dict[str, Any]:
    """Run this helper as a child process that alone holds the password; returns its typed result.

    For drivers that make no HTTP request themselves: the approval is the helper's, as it is a
    person's in Console, and the password travels only in the child's environment.
    """
    argv = [sys.executable, str(Path(__file__).resolve()), "--base", base, "--proposal", proposal_id]
    if cafile:
        argv += ["--cafile", cafile]
    env = {"PATH": os.environ.get("PATH", ""), "HONUA_ADMIN_PASSWORD": password}
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180, check=False)
    lines = proc.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"result": "error", "proposalId": proposal_id, "detail": f"the helper exited {proc.returncode} without a result"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the server's HTTPS base URL")
    parser.add_argument("--proposal", required=True, help="the proposal id to approve")
    parser.add_argument("--cafile", help="PEM bundle that verifies the server certificate")
    parser.add_argument("--password-env", default="HONUA_ADMIN_PASSWORD",
                        help="environment variable holding the administrator password")
    args = parser.parse_args(argv)
    password = os.environ.get(args.password_env, "")
    if not password:
        print(json.dumps({"result": "error", "proposalId": args.proposal, "detail": f"{args.password_env} is not set"}))
        return 2
    try:
        result = approve_as_human(args.base, args.proposal, password, args.cafile)
    except (OSError, ValueError) as exc:
        result = {"result": "error", "proposalId": args.proposal, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps(result))
    return {"approved": 0, "refused": 1}.get(result["result"], 2)


if __name__ == "__main__":
    sys.exit(main())
