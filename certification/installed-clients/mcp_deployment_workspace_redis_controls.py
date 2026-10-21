"""Workflow-owned isolated Redis controls; synthetic records earn no candidate credit."""
import hashlib
import json
import unittest
import mcp_ops_health_redis_controls as health
import mcp_deployment_workspace_state as state


class LiveDeploymentWorkspaceRedisControls(unittest.TestCase):
    setUp = health.LiveRedisHealthControls.setUp
    command = health.LiveRedisHealthControls.command
    tearDown = health.LiveRedisHealthControls.tearDown

    def provision_history(self, identifier="lifecycle-control", target="owned-target"):
        value = {"operationId": identifier, "kind": 0, "status": 4, "version": 3,
                 "deploy": {"targetId": target, "desiredRevision": "controlled-revision"},
                 "providerOperationId": "controlled-provider", "privateBackendParameters": "must-not-retain"}
        raw = json.dumps(value)
        self.command("SET", "controlplane:workflow:" + identifier, raw)
        self.command("ZADD", "controlplane:workflow:terminal", "1", identifier)
        return raw

    def test_actual_global_window_is_read_only_and_filters_unrelated_targets(self):
        raw = self.provision_history()
        self.provision_history("other-lifecycle-control", "unrelated-target")
        observed = state.workflow_history("owned-target")
        self.assertEqual(observed["terminalCount"], 2)
        self.assertEqual(observed["visibleCount"], 2)
        self.assertFalse(observed["isTruncated"])
        self.assertEqual(len(observed["operations"]), 1)
        self.assertEqual(observed["operations"][0]["recordSha256"], "sha256:" + hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn("privateBackendParameters", json.dumps(observed))
        self.assertEqual(self.command("GET", "controlplane:workflow:lifecycle-control"), raw)
        self.assertEqual(self.command("ZCARD", "controlplane:workflow:terminal"), "2")

    def test_absent_record_and_wrong_index_type_refuse_without_repair(self):
        self.command("ZADD", "controlplane:workflow:terminal", "1", "absent-owned-record")
        with self.assertRaises(ValueError): state.workflow_history("owned-target")
        self.assertEqual(self.command("EXISTS", "controlplane:workflow:absent-owned-record"), "0")
        self.command("DEL", "controlplane:workflow:terminal")
        self.command("SET", "controlplane:workflow:terminal", "wrong-type")
        with self.assertRaises(ValueError): state.workflow_history("owned-target")
        self.assertEqual(self.command("GET", "controlplane:workflow:terminal"), "wrong-type")

    def test_durable_identity_mismatch_and_malformed_json_refuse(self):
        self.provision_history()
        for raw in ("not-json", json.dumps({"operationId": "different-id"})):
            self.command("SET", "controlplane:workflow:lifecycle-control", raw)
            with self.assertRaises(ValueError): state.workflow_history("owned-target")
            self.assertEqual(self.command("GET", "controlplane:workflow:lifecycle-control"), raw)

    def test_actual_nested_proposal_bytes_are_sealed_without_rewriting(self):
        identifier = "proposal-lifecycle-control"
        plan = json.dumps({"executionPayload": json.dumps({"targetId": "owned-target", "desiredRevision": "previous", "currentRevision": "current", "priority": 1})}, separators=(",", ":"))
        digest = hashlib.sha256(plan.encode()).hexdigest()
        raw = '{"proposalId":' + json.dumps(identifier) + ',"sealedPlanHash":' + json.dumps(digest) + ',"plan":' + plan + '}'
        key = "controlplane:proposal:" + identifier
        self.command("SET", key, raw)
        observed = state.observe_proposal(identifier)
        self.assertEqual(observed["planBytesSha256"], digest)
        self.assertEqual(observed["payload"]["desiredRevision"], "previous")
        self.assertEqual(self.command("GET", key), raw)
        # Mutate the nested serialized string, preserving valid JSON.
        changed = json.loads(raw); changed["plan"]["executionPayload"] = changed["plan"]["executionPayload"].replace("previous", "tampered")
        self.command("SET", key, json.dumps(changed))
        with self.assertRaises(ValueError): state.observe_proposal(identifier)


if __name__ == "__main__":
    unittest.main()
