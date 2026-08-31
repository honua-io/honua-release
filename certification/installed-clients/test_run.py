import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("installed_cert", HERE / "run.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def inputs():
    return yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text()), json.loads((HERE / "matrix.json").read_text())


class InstalledCertificationTests(unittest.TestCase):
    def test_committed_release_inputs_are_exact(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mod.validate_release_inputs(*inputs())

    def test_release_mode_rejects_floating_versions(self):
        for version in ["latest", "1.*", "^1.2.3", "local"]:
            with self.subTest(version=version), mock.patch.dict(os.environ, {}, clear=True):
                manifest, matrix = inputs()
                manifest["clientArtifacts"]["honua-sdk-js"]["version"] = version
                with self.assertRaises(mod.CertificationError):
                    mod.validate_release_inputs(manifest, matrix)

    def test_release_mode_rejects_local_server_override(self):
        with mock.patch.dict(os.environ, {"HONUA_SERVER_IMAGE": "local:test"}, clear=True):
            with self.assertRaises(mod.CertificationError):
                mod.validate_release_inputs(*inputs())

    def test_receipt_materializes_every_non_pass(self):
        manifest, matrix = inputs()
        with mock.patch.object(mod, "install_npm", return_value=(True, "ok")), mock.patch.object(
            mod, "install_pypi", return_value=(False, "digest mismatch")
        ):
            receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(len(receipt["results"]), len(matrix["cells"]))
        self.assertEqual({r["status"] for r in receipt["results"]}, {"pass", "fail"})
        self.assertTrue(next(r for r in receipt["results"] if r["target"] == "nuget")["detail"].endswith("/57"))

    def test_matrix_includes_mcp_consumer(self):
        _, matrix = inputs()
        self.assertTrue(any(cell["artifact"] == "honua-mcp-server" for cell in matrix["cells"]))


if __name__ == "__main__":
    unittest.main()
