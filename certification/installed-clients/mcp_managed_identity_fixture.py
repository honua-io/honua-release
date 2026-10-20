"""Normal managed-key fixture; no primary journey or tenant qualification claim.

The caller owns an explicitly disposable candidate and supplies its private admin
credential. Key plaintext exists only in memory or an exclusive mode600 file.
"""
from __future__ import annotations
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("managed fixture redirect refused")


def guid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("managed key ID malformed")
    return value


def stamp():
    return datetime.now(timezone.utc).isoformat()


def managed_record(key_id, key, permissions, *, execute, salt, expected_name, active=True):
    """Read only actual Redis key bytes; never import a caller-claimed identity."""
    key_id = guid(key_id)
    if not isinstance(key, str) or not key or not re.fullmatch(r"[a-f0-9]{32}", salt):
        raise ValueError("managed private binding malformed")
    raw = execute(["GET", "honua:auth:admin-api-key:" + key_id])
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise ValueError("actual managed key record missing")
    # This record uses the production context's default PascalCase names.
    from mcp_map_package_validation import strict_json
    value = strict_json(raw)
    if (not isinstance(value, dict) or value.get("Id") != key_id
            or value.get("Permissions") != permissions or value.get("Name") != expected_name
            or not isinstance(value.get("KeyHash"), str)
            or not hmac.compare_digest(base64.b64decode(value["KeyHash"], validate=True), hashlib.sha256(key.encode()).digest())):
        raise ValueError("actual managed key binding differs")
    expires = value.get("ExpiresAt")
    expired = expires is not None and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    # The protected bounded RESP reader intentionally accepts bulk bytes and
    # arrays, not nil/integer responses. Observe both indexes atomically and
    # return explicit bulk representations without changing that decoder.
    indexes = execute(["EVAL_RO", "local i=redis.call('GET',KEYS[1]);local m=redis.call('SISMEMBER',KEYS[2],ARGV[1]);return {i or '',tostring(m)}", "2",
                       "honua:auth:admin-api-key:hash:" + hashlib.sha256(key.encode()).hexdigest(),
                       "honua:auth:admin-api-key:active-ids", key_id])
    if (not isinstance(indexes, list) or len(indexes) != 2 or not all(isinstance(value, bytes) for value in indexes)
            or indexes[1] not in (b"0", b"1")):
        raise ValueError("actual managed authentication index framing differs")
    index = indexes[0] or None
    membership = int(indexes[1])
    if active:
        if index != key_id.encode() or membership != 1 or value.get("RevokedAt") is not None or expired:
            raise ValueError("actual managed credential is not active")
    elif value.get("RevokedAt") is None or index is not None or membership != 0:
        raise ValueError("actual managed credential revocation missing")
    return {"keyId": key_id, "recordSha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "recordBytesBase64": base64.b64encode(raw).decode(),
            "permissions": permissions, "active": active, "hashIndexValue": index.decode() if isinstance(index, bytes) else None, "activeIndexMembership": membership,
            "canonicalActorId": "ApiKey:api-key:" + key_id,
            "studioActorId": key_id, "workspaceOwnerId": key_id,
            "credentialFingerprint": "scrypt:" + salt + ":" + hashlib.scrypt(key.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex(),
            "createdAt": value.get("CreatedAt"), "expiresAt": expires,
            "lastUsedAt": value.get("LastUsedAt"), "revokedAt": value.get("RevokedAt"), "observedAt": stamp()}


class ManagedIdentityFixture:
    """Restricted normal read:* fixture with owned cleanup and partial receipts.

    inspect_owned() must recheck the exact owned candidate tuple. The production
    adapter supplies its protected runtime inspector, not an arbitrary test hook.
    Injectable transports below exist for explicitly offline failure controls.
    """
    def __init__(self, *, admin_credential, private_path, inspect_owned, redis, credential_salt, http=None, profile="limited-read", receipt_path=None):
        if not isinstance(admin_credential, str) or not admin_credential or "\n" in admin_credential or "\r" in admin_credential:
            raise ValueError("private admin credential malformed")
        if profile not in ("limited-read", "admin"):
            raise ValueError("managed fixture profile refused")
        self.permissions = ["read:*"] if profile == "limited-read" else ["admin"]
        self.profile = profile
        self.runtime = None
        self.admin = admin_credential
        self.path = Path(private_path)
        self.receipt_path = Path(receipt_path) if receipt_path is not None else self.path.with_name(self.path.name + ".receipt.json")
        if self.path == self.receipt_path:
            raise ValueError("private material and public receipt paths must differ")
        self.inspect = inspect_owned
        self.redis = redis
        self.http = http or self.request
        self.name = "mcp-owned-read-" + secrets.token_hex(12)
        self.key = None
        self.key_id = None
        self.private_inode = None
        if not isinstance(credential_salt, str) or not re.fullmatch(r"[a-f0-9]{32}", credential_salt):
            raise ValueError("shared credential salt malformed")
        self.salt = credential_salt
        self.receipt = {"schema": "honua.mcp-managed-identity-fixture/v1", "name": self.name,
                        "profile": profile, "credentialSalt": self.salt, "permissions": self.permissions, "createdAt": stamp(), "events": [],
                        "limitedReadProven": False, "tenantIsolationProven": False, "cleanupComplete": False}

    def persist(self):
        # Only this nonsecret projection is durable; never serialize the instance,
        # create-response bytes, admin credential or one-time key material.
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt_path.with_name(self.receipt_path.name + ".writing-" + secrets.token_hex(8))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.receipt, stream, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(temporary, self.receipt_path)
            if self.private_inode is not None and self.key is not None and self.path.exists():
                private_descriptor = os.open(self.path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    observed = os.fstat(private_descriptor)
                    if (observed.st_ino != self.private_inode or observed.st_uid != os.getuid()
                            or observed.st_nlink != 1 or stat.S_IMODE(observed.st_mode) != 0o600):
                        raise ValueError("private fixture journal changed during receipt persistence")
                    journal = {"keyId": self.key_id, "key": self.key, "name": self.name, "profile": self.profile,
                               "credentialSalt": self.salt, "candidate": self.receipt["candidate"],
                               "receiptSha256": hashlib.sha256(json.dumps(self.receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
                    os.ftruncate(private_descriptor, 0)
                    with os.fdopen(private_descriptor, "w", closefd=False) as stream:
                        json.dump(journal, stream)
                finally:
                    os.close(private_descriptor)
        finally:
            if temporary.exists(): temporary.unlink()

    def ensure_owned(self):
        value = self.inspect()
        if not isinstance(value, dict) or value.get("running") is not True:
            raise ValueError("owned managed fixture runtime missing")
        selected = {key: value.get(key) for key in ("containerId", "imageId", "imageReference")}
        selected["sourceSha"] = value.get("candidateSourceSha")
        image = value.get("imageReadback")
        if (not isinstance(selected["sourceSha"], str) or not re.fullmatch(r"[a-f0-9]{40}", selected["sourceSha"])
                or value.get("candidateImage") != selected["imageReference"]
                or not isinstance(image, dict) or image.get("sourceSha") != selected["sourceSha"]
                or image.get("imageId") != selected["imageId"] or selected["imageReference"] not in image.get("repoDigests", [])):
            raise ValueError("owned managed fixture source/image binding differs")
        if not isinstance(selected["containerId"], str) or not re.fullmatch(r"[a-f0-9]{64}", selected["containerId"]):
            raise ValueError("owned managed fixture container missing")
        if self.runtime is not None and selected != self.runtime:
            raise ValueError("owned managed fixture runtime identity changed")
        self.runtime = selected
        self.receipt["candidate"] = selected

    def request(self, method, path, body=None):
        self.ensure_owned()
        if not re.fullmatch(r"/api/v1/admin/api-keys/(?:[a-f0-9-]{36}(?:/(?:effective-permissions|revoke))?)?", path):
            raise ValueError("managed fixture request outside fixed API")
        raw = b"" if body is None else json.dumps(body).encode()
        request = Request("http://127.0.0.1:8080" + path, data=raw if method == "POST" else None,
                          method=method, headers={"X-API-Key": self.admin, "Content-Type": "application/json"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or response.status not in (200, 201):
                raise ValueError("managed fixture response refused")
            from mcp_map_package_validation import strict_json
            value = strict_json(data)
            if not isinstance(value, dict) or value.get("success") is not True or "data" not in value:
                raise ValueError("managed fixture API envelope malformed")
            self.ensure_owned()
            # Raw create bytes contain a one-time secret: retain only their hash.
            self.receipt["events"].append({"method": method, "path": path, "status": response.status,
                "requestSha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "responseSha256": "sha256:" + hashlib.sha256(data).hexdigest(), "observedAt": stamp(),
                "requestBytesBase64": base64.b64encode(raw).decode(),
                **({} if method == "POST" and path == "/api/v1/admin/api-keys/" else {"responseBytesBase64": base64.b64encode(data).decode()})})
            return value["data"]

    def create(self):
        self.ensure_owned()
        try:
            result = self.http("POST", "/api/v1/admin/api-keys/", {"name": self.name, "permissions": self.permissions,
                               "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
            if isinstance(result, dict) and isinstance(result.get("apiKey"), dict):
                self.key_id = guid(result["apiKey"].get("id"))
            if (not isinstance(result, dict) or not isinstance(result.get("key"), str) or not re.fullmatch(r"hnua_[A-Za-z0-9_-]{43}", result["key"])
                    or self.key_id is None or result["apiKey"].get("name") != self.name
                    or result["apiKey"].get("permissions") != self.permissions):
                raise ValueError("created managed key differs from restricted request")
            self.key = result["key"]
            effective = self.http("GET", "/api/v1/admin/api-keys/" + self.key_id + "/effective-permissions")
            if (not isinstance(effective, dict) or set(effective) != {"id", "name", "status", "permissions", "canAuthenticate"} or effective.get("id") != self.key_id or effective.get("name") != self.name
                    or effective.get("permissions") != self.permissions or effective.get("status") != "active"
                    or effective.get("canAuthenticate") is not True):
                raise ValueError("normal effective-permissions readback differs")
            observed = managed_record(self.key_id, self.key, self.permissions, execute=self.redis, salt=self.salt, expected_name=self.name)
            self.ensure_owned()
            self.receipt.update({"keyId": self.key_id, "provisioned": {key: result["apiKey"].get(key) for key in ("id", "name", "permissions", "status")}, "effectivePermissions": effective, "identity": observed})
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            self.private_inode = os.fstat(descriptor).st_ino
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"keyId": self.key_id, "key": self.key, "name": self.name, "profile": self.profile,
                           "credentialSalt": self.salt, "candidate": self.receipt["candidate"],
                           "receiptSha256": hashlib.sha256(json.dumps(self.receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}, stream)
            if stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                raise ValueError("private managed fixture file mode differs")
            return self.receipt
        except Exception:
            self.receipt["failed"] = True
            try: self.revoke()
            except Exception: self.receipt["cleanupComplete"] = False
            raise ValueError("normal managed identity creation refused") from None
        finally:
            self.persist()

    def revoke(self):
        try:
            return self._revoke()
        except Exception as error:
            self.receipt["cleanupComplete"] = False
            self.receipt.setdefault("revocationErrors", []).append({"errorType": type(error).__name__, "observedAt": stamp()})
            raise
        finally:
            self.persist()

    def _revoke(self):
        self.ensure_owned()
        # Recover ambiguous/partial creation by the unique owned request name.
        if self.key_id is None:
            rows = self.http("GET", "/api/v1/admin/api-keys/")
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("owned partial-key cleanup census refused")
            matches = [guid(row.get("id")) for row in rows if isinstance(row, dict) and row.get("name") == self.name]
            if not matches and self.receipt.get("failed"):
                # A timed-out create can still finish after this read. Absence in
                # one list response is not terminal server-side cancellation.
                self.receipt["cleanupAmbiguous"] = True
                raise ValueError("ambiguous create has no terminal cleanup proof")
        else:
            matches = [self.key_id]
        if len(matches) > 1:
            raise ValueError("owned partial-key cleanup census is ambiguous")
        # Read and validate ALL ownership bindings before the first mutation.
        # A malformed create response is not authority to revoke its supplied ID.
        from mcp_map_package_validation import strict_json
        for identifier in matches:
            raw = self.redis(["GET", "honua:auth:admin-api-key:" + identifier])
            value = strict_json(raw) if isinstance(raw, bytes) and len(raw) <= 65536 else None
            if (not isinstance(value, dict) or value.get("Id") != identifier or value.get("Name") != self.name
                    or value.get("Permissions") != self.permissions or not isinstance(value.get("KeyHash"), str)):
                raise ValueError("managed revocation ownership is not independently bound")
            key_hash = base64.b64decode(value["KeyHash"], validate=True)
            if len(key_hash) != 32 or (self.key is not None and identifier == self.key_id
                    and not hmac.compare_digest(key_hash, hashlib.sha256(self.key.encode()).digest())):
                raise ValueError("managed revocation private key ownership differs")
        for identifier in matches:
            result = self.http("POST", "/api/v1/admin/api-keys/" + identifier + "/revoke")
            if not isinstance(result, dict) or result.get("id") != identifier or result.get("status") != "revoked":
                raise ValueError("normal managed revocation readback differs")
            raw = self.redis(["GET", "honua:auth:admin-api-key:" + identifier])
            from mcp_map_package_validation import strict_json
            value = strict_json(raw) if isinstance(raw, bytes) else None
            if (not isinstance(value, dict) or value.get("Id") != identifier or value.get("Name") != self.name
                    or value.get("Permissions") != self.permissions or value.get("RevokedAt") is None):
                raise ValueError("independent managed revocation missing")
            self.receipt.setdefault("revokedKeyIds", []).append(identifier)
            if identifier == self.key_id and self.key is not None:
                self.receipt["revocation"] = managed_record(identifier, self.key, self.permissions, execute=self.redis, salt=self.salt, expected_name=self.name, active=False)
        if self.key_id is not None:
            effective = self.http("GET", "/api/v1/admin/api-keys/" + self.key_id + "/effective-permissions")
            if (not isinstance(effective, dict) or set(effective) != {"id", "name", "status", "permissions", "canAuthenticate"}
                    or effective.get("id") != self.key_id or effective.get("name") != self.name
                    or effective.get("status") != "revoked" or effective.get("canAuthenticate") is not False
                    or effective.get("permissions") != self.permissions):
                raise ValueError("normal revoked effective-permissions readback differs")
            self.receipt["revokedEffectivePermissions"] = effective
        self.ensure_owned()
        if self.private_inode is not None and self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_ino != self.private_inode or stat.S_IMODE(self.path.stat().st_mode) != 0o600:
                raise ValueError("private fixture cleanup path changed")
            self.path.unlink()
        self.key = None
        self.receipt["cleanupComplete"] = True
        return self.receipt


def verify_receipt(receipt, candidate_runtime, profile):
    """Rejudge the original public helper receipt, never qualification booleans.

    Immutable producer provenance authenticates the observation of private key
    material. These public bytes rejudge normal API/store/lifecycle identities;
    they do not claim process attestation or infer an effective tenant.
    """
    from mcp_map_package_validation import strict_json, same_json
    permissions = ["read:*"] if profile == "limited-read" else ["admin"] if profile == "admin" else None
    if (permissions is None or not isinstance(receipt, dict)
            or receipt.get("schema") != "honua.mcp-managed-identity-fixture/v1"
            or receipt.get("profile") != profile or receipt.get("permissions") != permissions
            or receipt.get("cleanupComplete") is not True or receipt.get("failed") or receipt.get("cleanupAmbiguous") or receipt.get("revocationErrors")
            or not isinstance(receipt.get("credentialSalt"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", receipt["credentialSalt"])):
        raise ValueError("managed original receipt incomplete")
    expected_fields = {"schema", "name", "profile", "credentialSalt", "permissions", "createdAt", "events", "limitedReadProven", "tenantIsolationProven", "cleanupComplete", "candidate", "keyId", "provisioned", "effectivePermissions", "identity", "revocation", "revokedKeyIds", "revokedEffectivePermissions"}
    if (set(receipt) != expected_fields or receipt["limitedReadProven"] is not False or receipt["tenantIsolationProven"] is not False
            or receipt["revokedKeyIds"] != [receipt["keyId"]]):
        raise ValueError("managed fixture contains unknown claims or missing terminal observations")
    if (not isinstance(candidate_runtime, dict) or candidate_runtime.get("running") is not True
            or not re.fullmatch(r"[a-f0-9]{40}", candidate_runtime.get("candidateSourceSha", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", candidate_runtime.get("containerId", ""))
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", candidate_runtime.get("imageId", ""))
            or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", candidate_runtime.get("imageReference", ""))):
        raise ValueError("managed verifier requires actual runtime identity")
    expected_candidate = {key: candidate_runtime.get(key) for key in ("containerId", "imageId", "imageReference")}
    expected_candidate["sourceSha"] = candidate_runtime.get("candidateSourceSha")
    if not same_json(receipt.get("candidate"), expected_candidate):
        raise ValueError("managed fixture candidate binding differs")
    identifier = guid(receipt.get("keyId"))
    name = receipt.get("name")
    if (not isinstance(name, str) or not re.fullmatch(r"mcp-owned-read-[a-f0-9]{24}", name)
            or receipt.get("provisioned") != {"id": identifier, "name": name, "permissions": permissions, "status": "active"}):
        raise ValueError("managed normal provisioning differs")

    def record(observed, active):
        if not isinstance(observed, dict):
            raise ValueError("managed independent record missing")
        raw = base64.b64decode(observed.get("recordBytesBase64", ""), validate=True)
        if not 0 < len(raw) <= 65536 or observed.get("recordSha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("managed retained record bytes differ")
        value = strict_json(raw)
        if (not isinstance(value, dict) or value.get("Id") != identifier or value.get("Name") != name
                or value.get("Permissions") != permissions or not isinstance(value.get("KeyHash"), str)
                or len(base64.b64decode(value["KeyHash"], validate=True)) != 32):
            raise ValueError("managed original record identity differs")
        def instant(value):
            if not isinstance(value, str): raise ValueError("managed timestamp missing")
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.utcoffset() is None: raise ValueError("managed timestamp not absolute")
            return result
        at = instant(observed.get("observedAt"))
        if instant(value.get("CreatedAt")) > at or (active and (value.get("RevokedAt") is not None or value.get("ExpiresAt") is not None and instant(value["ExpiresAt"]) <= at)):
            raise ValueError("managed original active lifecycle differs")
        if not active and (value.get("RevokedAt") is None or instant(value["RevokedAt"]) > at):
            raise ValueError("managed original revocation lifecycle differs")
        fingerprint = observed.get("credentialFingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch("scrypt:" + receipt["credentialSalt"] + r":[a-f0-9]{64}", fingerprint):
            raise ValueError("managed shared-salt fingerprint malformed")
        derived = {"keyId": identifier, "recordSha256": observed["recordSha256"], "recordBytesBase64": observed["recordBytesBase64"],
                   "permissions": permissions, "active": active, "hashIndexValue": identifier if active else None, "activeIndexMembership": 1 if active else 0, "canonicalActorId": "ApiKey:api-key:" + identifier,
                   "studioActorId": identifier, "workspaceOwnerId": identifier, "credentialFingerprint": fingerprint,
                   "createdAt": value.get("CreatedAt"), "expiresAt": value.get("ExpiresAt"), "lastUsedAt": value.get("LastUsedAt"),
                   "revokedAt": value.get("RevokedAt"), "observedAt": observed["observedAt"]}
        if not same_json(derived, observed): raise ValueError("managed retained projection differs from original bytes")
        return derived, value

    initial, before = record(receipt.get("identity"), True)
    final, after = record(receipt.get("revocation"), False)
    if (before["KeyHash"] != after["KeyHash"] or before.get("CreatedAt") != after.get("CreatedAt")
            or initial["credentialFingerprint"] != final["credentialFingerprint"]
            or datetime.fromisoformat(final["revokedAt"].replace("Z", "+00:00")) < datetime.fromisoformat(initial["observedAt"].replace("Z", "+00:00"))):
        raise ValueError("managed initial/revoked identity changed")
    events = receipt.get("events")
    if not isinstance(events, list) or len(events) != 4:
        raise ValueError("normal managed API observations missing")
    effective = []
    expected_events = [("POST", "/api/v1/admin/api-keys/"), ("GET", "/api/v1/admin/api-keys/" + identifier + "/effective-permissions"),
                       ("POST", "/api/v1/admin/api-keys/" + identifier + "/revoke"), ("GET", "/api/v1/admin/api-keys/" + identifier + "/effective-permissions")]
    for event, expected_event in zip(events, expected_events):
        if not isinstance(event, dict): raise ValueError("managed API observation malformed")
        create = expected_event == expected_events[0]
        expected_fields = {"method", "path", "status", "requestSha256", "responseSha256", "observedAt", "requestBytesBase64"} | (set() if create else {"responseBytesBase64"})
        if (set(event) != expected_fields or (event.get("method"), event.get("path")) != expected_event
                or type(event.get("status")) is not int or event["status"] != (201 if create else 200)):
            raise ValueError("normal managed API sequence differs")
        raw_request = base64.b64decode(event.get("requestBytesBase64", ""), validate=True)
        if event.get("requestSha256") != "sha256:" + hashlib.sha256(raw_request).hexdigest():
            raise ValueError("managed original API request hash differs")
        if event.get("method") == "POST" and event.get("path") == "/api/v1/admin/api-keys/":
            if "responseBytesBase64" in event: raise ValueError("one-time creation secret must not be retained")
            request = strict_json(raw_request)
            if (not isinstance(request, dict) or set(request) != {"name", "permissions", "expiresAt"}
                    or request.get("name") != name or request.get("permissions") != permissions
                    or datetime.fromisoformat(request["expiresAt"].replace("Z", "+00:00")) != datetime.fromisoformat(before["ExpiresAt"].replace("Z", "+00:00"))
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", event.get("responseSha256", ""))):
                raise ValueError("managed original create request differs")
            continue
        raw = base64.b64decode(event.get("responseBytesBase64", ""), validate=True)
        if not 0 < len(raw) <= 1024 * 1024 or event.get("responseSha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("managed original API response hash differs")
        value = strict_json(raw)
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ValueError("managed normal API response refused")
        if event.get("method") == "POST":
            revoked = value.get("data")
            if (not isinstance(revoked, dict) or revoked.get("id") != identifier or revoked.get("name") != name
                    or revoked.get("permissions") != permissions or revoked.get("status") != "revoked"):
                raise ValueError("normal original revoke response differs")
        if event.get("method") == "GET" and event.get("path") == "/api/v1/admin/api-keys/" + identifier + "/effective-permissions":
            effective.append(value.get("data"))
    active_effective = {"id": identifier, "name": name, "status": "active", "permissions": permissions, "canAuthenticate": True}
    revoked_effective = {**active_effective, "status": "revoked", "canAuthenticate": False}
    if (not same_json(effective, [active_effective, revoked_effective]) or not same_json(receipt.get("effectivePermissions"), active_effective)
            or not same_json(receipt.get("revokedEffectivePermissions"), revoked_effective)):
        raise ValueError("managed original effective-permissions/lifecycle differs")
    return {**initial, "revokedAt": final["revokedAt"], "revokedRecordSha256": final["recordSha256"]}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "revoke"))
    parser.add_argument("--profile", choices=("admin", "limited-read"), required=True)
    parser.add_argument("--private-path", required=True)
    parser.add_argument("--receipt-path", required=True)
    parser.add_argument("--credential-salt", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args(argv)
    from mcp_execute_plan_state import redis_command
    from mcp_render_validation import inspect_candidate
    from mcp_map_package_validation import strict_json
    try:
        managed = ManagedIdentityFixture(admin_credential=os.environ.get("HONUA_MCP_ADMIN_API_KEY"),
            private_path=args.private_path, receipt_path=args.receipt_path, credential_salt=args.credential_salt,
            profile=args.profile, inspect_owned=lambda: inspect_candidate(args.candidate_source_sha, args.candidate_image), redis=redis_command)
        if args.action == "create":
            receipt = managed.create()
        else:
            private = managed.path.lstat()
            if (not stat.S_ISREG(private.st_mode) or stat.S_IMODE(private.st_mode) != 0o600
                    or private.st_uid != os.getuid() or private.st_nlink != 1 or private.st_size > 65536):
                raise ValueError("owned private journal boundary differs")
            journal = strict_json(managed.path.read_bytes())
            receipt = strict_json(managed.receipt_path.read_bytes())
            if (not isinstance(journal, dict) or set(journal) != {"keyId", "key", "name", "profile", "credentialSalt", "candidate", "receiptSha256"}
                    or journal.get("profile") != args.profile or journal.get("credentialSalt") != args.credential_salt
                    or journal.get("keyId") != receipt.get("keyId") or journal.get("name") != receipt.get("name")
                    or journal.get("candidate") != receipt.get("candidate")
                    or journal.get("receiptSha256") != hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()):
                raise ValueError("owned private/public managed journal differs")
            managed.key_id, managed.key = guid(journal["keyId"]), journal["key"]
            managed.name, managed.receipt = journal["name"], receipt
            managed.runtime, managed.private_inode = journal["candidate"], private.st_ino
            # Bind the private journal to the actual current managed record
            # before any revocation. An arbitrary key-ID JSON cannot act here.
            try:
                managed_record(managed.key_id, managed.key, managed.permissions, execute=redis_command, salt=managed.salt, expected_name=managed.name)
            except ValueError:
                # A previous normal revoke can have committed before a response
                # or later read failed. Permit cleanup of that exact revoked key;
                # retained error history still prevents a green final proof.
                managed_record(managed.key_id, managed.key, managed.permissions, execute=redis_command, salt=managed.salt, expected_name=managed.name, active=False)
            receipt = managed.revoke()
            verify_receipt(receipt, inspect_candidate(args.candidate_source_sha, args.candidate_image), args.profile)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception:
        print("normal owned managed identity fixture refused; retain its nonsecret receipt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
