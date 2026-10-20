"""Offline independent Redis projection controls, never candidate evidence."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import unittest

from mcp_direct_cancel_state import observe


class DirectCancelStateControls(unittest.TestCase):
    def setUp(self):
        self.plan = "mcp-direct-cancel-0123456789abcdef01234567"
        self.job = "gp-" + "1" * 24
        self.actor = "ApiKey:api-key:11111111-1111-4111-8111-111111111111"
        self.runtime = {"running": True, "candidateSourceSha": "1" * 40,
                        "imageReference": "ghcr.io/honua-io/honua-server@sha256:" + "2" * 64}
        self.record = {"operationId": self.job, "status": 2,
                       "createdAt": "2026-10-20T00:00:00Z", "updatedAt": "2026-10-20T00:00:01Z",
                       "completedAt": None, "artifactReferences": [], "committedEffectReferences": [],
                       "audit": {"requestedBy": self.actor, "tenantId": "public", "idempotencyKey": self.plan},
                       "spec": {"parameters": {"honua.geoprocessing.plan_id": self.plan,
                                               "honua.geoprocessing.process_definitions": "analytics.buffer-aggregate-managed"}}}

    def call(self, record=None, *, observed=None):
        record = copy.deepcopy(self.record if record is None else record)
        raw = json.dumps(record).encode()
        census = {"schema": "honua.mcp-execution-persisted/v1", "jobs": [
            {"jobId": self.job, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "binding": {}}],
            "results": []}
        read = lambda command: raw if command == ["GET", "controlplane:job:" + self.job] else None
        inspected = observed if observed is not None else self.runtime
        return observe(self.plan, self.job, "1" * 40, self.runtime["imageReference"],
                       read=read, census=lambda _: census, inspect=lambda *args: inspected,
                       clock=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))

    def test_original_job_hash_actor_plan_and_status_are_projected(self):
        value = self.call()
        self.assertEqual(value["job"]["requestedBy"], self.actor)
        self.assertEqual(value["job"]["planId"], self.plan)
        self.assertEqual(value["job"]["status"], 2)
        self.assertEqual(value["job"]["artifactReferences"], [])

    def test_mismatched_plan_actor_status_or_missing_record_refuse(self):
        for key, replacement in (("operationId", self.job + "x"), ("status", "Running")):
            record = copy.deepcopy(self.record); record[key] = replacement
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.call(record)
        for path, replacement in (("requestedBy", "other"), ("idempotencyKey", 4)):
            record = copy.deepcopy(self.record); record["audit"][path] = replacement
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call(record)
        record = copy.deepcopy(self.record); record["spec"]["parameters"]["honua.geoprocessing.plan_id"] = "other"
        with self.assertRaises(ValueError): self.call(record)

    def test_runtime_identity_must_be_stable(self):
        changed = dict(self.runtime, candidateSourceSha="f" * 40)
        with self.assertRaises(ValueError):
            self.call(observed=changed)


if __name__ == "__main__":
    unittest.main()
