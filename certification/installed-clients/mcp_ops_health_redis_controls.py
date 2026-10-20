"""Actual isolated Redis health/queue controls, never client/candidate evidence."""
import json
import os
import re
import subprocess
import unittest
import mcp_ops_health_state as state


class LiveRedisHealthControls(unittest.TestCase):
    def setUp(self):
        self.container = os.environ.get("CONTROL_REDIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", self.container):
            raise ValueError("health Redis controls require workflow-owned isolated service")
        self.keys = []

    def command(self, command, key, *arguments):
        if key not in self.keys: self.keys.append(key)
        result = subprocess.run(["docker", "exec", self.container, "redis-cli", "--raw", command, key, *arguments],
                                check=True, capture_output=True, text=True, timeout=15)
        if result.stderr: raise ValueError("owned Redis provisioning refused")
        return result.stdout.strip()

    def tearDown(self):
        for key in self.keys: self.command("DEL", key)

    def provision(self):
        row = {"operationId": "health-controlled", "status": 1, "spec": {"backend": "local"}}
        self.command("SET", "controlplane:job:health-controlled", json.dumps(row))
        self.command("SADD", "controlplane:job:active", "health-controlled")

    def test_actual_ping_cache_roundtrip_cleanup_and_nonempty_queue(self):
        self.provision()
        observed = state.queue_probe("123456abcdef")
        self.assertTrue(observed["cacheRoundtrip"] and observed["probeRemoved"])
        self.assertEqual(observed["ping"], "PONG")
        self.assertEqual(observed["activeIds"], ["health-controlled"])
        self.assertEqual(observed["jobs"][0]["backend"], "local")
        self.assertEqual(self.command("EXISTS", "honua:certification:ops-health:123456abcdef"), "0")
        self.assertEqual(self.command("SCARD", "controlplane:job:active"), "1")

    def test_probe_collision_refuses_without_clobbering_existing_key(self):
        key = "honua:certification:ops-health:abcdef123456"
        self.command("SET", key, "existing-owned-marker")
        with self.assertRaises(ValueError): state.queue_probe("abcdef123456")
        self.assertEqual(self.command("GET", key), "existing-owned-marker")

    def test_malformed_durable_record_and_wrong_index_type_refuse(self):
        self.command("SET", "controlplane:job:health-controlled", "not-json")
        with self.assertRaises(ValueError): state.queue_probe("abcdef123456")
        self.command("DEL", "controlplane:job:health-controlled")
        self.command("SET", "controlplane:job:active", "wrong-type")
        with self.assertRaises(ValueError): state.queue_probe("abcdef123456")
        self.assertEqual(self.command("EXISTS", "honua:certification:ops-health:abcdef123456"), "0")


if __name__ == "__main__":
    unittest.main()
