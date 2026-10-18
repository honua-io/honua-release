#!/usr/bin/env python3
"""Live Docker regressions; run before the full gate (no document or test exclusions)."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import run
from context_rehearsal import rehearse


class DockerRegressions(unittest.TestCase):
    def test_explicit_context_package_and_project_state(self):
        # Local third-party fixture packages and a BCL project exercise the real
        # runner. They cannot produce a public-package/candidate qualification.
        with tempfile.TemporaryDirectory(prefix="execdocs-context-") as directory:
            report = rehearse(Path(directory), run.ROOT / "artifacts/executable-docs/context-rehearsal.json")
            self.assertEqual(report["status"], "pass")
            self.assertFalse(report["qualification"])
            self.assertEqual(len(report["controls"]), 5)
            self.assertLessEqual(report["peakOwnedContextContainers"], 3)

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

    def test_node_context_once_completion_imports_and_owned_cleanup(self):
        # Actual digest-pinned Docker transport; these local controls do not
        # qualify public packages or a candidate server.
        self.session.env["npm_config_registry"] = "http://source-admission.invalid/npm/"
        first = self.session.run_js(
            "const client = {value: 42}; require('node:fs').appendFileSync('effects', 'first\\n');",
            "javascript", 20)
        self.assertEqual(first.status, "pass", first.stderr)
        second = self.session.run_js("console.log(client.value);", "javascript", 20)
        self.assertEqual(second.status, "pass", second.stderr)
        self.assertEqual(second.stdout, "42\n")
        self.assertEqual((self.work / "app/effects").read_text(), "first\n")
        injected = self.session.run_js("console.log(process.env.npm_config_registry);", "javascript", 20)
        self.assertEqual(injected.stdout, "http://source-admission.invalid/npm/\n")
        self.session.env.pop("npm_config_registry")
        restored = self.session.run_js("console.log(process.env.npm_config_registry);", "javascript", 20)
        self.assertEqual(restored.status, "pass", restored.stderr)
        self.assertEqual(restored.stdout, self.session.baselines["node"]["npm_config_registry"] + "\n")
        version = self.session.run_js("console.log(process.version);", "javascript", 20)
        self.assertEqual(version.status, "pass", version.stderr)
        native_fds = self.session.run_js("require('node:fs').writeSync(1, 'direct-output\\n'); "
            "require('node:fs').writeSync(2, 'direct-error\\n');", "javascript", 20)
        self.assertEqual(native_fds.status, "pass", native_fds.stderr)
        self.assertEqual(native_fds.stdout, "direct-output\n")
        self.assertEqual(native_fds.stderr, "direct-error\n")
        inherited = self.session.run_js("require('node:child_process').spawn(process.execPath, ['-e', "
            "\"console.log('child-output'); console.error('child-error')\"], {stdio: 'inherit'});",
            "javascript", 20)
        self.assertEqual(inherited.status, "pass", inherited.stderr)
        self.assertEqual(inherited.stdout, "child-output\n")
        self.assertEqual(inherited.stderr, "child-error\n")
        self.assertEqual(self.session.run_js("console.log(client.value);", "javascript", 20).stdout, "42\n")

        shell = self.session.run_shell("mkdir later; echo \"export default 'later';\" > later/local.mjs; cd later",
                                       "node", 20, False)
        self.assertEqual(shell.status, "pass", shell.stderr)
        imported = self.session.run_js("import current from './local.mjs';\n"
            "const dynamic = await import('./local.mjs'); console.log(current, dynamic.default);", "javascript", 20)
        self.assertEqual(imported.status, "pass", imported.stderr)
        self.assertEqual(imported.stdout, "later later\n")
        completed = self.session.run_js("new Promise(resolve => setTimeout(() => resolve(42), 10))"
            ".then(value => { require('node:fs').appendFileSync('query-effect', 'once'); console.log(value); });",
            "javascript", 20)
        self.assertEqual(completed.status, "pass", completed.stderr)
        self.assertEqual(completed.stdout, "42\n")
        self.assertEqual((self.work / "app/later/query-effect").read_text(), "once")

        healthy = self.session.execution_context("healthy", "node")
        self.assertEqual(healthy.run_js("const owned = 'healthy';", "javascript", 20).status, "pass")
        failed_name = self.session.containers["node"]
        failure = self.session.run_js("setTimeout(() => { throw new Error('late Docker failure'); }, 10);",
                                      "javascript", 20)
        self.assertEqual(failure.status, "fail")
        self.assertTrue(failure.command_failed)
        self.assertIn("late Docker failure", failure.stderr)
        self.assertTrue(self.session.js.broken)
        self.assertNotEqual(run.docker("inspect", failed_name).returncode, 0)
        self.assertIn("refusing to restart", self.session.run_js("console.log(client);", "javascript", 20).detail)
        self.assertEqual(healthy.run_js("console.log(owned);", "javascript", 20).stdout, "healthy\n")

        missing_names = []
        for binding, declaration in [("named", "{ missing }"), ("default", "missing")]:
            missing = self.session.execution_context(f"missing-{binding}", "node")
            missing.put("node", Path(missing.cwd, "exports.mjs"), "export const present = 42;")
            missing_names.append(missing.containers["node"])
            result = missing.run_js(f"import {declaration} from './exports.mjs';\n"
                "(await import('node:fs')).writeFileSync('invalid-export-effect', 'bad');", "javascript", 20)
            self.assertEqual(result.status, "fail")
            self.assertTrue(result.command_failed)
            self.assertIn("SyntaxError", result.stderr)
            self.assertFalse(Path(missing.cwd, "invalid-export-effect").exists())
            self.assertNotEqual(run.docker("inspect", missing_names[-1]).returncode, 0)

        timed = self.session.execution_context("timed", "node")
        self.assertEqual(timed.run_js("const established = 1;", "javascript", 20).status, "pass")
        timed_name = timed.containers["node"]
        timeout = timed.run_js("require('node:fs').writeFileSync('started', 'yes'); "
            "require('node:child_process').spawn(process.execPath, ['-e', "
            "\"setTimeout(() => require('node:fs').writeFileSync('late', 'bad'), 3000)\"], {stdio:'ignore'}); "
            "await new Promise(() => {});", "javascript", 1)
        self.assertEqual(timeout.status, "fail")
        self.assertIn("timed out", timeout.detail)
        self.assertEqual(Path(timed.cwd, "started").read_text(), "yes")
        self.assertNotEqual(run.docker("inspect", timed_name).returncode, 0)
        self.assertIn("refusing to restart", timed.run_js("console.log(established);", "javascript", 20).detail)
        self.assertFalse(Path(timed.cwd, "late").exists())
        self.assertEqual(healthy.run_js("console.log(owned);", "javascript", 20).stdout, "healthy\n")
        owned = [failed_name, timed_name, healthy.containers["node"], *missing_names]
        self.session.close()
        self.assertTrue(all(run.docker("inspect", name).returncode != 0 for name in owned))
        report = {"schemaVersion": "executable-docs-node-context-rehearsal.v1", "qualification": False,
            "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run.ROOT, text=True).strip(),
            "runtimeImage": self.session.runtimes["node"], "nodeVersion": version.stdout.strip(),
            "status": "pass", "sourceHashes": {name: hashlib.sha256((run.HERE / name).read_bytes()).hexdigest()
                for name in ["jssession.mjs", "jssession.py", "run.py", "docker_regressions.py"]},
            "controls": {name: "pass" for name in ["persistentStateOnce", "currentCwdStaticDynamicImports",
                "nativeFdOutputCaptured", "inheritedChildOutputCaptured",
                "temporarySourceEnvRestored", "unawaitedCompletionOnce", "lateErrorRetained", "failedContextStopped", "otherContextHealthy",
                "missingNamedExportRefused", "missingDefaultExportRefused", "timeoutChildContainerStopped",
                "timeoutRestartRefused", "allOwnedContainersAbsent"]}}
        path = run.ROOT / "artifacts/executable-docs/node-context-rehearsal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")

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
