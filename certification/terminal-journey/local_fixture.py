"""Short-lived journey principals; private material never enters evidence.

Local Docker mints them against the isolated compose stack, which also injects an ephemeral
JWT signing key for the second-tenant bearer. A cloud cell (`aws-ecs`, `aws-serverless`) mints
the same four API keys through the cell's admin REST API with the cell's bootstrap credential and
revokes them when the run ends. A cell has no harness-held signing key, so no second-tenant
bearer is fabricated there; the target documents that principal as unavailable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import probes
from transport import ExecutionError, Transport

ISSUER = "https://terminal-journey.invalid"
AUDIENCE = "terminal-journey"
# The operator publishes, styles and runs GP as a full admin. The proposer is the
# Studio author and must NOT resolve to the `admin` role: an admin caller publishes
# immediately (StudioProposePublicationTool PublishImmediately) and stage 7 would never
# observe a durable AwaitingApproval proposal. `write:journey` is the narrowest key
# grant that authenticates as the non-admin `layer-write-key` role; Studio authoring
# for that role comes from the RBAC grants below. The approver is a separate
# principal holding only `admin:approve`.
GRANTS = {"operator": ["admin:write"], "proposer": ["write:journey"], "approver": ["admin:approve"],
          "viewer": ["read:journey"]}
# API keys carry only server-stamped roles (ApiKeyAuthenticationHandler); there is no
# key grant that confers Studio authoring to a non-admin key. The isolated fixture
# therefore binds StudioDraft operator grants to the proposer's stamped role.
AUTHOR_ROLE = "layer-write-key"
AUTHOR_GRANTS = [{"service": "StudioDraft", "layer": "*", "operation": operation}
                 for operation in ("Create", "Read", "Update", "Publish")]
# Targets whose principals are minted through the candidate's own admin API.
CLOUD_KINDS = ("aws-ecs", "aws-serverless")
MINTED_KINDS = ("local-docker", *CLOUD_KINDS)
KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")


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
    if target.get("kind") == "local-docker" and "replicaPort" in (target.get("compose") or {}):
        return f"http://127.0.0.1:{replica_port(target)}"
    return target.get("replicaBaseUrl")


def replica_topology(target):
    """The declared serving topology behind a cloud cell's single endpoint, or None.

    Only a cloud kind may re-read through its own endpoint, and only with a named topology.
    """
    topology = target.get("replicaTopology")
    if (target.get("kind") in CLOUD_KINDS and isinstance(topology, dict)
            and isinstance(topology.get("id"), str) and isinstance(topology.get("description"), str)):
        return {"id": topology["id"], "description": topology["description"]}
    return None


def _bootstrap(target):
    return probes.resolve_env_default(target["adminPassword"]["env"], target["adminPassword"].get("default", ""))


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
    kind = target.get("kind")
    path = _path(workdir)
    if mint and kind in CLOUD_KINDS and not path.exists():
        # No signing key: a cloud cell trusts no harness-held issuer.
        _save(path, {"keys": {}, "keyIds": {}})
    if kind not in MINTED_KINDS or not path.exists():
        return resolved
    private = json.loads(path.read_text())
    if mint:
        transport = Transport(base_url, None, None, workdir, {"bootstrap": _bootstrap(target)})
        _grant_author_role(transport)
        for name, grants in GRANTS.items():
            raw, _ = transport.http("POST", "/api/v1/admin/api-keys/", principal="bootstrap", expected=(201,), body={
                "name": f"journey-{Path(workdir).name}-{name}", "permissions": grants,
                "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            created = json.loads(raw)["data"]
            key_id, key = created["apiKey"]["id"], created["key"]
            if not isinstance(key_id, str) or not KEY_ID.match(key_id) or not isinstance(key, str) or not key:
                raise ExecutionError("mint journey principals", "candidate returned no bounded key identity")
            private["keys"][name] = key
            # Retained so the run can revoke what it minted; an id is not key material.
            private.setdefault("keyIds", {})[name] = key_id
            _save(path, private)
            effective = transport.get_json(f"/api/v1/admin/api-keys/{key_id}/effective-permissions", principal="bootstrap")["data"]
            if (sorted(effective["permissions"]) != sorted(grants) or effective["status"] != "active"
                    or effective["canAuthenticate"] is not True):
                raise ExecutionError("mint journey principals", "candidate effective permissions differ from fixture grants")
    resolved.update(private["keys"])
    if private.get("signingKey"):
        # A fresh single-use bearer avoids weakening the candidate's replay protection.
        resolved["other-tenant"] = lambda: _other_tenant_token(private["signingKey"])
    return resolved


def revoke(target, workdir, base_url):
    """Best effort: revoke every API key this run minted on a cloud cell.

    Returns (revoked, failed) principal-name lists. Key material is never returned or logged;
    a key that could not be revoked still expires an hour after minting and dies with the cell.
    """
    path = _path(workdir)
    if target.get("kind") not in CLOUD_KINDS or not path.exists():
        return [], []
    try:
        key_ids = dict(json.loads(path.read_text()).get("keyIds") or {})
    except (OSError, ValueError, TypeError):
        return [], ["unreadable private principal state"]
    try:
        transport = Transport(base_url, None, None, workdir, {"bootstrap": _bootstrap(target)})
    except Exception:
        return [], sorted(key_ids)
    revoked, failed = [], []
    for name, key_id in sorted(key_ids.items()):
        try:
            raw, _ = transport.http("POST", f"/api/v1/admin/api-keys/{key_id}/revoke",
                                    principal="bootstrap", expected=(200,))
            document = json.loads(raw)
            data = document.get("data", document) if isinstance(document, dict) else None
            if not isinstance(data, dict) or data.get("id") != key_id or data.get("status") != "revoked":
                raise ExecutionError("revoke journey principal", "candidate did not report the key revoked")
            revoked.append(name)
        except Exception:
            failed.append(name)
    return revoked, failed


def _grant_author_role(transport):
    roles = transport.get_json("/api/v1/admin/roles/", principal="bootstrap")["data"]
    existing = [role for role in roles if role.get("name") == AUTHOR_ROLE]
    if existing:
        role_id = existing[0]["roleId"]
    else:
        raw, _ = transport.http("POST", "/api/v1/admin/roles/", principal="bootstrap", expected=(201,),
                                body={"name": AUTHOR_ROLE, "description": "terminal journey Studio author"})
        role_id = json.loads(raw)["data"]["roleId"]
    raw, _ = transport.http("PUT", f"/api/v1/admin/roles/{role_id}/permissions", principal="bootstrap",
                            body={"permissions": AUTHOR_GRANTS})
    granted = {(g["service"], g["layer"], g["operation"]) for g in json.loads(raw)["data"]}
    if not {(g["service"], g["layer"], g["operation"]) for g in AUTHOR_GRANTS} <= granted:
        raise ExecutionError("grant Studio author role", "candidate did not persist the StudioDraft author grants")


def cleanup(workdir):
    _path(workdir).unlink(missing_ok=True)
