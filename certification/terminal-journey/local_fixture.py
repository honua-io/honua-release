"""Short-lived local Docker principals; private material never enters evidence."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import probes
from transport import ExecutionError, Transport

ISSUER = "https://terminal-journey.invalid"
AUDIENCE = "terminal-journey"
GRANTS = {"proposer": ["admin:write"], "approver": ["admin:approve"], "viewer": ["read:journey"]}


def _path(workdir):
    return Path(workdir) / "private-principals.json"


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Set permissions before writing any credential bytes.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)


def compose_env(target, workdir):
    if target.get("kind") != "local-docker":
        return {}
    path = _path(workdir)
    if not path.exists():
        _save(path, {"signingKey": secrets.token_urlsafe(48), "keys": {}})
    secret = json.loads(path.read_text())["signingKey"]
    # The authored datasource uses the isolated compose database only.
    os.environ.setdefault("HONUA_JOURNEY_DATASOURCE_PASSWORD", "honua")
    return {"HONUA_JOURNEY_SIGNING_KEY": secret,
            "HONUA_JOURNEY_REPLICA_PORT": str(replica_port(target))}


def replica_port(target):
    cfg = target["compose"]
    port = int(os.environ.get(cfg["replicaPortEnv"], cfg["replicaPort"]))
    if not 1 <= port <= 65535:
        raise ExecutionError("replica port", "configured port is outside the TCP range")
    return port


def replica_url(target):
    if target.get("kind") == "local-docker" and "replicaPort" in target.get("compose", {}):
        return f"http://127.0.0.1:{replica_port(target)}"
    return target.get("replicaBaseUrl")


def _other_tenant_token(signing_key):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    body = encode({"alg": "HS256", "typ": "JWT"}) + b"." + encode({
        "iss": ISSUER, "aud": AUDIENCE, "sub": "journey-other-tenant", "tenant_id": "journey-other",
        "role": "user", "iat": now, "nbf": now, "exp": now + 300, "jti": secrets.token_hex(16)})
    signature = base64.urlsafe_b64encode(hmac.new(signing_key.encode(), body, hashlib.sha256).digest()).rstrip(b"=")
    return "Bearer " + (body + b"." + signature).decode()


def credentials(target, workdir, base_url, *, mint=False):
    refs = target.get("principals") or {}
    resolved = {name: probes.resolve_env_default(ref, "") for name, ref in refs.items()}
    if target.get("kind") != "local-docker" or not _path(workdir).exists():
        return resolved
    path = _path(workdir)
    private = json.loads(path.read_text())
    if mint:
        bootstrap = probes.resolve_env_default(target["adminPassword"]["env"], target["adminPassword"]["default"])
        transport = Transport(base_url, None, None, workdir, {"bootstrap": bootstrap})
        for name, grants in GRANTS.items():
            raw, _ = transport.http("POST", "/api/v1/admin/api-keys/", principal="bootstrap", expected=(201,), body={
                "name": f"journey-{Path(workdir).name}-{name}", "permissions": grants,
                "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            created = json.loads(raw)["data"]
            key_id, key = created["apiKey"]["id"], created["key"]
            private["keys"][name] = key
            _save(path, private)
            effective = transport.get_json(f"/api/v1/admin/api-keys/{key_id}/effective-permissions", principal="bootstrap")["data"]
            if (sorted(effective["permissions"]) != sorted(grants) or effective["status"] != "active"
                    or effective["canAuthenticate"] is not True):
                raise ExecutionError("mint journey principals", "candidate effective permissions differ from fixture grants")
    resolved.update(private["keys"])
    # A fresh single-use bearer avoids weakening the candidate's replay protection.
    resolved["other-tenant"] = lambda: _other_tenant_token(private["signingKey"])
    return resolved


def cleanup(workdir):
    _path(workdir).unlink(missing_ok=True)
