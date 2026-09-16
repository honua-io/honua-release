import importlib.util
import json
from pathlib import Path
import unittest

import yaml

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("console_read_approve", HERE / "run.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
MANIFEST = HERE.parents[1] / "platform-manifest.yaml"
RECEIPT = HERE / "receipt.nightly-2cc2213.console-dcb9eb2.json"


class ConsoleReadApproveReceiptTests(unittest.TestCase):
    def test_manifest_reader_matches_yaml_for_the_console_pin(self):
        expected = yaml.safe_load(MANIFEST.read_text())["components"]["honua-console"]
        actual = mod.manifest_component(MANIFEST, "honua-console")
        for field in ["sha", "image", "digest", "artifactSourceRevision"]:
            self.assertEqual(actual[field], expected[field], field)

    def test_committed_receipt_status_follows_its_checks(self):
        receipt = json.loads(RECEIPT.read_text())
        failed = sorted(name for name, check in receipt["checks"].items() if check["status"] != "passed")
        self.assertEqual(receipt["failedChecks"], failed)
        self.assertEqual(receipt["status"], "passed" if not failed else "failed")

    def test_committed_receipt_carries_no_credential_fields(self):
        text = RECEIPT.read_text().lower()
        for marker in ['"key":', '"apikey"', '"password"', '"secret"', '"token"', "bearer ey"]:
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
