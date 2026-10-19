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
from resource_rehearsal import published_sdk_pool_control, socket_after_pass_control


class DockerRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="execdocs-regression-tools-")
        images = json.loads((run.HERE / "sources.json").read_text())["runtimes"]
        cls.tools = run.prepare_tools(images, Path(cls.shared.name))

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def test_explicit_context_package_and_project_state(self):
        # Local third-party fixture packages and a BCL project exercise the real
        # runner. They cannot produce a public-package/candidate qualification.
        with tempfile.TemporaryDirectory(prefix="execdocs-context-") as directory:
            report = rehearse(Path(directory), run.ROOT / "artifacts/executable-docs/context-rehearsal.json",
                              self.tools)
            self.assertEqual(report["status"], "pass")
            self.assertFalse(report["qualification"])
            self.assertEqual(len(report["controls"]), 5)
            self.assertLessEqual(report["peakOwnedContextContainers"], 3)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="execdocs-regression-")
        self.work = Path(self.directory.name)
        runtimes = json.loads((run.HERE / "sources.json").read_text())["runtimes"]
        self.session = run.Session("regression", self.work, runtimes, "http://127.0.0.1:1", self.tools,
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
        commonjs = self.session.run_js("require('node:fs'); exports.original = true; "
            "const earlierModule = module; const earlierExports = exports; module.exports = {answer: 42}; "
            "console.log(module.exports.answer, exports.original, exports === module.exports);", "javascript", 20)
        self.assertEqual(commonjs.status, "pass", commonjs.stderr)
        self.assertEqual(commonjs.stdout, "42 true false\n")
        next_module = self.session.run_js("require('node:fs'); console.log(earlierModule.exports.answer, "
            "earlierExports.original, earlierModule !== module, exports === module.exports, Object.keys(exports).length);",
            "javascript", 20)
        self.assertEqual(next_module.status, "pass", next_module.stderr)
        self.assertEqual(next_module.stdout, "42 true true true 0\n")
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
        self.session.put("node", Path(self.session.cwd, "live.mjs"),
                         "export let value = 0; export function increment() { value++; }\n")
        live = self.session.run_js("import {value, increment} from './live.mjs'; "
                                   "increment(); console.log(value);", "javascript", 20)
        self.assertEqual(live.status, "pass", live.stderr)
        self.assertEqual(live.stdout, "1\n")
        self.assertEqual(self.session.run_js("increment(); console.log(value);", "javascript", 20).stdout,
                         "2\n")
        hashbang = self.session.run_js("#!/usr/bin/env node\n"
                                       "console.log(eval?.('typeof value'), value);", "javascript", 20)
        self.assertEqual(hashbang.status, "pass", hashbang.stderr)
        self.assertEqual(hashbang.stdout, "undefined 2\n")
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

        linking = self.session.execution_context("linking", "node")
        linking_names = []
        for module in ["first", "second"]:
            linking.put("node", Path(linking.cwd, module + ".mjs"),
                f"import {{ writeFileSync }} from 'node:fs'; writeFileSync('{module}-effect', 'bad'); export const present = 42;")
        linking_names.append(linking.containers["node"])
        linked = linking.run_js("import './first.mjs';\nimport { missing } from './second.mjs';", "javascript", 20)
        self.assertEqual(linked.status, "fail")
        self.assertIn("SyntaxError", linked.stderr)
        self.assertFalse(any(Path(linking.cwd, module + "-effect").exists() for module in ["first", "second"]))
        self.assertNotEqual(run.docker("inspect", linking_names[-1]).returncode, 0)

        exit_status = self.session.execution_context("exit-status", "node")
        self.assertEqual(exit_status.run_js("const alive = 1;", "javascript", 20).status, "pass")
        exit_name = exit_status.containers["node"]
        nonzero = exit_status.run_js("process.exitCode = 7;", "javascript", 20)
        self.assertEqual(nonzero.status, "fail")
        self.assertEqual(nonzero.exit_code, 7)
        self.assertTrue(nonzero.command_failed)
        self.assertNotEqual(run.docker("inspect", exit_name).returncode, 0)
        self.assertIn("refusing to restart", exit_status.run_js("console.log(alive);", "javascript", 20).detail)

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
        owned = [failed_name, timed_name, healthy.containers["node"], *missing_names, *linking_names, exit_name]
        self.session.close()
        self.assertTrue(all(run.docker("inspect", name).returncode != 0 for name in owned))
        report = {"schemaVersion": "executable-docs-node-context-rehearsal.v1", "qualification": False,
            "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run.ROOT, text=True).strip(),
            "runtimeImage": self.session.runtimes["node"], "nodeVersion": version.stdout.strip(),
            "status": "pass", "sourceHashes": {name: hashlib.sha256((run.HERE / name).read_bytes()).hexdigest()
                for name in ["jssession.mjs", "jsresources.mjs", "jssession.py", "run.py", "docker_regressions.py"]},
            "controls": {name: "pass" for name in ["persistentStateOnce", "currentCwdStaticDynamicImports",
                "nativeFdOutputCaptured", "inheritedChildOutputCaptured",
                "fullImportPrefixLinkedBeforeEffects", "nonzeroExitStatusRetained",
                "commonJsExportsAliasReplacementAndFreshModule",
                "temporarySourceEnvRestored", "unawaitedCompletionOnce", "lateErrorRetained", "failedContextStopped", "otherContextHealthy",
                "missingNamedExportRefused", "missingDefaultExportRefused", "timeoutChildContainerStopped",
                "timeoutRestartRefused", "allOwnedContainersAbsent"]}}
        path = run.ROOT / "artifacts/executable-docs/node-context-rehearsal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n")

    def test_node_owned_resources_finish_before_publication(self):
        healthy = self.session.execution_context("resource-healthy", "node")
        controls = []
        for index, schedule in enumerate(["setTimeout(effect, 20).unref();",
                "setInterval(effect, 20).unref();", "setImmediate(effect).unref();",
                "require('node:timers/promises').setTimeout(20, undefined, {ref:false}).then(effect);"]):
            context = self.session.execution_context(f"resource-timer-{index}", "node")
            first = context.run_js("const effect = () => require('node:fs').appendFileSync('stale', 'bad'); "
                                   + schedule, "javascript", 20)
            self.assertEqual(first.status, "pass", first)
            path = Path(context.cwd, "stale")
            before = path.read_bytes() if path.exists() else b""
            self.assertEqual(context.run_js("await new Promise(resolve => setTimeout(resolve, 80)); "
                "console.log('healthy');", "javascript", 20).stdout, "healthy\n")
            self.assertEqual(path.read_bytes() if path.exists() else b"", before)
            if "Immediate" not in schedule:
                self.assertEqual(before, b"")
            controls.append(["timeout", "interval", "immediate", "promiseTimer"][index] + "NoAfterPassEffects")
        refreshed = healthy.run_js("let fires = 0; const reusable = setTimeout(() => fires++, 5); "
            "await new Promise(resolve => setTimeout(resolve, 10)); console.log(fires);", "javascript", 20)
        self.assertEqual(refreshed.stdout, "1\n")
        self.assertEqual(healthy.run_js("reusable.refresh().unref();", "javascript", 20).status, "pass")
        self.assertEqual(healthy.run_js("await new Promise(resolve => setTimeout(resolve, 40)); "
            "console.log(fires);", "javascript", 20).stdout, "1\n")
        controls.append("refreshedTimeoutFreshOwnership")
        healthy.put("node", Path(healthy.cwd, "scheduler.mjs"), "import {writeFileSync} from 'node:fs'; "
            "setInterval(() => writeFileSync('import-stale', 'bad'), 20).unref(); export const value = 42;")
        imported = healthy.run_js("import {value} from './scheduler.mjs'; console.log(value);", "javascript", 20)
        self.assertEqual(imported.stdout, "42\n")
        self.assertEqual(healthy.run_js("await new Promise(resolve => setTimeout(resolve, 80)); "
            "console.log(value);", "javascript", 20).stdout, "42\n")
        self.assertFalse(Path(healthy.cwd, "import-stale").exists())
        controls.append("moduleImportSchedulingOwned")
        socket_after_pass_control(healthy)
        controls.append("nativeSocketAfterPassDeliveryStopped")
        port = healthy.run_js("const channel = new (require('node:worker_threads').MessageChannel)(); "
            "channel.port1.on('message', () => require('node:fs').writeFileSync('port-stale', 'bad')); "
            "channel.port1.unref(); channel.port2.unref();", "javascript", 20)
        self.assertEqual(port.status, "pass", port)
        self.assertEqual(healthy.run_js("channel.port2.postMessage('late'); "
            "await new Promise(resolve => setTimeout(resolve, 30)); console.log('healthy');",
            "javascript", 20).stdout, "healthy\n")
        self.assertFalse(Path(healthy.cwd, "port-stale").exists())
        controls.append("messagePortsClosedBeforePass")
        listener = healthy.run_js("const listener = require('node:net').createServer(socket => { "
            "require('node:fs').writeFileSync('server-stale', 'bad'); socket.end(); }); "
            "await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve)); "
            "const listenPort = listener.address().port; listener.on('close', () => console.log('owned-close')); "
            "listener.unref();", "javascript", 20)
        self.assertEqual(listener.status, "pass", listener)
        self.assertEqual(listener.stdout, "owned-close\n")
        attempt = healthy.run_js("console.log(await new Promise(resolve => { "
            "const s = require('node:net').connect(listenPort, '127.0.0.1'); "
            "s.on('error', error => resolve(error.code)); s.on('connect', () => { "
            "s.destroy(); resolve('unexpected'); }); }));", "javascript", 20)
        self.assertEqual(attempt.stdout, "ECONNREFUSED\n")
        self.assertFalse(Path(healthy.cwd, "server-stale").exists())
        controls.append("listeningServerClosedBeforePass")
        controls.append("cleanupOutputAttributedToOriginalBlock")
        inactive = healthy.run_js("let prelistenCloses = 0; const prelisten = require('node:net').createServer().unref(); "
            "prelisten.on('close', () => prelistenCloses++); console.log('inactive');", "javascript", 20)
        self.assertEqual(inactive.status, "pass", inactive)
        self.assertEqual(inactive.stdout, "inactive\n")
        prelisten = healthy.run_js(
            "await new Promise(resolve => prelisten.listen(0, '127.0.0.1', resolve)); "
            "console.log('prepared', prelistenCloses);", "javascript", 20)
        self.assertEqual(prelisten.status, "pass", prelisten)
        self.assertEqual(prelisten.stdout, "prepared 0\n")
        reused = healthy.run_js("await new Promise(resolve => prelisten.listen(0, '127.0.0.1', resolve)); "
            "const reusedPort = prelisten.address().port; console.log('reused', prelistenCloses);", "javascript", 20)
        self.assertEqual(reused.status, "pass", reused)
        self.assertEqual(reused.stdout, "reused 1\n")
        attempt = healthy.run_js("console.log(await new Promise(resolve => { "
            "const s = require('node:net').connect(reusedPort, '127.0.0.1'); "
            "s.on('error', error => resolve(error.code)); s.on('connect', () => { "
            "s.destroy(); resolve('unexpected'); }); }));", "javascript", 20)
        self.assertEqual(attempt.stdout, "ECONNREFUSED\n")
        controls.append("prelistenUnrefInactiveAndRelistenRefStatePreserved")
        repeat = healthy.run_js("let inactiveCloses = 0; const userClose = () => inactiveCloses++; "
            "const repeatedInactive = require('node:net').createServer(); "
            "repeatedInactive.on('close', userClose);", "javascript", 20)
        self.assertEqual(repeat.status, "pass", repeat)
        for _ in range(12):
            completed = healthy.run_js("repeatedInactive.unref();", "javascript", 20)
            self.assertEqual(completed.status, "pass", completed)
            self.assertEqual(completed.stderr, "")
            observed = healthy.run_js("console.log(repeatedInactive.listenerCount('close'), "
                "repeatedInactive.listeners('close')[0] === userClose, inactiveCloses);", "javascript", 20)
            self.assertEqual(observed.status, "pass", observed)
            self.assertEqual(observed.stdout, "1 true 0\n")
            self.assertEqual(observed.stderr, "")
        controls.append("inactiveUnrefRepeatedOwnedObserverRemovedUserListenerPreserved")
        cleanup_output = healthy.run_js("const idleCleanup = require('node:net').createServer(); "
            "idleCleanup.on('removeListener', kind => { if (kind === 'close') setTimeout(() => { "
            "require('node:fs').appendFileSync('cleanup-once', 'once'); console.log('cleanup-output'); "
            "}, 10); }); idleCleanup.unref();", "javascript", 20)
        self.assertEqual(cleanup_output.status, "pass", cleanup_output)
        self.assertEqual(cleanup_output.stdout, "cleanup-output\n")
        self.assertEqual(Path(healthy.cwd, "cleanup-once").read_text(), "once")
        self.assertEqual(healthy.run_js("console.log('healthy');", "javascript", 20).stdout, "healthy\n")
        self.assertEqual(Path(healthy.cwd, "cleanup-once").read_text(), "once")
        controls.append("synchronousCleanupNewAsyncOutputRetainedOnce")
        for name, callback, expected in [
                ("sync-cleanup-error", "setTimeout(() => { throw new Error('cleanup-async-error'); }, 10);", "cleanup-async-error"),
                ("sync-cleanup-timeout", "setInterval(() => {}, 1000);", "timed out")]:
            context = self.session.execution_context(name, "node")
            result = context.run_js("const s = require('node:net').createServer(); "
                "s.on('removeListener', kind => { if (kind === 'close') { " + callback + " } }); s.unref();",
                "javascript", 2)
            self.assertEqual(result.status, "fail", result)
            self.assertIn(expected, result.stderr + result.detail)
            self.assertNotEqual(run.docker("inspect", context.containers["node"]).returncode, 0)
            controls.append(name + "OriginalBlockFailedAndStopped")
        idle = self.session.execution_context("before-exit", "node")
        ordered = idle.run_js("const normalIdle = () => console.log('normal'); "
            "process.on('beforeExit', normalIdle); process.once('beforeExit', () => console.log('once')); "
            "process.prependOnceListener('beforeExit', () => console.log('prepend'));", "javascript", 20)
        self.assertEqual(ordered.status, "pass", ordered)
        self.assertEqual(ordered.stdout, "prepend\nnormal\nonce\n")
        controls.append("beforeExitNativeListenerOrder")
        nested = idle.run_js("process.removeListener('beforeExit', normalIdle); "
            "process.once('beforeExit', () => process.nextTick(() => Promise.resolve().then(() => setTimeout(() => { "
            "require('node:fs').appendFileSync('idle-effect', 'once'); console.log('nested-output'); }, 10))));",
            "javascript", 20)
        self.assertEqual(nested.status, "pass", nested)
        self.assertEqual(nested.stdout, "nested-output\n")
        self.assertEqual(Path(idle.cwd, "idle-effect").read_text(), "once")
        controls.append("beforeExitNestedNextTickPromiseTimerOutputOnce")
        repeated = idle.run_js("let idleEvents = 0; const repeatIdle = () => { "
            "console.log('idle', ++idleEvents); if (idleEvents < 3) "
            "setTimeout(() => console.log('work', idleEvents), 10); }; process.on('beforeExit', repeatIdle);",
            "javascript", 20)
        self.assertEqual(repeated.status, "pass", repeated)
        self.assertEqual(repeated.stdout, "idle 1\nwork 1\nidle 2\nwork 2\nidle 3\n")
        controls.append("beforeExitRealReferencedEventsRetained")
        unref = idle.run_js("process.removeListener('beforeExit', repeatIdle); let idleUnrefEvents = 0; "
            "const unrefIdle = () => { console.log('idle', ++idleUnrefEvents); "
            "setTimeout(() => require('node:fs').writeFileSync('idle-stale', 'bad'), 20).unref(); }; "
            "process.on('beforeExit', unrefIdle);", "javascript", 20)
        self.assertEqual(unref.status, "pass", unref)
        self.assertEqual(unref.stdout, "idle 1\n")
        later = idle.run_js("process.removeListener('beforeExit', unrefIdle); "
            "await new Promise(resolve => setTimeout(resolve, 60)); console.log(idleUnrefEvents);", "javascript", 20)
        self.assertEqual(later.stdout, "1\n")
        self.assertFalse(Path(idle.cwd, "idle-stale").exists())
        controls.append("beforeExitUnrefOwnedWithoutArtificialEvents")
        settled = idle.run_js("let resolveAtExit; const atExit = new Promise(resolve => { resolveAtExit = resolve; }); "
            "process.once('beforeExit', () => resolveAtExit(42)); console.log(await atExit);", "javascript", 20)
        self.assertEqual(settled.status, "pass", settled)
        self.assertEqual(settled.stdout, "42\n")
        controls.append("beforeExitCanSettleTopLevelAwait")
        for name, source, expected in [
                ("before-exit-error", "process.once('beforeExit', () => setTimeout(() => { "
                 "throw new Error('before-exit-late'); }, 10));", "before-exit-late"),
                ("before-exit-timeout", "process.on('beforeExit', () => setTimeout(() => {}, 10));", "timed out"),
                ("before-exit-status", "process.once('beforeExit', () => { process.exitCode = 7; });", "exit code 7")]:
            context = self.session.execution_context(name, "node")
            result = context.run_js(source, "javascript", 2)
            self.assertEqual(result.status, "fail", result)
            self.assertIn(expected, result.stderr + result.detail)
            if name == "before-exit-status":
                self.assertEqual(result.exit_code, 7)
            self.assertNotEqual(run.docker("inspect", context.containers["node"]).returncode, 0)
            controls.append(name + "OriginalBlockFailedAndStopped")
        healthy.put("node", Path(healthy.cwd, "watched"), "initial")
        watcher = healthy.run_js("const watcher = require('node:fs').watch('watched', {persistent:false}, "
            "() => require('node:fs').writeFileSync('watch-stale', 'bad'));", "javascript", 20)
        self.assertEqual(watcher.status, "pass", watcher)
        self.assertEqual(healthy.run_js("require('node:fs').writeFileSync('watched', 'later'); "
            "await new Promise(resolve => setTimeout(resolve, 30)); console.log('healthy');",
            "javascript", 20).stdout, "healthy\n")
        self.assertFalse(Path(healthy.cwd, "watch-stale").exists())
        controls.append("watcherClosedBeforePass")
        for name, callback, expected in [("cleanup-error", "throw new Error('cleanup failure');", "cleanup failure"),
                ("cleanup-timeout", "setInterval(() => {}, 1000);", "timed out")]:
            context = self.session.execution_context(name, "node")
            result = context.run_js("const s = require('node:net').createServer(); "
                "await new Promise(resolve => s.listen(0, '127.0.0.1', resolve)); "
                f"s.on('close', () => {{ {callback} }}); s.unref();", "javascript", 2)
            self.assertEqual(result.status, "fail", result)
            self.assertIn(expected, result.stderr + result.detail)
            self.assertNotEqual(run.docker("inspect", context.containers["node"]).returncode, 0)
            controls.append(name + "OriginalBlockFailedAndStopped")
        refused = self.session.execution_context("unsupported-udp", "node")
        udp = refused.run_js("const udp = require('node:dgram').createSocket('udp4'); "
            "await new Promise(resolve => udp.bind(0, '127.0.0.1', resolve)); udp.unref();",
            "javascript", 20)
        self.assertEqual(udp.status, "fail", udp)
        self.assertIn("unsupported owned unref resource: UDP", udp.stderr)
        self.assertNotEqual(run.docker("inspect", refused.containers["node"]).returncode, 0)
        controls.append("unsupportedOwnedUnrefRefused")
        dormant = self.session.execution_context("unsupported-dormant-udp", "node")
        result = dormant.run_js("const dormantUdp = require('node:dgram').createSocket('udp4');",
            "javascript", 20)
        self.assertEqual(result.status, "fail", result)
        self.assertIn("unsupported owned dormant resource: UDP", result.stderr)
        self.assertNotEqual(run.docker("inspect", dormant.containers["node"]).returncode, 0)
        controls.append("nativeValidDormantUdpExplicitlyUnsupported")
        for index, (source, kind) in enumerate([
                ("require('node:fs').watchFile('watched', {persistent:false}, () => {});", "StatWatcher"),
                ("require('node:fs/promises').watch('watched', {persistent:false});", "FSWatchIterator"),
                ("require('node:child_process').spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], "
                 "{stdio:'ignore'}).unref();", "ChildProcess"),
                ("new (require('node:worker_threads').Worker)('setInterval(() => {}, 1000)', {eval:true}).unref();", "Worker")]):
            context = self.session.execution_context(f"unsupported-unref-{index}", "node")
            context.put("node", Path(context.cwd, "watched"), "initial")
            result = context.run_js(source, "javascript", 20)
            self.assertEqual(result.status, "fail", result)
            self.assertIn(f"unsupported owned unref resource: {kind}", result.stderr)
            self.assertNotEqual(run.docker("inspect", context.containers["node"]).returncode, 0)
            controls.append(f"unsupported{kind}UnrefRefused")
        sdk = self.session.execution_context("published-sdk-pool", "node")
        artifact = published_sdk_pool_control(sdk, run.ROOT)
        controls.append("publishedSdkNativeAndPersistentPoolReuse")
        self.assertEqual(healthy.run_js("console.log(value);", "javascript", 20).stdout, "42\n")
        controls.append("otherContextHealthyAfterCleanupFailures")
        owned = [name for context in [self.session, *self.session.contexts.values()]
                 for name in context.containers.values()]
        self.session.close()
        self.assertTrue(all(run.docker("inspect", name).returncode != 0 for name in owned))
        controls.append("allOwnedContainersAbsent")
        report = {"schemaVersion": "executable-docs-node-resource-rehearsal.v1", "qualification": False,
            "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run.ROOT, text=True).strip(),
            "runtimeImage": self.session.runtimes["node"], "status": "pass", "publicSdkArtifact": artifact,
            "controls": {name: "pass" for name in controls},
            "sourceHashes": {name: hashlib.sha256((run.HERE / name).read_bytes()).hexdigest()
                for name in ["jssession.mjs", "jsresources.mjs", "jssession.py", "run.py",
                             "resource_rehearsal.py", "docker_regressions.py"]}}
        path = run.ROOT / "artifacts/executable-docs/node-resource-rehearsal.json"
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
