"""Real workflow-owned Redis controls only; no installed-candidate credit."""
import json
import unittest
import mcp_publish_result_state as state
import mcp_execute_plan_redis_controls as redis_controls


class WorkflowCensusControls(unittest.TestCase):
    setUp = redis_controls.RedisCensusControls.setUp
    tearDown = redis_controls.RedisCensusControls.tearDown
    command = redis_controls.RedisCensusControls.command

    def test_real_atomic_census_selects_records_and_excludes_typed_coordination(self):
        runid = "wf-"+"a"*32
        self.command("SET","orchestration:run:"+runid,json.dumps({"runId":runid,"workflowId":"definition-unicode-été","stepStates":[{"stepId":"sink"}]}))
        self.command("SET","orchestration:def:definition-unicode-été",json.dumps({"workflowId":"definition-unicode-été"}))
        self.command("SET","controlplane:proposal:owned-proposal",json.dumps({"proposalId":"owned-proposal"}))
        self.command("SET","controlplane:operation-instance:owned-operation",json.dumps({"operationInstanceId":"owned-operation"}))
        for key in ("orchestration:run:active","orchestration:run:wf:definition-unicode-été","orchestration:def:all","controlplane:proposal:active","controlplane:proposal:active:production","controlplane:operation-instance:active","controlplane:operation-instance:active:production"):
            self.command("SADD",key,"owned-record")
        for prefix in ("orchestration:run:","controlplane:proposal:","controlplane:operation-instance:"):
            self.command("SET",prefix+"lease:owned-record","owned-token")
        census,parsed = state.read_state()
        self.assertEqual([r["id"] for r in census["workflows"]],[runid])
        self.assertEqual([r["id"] for r in census["definitions"]],["definition-unicode-été"])
        self.assertEqual([r["id"] for r in census["proposals"]],["owned-proposal"])
        self.assertEqual([r["id"] for r in census["operations"]],["owned-operation"])
        self.assertEqual(len(parsed),4)

    def test_real_durable_wrong_type_or_malformed_payload_refused(self):
        for key in ("orchestration:run:wf-"+"a"*32,"orchestration:def:owned","controlplane:proposal:owned","controlplane:operation-instance:owned"):
            with self.subTest(key=key):
                self.command("SADD",key,"wrong-type")
                with self.assertRaises(ValueError): state.read_state()
                self.command("DEL",key); self.command("SET",key,"not-json")
                with self.assertRaises(ValueError): state.read_state()
                self.command("DEL",key)

    def test_real_wrong_coordination_type_and_key_payload_identity_refused(self):
        for key in ("orchestration:run:active","orchestration:run:wf:owned","orchestration:def:all","controlplane:proposal:active","controlplane:operation-instance:active"):
            self.command("SET",key,"wrong-type")
            with self.assertRaises(ValueError): state.read_state()
            self.command("DEL",key)
        self.command("SET","orchestration:run:wf-"+"a"*32,json.dumps({"runId":"wf-"+"b"*32}))
        with self.assertRaises(ValueError): state.read_state()

    def test_unprojected_durable_record_change_alters_census_hash(self):
        key = "orchestration:def:owned"
        self.command("SET",key,json.dumps({"workflowId":"owned","unprojected":"first"}))
        first,_ = state.read_state()
        self.command("SET",key,json.dumps({"workflowId":"owned","unprojected":"changed"}))
        second,_ = state.read_state()
        self.assertNotEqual(first["definitions"][0]["sha256"],second["definitions"][0]["sha256"])


if __name__ == "__main__":
    unittest.main()
