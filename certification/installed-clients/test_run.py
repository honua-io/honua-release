import copy
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
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
    def test_release_mode_rejects_each_omitted_artifact(self):
        manifest, matrix = inputs()
        for artifact in manifest["clientArtifacts"]:
            with self.subTest(artifact=artifact):
                reduced = copy.deepcopy(matrix)
                reduced["cells"] = [c for c in reduced["cells"] if c["artifact"] != artifact]
                with self.assertRaisesRegex(mod.CertificationError, "omits required"):
                    mod.validate_release_inputs(manifest, reduced)

    def test_admin_import_failure_fails_certification(self):
        pin = inputs()[0]["clientArtifacts"]["honua-admin-python-wheel"].copy()
        wheel = b"test archive bytes"
        pin["digest"] = "sha256:" + hashlib.sha256(wheel).hexdigest()
        metadata = {"urls": [{"filename": pin["filename"], "url": "https://example.invalid/wheel"}]}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "install"

            def run(cmd, **kwargs):
                if "install" in cmd:
                    package = work / "site-packages" / "honua_admin"
                    package.mkdir()
                    (package / "__init__.py").touch()
                failed = "-c" in cmd
                return subprocess.CompletedProcess(cmd, int(failed), "", "missing dependency" if failed else "")

            with mock.patch.object(mod.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(wheel)
            ]), mock.patch.object(mod, "_run", side_effect=run):
                ok, detail = mod.install_pypi(pin, work)
            self.assertFalse(ok)
            self.assertIn("installed admin import failed", detail)

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
