"""Synthetic offline checks; no provider, browser or product endpoint."""
import copy
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import outcome_eval as h


class OutcomeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "readback.json").write_text('{"independentSyntheticEvidence": true}\n')
        self.grade = {"status": "success", "grader": "synthetic-only",
                      "workflow": "style", "modality": "agent",
                      "trialId": "r1-gpt-6.1-sol-style-A",
                      "postconditionsPassed": True, "controlsPassed": True,
                      "sentenceBoundariesReviewed": True, "terminalReadback": "readback.json",
                      "evidence": {"readback.json": h.digest(self.root / "readback.json")}}
        self.events = [
            {"id": "start", "seconds": 0, "type": "dispatch"},
            {"id": "brief", "seconds": 0, "type": "person", "text": "Style it. Keep 1.5 unchanged.",
             "sentences": ["Style it.", "Keep 1.5 unchanged."]},
            {"id": "call", "seconds": 5, "type": "tool-attempt"},
            {"id": "read", "seconds": 60, "type": "terminal-readback"},
        ]

    def count(self, events=None, grade=None):
        recorded = self.events if events is None else events
        grading = copy.deepcopy(self.grade if grade is None else grade)
        events_path = self.root / "events.jsonl"
        events_path.write_text("\n".join(json.dumps(e) for e in recorded))
        grading["evidence"]["events.jsonl"] = h.digest(events_path)
        grade_path = self.root / "grade.json"
        grade_path.write_text(json.dumps(grading))
        return h.summarize(events_path, grade_path)

    def test_balanced_deterministic_plan(self):
        p = h.plan()
        self.assertEqual(p, h.plan())
        self.assertEqual(len(p["trials"]), 192)
        self.assertEqual(len(p["baselines"]), 24)
        self.assertEqual(len({r["id"] for r in p["trials"]}), 192)
        counts = Counter((r["pass"], r["model"], r["arm"]) for r in p["trials"])
        self.assertEqual(set(counts.values()), {8})
        pairs = Counter((r["model"], r["baselineId"]) for r in p["trials"])
        self.assertEqual(set(pairs.values()), {4})

    def test_arm_positions_are_balanced_per_model(self):
        rows = h.plan()["trials"]
        for model in h.MODELS:
            model_rows = [r for r in rows if r["model"] == model]
            counts = Counter((r["arm"], i % 4) for i, r in enumerate(model_rows))
            self.assertEqual(len(counts), 16)
            self.assertEqual(set(counts.values()), {6})

    def test_identity_and_pairing(self):
        result = self.count()
        self.assertEqual(result["trialId"], self.grade["trialId"])
        self.assertEqual(result["baselineId"], "r1-browser-style")
        for key, value in (("trialId", "unknown"), ("workflow", "share"), ("arm", "D")):
            grade = dict(self.grade, **{key: value})
            with self.assertRaises(ValueError): self.count(grade=grade)

    def test_excluded_before_dispatch_is_retained(self):
        self.grade.update(status="excluded", reason="corrupt before dispatch")
        self.assertIsNone(self.count(events=[])["elapsedMinutes"])

    def test_success_cannot_also_stop(self):
        self.events.insert(-1, {"id": "stop", "seconds": 59, "type": "stop"})
        with self.assertRaises(ValueError): self.count()

    def test_failed_beyond_limit_requires_censored_status(self):
        self.grade.update(status="failed", reason="timeout")
        self.events[-1].update(type="stop", seconds=901)
        with self.assertRaises(ValueError): self.count()
        self.grade["status"] = "censored"
        self.assertTrue(self.count()["limitExceeded"])

    def test_initial_and_followup_sentences(self):
        self.events.insert(2, {"id": "followup", "seconds": 2, "type": "person",
                               "text": "Yes, approve.", "sentences": ["Yes, approve."]})
        result = self.count()
        self.assertEqual(result["personSentences"], 3)
        self.assertEqual(result["personWords"], 8)
        self.assertEqual(result["elapsedMinutes"], 1)
        self.assertIsNone(result["tokens"]["input"])

    def test_invalid_segmentation_or_unreviewed_rejected(self):
        self.events[1]["sentences"] = ["Style it."]
        with self.assertRaises(ValueError): self.count()
        self.events[1]["sentences"] = [self.events[1]["text"]]
        self.grade["sentenceBoundariesReviewed"] = False
        with self.assertRaises(ValueError): self.count()

    def test_browser_retries_fields_and_double_click(self):
        self.grade.update(modality="browser", trialId="r1-browser-style")
        self.events[1:2] = [
            {"id": "click", "seconds": 1, "type": "click", "count": 2},
            {"id": "f1", "seconds": 2, "type": "field", "fieldId": "name", "characters": 3},
            {"id": "f2", "seconds": 3, "type": "field", "fieldId": "name", "characters": 4},
        ]
        r = self.count()
        self.assertEqual((r["clicks"], r["fieldActions"], r["distinctFields"], r["enteredCharacters"]),
                         (2, 2, 1, 7))

    def test_unknown_token_usage_stays_unknown(self):
        self.events[2:2] = [
            {"id": "t1", "seconds": 2, "type": "tokens", "responseId": "one", "input": 5, "output": 2, "cache": 1},
            {"id": "t2", "seconds": 3, "type": "tokens", "responseId": "two", "input": None, "output": 3},
        ]
        self.assertEqual(self.count()["tokens"], {"input": None, "output": 5, "cache": None})
        self.events[3]["responseId"] = "one"
        with self.assertRaises(ValueError): self.count()

    def test_no_proof_no_success(self):
        for key in ("controlsPassed", "postconditionsPassed", "terminalReadback"):
            grade = copy.deepcopy(self.grade)
            del grade[key]
            with self.assertRaises(ValueError): self.count(grade=grade)
        (self.root / "readback.json").write_text("changed")
        with self.assertRaises(ValueError): self.count()

    def test_nonfinite_negative_and_duplicate_events_rejected(self):
        for bad in (-1, float("nan"), float("inf"), True):
            events = copy.deepcopy(self.events)
            events[2]["seconds"] = bad
            with self.assertRaises(ValueError): self.count(events=events)
        self.events[2]["id"] = "brief"
        with self.assertRaises(ValueError): self.count()

    def test_failure_cost_retained(self):
        self.grade.update(status="censored", reason="time limit")
        self.events[-1]["type"] = "stop"
        result = self.count()
        self.assertEqual(result["personSentences"], 2)
        self.assertEqual(result["toolCalls"], 1)

    def test_not_run_requires_no_actions(self):
        self.grade.update(status="not-run", reason="fixture unavailable")
        self.assertIsNone(self.count(events=[])["elapsedMinutes"])
        with self.assertRaises(ValueError): self.count()

    def test_boundaries_and_time_limit(self):
        self.events[-1]["seconds"] = 901
        with self.assertRaises(ValueError): self.count()
        self.events[-1]["seconds"] = 4
        with self.assertRaises(ValueError): self.count()

    def test_pin_confinement_and_symlinks(self):
        for name in ("../readback.json", str(self.root / "readback.json")):
            with self.assertRaises(ValueError): h.confined(self.root, name)
        (self.root / "escape").symlink_to(Path(__file__).resolve())
        with self.assertRaises(ValueError): h.confined(self.root, "escape")

    def test_public_api_rejects_changed_transcript(self):
        self.count()
        events = self.root / "events.jsonl"
        events.write_text(events.read_text().replace("Style it.", "Skip it."))
        with self.assertRaisesRegex(ValueError, "pin the raw event transcript"):
            h.summarize(events, self.root / "grade.json")

    def test_malformed_events_and_large_integer_rejected(self):
        for bad in (None, [], "event", {"id": "bad", "seconds": 10 ** 400, "type": "observer"},
                    {"id": "bad", "seconds": 0, "type": []}):
            with self.subTest(event=bad), self.assertRaises(ValueError):
                self.count(events=[bad])

    def test_malformed_grade_and_evidence_rejected(self):
        self.count()
        for bad in (None, [], {"evidence": []}, {"evidence": None}):
            (self.root / "grade.json").write_text(json.dumps(bad))
            with self.subTest(grade=bad), self.assertRaises(ValueError):
                h.summarize(self.root / "events.jsonl", self.root / "grade.json")

    def test_cli_pins_transcript_and_no_overwrite(self):
        events = self.root / "events.jsonl"
        events.write_text("\n".join(json.dumps(e) for e in self.events))
        grade = self.root / "grade.json"
        grade.write_text(json.dumps(self.grade))
        output = self.root / "summary.json"
        command = [sys.executable, str(Path(h.__file__)), "summarize", str(events), str(grade), str(output)]
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.grade["evidence"]["events.jsonl"] = h.digest(events)
        grade.write_text(json.dumps(self.grade))
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        retained = output.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(output.read_bytes(), retained)


if __name__ == "__main__":
    unittest.main(verbosity=2)
