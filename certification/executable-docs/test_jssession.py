"""Execute real Node controls: persistent state, once-only effects and owned cleanup."""
import os
import signal
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from jssession import JavaScriptSession  # noqa: E402
from run import Session, run_document  # noqa: E402


@pytest.fixture
def reader(tmp_path, monkeypatch):
    workers = []
    def worker(command, stop):
        # Only the transport changes: run the exact in-container worker on real Node.
        def stop_group():
            try:
                os.killpg(process.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process = JavaScriptSession(["node", "--no-warnings", str(HERE / "jssession.mjs")], stop_group)
        workers.append(process)
        return process
    monkeypatch.setattr("run.JavaScriptSession", worker)
    monkeypatch.setattr(Session, "container", lambda self, runtime: "owned-test-context")
    monkeypatch.setattr(Session, "put", lambda self, runtime, path, code: path.write_text(code))
    monkeypatch.setattr(Session, "installed_honua", lambda *args: {})
    session = Session("javascript", tmp_path, {"node": "controlled"}, "http://guard", tmp_path,
                      False, "host", "once", "5.9.3")
    yield session
    session.close()
    for process in workers:
        if process.proc.poll() is None:
            process.close()


def run(reader, code, language="javascript", timeout=5):
    return reader.run_js(code, language, timeout)


def test_lexical_state_imports_await_require_and_typescript_share_one_context(reader):
    Path(reader.cwd, "module.mjs").write_text("export const value = 40; export default 2;")
    first = run(reader, "import increment, { value as start } from './module.mjs';\n"
                "const client = await Promise.resolve({ value: start + increment });\n"
                "let count = 0; console.log('ready');")
    assert first.status == "pass", first.stderr
    assert first.stdout == "ready\n"
    second = run(reader, "count++; console.log(client.value, count, require('node:path').basename(process.cwd()));")
    assert second.status == "pass", second.stderr
    assert second.stdout == "42 1 app\n"
    third = run(reader, "const typed: number = client.value; console.log(typed, ++count);", "typescript")
    assert third.status == "pass", third.stderr
    assert third.stdout == "42 2\n"


def test_real_document_earlier_write_occurs_once(reader):
    text = "```js\nrequire('node:fs').appendFileSync('effects', 'first\\n'); const client = { value: 42 };\n```\n" \
           "```js\nrequire('node:fs').appendFileSync('effects', String(client.value) + '\\n'); console.log(client.value);\n```\n"
    result, _ = run_document({"runtime": "node"}, text, reader, {"_pins": {}},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "pass", result
    assert Path(reader.cwd, "effects").read_text() == "first\n42\n"
    assert not any("continues-earlier-blocks" in row.get("mode", "") for row in result["blocks"])


def test_partial_failure_is_retained_without_replaying_either_write(reader):
    text = "```js\nrequire('node:fs').appendFileSync('effects', 'first\\n'); const client = 42;\n```\n" \
           "```js\nrequire('node:fs').appendFileSync('effects', 'partial\\n'); missingClient.value;\n```\n"
    result, _ = run_document({"runtime": "node"}, text, reader, {"_pins": {}},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail"
    assert result["blocks"][0]["status"] == "pass"
    assert result["blocks"][1]["status"] == "fail"
    assert "ReferenceError" in result["blocks"][1]["stderrTail"]
    assert Path(reader.cwd, "effects").read_text() == "first\npartial\n"


def test_declared_contexts_do_not_share_lexical_or_filesystem_state(reader):
    first = reader.execution_context("first", "node")
    second = reader.execution_context("second", "node")
    assert run(first, "const secret = 7; require('node:fs').writeFileSync('owned', 'first');").status == "pass"
    result = run(second, "console.log(typeof secret, require('node:fs').existsSync('owned'));")
    assert result.status == "pass", result.stderr
    assert result.stdout == "undefined false\n"
    assert run(first, "console.log(secret);").stdout == "7\n"


def test_shell_environment_and_cwd_changes_reach_existing_worker(reader):
    assert run(reader, "let counter = 1;").status == "pass"
    reader.env["DOC_OWNED_VALUE"] = "later-shell-export"
    next_dir = Path(reader.cwd, "later")
    next_dir.mkdir()
    reader.cwd = str(next_dir)
    result = run(reader, "console.log(++counter, process.env.DOC_OWNED_VALUE, require('node:path').basename(process.cwd()));")
    assert result.status == "pass", result.stderr
    assert result.stdout == "2 later-shell-export later\n"


@pytest.mark.parametrize("source", [
    "setTimeout(() => require('node:fs').writeFileSync('late', 'bad'), 800); await new Promise(() => {});",
    "setTimeout(() => require('node:fs').writeFileSync('late', 'bad'), 800); while (true) {}",
    "require('node:child_process').spawn(process.execPath, ['-e', "
    "\"setTimeout(() => require('node:fs').writeFileSync('late', 'bad'), 800)\"], {stdio: 'ignore'}); "
    "await new Promise(() => {});",
])
def test_timeout_kills_worker_and_refuses_restart_or_late_side_effect(reader, source):
    assert run(reader, "const establishedState = 42;").status == "pass"
    result = run(reader, "require('node:fs').writeFileSync('started', 'yes'); " + source, timeout=0.3)
    assert result.status == "fail" and result.exit_code is None
    assert "timed out" in result.detail
    assert Path(reader.cwd, "started").read_text() == "yes"
    assert not result.command_failed
    proc = reader.js.proc
    assert proc.poll() is not None
    again = run(reader, "require('node:fs').writeFileSync('restarted', 'bad');")
    assert again.status == "fail" and "refusing to restart" in again.detail
    assert reader.js.proc is proc
    time.sleep(0.9)
    assert not Path(reader.cwd, "late").exists()
    assert not Path(reader.cwd, "restarted").exists()


def test_lost_context_does_not_stop_another_owned_context(reader):
    first = reader.execution_context("lost", "node")
    other = reader.execution_context("healthy", "node")
    assert run(first, "const owned = 'lost';").status == "pass"
    assert run(other, "const owned = 'healthy';").status == "pass"
    assert run(first, "while (true) {}", timeout=0.3).status == "fail"
    result = run(other, "console.log(owned);")
    assert result.status == "pass" and result.stdout == "healthy\n"


def test_worker_exit_loses_context_without_replay(reader):
    assert run(reader, "const state = 42;").status == "pass"
    result = run(reader, "process.exit(0);")
    assert result.status == "fail" and "context was lost" in result.detail
    assert not result.command_failed
    assert "refusing to restart" in run(reader, "console.log(state);").detail


def test_import_parser_does_not_rewrite_template_text_and_rejects_unknown_syntax(reader):
    result = run(reader, "const text = `import { fake } from 'fake';`; console.log(text);")
    assert result.status == "pass" and result.stdout == "import { fake } from 'fake';\n"
    invalid = run(reader, "import { value } from './module.mjs' with { type: 'json' };\n"
                  "require('node:fs').writeFileSync('bad-import', 'bad');")
    assert invalid.status == "fail" and invalid.command_failed
    assert not Path(reader.cwd, "bad-import").exists()
