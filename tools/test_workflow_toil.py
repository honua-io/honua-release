"""Small in-process tests of GA counter semantics and comparison refusal."""
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_toil import ToilRecorder, compare

BINDINGS = {key: "fixture-" + key for key in (
    "model", "modelSettingsDigest", "serverDigest", "clientArtifactsDigest", "fixtureRevision",
    "configRevision", "authPolicyRevision", "journeyDigest", "environment")}


def recorder(arm="imperative", trial=1):
    now = [0.0]
    value = ToilRecorder({"experimentId": "test", "arm": arm, "trial": trial},
                         ["one", "two"], clock=lambda: now[0])
    return value, now


def receipt(arm, trial, status="pass"):
    value, now = recorder(arm, trial)
    value.start("one")
    value.model_call("one", {"prompt_tokens": 10, "completion_tokens": 3}, 50)
    now[0] = 1
    value.action("one", "tool_call", "list", "pass", 30)
    value.finish("one", "pass")
    value.start("two")
    now[0] = 2
    value.finish("two", "pass" if status == "pass" else "fail")
    return value.snapshot(bindings=BINDINGS, status=status)


class ToilTests(unittest.TestCase):
    def test_unknown_usage_is_null_not_free_tokens(self):
        value, _ = recorder()
        value.start("one")
        value.model_call("one", {}, 12)
        value.model_call("one", {"prompt_tokens": 20, "completion_tokens": 4}, 20)
        metrics = value.snapshot(bindings=BINDINGS, status="fail")["stages"][0]["metrics"]
        self.assertIsNone(metrics["inputTokens"])
        self.assertIsNone(metrics["outputTokens"])
        self.assertEqual(metrics["modelCalls"], 2)

    def test_reissue_after_tool_error_counts_separately_from_format_repair(self):
        value, _ = recorder()
        value.start("one")
        value.action("one", "tool_call", "publish", "fail", 10)
        value.action("one", "tool_call", "inspect", "pass", 15)
        value.repair("one")
        value.action("one", "tool_call", "publish", "pass", 10)
        value.action("one", "tool_call", "publish", "pass", 10)
        metrics = value.snapshot(bindings=BINDINGS, status="fail")["stages"][0]["metrics"]
        self.assertEqual((metrics["toolCalls"], metrics["modelRetries"], metrics["formatRepairs"]), (4, 1, 1))

    def test_no_operation_names_or_inputs_are_persisted(self):
        value, _ = recorder()
        value.start("one")
        value.action("one", "tool_call", "canary-secret-value", "fail", 1)
        self.assertNotIn("canary-secret-value", json.dumps(value.snapshot(bindings=BINDINGS, status="fail")))

    def test_interrupted_stage_retains_duration_and_unstarted_stages(self):
        value, now = recorder()
        value.start("one")
        now[0] = 2.5
        result = value.snapshot(bindings=BINDINGS, status="fail")
        self.assertEqual(result["stages"][0]["status"], "fail")
        self.assertEqual(result["stages"][0]["metrics"]["wallMs"], 2500)
        self.assertEqual(result["stages"][1]["status"], "not-run")
        self.assertIn("humanReviewMs", result["unmeasured"])

    def test_snapshot_cannot_mutate_recorder_or_bindings(self):
        value, _ = recorder()
        result = value.snapshot(bindings=BINDINGS, status="blocked")
        result["stages"][0]["metrics"]["toolCalls"] = 100
        self.assertEqual(value.rows["one"]["metrics"]["toolCalls"], 0)

    def test_invalid_measurements_and_out_of_stage_writes_refused(self):
        value, _ = recorder()
        with self.assertRaises(ValueError):
            value.model_call("one", {}, 1)
        value.start("one")
        for bad in (-1, True, math.nan, math.inf):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                value.model_call("one", {}, bad)

    def test_complete_pair_keeps_failures_in_denominator_and_medians(self):
        records = [receipt(arm, n, "fail" if arm == "spec" and n == 5 else "pass")
                   for arm in ("imperative", "spec") for n in range(1, 6)]
        result = compare(records)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["arms"]["spec"]["completionRate"], 0.8)
        self.assertEqual(result["arms"]["spec"]["stages"][1]["attempted"], 5)

    def test_missing_attempt_is_not_a_complete_comparison(self):
        result = compare([receipt("imperative", 1)])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["missing"]), 9)

    def test_pin_drift_and_duplicate_trial_refused(self):
        first = receipt("imperative", 1)
        with self.assertRaises(ValueError):
            compare([first, first])
        for key in BINDINGS:
            second = receipt("spec", 1)
            second["bindings"][key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare([first, second])

    def test_false_success_nan_and_negative_counters_refused(self):
        first = receipt("imperative", 1)
        for patch in (lambda r: r["stages"][0].update(status="blocked"),
                      lambda r: r["stages"][0]["metrics"].update(toolCalls=-1),
                      lambda r: r["stages"][0]["metrics"].update(wallMs=math.nan)):
            invalid = copy.deepcopy(first)
            patch(invalid)
            with self.assertRaises(ValueError):
                compare([invalid])

    def test_unknown_tokens_report_observation_coverage(self):
        first, second = receipt("imperative", 1), receipt("spec", 1)
        second["stages"][0]["metrics"]["inputTokens"] = None
        result = compare([first, second], trials=1)
        metric = result["arms"]["spec"]["stages"][0]["metrics"]["inputTokens"]
        self.assertEqual(metric, {"median": None, "observed": 0, "missing": 1})


if __name__ == "__main__":
    unittest.main()
