import json
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((ROOT / "certification.schema.json").read_text())
RECEIPT = json.loads((ROOT / "certification.issue-122.json").read_text())


class CertificationTest(unittest.TestCase):
    def test_issue_122_receipt_is_valid_and_fail_closed(self):
        jsonschema.validate(RECEIPT, SCHEMA, format_checker=jsonschema.FormatChecker())
        self.assertEqual("blocked", RECEIPT["status"])
        self.assertFalse(RECEIPT["promotionEligible"])
        self.assertEqual(list(range(1, 9)), [stage["number"] for stage in RECEIPT["stages"]])
        self.assertTrue(all(stage["status"] == "blocked" and stage["blockers"] for stage in RECEIPT["stages"]))
        self.assertTrue(all(gate["status"] == "blocked" and gate["evidence"] for gate in RECEIPT["andGates"].values()))
        self.assertFalse(any(RECEIPT["requiredOutcomes"].values()))

    def test_red_certification_cannot_claim_promotion(self):
        changed = json.loads(json.dumps(RECEIPT))
        changed["promotionEligible"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(changed, SCHEMA)

    def test_green_certification_requires_every_and_gate(self):
        changed = json.loads(json.dumps(RECEIPT))
        changed["status"] = "pass"
        changed["promotionEligible"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(changed, SCHEMA)

    def test_certification_requires_the_exact_named_and_gates(self):
        for gate in tuple(RECEIPT["andGates"]):
            with self.subTest(gate=gate):
                changed = json.loads(json.dumps(RECEIPT))
                del changed["andGates"][gate]
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(changed, SCHEMA)

        changed = json.loads(json.dumps(RECEIPT))
        changed["andGates"]["substituteGate"] = changed["andGates"].pop("localCandidate")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(changed, SCHEMA)


if __name__ == "__main__":
    unittest.main()
