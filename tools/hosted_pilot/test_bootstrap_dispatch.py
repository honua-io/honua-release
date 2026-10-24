"""The credential-free release wrapper forwards one exact IaC phase."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(path):
    return yaml.safe_load((ROOT / path).read_text())


def inputs(value):
    return value.get("on", value.get(True))["workflow_dispatch"]["inputs"]


class FoundationDispatch(unittest.TestCase):
    def test_caller_requires_same_explicit_phase_and_mode_as_installed_template(self):
        caller = workflow(".github/workflows/hosted-pilot-bootstrap.yml")
        target = workflow("certification/hosted-pilot/bootstrap/foundation-bootstrap-workflow.yml")
        for name, choices in [("mode", ["plan", "apply"]), ("phase", ["custody-only", "remote-backed-foundation"])]:
            self.assertTrue(inputs(caller)[name]["required"])
            self.assertEqual(inputs(caller)[name]["options"], choices)
            self.assertEqual(inputs(caller)[name]["options"], inputs(target)[name]["options"])
        self.assertEqual(caller["permissions"], {"contents": "read"})
        job = caller["jobs"]["dispatch-foundation-bootstrap"]
        self.assertEqual(job["if"], "github.ref == 'refs/heads/trunk'")
        self.assertEqual(len(job["steps"]), 1)
        self.assertEqual(job["steps"][0]["env"]["PHASE"], "${{ inputs.phase }}")
        self.assertNotIn("environment", job)
        self.assertNotIn("permissions", job)
        self.assertFalse(any("AWS" in name for name in job["steps"][0]["env"]))

    def test_real_dispatch_shell_preserves_every_selected_mode_phase_and_binding(self):
        step = workflow(".github/workflows/hosted-pilot-bootstrap.yml")["jobs"]["dispatch-foundation-bootstrap"]["steps"][0]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture = directory / "dispatch.json"
            fake = directory / "gh"
            fake.write_text("#!" + sys.executable + "\nimport json,os,sys\nwith open(os.environ['DISPATCH_CAPTURE'],'w') as target: json.dump(sys.argv[1:],target)\n")
            fake.chmod(0o700)
            expected = {
                "release_root_sha": "a" * 40,
                "config_path": "certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json",
                "region": "us-east-1", "recovery_run_id": "", "recovery_artifact_id": "", "recovery_sha256": ""
            }
            for mode in ["plan", "apply"]:
                for phase in ["custody-only", "remote-backed-foundation"]:
                    with self.subTest(mode=mode, phase=phase):
                        env = {"PATH": str(directory) + os.pathsep + os.defpath, "DISPATCH_CAPTURE": str(capture),
                               "GH_TOKEN": "offline-wrapper-fixture", "ROOT_SHA": expected["release_root_sha"],
                               "CONFIG": expected["config_path"], "REGION": expected["region"], "MODE": mode, "PHASE": phase,
                               "RECOVERY_RUN": "", "RECOVERY_ARTIFACT": "", "RECOVERY_SHA": ""}
                        result = subprocess.run(["/bin/bash", "-c", step["run"]], env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        args = json.loads(capture.read_text())
                        self.assertEqual(args[:7], ["workflow", "run", "hosted-pilot-bootstrap.yml", "--repo", "honua-io/honua-iac", "--ref", "trunk"])
                        self.assertEqual(args[7::2], ["-f"] * 8)
                        actual = dict(value.split("=", 1) for value in args[8::2])
                        self.assertEqual(actual, {**expected, "mode": mode, "phase": phase})


if __name__ == "__main__":
    unittest.main()
