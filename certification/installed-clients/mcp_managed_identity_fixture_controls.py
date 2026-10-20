"""Explicit offline normal-API/managed-store fixture controls, no candidate credit."""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import mcp_managed_identity_fixture as fixture
from mcp_render_validation_controls import runtime

SALT = "a" * 32
PRIVATE_ADMIN = "controlled-private-admin-credential"
PRIVATE_KEY = "hnua_" + base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
KEY_ID = "11111111-1111-4111-8111-111111111111"


class OfflineNormalApi:
    def __init__(self, fault=None):
        self.value = None
        self.index = None
        self.active = False
        self.fault = fault
        self.create_calls = 0
        self.revoke_calls = 0

    def redis(self, args):
        command, key, *rest = args
        if command == "GET" and key == "honua:auth:admin-api-key:" + KEY_ID:
            return json.dumps(self.value).encode() if self.value else None
        if command == "GET" and key.startswith("honua:auth:admin-api-key:hash:"):
            return self.index.encode() if self.index else None
        if command == "SISMEMBER": return 1 if self.active else 0
        raise AssertionError("offline observer attempted a mutation/unrelated key")

    def metadata(self):
        return {"id": KEY_ID, "name": self.value["Name"], "permissions": self.value["Permissions"],
                "status": "revoked" if self.value["RevokedAt"] else "active"}

    def open(self, request, timeout):
        assert request.full_url.startswith("http://127.0.0.1:8080/api/v1/admin/api-keys/")
        assert request.get_header("X-api-key") == PRIVATE_ADMIN
        path = urlsplit(request.full_url).path
        status = 200
        if request.method == "POST" and path == "/api/v1/admin/api-keys/":
            self.create_calls += 1
            if self.fault == "ambiguous-empty-create": raise TimeoutError("controlled timeout")
            value = json.loads(request.data)
            now = datetime.now(timezone.utc).isoformat()
            self.value = {"Id": KEY_ID, "Name": value["name"], "KeyPrefix": PRIVATE_KEY[:12],
                          "KeyHash": base64.b64encode(hashlib.sha256(PRIVATE_KEY.encode()).digest()).decode(),
                          "Permissions": value["permissions"], "CreatedAt": now, "UpdatedAt": now,
                          "ExpiresAt": value["expiresAt"], "LastUsedAt": None, "RevokedAt": None, "RotatedAt": None, "CreatedBy": "controlled-admin"}
            self.index, self.active = KEY_ID, True
            if self.fault == "timeout-after-create": raise TimeoutError("controlled created-but-unreceived")
            data = {"apiKey": self.metadata(), "key": PRIVATE_KEY}
            if self.fault == "foreign-response-id": data["apiKey"]["id"] = "22222222-2222-4222-8222-222222222222"
            elif self.fault == "foreign-response-name":
                self.value["Name"] = "unowned-existing-key"; data["apiKey"]["name"] = self.value["Name"]
            elif self.fault == "foreign-response-permission":
                self.value["Permissions"] = ["admin"]; data["apiKey"]["permissions"] = ["admin"]
            elif self.fault == "foreign-response-hash": self.value["KeyHash"] = base64.b64encode(b"z" * 32).decode()
            status = 201
        elif request.method == "GET" and path.endswith("/effective-permissions"):
            data = {**self.metadata(), "canAuthenticate": not bool(self.value["RevokedAt"])}
            if self.fault == "wrong-effective": data["permissions"] = ["admin"]
        elif request.method == "GET" and path == "/api/v1/admin/api-keys/":
            data = [self.metadata()] if self.value else []
        elif request.method == "POST" and path.endswith("/revoke"):
            self.revoke_calls += 1
            if self.fault == "revoke-failure": raise TimeoutError("controlled revoke failure")
            self.value["RevokedAt"] = datetime.now(timezone.utc).isoformat()
            self.value["UpdatedAt"] = self.value["RevokedAt"]
            self.index, self.active = None, False
            data = self.metadata()
        else: raise AssertionError("unowned normal API request")
        payload = json.dumps({"success": True, "data": data}).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return payload[:limit]
        response = Response(); response.status = status
        return response


class ManagedIdentityControls(unittest.TestCase):
    def setup_fixture(self, directory, api, profile="limited-read", inspect=None):
        return fixture.ManagedIdentityFixture(admin_credential=PRIVATE_ADMIN,
            private_path=Path(directory) / "private-key.json", receipt_path=Path(directory) / "public-receipt.json",
            inspect_owned=inspect or runtime, redis=api.redis, credential_salt=SALT, profile=profile)

    def complete(self, directory, profile="limited-read"):
        api = OfflineNormalApi(); managed = self.setup_fixture(directory, api, profile)
        with patch.object(fixture, "build_opener", return_value=api):
            managed.create(); managed.revoke()
        return managed, api

    def test_complete_normal_api_profiles_have_independently_rejudged_identity_and_cleanup(self):
        for profile in ("admin", "limited-read"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                managed, api = self.complete(directory, profile)
                value = fixture.verify_receipt(managed.receipt, runtime(), profile)
                self.assertEqual(value["keyId"], KEY_ID)
                self.assertEqual(value["canonicalActorId"], "ApiKey:api-key:" + KEY_ID)
                self.assertEqual(value["studioActorId"], KEY_ID)
                self.assertEqual(value["workspaceOwnerId"], KEY_ID)
                self.assertEqual(value["credentialFingerprint"].split(":")[1], SALT)
                self.assertFalse(managed.path.exists())
                self.assertFalse(api.active)
                public = managed.receipt_path.read_text()
                self.assertNotIn(PRIVATE_ADMIN, public); self.assertNotIn(PRIVATE_KEY, public)
                self.assertEqual(json.loads(public), managed.receipt)
                self.assertNotIn("responseBytesBase64", managed.receipt["events"][0])

    def test_live_private_file_is_exclusive600_and_never_overwrites_existing_material(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OfflineNormalApi(); managed = self.setup_fixture(directory, api)
            with patch.object(fixture, "build_opener", return_value=api):
                managed.create()
                self.assertEqual(managed.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(managed.path.read_text())["key"], PRIVATE_KEY)
                managed.revoke()
        with tempfile.TemporaryDirectory() as directory:
            api = OfflineNormalApi(); managed = self.setup_fixture(directory, api)
            managed.path.write_text("existing-unrelated-private-material")
            with patch.object(fixture, "build_opener", return_value=api), self.assertRaises(ValueError): managed.create()
            self.assertEqual(managed.path.read_text(), "existing-unrelated-private-material")
            self.assertFalse(api.active)

    def test_partial_create_is_revoked_but_unknown_inflight_create_stays_ambiguous(self):
        for fault in ("timeout-after-create", "ambiguous-empty-create", "wrong-effective"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                api = OfflineNormalApi(fault); managed = self.setup_fixture(directory, api)
                with patch.object(fixture, "build_opener", return_value=api), self.assertRaises(ValueError): managed.create()
                self.assertTrue(managed.receipt["failed"])
                self.assertTrue(managed.receipt_path.exists())
                self.assertFalse(managed.path.exists())
                if fault == "ambiguous-empty-create":
                    self.assertFalse(managed.receipt["cleanupComplete"])
                    self.assertTrue(managed.receipt["cleanupAmbiguous"])
                else: self.assertFalse(api.active)
                with self.assertRaises(ValueError): fixture.verify_receipt(managed.receipt, runtime(), "limited-read")

    def test_failed_revoke_retains_pending_public_receipt_and_live_private_material(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OfflineNormalApi(); managed = self.setup_fixture(directory, api)
            with patch.object(fixture, "build_opener", return_value=api):
                managed.create(); api.fault = "revoke-failure"
                with self.assertRaises(TimeoutError): managed.revoke()
                self.assertFalse(managed.receipt["cleanupComplete"])
                self.assertTrue(managed.path.exists())
                with self.assertRaises(ValueError): fixture.verify_receipt(managed.receipt, runtime(), "limited-read")
                api.fault = None; managed.revoke()
                with self.assertRaises(ValueError): fixture.verify_receipt(managed.receipt, runtime(), "limited-read")
                self.assertTrue(managed.receipt["cleanupComplete"])

    def test_malformed_foreign_create_response_cannot_revoke_unowned_key(self):
        for fault in ("foreign-response-id", "foreign-response-name", "foreign-response-permission", "foreign-response-hash"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                api = OfflineNormalApi(fault); managed = self.setup_fixture(directory, api)
                with patch.object(fixture, "build_opener", return_value=api), self.assertRaises(ValueError): managed.create()
                self.assertEqual(api.revoke_calls, 0)
                self.assertTrue(api.active)
                self.assertFalse(managed.receipt["cleanupComplete"])
                self.assertTrue(managed.receipt_path.exists())

    def test_foreign_or_unbound_cleanup_identity_never_reaches_revoke_post(self):
        for mutation in ("id", "name", "permission", "hash", "malformed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                api = OfflineNormalApi(); managed = self.setup_fixture(directory, api)
                with patch.object(fixture, "build_opener", return_value=api):
                    managed.create()
                    if mutation == "id": api.value["Id"] = "22222222-2222-4222-8222-222222222222"
                    elif mutation == "name": api.value["Name"] = "unowned-existing-key"
                    elif mutation == "permission": api.value["Permissions"] = ["admin"]
                    elif mutation == "hash": api.value["KeyHash"] = base64.b64encode(b"x" * 32).decode()
                    else: api.value["KeyHash"] = "not-base64"
                    with self.assertRaises(ValueError): managed.revoke()
                self.assertEqual(api.revoke_calls, 0)
                self.assertTrue(api.active)
                self.assertFalse(managed.receipt["cleanupComplete"])
                self.assertTrue(managed.path.exists())

    def test_original_receipt_mutations_cannot_grant_identity_or_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            managed, api = self.complete(directory)
            for mutation in ("projection", "raw-record", "record-hash", "fingerprint", "salt", "active-index", "effective-bytes", "revoke-bytes", "missing-event", "wrong-profile", "key-id", "runtime", "extra-claim", "cleanup"):
                with self.subTest(mutation=mutation):
                    value = copy.deepcopy(managed.receipt)
                    if mutation == "projection": value["identity"]["studioActorId"] = "wrong-owner"
                    elif mutation == "raw-record": value["identity"]["recordBytesBase64"] = base64.b64encode(b"{}").decode()
                    elif mutation == "record-hash": value["identity"]["recordSha256"] = "sha256:" + "0" * 64
                    elif mutation == "fingerprint": value["identity"]["credentialFingerprint"] = "scrypt:" + SALT + ":" + "0" * 64
                    elif mutation == "salt": value["credentialSalt"] = "b" * 32
                    elif mutation == "active-index": value["identity"]["hashIndexValue"] = "wrong-key"
                    elif mutation == "effective-bytes": value["events"][1]["responseBytesBase64"] = base64.b64encode(b"{}").decode()
                    elif mutation == "revoke-bytes": value["events"][2]["responseSha256"] = "sha256:" + "0" * 64
                    elif mutation == "missing-event": value["events"].pop()
                    elif mutation == "wrong-profile": value["profile"] = "admin"
                    elif mutation == "key-id": value["keyId"] = "22222222-2222-4222-8222-222222222222"
                    elif mutation == "runtime": value["candidate"]["containerId"] = "b" * 64
                    elif mutation == "extra-claim": value["authorityPassed"] = True
                    else: value["cleanupComplete"] = True; value.pop("revocation")
                    with self.assertRaises(ValueError): fixture.verify_receipt(value, runtime(), "limited-read")

    def test_changed_runtime_and_unknown_profiles_are_refused_before_provisioning(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OfflineNormalApi()
            with self.assertRaises(ValueError): self.setup_fixture(directory, api, "approved-operation")
            managed = self.setup_fixture(directory, api, inspect=lambda: {})
            with self.assertRaises(ValueError): managed.create()
            self.assertEqual(api.create_calls, 0)


if __name__ == "__main__":
    unittest.main()
