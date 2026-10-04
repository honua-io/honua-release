#!/usr/bin/env python3
"""Live Docker regressions; run before the full gate (no document or test exclusions)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import run


class DockerRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="execdocs-regression-")
        self.work = Path(self.directory.name)
        runtimes = json.loads((run.HERE / "sources.json").read_text())["runtimes"]
        self.session = run.Session("regression", self.work, runtimes, "http://127.0.0.1:1", self.work,
                                   False, "host", f"regression-{os.getpid()}", "5.9.3")

    def tearDown(self):
        self.session.close()
        # A root-created fixture must be removed by its owner before the host removes the directory.
        run.docker("run", "--rm", "--user", "0", "-v", f"{self.work}:{self.work}",
                   self.session.runtimes["node"], "rm", "-f", str(self.work / "app/Program.cs"), check=True)
        self.directory.cleanup()

    def test_root_owned_file_produces_report_and_next_block_runs(self):
        target = self.work / "app/Program.cs"
        run.docker("run", "--rm", "--user", "0", "-v", f"{self.work}:{self.work}",
                   self.session.runtimes["node"], "sh", "-c", f"echo root-owned > {target}", check=True)
        # Docker's root maps to nobody on a host using user namespaces.
        self.assertNotEqual(target.stat().st_uid, os.getuid())
        owner = run.docker("run", "--rm", "--user", "0", "-v", f"{self.work}:{self.work}",
                           self.session.runtimes["node"], "stat", "-c", "%u", str(target), check=True)
        self.assertEqual(owner.stdout.strip(), "0")
        result, _ = run.run_document({"runtime": "node"},
            "<!-- doc-run: file=Program.cs -->\n```csharp\nConsole.WriteLine(1);\n```\n```sh\ntrue\n```",
            self.session, {}, {"env": {}, "substitute": {}}, "sha256:test", [], set())
        report = self.work / "report.json"
        report.write_text(json.dumps(result))
        result = json.loads(report.read_text())
        self.assertEqual(result["status"], "fail")
        self.assertEqual([b["status"] for b in result["blocks"]], ["fail", "pass"])
        self.assertIn("Traceback", result["blocks"][0]["stderrTail"])
        self.assertIn("Permission denied", result["blocks"][0]["stderrTail"])

    def test_normal_container_files_are_owned_by_host(self):
        result = self.session.run_shell("mkdir child; echo owned > child/file", "node", 10, False)
        self.assertEqual(result.status, "pass")
        self.assertEqual((self.work / "app/child/file").stat().st_uid, os.getuid())
        (self.work / "app/child/file").write_text("host can update it")

    def test_live_readiness_and_timer_without_oracle(self):
        previous = run.SERVE_WINDOW
        run.SERVE_WINDOW = 2
        try:
            code = "node -e 'console.log(\"READY\"); setInterval(()=>{},1000)'"
            for oracle, status in [(None, "needs-input"), ({"log": "READY"}, "pass"),
                                   ({"log": "NOT READY"}, "fail")]:
                with self.subTest(oracle=oracle):
                    self.assertEqual(self.session.run_shell(code, "node", 10, True, oracle).status, status)
        finally:
            run.SERVE_WINDOW = previous


if __name__ == "__main__":
    unittest.main()
