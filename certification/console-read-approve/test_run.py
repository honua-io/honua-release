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
RECEIPTS = sorted(HERE.glob("receipt.*.json"))


class ConsoleReadApproveReceiptTests(unittest.TestCase):
    def test_manifest_reader_matches_yaml_for_the_console_pin(self):
        expected = yaml.safe_load(MANIFEST.read_text())["components"]["honua-console"]
        actual = mod.manifest_component(MANIFEST, "honua-console")
        for field in ["sha", "image", "digest", "artifactSourceRevision"]:
            self.assertEqual(actual[field], expected[field], field)

    def test_committed_receipt_status_follows_its_checks(self):
        self.assertTrue(RECEIPTS)
        for path in RECEIPTS:
            with self.subTest(receipt=path.name):
                receipt = json.loads(path.read_text())
                failed = sorted(name for name, check in receipt["checks"].items() if check["status"] != "passed")
                self.assertEqual(receipt["failedChecks"], failed)
                self.assertEqual(receipt["status"], "passed" if not failed else "failed")

    def test_committed_receipt_carries_no_credential_fields(self):
        for path in RECEIPTS:
            with self.subTest(receipt=path.name):
                text = path.read_text().lower()
                for marker in ['"key":', '"apikey"', '"password"', '"secret"', '"token"', "bearer ey"]:
                    self.assertNotIn(marker, text)

    def test_jobs_denial_fails_for_either_read_level_key(self):
        for key in ["readApprove", "readOnly"]:
            for denied in [401, 403, 404]:
                with self.subTest(key=key, status=denied):
                    row = {"path": "/api/v1/admin/jobs", "admin": 200, "readApprove": 200, "readOnly": 200}
                    row[key] = denied
                    result = mod.admin_get_results([row])
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["statusDiffersFromFullAdmin"], ["/api/v1/admin/jobs"])

    def test_jobs_success_keeps_explicit_response_evidence(self):
        row = {"path": "/api/v1/admin/jobs", "admin": 200, "readApprove": 200, "readOnly": 200}
        result = mod.admin_get_results([row])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["responses"], [row])
        self.assertEqual(result["authorizationDenials"], 0)
        self.assertEqual(result["readOnlyAuthorizationDenials"], 0)

    def test_empty_route_sweep_cannot_pass(self):
        self.assertEqual(mod.admin_get_results([])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
