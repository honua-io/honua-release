"""Approve one proposal in a principal's own signed-in browser session, as a person does in Console.

The server refuses approval from bearers, API keys and MCP tokens ("Human approval requires a live
interactive browser session and a valid review capability"). authenticate.py keeps each principal's
real Keycloak sign-in session; this reuses it over the stack's HTTPS listener: CSRF token, approval
review capability, then approve (e2e/harness/lib/human_approval.py, staged by run.py).

Usage: approve-session.py <user> <proposal-id>. Prints one JSON result; exit 0 only when approved.
"""
import json
import sys
from pathlib import Path

from human_approval import HumanSession, Refused

HERE = Path(__file__).resolve().parent
SECURE_BASE = "https://127.0.0.1:19817"


def session_cookies(saved: dict) -> dict:
    """Only the server's own admin cookies travel; IdP cookies stay with the IdP."""
    return {name: value for name, value in saved.items() if name.startswith("honua_admin_")}


def approve(user: str, proposal_id: str) -> dict:
    session = HumanSession(SECURE_BASE, str(HERE / "certs/kc.crt"))
    session.cookies.update(session_cookies(json.loads((HERE / f"{user}-cookies.json").read_text())))
    try:
        session.refresh_csrf()
        return session.approve(proposal_id)
    except Refused as exc:
        return {"result": "refused", "proposalId": proposal_id, "stage": exc.stage, "httpStatus": exc.status,
                "detail": exc.detail}
    except (OSError, ValueError) as exc:  # transport or malformed reply: report it, never retry the mutation
        return {"result": "error", "proposalId": proposal_id, "detail": f"{type(exc).__name__}: {exc}"[:300]}


if __name__ == "__main__":
    result = approve(sys.argv[1], sys.argv[2])
    print(json.dumps(result))
    sys.exit(0 if result["result"] == "approved" else 1)
