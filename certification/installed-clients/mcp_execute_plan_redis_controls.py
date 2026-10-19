"""Execute the real read-only Lua census against this workflow's Redis service.

Offline producer controls only: no server/client/candidate qualification.
"""
import json
import os
import re
import subprocess
import unittest

import mcp_execute_plan_state as state


class RedisCensusControls(unittest.TestCase):
    def setUp(self):
        self.container = os.environ.get("CONTROL_REDIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", self.container):
            raise ValueError("real Redis controls require the workflow-owned isolated service")
        self.keys = []

    def command(self, command, key, *arguments):
        if key not in self.keys:
            self.keys.append(key)
        result = subprocess.run(["docker", "exec", self.container, "redis-cli", "--raw", command, key, *arguments],
                                capture_output=True, text=True, timeout=15)
        if result.returncode or result.stderr:
            raise ValueError("owned Redis control provisioning refused")
        return result.stdout

    def tearDown(self):
        for key in self.keys:
            self.command("DEL", key)

    def job(self, identifier="controlled-job", **extra):
        return {"operationId": identifier, "status": 3, "createdAt": "2026-10-19T00:00:00Z", "audit": {"idempotencyKey": "controlled-key"},
                "spec": {"parameters": {"honua.geoprocessing.plan_id": "owned-plan"}}, **extra}

    def test_atomic_census_excludes_actual_coordination_types(self):
        self.command("SET", "controlplane:job:controlled-job", json.dumps(self.job()))
        self.command("SET", "controlplane:job:gp-result:controlled-job", json.dumps({"resultPackageId": "controlled-result", "status": 5, "artifacts": [], "errors": []}))
        self.command("SADD", "controlplane:job:active", "controlled-job")
        self.command("SADD", "controlplane:job:active:geoprocessing", "controlled-job")
        self.command("ZADD", "controlplane:job:index:created:all", "1", "controlled-job")
        self.command("SET", "controlplane:job:lease:controlled-job", "owned-coordination-token")
        self.command("RPUSH", "controlplane:job:log:controlled-job", "controlled-log")
        value = state.snapshot("owned-plan")
        self.assertEqual([row["jobId"] for row in value["jobs"]], ["controlled-job"])
        self.assertEqual([row["jobId"] for row in value["results"]], ["controlled-job"])
        self.assertEqual(value["jobs"][0]["binding"]["operationId"], "controlled-job")

    def test_unprojected_job_bytes_still_change_full_hash(self):
        key = "controlplane:job:controlled-job"
        self.command("SET", key, json.dumps(self.job(unprojected="first")))
        before = state.snapshot("owned-plan")
        self.command("SET", key, json.dumps(self.job(unprojected="changed")))
        after = state.snapshot("owned-plan")
        self.assertEqual(before["jobs"][0]["binding"], after["jobs"][0]["binding"])
        self.assertNotEqual(before["jobs"][0]["sha256"], after["jobs"][0]["sha256"])

    def test_malformed_durable_strings_are_not_filtered_away(self):
        for key, payload in (("controlplane:job:malformed", "not-json"),
                             ("controlplane:job:wrong-id", json.dumps(self.job())),
                             ("controlplane:job:unexpected:nested", json.dumps(self.job())),
                             ("controlplane:job:gp-result:unexpected:nested", "{}")):
            with self.subTest(key=key):
                self.command("SET", key, payload)
                with self.assertRaises(ValueError):
                    state.snapshot("owned-plan")
                self.command("DEL", key)

    def test_durable_wrong_type_and_malformed_coordination_refuse(self):
        for key in ("controlplane:job:controlled-job", "controlplane:job:gp-result:controlled-job", "controlplane:job:lease:controlled-job", "controlplane:job:index:created:all"):
            with self.subTest(key=key):
                self.command("SADD", key, "wrong-type")
                with self.assertRaises(ValueError):
                    state.snapshot("owned-plan")
                self.command("DEL", key)


if __name__ == "__main__":
    unittest.main()
