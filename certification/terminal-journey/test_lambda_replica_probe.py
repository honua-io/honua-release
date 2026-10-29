"""Real bounded HTTP concurrency controls; these loopback responses are not cloud evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import executor
import stages
from transport import Transport

SAVED = {"itemId": "item", "versionId": "version", "contentHash": "hash",
         "envelope": {"family": "map", "body": {}}}


class LambdaReplicaProbeTests(unittest.TestCase):
    def run_probe(self, mode, kind="aws-serverless"):
        state = {"reads": 0, "active": 0, "maxActive": 0, "credentials": [], "barrierFailures": 0}
        lock = threading.Lock()
        overlap = threading.Barrier(executor.REPLICA_READS, timeout=5)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                with lock:
                    index = state["reads"]
                    state["reads"] += 1
                    state["credentials"].append(self.headers.get("Authorization"))
                    if index:
                        state["active"] += 1
                        state["maxActive"] = max(state["maxActive"], state["active"])
                if index:
                    try:
                        # A sequential implementation cannot satisfy this peer. Every read must
                        # actually overlap at the HTTP server, not merely create several clients.
                        overlap.wait()
                    except threading.BrokenBarrierError:
                        with lock:
                            state["barrierFailures"] += 1
                body = copy.deepcopy(SAVED)
                instance = "lambda-0" if mode == "one-instance" else f"lambda-{index % 2}"
                if index == executor.REPLICA_READS:
                    if mode == "stale-last":
                        body["contentHash"] = "stale"
                    elif mode == "missing-last":
                        instance = None
                    elif mode == "malformed-last":
                        instance = "invalid instance"
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if instance is not None:
                    self.send_header("Honua-Instance", instance)
                self.end_headers()
                self.wfile.write(raw)
                if index:
                    with lock:
                        state["active"] -= 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # The default listen backlog is smaller than this intentionally simultaneous batch.
        server.request_queue_size = executor.REPLICA_READS
        server.socket.listen(executor.REPLICA_READS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token_count = 0

        def credential():
            nonlocal token_count
            with lock:
                token_count += 1
                return f"Bearer loopback-probe-token-{token_count}"

        try:
            with TemporaryDirectory() as workdir:
                endpoint = f"http://127.0.0.1:{server.server_port}"
                transport = Transport(endpoint, None, None, workdir, {"proposer": credential})
                target = {"kind": kind, "replicaBaseUrl": endpoint,
                          "replicaTopology": {"id": "lambda-function", "description": "loopback concurrency control"}}
                observation = stages.Observation(setup_view_present=True,
                    setup_discovery={"tools": [], "metadata": {}})
                engine = executor.JourneyExecutor({"workspaceId": "workspace"}, target, observation, transport)
                engine.fixture = {"mapBody": {}}
                engine.resources.update(itemId="item", versionId="version", contentHash="hash")
                engine.check_map()
                row = engine.evidence["checks"]["6"]["replica-map"]
                proof = engine.evidence.get("proofs", {}).get("replica-map")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(state["barrierFailures"], 0)
        self.assertEqual(state["reads"], executor.REPLICA_READS + 1)
        self.assertEqual(state["maxActive"], executor.REPLICA_READS)
        self.assertEqual(len(set(state["credentials"])), executor.REPLICA_READS + 1)
        return row, proof

    def test_concurrent_reads_require_distinct_real_response_headers_and_matching_maps(self):
        row, proof = self.run_probe("two-instances")
        self.assertEqual(row["status"], "pass")
        self.assertEqual(proof["reads"], executor.REPLICA_READS)
        self.assertEqual({item["id"] for item in proof["instances"]}, {"lambda-0", "lambda-1"})

    def test_eks_reads_are_concurrent_so_a_pooled_lb_connection_cannot_pin_them(self):
        # The EKS Service's classic ELB pools backend connections; sequential reads all rode one
        # connection to one pod on e2e-cloud-aws run 40857079489. The peer barrier above fails
        # any read that does not overlap the others.
        row, proof = self.run_probe("two-instances", kind="aws-eks")
        self.assertEqual(row["status"], "pass")
        self.assertEqual({item["id"] for item in proof["instances"]}, {"lambda-0", "lambda-1"})
        row, proof = self.run_probe("one-instance", kind="aws-eks")
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(proof)

    def test_concurrency_does_not_turn_one_instance_into_a_pass(self):
        row, proof = self.run_probe("one-instance")
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(proof)
        self.assertIn("topology served one instance", row["detail"])

    def test_every_response_is_checked_after_two_instances_have_already_answered(self):
        for mode in ("stale-last", "missing-last", "malformed-last"):
            with self.subTest(mode=mode):
                row, proof = self.run_probe(mode)
                self.assertEqual(row["status"], "fail")
                self.assertIsNone(proof)


if __name__ == "__main__":
    unittest.main()
