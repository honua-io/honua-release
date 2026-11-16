"""Synthetic cohort replay, denominator conservation and paired comparison checks."""
import copy
from itertools import product
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import outcome_eval as evaluation
import outcome_report as reporting


class CohortChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "cohort.json"
        self.plan = evaluation.plan()
        self.candidate = {"serverSha": "a" * 40, "imageDigest": "sha256:" + "b" * 64,
                          "platformLockDigest": "sha256:" + "c" * 64}
        self.manifest = {"schema": reporting.SCHEMA, "plan": "plan.json", "files": {},
                         "candidate": copy.deepcopy(self.candidate),
                         "trials": [{"trialId": row["id"], "eligible": False,
                                     "reason": "Synthetic fixture not admitted"}
                                    for row in self.plan["trials"] + self.plan["baselines"]]}
        self.store("plan.json", self.plan)

    def store(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        self.manifest["files"][name] = evaluation.digest(path)

    def report(self):
        self.path.write_text(json.dumps(self.manifest))
        return reporting.report(self.path)

    def record(self, trial_id, status="success", sentences=2):
        entry = next(e for e in self.manifest["trials"] if e["trialId"] == trial_id)
        trial = next(r for r in self.plan["trials"] + self.plan["baselines"] if r["id"] == trial_id)
        events = [{"id": "start", "type": "dispatch", "seconds": 0}]
        if sentences:
            events.append({"id": "brief", "type": "person", "seconds": 0,
                           "text": "Go. " * sentences, "sentences": ["Go."] * sentences})
        events.append({"id": "click", "type": "click", "seconds": 1, "count": 2})
        events.append({"id": "end", "seconds": 60,
                       "type": "terminal-readback" if status == "success" else "stop"})
        if status == "not-run":
            events = []
        event_name, grade_name = f"{trial_id}.jsonl", f"{trial_id}.grade.json"
        self.store(f"{trial_id}.readback.json", {"synthetic": True})
        (self.root / event_name).write_text("\n".join(json.dumps(e) for e in events))
        self.manifest["files"][event_name] = evaluation.digest(self.root / event_name)
        grade = {"trialId": trial_id, "workflow": trial["workflow"],
                 "candidate": copy.deepcopy(self.candidate),
                 "modality": "agent" if "model" in trial else "browser",
                 "status": status, "grader": "synthetic-only", "reason": "Synthetic grade",
                 "postconditionsPassed": True, "controlsPassed": True,
                 "sentenceBoundariesReviewed": True,
                 "terminalReadback": f"{trial_id}.readback.json",
                 "evidence": {event_name: self.manifest["files"][event_name],
                              f"{trial_id}.readback.json": self.manifest["files"][
                                  f"{trial_id}.readback.json"]}}
        self.store(grade_name, grade)
        entry.update(eligible=status != "excluded", events=event_name, grade=grade_name)

    def test_complete_unadmitted_cohort_retained(self):
        report = self.report()
        self.assertEqual(len(report["trials"]), 216)
        self.assertEqual(len(report["groups"]), 72)
        self.assertEqual(len(report["browserPairs"]), 192)
        self.assertEqual(len(report["armPairs"]), 144)
        self.assertFalse(report["live"])
        self.assertEqual(report["status"], "OFFLINE REPORT")
        self.assertEqual(report["cohortSha256"], evaluation.digest(self.path))
        self.assertEqual(report["schema"], "honua.outcome-report/v2")
        self.assertEqual(report["candidate"], self.candidate)
        for group in report["groups"]:
            self.assertEqual(group["planned"], 3)
            self.assertEqual(group["not-run"], 3)
            self.assertEqual(group["eligible"], 0)
            self.assertEqual(group["attempted"], 0)
            self.assertEqual(group["successPerPlanned"], 0)
            self.assertIsNone(group["successPerAttempted"])
        self.assertTrue(all(p["clicksPerPersonSentence"] is None for p in report["browserPairs"]))

    def test_candidate_required_even_for_entirely_unrun_cohort(self):
        for candidate in (None, {}, [], "unresolved", dict(self.candidate, unexpected="x")):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.manifest["candidate"] = candidate
                self.report()

    def test_passes_retain_unattempted_models_and_browser_denominators(self):
        passes = self.report()["passes"]
        self.assertEqual(len(passes), 9)
        self.assertEqual(sum(row["planned"] for row in passes), 216)
        for row in passes:
            self.assertIn(row["passNumber"], (1, 2, 3))
            self.assertEqual(row["planned"], 8 if row["modality"] == "browser" else 32)
            self.assertEqual(row["not-run"], row["planned"])
            self.assertEqual((row["attempted"], row["success"], row["stopEvents"]), (0, 0, 0))
            for metric in ("personSentences", "clicks"):
                self.assertIsNone(row["attemptedMetrics"][metric]["median"])

    def test_pass_metrics_preserve_failure_costs_without_mixing_models_or_baselines(self):
        self.record("r1-gpt-6.1-sol-style-A", sentences=4)
        self.record("r1-gpt-6.1-sol-style-D", "failed", sentences=2)
        self.record("r1-gpt-6.1-sol-share-B", "censored", sentences=6)
        self.record("r1-gpt-6.1-sol-share-C", "excluded", sentences=8)
        self.record("r2-gpt-6.1-sol-style-A", sentences=1)
        self.record("r1-claude-opus-5-5-style-A", sentences=9)
        self.record("r1-browser-style", sentences=0)
        passes = self.report()["passes"]
        sol = next(r for r in passes if r["passNumber"] == 1 and r["model"] == "gpt-6.1-sol")
        self.assertEqual((sol["planned"], sol["attempted"], sol["success"],
                          sol["stopEvents"], sol["not-run"]), (32, 4, 1, 3, 28))
        self.assertEqual(sol["attemptedMetrics"]["personSentences"]["median"], 5)
        self.assertEqual(sol["attemptedMetrics"]["clicks"]["median"], 2)
        browser = next(r for r in passes if r["passNumber"] == 1 and r["model"] is None)
        self.assertEqual((browser["attempted"], browser["success"], browser["stopEvents"]), (1, 1, 0))
        self.assertEqual(browser["attemptedMetrics"]["personSentences"]["median"], 0)

    def test_empty_exclusion_is_not_a_recorded_stop(self):
        trial_id = "r1-gpt-6.1-sol-style-A"
        self.record(trial_id, "excluded")
        event_name, grade_name = f"{trial_id}.jsonl", f"{trial_id}.grade.json"
        (self.root / event_name).write_text("")
        self.manifest["files"][event_name] = evaluation.digest(self.root / event_name)
        grade = json.loads((self.root / grade_name).read_text())
        grade["evidence"][event_name] = self.manifest["files"][event_name]
        self.store(grade_name, grade)
        row = next(r for r in self.report()["passes"]
                   if r["passNumber"] == 1 and r["model"] == "gpt-6.1-sol")
        self.assertEqual((row["excluded"], row["attempted"], row["stopEvents"]), (1, 0, 0))

    def test_candidate_pins_must_be_canonical_full_identities(self):
        for key in self.candidate:
            for value in (None, True, "latest", "f" * 7, self.candidate[key].upper(),
                          self.candidate[key] + "\n"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.manifest["candidate"] = dict(self.candidate, **{key: value})
                    self.report()

    def test_mixed_candidate_grade_rejected_even_when_repinned(self):
        trial_ids = ("r1-gpt-6.1-sol-style-A", "r1-browser-style")
        statuses = ("success", "failed", "censored", "excluded", "not-run")
        for trial_id, status, key in product(trial_ids, statuses, self.candidate):
            self.record(trial_id, status)
            name = f"{trial_id}.grade.json"
            grade = json.loads((self.root / name).read_text())
            grade["candidate"][key] = ("d" * 40 if key == "serverSha"
                                       else "sha256:" + "d" * 64)
            self.store(name, grade)
            with (self.subTest(trial=trial_id, status=status, key=key),
                  self.assertRaisesRegex(ValueError, "candidates differ")):
                self.report()
            self.record(trial_id)

    def test_recorded_grade_requires_candidate(self):
        self.record("r1-browser-style")
        name = "r1-browser-style.grade.json"
        grade = json.loads((self.root / name).read_text())
        del grade["candidate"]
        self.store(name, grade)
        with self.assertRaisesRegex(ValueError, "candidate requires"):
            self.report()

    def test_matching_candidate_preserves_each_grade_status(self):
        for status in ("success", "failed", "censored", "excluded", "not-run"):
            with self.subTest(status=status):
                self.record("r1-browser-style", status)
                row = next(r for r in self.report()["trials"]
                           if r["trialId"] == "r1-browser-style")
                self.assertEqual(row["status"], status)

    def test_legacy_unbound_cohort_rejected(self):
        self.manifest["schema"] = "honua.outcome-cohort/v1"
        with self.assertRaises(ValueError):
            self.report()

    def test_no_dropped_duplicated_invented_or_reordered_trial(self):
        original = copy.deepcopy(self.manifest["trials"])
        variants = [original[:-1], original + [original[0]],
                    [dict(original[0], trialId="invented")] + original[1:],
                    [original[1], original[0]] + original[2:]]
        for trials in variants:
            with self.subTest(trials=len(trials)), self.assertRaises(ValueError):
                self.manifest["trials"] = trials
                self.report()

    def test_plan_change_cannot_be_hidden_by_repinning(self):
        for replacement in (True, 2):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                changed = copy.deepcopy(self.plan)
                changed["trials"][0]["pass"] = replacement
                self.store("plan.json", changed)
                self.report()

    def test_eligibility_and_not_run_reason_required(self):
        for entry in ({"eligible": None}, {"eligible": 0}, {"reason": ""}, {"reason": 4}):
            original = copy.deepcopy(self.manifest["trials"][0])
            self.manifest["trials"][0].update(entry)
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.report()
            self.manifest["trials"][0] = original

    def test_matching_success_ratio_and_primary_pair(self):
        self.record("r1-gpt-6.1-sol-style-A", sentences=4)
        self.record("r1-gpt-6.1-sol-style-D")
        self.record("r1-browser-style")
        report = self.report()
        pair = next(p for p in report["browserPairs"] if p["trialId"] == "r1-gpt-6.1-sol-style-D")
        self.assertEqual((pair["baselineClicks"], pair["agentSentences"]), (2, 2))
        self.assertEqual(pair["clicksPerPersonSentence"], 1)
        self.assertEqual(pair["agentClicks"], 2)
        arm = next(p for p in report["armPairs"] if p["treatmentId"] == pair["trialId"])
        self.assertTrue(arm["primary"])
        self.assertEqual(arm["treatmentMinusControl"]["personSentences"], -2)

    def test_failures_censoring_exclusions_and_unknown_tokens_survive(self):
        for repetition, status in enumerate(("success", "censored", "excluded"), 1):
            self.record(f"r{repetition}-gpt-6.1-sol-style-D", status)
        report = self.report()
        group = next(g for g in report["groups"] if
                     (g["workflow"], g["model"], g["arm"]) == ("style", "gpt-6.1-sol", "D"))
        self.assertEqual((group["planned"], group["eligible"], group["attempted"]), (3, 2, 3))
        self.assertEqual((group["success"], group["censored"], group["excluded"]), (1, 1, 1))
        self.assertEqual(group["successPerAttempted"], 1 / 3)
        self.assertEqual(group["attemptedMetrics"]["clicks"]["median"], 2)
        self.assertEqual(group["attemptedMetrics"]["tokens"]["input"]["missing"], 3)
        self.assertTrue(all(p["clicksPerPersonSentence"] is None for p in report["browserPairs"]))

    def test_failed_pair_and_zero_denominator_are_undefined(self):
        for status, sentences in (("failed", 2), ("success", 0)):
            with self.subTest(status=status):
                self.record("r1-gpt-6.1-sol-style-D", status, sentences)
                self.record("r1-browser-style")
                pair = next(p for p in self.report()["browserPairs"] if
                            p["trialId"] == "r1-gpt-6.1-sol-style-D")
                self.assertIsNone(pair["clicksPerPersonSentence"])

    def test_summary_fields_do_not_override_replayed_counts(self):
        self.record("r1-gpt-6.1-sol-style-A")
        self.manifest["trials"] = [dict(e, personSentences=0, status="success")
                                   for e in self.manifest["trials"]]
        report = self.report()
        row = next(r for r in report["trials"] if r["trialId"] == "r1-gpt-6.1-sol-style-A")
        self.assertEqual(row["personSentences"], 2)
        self.assertEqual(sum(r["status"] == "success" for r in report["trials"]), 1)

    def test_changed_evidence_or_unpinned_grade_rejected(self):
        self.record("r1-gpt-6.1-sol-style-A")
        entry = next(e for e in self.manifest["trials"] if "grade" in e)
        self.manifest["files"].pop(entry["grade"])
        with self.assertRaises(ValueError):
            self.report()
        self.manifest["files"][entry["grade"]] = evaluation.digest(self.root / entry["grade"])
        (self.root / entry["events"]).write_text("")
        with self.assertRaises(ValueError):
            self.report()

    def test_readback_must_also_be_pinned_by_cohort(self):
        self.record("r1-gpt-6.1-sol-style-A")
        self.manifest["files"].pop("r1-gpt-6.1-sol-style-A.readback.json")
        with self.assertRaises(ValueError):
            self.report()

    def test_grade_cannot_be_assigned_to_another_trial(self):
        self.record("r1-gpt-6.1-sol-style-A")
        source = next(e for e in self.manifest["trials"] if "grade" in e)
        destination = self.manifest["trials"][-1]
        destination.update(events=source["events"], grade=source["grade"])
        with self.assertRaises(ValueError):
            self.report()

    def test_eligibility_cannot_hide_an_attempt_or_include_an_exclusion(self):
        for status in ("success", "failed", "censored", "excluded"):
            with self.subTest(status=status):
                self.record("r1-gpt-6.1-sol-style-A", status)
                entry = next(e for e in self.manifest["trials"] if "grade" in e)
                entry["eligible"] = not entry["eligible"]
                with self.assertRaises(ValueError):
                    self.report()

    def test_failed_baseline_does_not_produce_a_ratio(self):
        self.record("r1-gpt-6.1-sol-style-A")
        self.record("r1-browser-style", "failed")
        report = self.report()
        pair = next(p for p in report["browserPairs"] if p["trialId"] == "r1-gpt-6.1-sol-style-A")
        self.assertEqual(pair["baselineClicks"], 2)
        self.assertIsNone(pair["clicksPerPersonSentence"])

    def test_missing_recording_half_is_an_error(self):
        self.record("r1-gpt-6.1-sol-style-A")
        entry = next(e for e in self.manifest["trials"] if "grade" in e)
        del entry["events"]
        with self.assertRaises(KeyError):
            self.report()

    def test_cli_keeps_existing_report(self):
        self.report()
        output = self.root / "report.json"
        command = [sys.executable, str(Path(reporting.__file__)), str(self.path), str(output)]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        retained = output.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(output.read_bytes(), retained)


if __name__ == "__main__":
    unittest.main()
