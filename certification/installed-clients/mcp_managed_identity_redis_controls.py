"""Actual workflow-owned Redis observer controls; no normal API/candidate credit."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
import mcp_managed_identity_fixture as fixture
import mcp_ops_health_redis_controls as health
from mcp_managed_identity_fixture_controls import PRIVATE_KEY, KEY_ID, SALT


class LiveManagedIdentityRedisControls(unittest.TestCase):
    setUp = health.LiveRedisHealthControls.setUp
    command = health.LiveRedisHealthControls.command
    tearDown = health.LiveRedisHealthControls.tearDown

    def provision(self):
        now = datetime.now(timezone.utc)
        value = {"Id": KEY_ID, "Name": "mcp-owned-read-" + "a" * 24,
                 "KeyHash": base64.b64encode(hashlib.sha256(PRIVATE_KEY.encode()).digest()).decode(),
                 "Permissions": ["read:*"], "CreatedAt": now.isoformat(), "UpdatedAt": now.isoformat(),
                 "ExpiresAt": (now + timedelta(hours=1)).isoformat(), "RevokedAt": None, "LastUsedAt": None}
        self.record_key = "honua:auth:admin-api-key:" + KEY_ID
        self.index_key = "honua:auth:admin-api-key:hash:" + hashlib.sha256(PRIVATE_KEY.encode()).hexdigest()
        self.active_key = "honua:auth:admin-api-key:active-ids"
        self.command("SET", self.record_key, json.dumps(value))
        self.command("SET", self.index_key, KEY_ID)
        self.command("SADD", self.active_key, KEY_ID)
        return value

    def observe(self, active=True):
        from mcp_execute_plan_state import redis_command
        return fixture.managed_record(KEY_ID, PRIVATE_KEY, ["read:*"], execute=redis_command, salt=SALT, expected_name="mcp-owned-read-" + "a" * 24, active=active)

    def test_actual_active_record_hash_index_and_canonical_identity_are_read_without_mutation(self):
        value = self.provision(); raw = self.command("GET", self.record_key)
        observed = self.observe()
        self.assertEqual(observed["keyId"], KEY_ID)
        self.assertEqual(observed["hashIndexValue"], KEY_ID)
        self.assertEqual(observed["activeIndexMembership"], 1)
        self.assertEqual(observed["canonicalActorId"], "apikey:api-key:" + KEY_ID)
        self.assertEqual(json.loads(base64.b64decode(observed["recordBytesBase64"])), value)
        self.assertEqual(self.command("GET", self.record_key), raw)

    def test_actual_hash_permission_index_and_membership_mismatches_refuse(self):
        baseline = self.provision()
        for field, changed in (("Id", "22222222-2222-4222-8222-222222222222"), ("Permissions", ["admin"]),
                               ("KeyHash", base64.b64encode(b"z" * 32).decode())):
            value = copy.deepcopy(baseline); value[field] = changed
            self.command("SET", self.record_key, json.dumps(value))
            with self.assertRaises(ValueError): self.observe()
        self.command("SET", self.record_key, json.dumps(baseline))
        self.command("SET", self.index_key, "22222222-2222-4222-8222-222222222222")
        with self.assertRaises(ValueError): self.observe()
        self.command("SET", self.index_key, KEY_ID)
        self.command("SREM", self.active_key, KEY_ID)
        with self.assertRaises(ValueError): self.observe()

    def test_actual_revocation_requires_removed_authentication_indexes_and_retains_original_bytes(self):
        value = self.provision(); value["RevokedAt"] = datetime.now(timezone.utc).isoformat()
        self.command("SET", self.record_key, json.dumps(value))
        with self.assertRaises(ValueError): self.observe()
        with self.assertRaises(ValueError): self.observe(False)
        self.command("DEL", self.index_key); self.command("SREM", self.active_key, KEY_ID)
        observed = self.observe(False)
        self.assertFalse(observed["active"])
        self.assertIsNone(observed["hashIndexValue"])
        self.assertEqual(observed["activeIndexMembership"], 0)
        self.assertEqual(json.loads(base64.b64decode(observed["recordBytesBase64"]))["RevokedAt"], value["RevokedAt"])


if __name__ == "__main__":
    unittest.main()
