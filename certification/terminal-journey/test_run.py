import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("terminal_gate", HERE / "run.py")
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


class TerminalGateTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((HERE / "control-plane-roster.v1.json").read_text())

    def test_policy_names_exactly_eleven_audited_exclusions(self):
        gate.validate_policy(self.policy)

    def test_missing_upstream_rosters_is_blocked_not_pass(self):
        self.assertEqual(gate.roster_verdict(self.policy, None, None)["status"], "blocked")

    def test_exact_partition_passes(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in self.policy["exclusions"]]
        rest = {"operationIds": projected + excluded}
        mcp = {"projectedOperationIds": projected, "exclusions": excluded}
        self.assertEqual(gate.roster_verdict(self.policy, rest, mcp)["status"], "pass")

    def test_duplicate_or_missing_operation_fails(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in self.policy["exclusions"]]
        rest = {"operationIds": projected + excluded}
        mcp = {"projectedOperationIds": projected[:-1] + [projected[0]], "exclusions": excluded}
        verdict = gate.roster_verdict(self.policy, rest, mcp)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(verdict["problems"])

    def test_same_size_partition_with_wrong_exclusion_fails_and_names_drift(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in self.policy["exclusions"]]
        swapped_secret = excluded[0]
        swapped_projection = projected[0]
        rest = {"operationIds": projected + excluded}
        mcp = {
            "projectedOperationIds": projected[1:] + [swapped_secret],
            "exclusions": excluded[1:] + [swapped_projection],
        }

        verdict = gate.roster_verdict(self.policy, rest, mcp)

        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(any(swapped_secret in problem for problem in verdict["problems"]))
        self.assertTrue(any(swapped_projection in problem for problem in verdict["problems"]))

    def test_every_stage_is_attributed_and_non_pass(self):
        journey = json.loads((HERE / "journey.v1.json").read_text())
        self.assertEqual([s["number"] for s in journey["stages"]], list(range(1, 9)))
        self.assertTrue(all(s["blockedBy"] for s in journey["stages"]))


if __name__ == "__main__": unittest.main()
