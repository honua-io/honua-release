"""Execute real Node controls: persistent state, once-only effects and owned cleanup."""
import os
import signal
import subprocess
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


def test_commonjs_exports_alias_replacement_and_fresh_per_block_module(reader):
    source = "require('node:fs'); module.exports.answer = 42; exports.named = 'yes'; " \
        "console.log(module.exports.answer, exports.named, exports === module.exports); " \
        "module.exports = { replacement: true }; exports.legacy = 'old'; " \
        "console.log(module.exports.replacement, module.exports.legacy, exports.legacy, exports === module.exports);"
    native = Path(reader.cwd, 'native.cjs')
    native.write_text(source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0
    result = run(reader, source + ' const previousModule = module; const previousExports = exports;')
    assert result.status == 'pass', result
    assert result.stdout == oracle.stdout
    next_block = run(reader, "require('node:fs'); console.log(previousModule.exports.replacement, "
        "previousExports.legacy, previousModule !== module, exports === module.exports, Object.keys(exports).length);")
    assert next_block.status == 'pass' and next_block.stdout == 'true old true true 0\n'


def test_esm_blocks_do_not_gain_commonjs_module_or_exports_globals(reader):
    assert run(reader, "require('node:fs'); module.exports.value = 42;").status == 'pass'
    result = run(reader, "console.log(typeof module, typeof exports);")
    assert result.status == 'pass' and result.stdout == 'undefined undefined\n'


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


def test_temporary_source_environment_restores_container_baseline_and_removes_new_keys(reader, monkeypatch):
    # The real worker can start while an admitted source block overrides the
    # container's registry. Its startup environment is not the baseline.
    monkeypatch.setenv("DOC_REGISTRY_CONTROL", "source-only-startup")
    reader.baselines["node"] = {"DOC_REGISTRY_CONTROL": "candidate-baseline"}
    reader.env.update({"DOC_REGISTRY_CONTROL": "source-only", "DOC_TEMP_CONTROL": "temporary"})
    first = run(reader, "process.env.DOC_INTERPRETER_STATE = 'kept'; "
                "console.log(process.env.DOC_REGISTRY_CONTROL, process.env.DOC_TEMP_CONTROL);")
    assert first.status == "pass" and first.stdout == "source-only temporary\n"
    reader.env.clear()
    second = run(reader, "console.log(process.env.DOC_REGISTRY_CONTROL, "
                 "typeof process.env.DOC_TEMP_CONTROL, process.env.DOC_INTERPRETER_STATE);")
    assert second.status == "pass", second.stderr
    assert second.stdout == "candidate-baseline undefined kept\n"


def test_static_and_dynamic_imports_follow_shell_cwd_change(reader):
    Path(reader.cwd, "local.mjs").write_text("export default 'original';")
    assert run(reader, "import original from './local.mjs'; console.log(original);").stdout == "original\n"
    later = Path(reader.cwd, "later")
    later.mkdir()
    (later / "local.mjs").write_text("export default 'later';")
    reader.cwd = str(later)
    result = run(reader, "import current from './local.mjs';\n"
                 "const dynamic = await import('./local.mjs'); console.log(original, current, dynamic.default);")
    assert result.status == "pass", result.stderr
    assert result.stdout == "original later later\n"


@pytest.mark.parametrize("source", [
    "setTimeout(() => { throw new Error('late failure'); }, 10);",
    "Promise.reject(new Error('late failure'));",
])
def test_last_block_async_failure_cannot_be_reported_as_pass(reader, source):
    text = "```js\n" + source + "\n```\n"
    result, _ = run_document({"runtime": "node"}, text, reader, {"_pins": {}},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "fail", result
    assert result["blocks"][0]["status"] == "fail"
    assert "late failure" in result["blocks"][0]["stderrTail"]


def test_last_block_waits_for_unawaited_query_output_and_effect_once(reader):
    text = "```js\nconst query = () => new Promise(resolve => setTimeout(() => resolve(42), 10));\n" \
           "query().then(value => { require('node:fs').appendFileSync('query-effects', 'once'); console.log(value); });\n```\n" \
           "It prints:\n\n```text\n42\n```\n"
    result, _ = run_document({"runtime": "node"}, text, reader, {"_pins": {}},
                             {"env": {}, "substitute": {}}, "sha256:test", [], set())
    assert result["status"] == "pass", result
    assert Path(reader.cwd, "query-effects").read_text() == "once"


@pytest.mark.parametrize("source", [
    "require('node:fs').writeSync(1, 'direct-output\\n'); require('node:fs').writeSync(2, 'direct-error\\n');",
    "require('node:child_process').spawn(process.execPath, ['-e', "
    "\"console.log('child-output'); console.error('child-error')\"], {stdio: 'inherit'});",
])
def test_native_fd_and_inherited_child_output_do_not_share_control_channel(reader, source):
    native = Path(reader.cwd, 'native.mjs')
    native.write_text("import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);\n" + source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0
    result = run(reader, source)
    assert result.status == 'pass', result
    assert result.stdout == oracle.stdout and result.stderr == oracle.stderr
    assert run(reader, "const stillHealthy = 42; console.log(stillHealthy);").stdout == '42\n'


@pytest.mark.parametrize('schedule,expected_effect', [
    ('Promise.resolve().then', True), ('setImmediate', False), ('setTimeout', False),
])
def test_sync_error_retains_native_microtasks_but_stops_later_turns(reader, schedule, expected_effect):
    source = f"{schedule}(() => require('node:fs').writeFileSync('effect', 'once')); throw new Error('sync failure');"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text("import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);\n" + source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    effect = Path(reader.cwd, 'effect')
    assert oracle.returncode != 0 and effect.exists() is expected_effect
    effect.unlink(missing_ok=True)
    result = run(reader, source)
    assert result.status == 'fail' and result.command_failed
    assert reader.js.broken and reader.js.proc.poll() is not None
    assert effect.exists() is expected_effect


@pytest.mark.parametrize('corruption', ['stale-id', 'wrong-file', 'partial-json'])
def test_completion_response_must_be_atomic_and_match_this_request(tmp_path, corruption):
    script = """const fs = require('node:fs'); process.stdin.once('data', chunk => {
      const request = JSON.parse(chunk); const kind = process.argv[1];
      const answer = {file: request.file, id: request.id, ok: true, lost: false, error: '', exitCode: 0};
      if (kind === 'stale-id') answer.id = 'stale';
      if (kind === 'wrong-file') answer.file = 'another-block';
      fs.writeFileSync(request.response, kind === 'partial-json' ? '{' : JSON.stringify(answer));
    });"""
    stopped = []
    worker = JavaScriptSession(['node', '-e', script, corruption], lambda: stopped.append(True))
    path = tmp_path / 'block.mjs'
    path.write_text('console.log(42);')
    # A stale prior completion must be removed before the request is sent.
    Path(str(path) + '.response.json').write_text('{"ok":true}')
    with pytest.raises((ValueError, RuntimeError)):
        worker.execute(path, str(tmp_path), {}, 'javascript', 5)
    assert worker.broken and stopped == [True] and worker.proc.poll() is not None


@pytest.mark.parametrize('declaration', ["import { missing } from './second.mjs';", "import absent from './second.mjs';"])
def test_all_static_imports_link_before_any_dependency_effect(reader, declaration):
    for name in ['first', 'second']:
        Path(reader.cwd, name + '.mjs').write_text(
            f"import {{ writeFileSync }} from 'node:fs'; writeFileSync('{name}-effect', 'bad'); export const present = 42;")
    source = "import './first.mjs';\n" + declaration + "\nconsole.log('body');"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text(source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode != 0 and 'SyntaxError' in oracle.stderr
    assert not any(Path(reader.cwd, name + '-effect').exists() for name in ['first', 'second'])
    result = run(reader, source)
    assert result.status == 'fail' and 'SyntaxError' in result.stderr
    assert not any(Path(reader.cwd, name + '-effect').exists() for name in ['first', 'second'])


def test_native_import_bridge_evaluates_shared_dependency_once_across_blocks(reader):
    Path(reader.cwd, 'shared.mjs').write_text(
        "import { appendFileSync } from 'node:fs'; appendFileSync('dependency-effects', 'once'); "
        "export const value = 42; export default undefined;")
    first = run(reader, "import './shared.mjs';\nimport absent, {value} from './shared.mjs'; console.log(value, absent);")
    assert first.status == 'pass' and first.stdout == '42 undefined\n'
    second = run(reader, "import * as shared from './shared.mjs'; console.log(value, shared.value);")
    assert second.status == 'pass' and second.stdout == '42 42\n'
    assert Path(reader.cwd, 'dependency-effects').read_text() == 'once'


@pytest.mark.parametrize('code', ["process.exitCode = 7;", "process.exitCode = '9';", "process.exitCode = -1;",
    "process.exitCode = 256;", "process.exitCode = 7; process.exitCode = 0;"])
def test_final_exit_status_matches_native_esm_completion(reader, code):
    native = Path(reader.cwd, 'native.mjs')
    native.write_text(code)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    result = run(reader, code)
    assert result.exit_code == oracle.returncode
    assert (result.status == 'pass') is (oracle.returncode == 0)
    if oracle.returncode:
        assert result.command_failed and reader.js.broken
        assert 'refusing to restart' in run(reader, 'console.log(42);').detail
    else:
        assert run(reader, 'console.log(process.exitCode);').stdout == 'undefined\n'


@pytest.mark.parametrize('language', ['javascript', 'typescript'])
def test_import_meta_remains_explicit_failed_esm_fidelity_requirement(reader, language):
    source = "console.log(import.meta.url); console.log(import.meta.resolve('./local.mjs'));"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text(source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0 and native.as_uri() in oracle.stdout
    result = run(reader, source, language)
    assert result.status == 'fail' and result.command_failed
    assert "Cannot use 'import.meta' outside a module" in result.stderr
    assert reader.js.broken


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


def test_missing_builtin_import_is_an_actual_error_not_repl_auto_loading(reader):
    result = run(reader, "fs.writeFileSync('implicit-import', 'bad');")
    assert result.status == "fail" and result.command_failed
    assert "ReferenceError" in result.stderr and "fs is not defined" in result.stderr
    assert not Path(reader.cwd, "implicit-import").exists()


@pytest.mark.parametrize("source", ["implicitReaderValue = 42;", "await Promise.resolve(); implicitReaderValue = 42;"])
def test_strict_mode_does_not_silently_create_an_undeclared_variable(reader, source):
    result = run(reader, source + " require('node:fs').writeFileSync('implicit-variable', 'bad');")
    assert result.status == "fail" and result.command_failed
    assert "ReferenceError" in result.stderr
    assert not Path(reader.cwd, "implicit-variable").exists()
    assert "refusing to restart" in run(reader, "console.log(typeof implicitReaderValue);").detail


def test_unsettled_top_level_await_at_quiescence_stops_context(reader):
    assert run(reader, "const priorState = 42;").status == "pass"
    result = run(reader, "await new Promise(() => {});")
    assert result.status == "fail" and "did not settle" in result.stderr
    assert reader.js.broken and reader.js.proc.poll() is not None
    assert "refusing to restart" in run(reader, "console.log(priorState);").detail


@pytest.mark.parametrize('schedule', [
    "setTimeout(effect, 20).unref();",
    "setInterval(effect, 20).unref();",
    "setImmediate(effect).unref();",
    "require('node:timers/promises').setTimeout(20, undefined, {ref:false}).then(effect);",
])
def test_owned_unref_schedulers_cannot_mutate_after_completion(reader, schedule):
    first = run(reader, "const effect = () => require('node:fs').appendFileSync('stale', 'bad'); " + schedule)
    assert first.status == 'pass', first
    # Immediates can run before quiescence, depending on the native event-loop
    # phase. The invariant here is no further effects AFTER the published pass.
    path = Path(reader.cwd, 'stale')
    before = path.read_bytes() if path.exists() else b''
    later = run(reader, "await new Promise(resolve => setTimeout(resolve, 80)); console.log('healthy');")
    assert later.status == 'pass' and later.stdout == 'healthy\n'
    assert (path.read_bytes() if path.exists() else b'') == before
    if 'Immediate' not in schedule:
        assert before == b''


def test_fired_timeout_refresh_gets_fresh_block_ownership(reader):
    first = run(reader, "let fired = 0; const reusable = setTimeout(() => fired++, 5); "
                "await new Promise(resolve => setTimeout(resolve, 10)); console.log(fired);")
    assert first.status == 'pass' and first.stdout == '1\n'
    refreshed = run(reader, "reusable.refresh().unref();")
    assert refreshed.status == 'pass', refreshed
    later = run(reader, "await new Promise(resolve => setTimeout(resolve, 40)); console.log(fired);")
    assert later.status == 'pass' and later.stdout == '1\n'


def test_module_import_scheduled_unref_work_is_owned(reader):
    Path(reader.cwd, 'scheduler.mjs').write_text("import {writeFileSync} from 'node:fs'; "
        "setInterval(() => writeFileSync('import-stale', 'bad'), 20).unref(); export const value = 42;")
    first = run(reader, "import {value} from './scheduler.mjs'; console.log(value);")
    assert first.status == 'pass' and first.stdout == '42\n'
    assert run(reader, "await new Promise(resolve => setTimeout(resolve, 80)); console.log(value);").stdout == '42\n'
    assert not Path(reader.cwd, 'import-stale').exists()


def test_owned_unref_listening_server_closes_before_publication(reader):
    first = run(reader, "const listener = require('node:net').createServer(socket => { "
        "require('node:fs').writeFileSync('accepted-stale', 'bad'); socket.end(); }); "
        "await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve)); "
        "const listenerPort = listener.address().port; listener.unref();")
    assert first.status == 'pass', first
    result = run(reader, "console.log(await new Promise(resolve => { "
        "const attempted = require('node:net').connect(listenerPort, '127.0.0.1'); "
        "attempted.on('error', error => resolve(error.code)); attempted.on('connect', () => { "
        "attempted.destroy(); resolve('unexpected-connect'); }); }));")
    assert result.status == 'pass' and result.stdout == 'ECONNREFUSED\n'
    assert not Path(reader.cwd, 'accepted-stale').exists()


@pytest.mark.parametrize('listen_first', [False, True])
def test_native_prelisten_unref_and_reused_server_keep_public_ref_state(reader, listen_first):
    setup = "let prelistenCloses = 0; const prelisten = require('node:net').createServer().unref(); " \
        "prelisten.on('close', () => prelistenCloses++); "
    listening = "await new Promise(resolve => prelisten.listen(0, '127.0.0.1', resolve)); "
    source = setup + (listening if listen_first else '') + "console.log('prepared');"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text("import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);\n" + source
        + ("await new Promise(resolve => prelisten.close(resolve)); " if listen_first else '')
        + listening + "console.log('native-reused', prelistenCloses);")
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0 and oracle.stdout == f'prepared\nnative-reused {int(listen_first)}\n', oracle
    first = run(reader, source)
    assert first.status == 'pass' and first.stdout == 'prepared\n', first
    # Native Server retains unref across close/listen. No second unref call may
    # be required to stop this newly listening server before another pass.
    reused = run(reader, listening + "const reusedPort = prelisten.address().port; console.log('reused', prelistenCloses);")
    assert reused.status == 'pass' and reused.stdout == f'reused {int(listen_first)}\n', reused
    result = run(reader, "console.log(await new Promise(resolve => { "
        "const s = require('node:net').connect(reusedPort, '127.0.0.1'); "
        "s.on('error', error => resolve(error.code)); s.on('connect', () => { "
        "s.destroy(); resolve('unexpected-connect'); }); }));")
    assert result.status == 'pass' and result.stdout == 'ECONNREFUSED\n', result


@pytest.mark.parametrize('user_listener', [False, True])
def test_repeated_inactive_unref_does_not_retain_owned_listeners_or_remove_user_listener(reader, user_listener):
    setup = "let inactiveCloseEffects = 0; const inactiveUserClose = () => inactiveCloseEffects++; " \
        "const repeatedInactive = require('node:net').createServer(); "
    if user_listener:
        setup += "repeatedInactive.on('close', inactiveUserClose); "
    inspect = "console.log(repeatedInactive.listenerCount('close'), " \
        "repeatedInactive.listeners('close').includes(inactiveUserClose), inactiveCloseEffects);"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text("import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);\n"
        + setup + "for (let i = 0; i < 12; i++) { repeatedInactive.unref(); " + inspect + " }")
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    expected = f"{int(user_listener)} {str(user_listener).lower()} 0\n"
    assert oracle.returncode == 0 and oracle.stdout == expected * 12 and oracle.stderr == '', oracle
    assert run(reader, setup).status == 'pass'
    for _ in range(12):
        completed = run(reader, "repeatedInactive.unref();")
        assert completed.status == 'pass' and completed.stderr == '', completed
        observed = run(reader, inspect)
        assert observed.status == 'pass' and observed.stdout == expected and observed.stderr == '', observed


@pytest.mark.parametrize('mode', ['output', 'error', 'timeout'])
def test_synchronous_cleanup_new_referenced_work_stays_in_original_block(reader, mode):
    callbacks = {
        'output': "setTimeout(() => { require('node:fs').appendFileSync('cleanup-once', 'once'); "
                  "console.log('cleanup-output'); }, 10);",
        'error': "setTimeout(() => { throw new Error('cleanup-async-error'); }, 10);",
        'timeout': "setInterval(() => {}, 1000);",
    }
    source = "const idleCleanup = require('node:net').createServer(); " \
        "idleCleanup.on('removeListener', kind => { if (kind === 'close') { " \
        + callbacks[mode] + " } }); idleCleanup.unref();"
    result = run(reader, source, timeout=0.5)
    if mode == 'output':
        assert result.status == 'pass' and result.stdout == 'cleanup-output\n', result
        assert Path(reader.cwd, 'cleanup-once').read_text() == 'once'
        later = run(reader, "console.log('healthy');")
        assert later.status == 'pass' and later.stdout == 'healthy\n'
        assert Path(reader.cwd, 'cleanup-once').read_text() == 'once'
    else:
        expected = 'cleanup-async-error' if mode == 'error' else 'timed out'
        assert result.status == 'fail' and expected in result.stderr + result.detail, result
        assert reader.js.broken and reader.js.proc.poll() is not None
        assert 'refusing to restart' in run(reader, "console.log('bad');").detail


def native_before_exit(reader, source):
    native = Path(reader.cwd, 'native-before-exit.mjs')
    native.write_text("import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);\n" + source)
    return subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)


def test_before_exit_listener_order_matches_native(reader):
    source = "process.on('beforeExit', () => console.log('normal')); " \
        "process.once('beforeExit', () => console.log('once')); " \
        "process.prependOnceListener('beforeExit', () => console.log('prepend'));"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 0 and oracle.stdout == 'prepend\nnormal\nonce\n', oracle
    result = run(reader, source)
    assert result.status == 'pass' and result.stdout == oracle.stdout, result


def test_before_exit_nested_nexttick_promise_timer_output_and_effect_are_retained_once(reader):
    source = "process.once('beforeExit', () => process.nextTick(() => Promise.resolve().then(() => " \
        "setTimeout(() => { require('node:fs').appendFileSync('before-exit-effect', 'once'); " \
        "console.log('nested-output'); }, 10))));"
    oracle = native_before_exit(reader, source)
    path = Path(reader.cwd, 'before-exit-effect')
    assert oracle.returncode == 0 and oracle.stdout == 'nested-output\n' and path.read_text() == 'once'
    path.unlink()
    result = run(reader, source)
    assert result.status == 'pass' and result.stdout == oracle.stdout and path.read_text() == 'once', result
    assert run(reader, "console.log('healthy');").stdout == 'healthy\n'
    assert path.read_text() == 'once'


def test_before_exit_repeats_for_real_referenced_work_without_extra_harness_events(reader):
    source = "let idleEvents = 0; const idleListener = () => { idleEvents++; console.log('idle', idleEvents); " \
        "if (idleEvents < 3) setTimeout(() => console.log('work', idleEvents), 10); }; " \
        "process.on('beforeExit', idleListener);"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 0 and oracle.stdout == 'idle 1\nwork 1\nidle 2\nwork 2\nidle 3\n', oracle
    result = run(reader, source)
    assert result.status == 'pass' and result.stdout == oracle.stdout, result
    later = run(reader, "process.removeListener('beforeExit', idleListener); console.log(idleEvents);")
    assert later.status == 'pass' and later.stdout == '3\n'


def test_before_exit_unref_work_is_owned_without_artificial_extra_events(reader):
    source = "let idleEvents = 0; const idleListener = () => { console.log('idle', ++idleEvents); " \
        "setTimeout(() => require('node:fs').writeFileSync('idle-stale', 'bad'), 20).unref(); }; " \
        "process.on('beforeExit', idleListener);"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 0 and oracle.stdout == 'idle 1\n' and not Path(reader.cwd, 'idle-stale').exists()
    result = run(reader, source)
    assert result.status == 'pass' and result.stdout == oracle.stdout, result
    later = run(reader, "process.removeListener('beforeExit', idleListener); "
        "await new Promise(resolve => setTimeout(resolve, 60)); console.log(idleEvents);")
    assert later.status == 'pass' and later.stdout == '1\n'
    assert not Path(reader.cwd, 'idle-stale').exists()


def test_before_exit_late_error_fails_original_block_like_native(reader):
    source = "process.once('beforeExit', () => setTimeout(() => { throw new Error('before-exit-late'); }, 10));"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 1 and 'before-exit-late' in oracle.stderr
    result = run(reader, source)
    assert result.status == 'fail' and 'before-exit-late' in result.stderr, result
    assert reader.js.broken and reader.js.proc.poll() is not None
    assert 'refusing to restart' in run(reader, "console.log('bad');").detail


def test_before_exit_repeated_reference_work_obeys_original_timeout(reader):
    result = run(reader, "process.on('beforeExit', () => setTimeout(() => {}, 10));", timeout=0.5)
    assert result.status == 'fail' and 'timed out' in result.detail, result
    assert reader.js.broken and reader.js.proc.poll() is not None


def test_before_exit_final_exit_status_matches_native(reader):
    source = "process.once('beforeExit', () => { process.exitCode = 7; });"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 7
    result = run(reader, source)
    assert result.status == 'fail' and result.exit_code == 7 and reader.js.broken, result


def test_before_exit_can_settle_top_level_await_before_completion(reader):
    source = "let resolveAtExit; const atExit = new Promise(resolve => { resolveAtExit = resolve; }); " \
        "process.once('beforeExit', () => resolveAtExit(42)); console.log(await atExit);"
    oracle = native_before_exit(reader, source)
    assert oracle.returncode == 0 and oracle.stdout == '42\n', oracle
    result = run(reader, source)
    assert result.status == 'pass' and result.stdout == oracle.stdout, result


def test_owned_unref_message_ports_close_before_next_block(reader):
    first = run(reader, "const channel = new (require('node:worker_threads').MessageChannel)(); "
        "channel.port1.on('message', () => require('node:fs').writeFileSync('port-stale', 'bad')); "
        "channel.port1.unref(); channel.port2.unref();")
    assert first.status == 'pass', first
    next_block = run(reader, "channel.port2.postMessage('late'); "
        "await new Promise(resolve => setTimeout(resolve, 30)); console.log('healthy');")
    assert next_block.status == 'pass' and next_block.stdout == 'healthy\n'
    assert not Path(reader.cwd, 'port-stale').exists()


def test_owned_unref_watcher_closes_before_later_file_change(reader):
    Path(reader.cwd, 'watched').write_text('initial')
    first = run(reader, "const watchHandle = require('node:fs').watch('watched', {persistent:false}, "
        "() => require('node:fs').writeFileSync('watch-stale', 'bad'));")
    assert first.status == 'pass', first
    next_block = run(reader, "require('node:fs').writeFileSync('watched', 'later'); "
        "await new Promise(resolve => setTimeout(resolve, 30)); console.log('healthy');")
    assert next_block.status == 'pass' and next_block.stdout == 'healthy\n'
    assert not Path(reader.cwd, 'watch-stale').exists()


def test_owned_unref_socket_cannot_receive_native_io_after_pass(reader):
    from resource_rehearsal import socket_after_pass_control
    socket_after_pass_control(reader)


def test_cleanup_callbacks_are_captured_before_pass_with_explicit_native_difference(reader):
    source = "const s = require('node:net').createServer(); " \
        "await new Promise(resolve => s.listen(0, '127.0.0.1', resolve)); " \
        "s.on('close', () => console.log('cleanup-close')); s.unref();"
    native = Path(reader.cwd, 'native.mjs')
    native.write_text("import {createRequire} from 'node:module'; const require=createRequire(import.meta.url);\n" + source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0 and oracle.stdout == ''
    result = run(reader, source)
    # Deliberate bounded cleanup contract, NOT preservation of native process exit.
    assert result.status == 'pass' and result.stdout == 'cleanup-close\n'


@pytest.mark.parametrize('callback,expected', [
    ("throw new Error('cleanup failure');", 'cleanup failure'),
    ("setInterval(() => {}, 1000);", 'timed out'),
])
def test_cleanup_errors_or_nondraining_callbacks_fail_original_block(reader, callback, expected):
    result = run(reader, "const s = require('node:net').createServer(); "
        "await new Promise(resolve => s.listen(0, '127.0.0.1', resolve)); "
        f"s.on('close', () => {{ {callback} }}); s.unref();", timeout=0.5)
    assert result.status == 'fail' and expected in result.stderr + result.detail
    assert reader.js.broken and reader.js.proc.poll() is not None
    assert 'refusing to restart' in run(reader, "console.log('bad');").detail


def test_unsupported_owned_unref_udp_refuses_completion(reader):
    result = run(reader, "const udp = require('node:dgram').createSocket('udp4'); "
        "await new Promise(resolve => udp.bind(0, '127.0.0.1', resolve)); udp.unref();")
    assert result.status == 'fail' and 'unsupported owned unref resource: UDP' in result.stderr
    assert reader.js.broken


@pytest.mark.parametrize('source,kind', [
    ("require('node:fs').watchFile('watched', {persistent:false}, () => {});", 'StatWatcher'),
    ("const stat = require('node:fs').watchFile('watched', () => {}); stat.unref();", 'StatWatcher'),
    ("require('node:fs/promises').watch('watched', {persistent:false});", 'FSWatchIterator'),
])
def test_unsupported_unref_watcher_forms_fail_even_when_caught(reader, source, kind):
    Path(reader.cwd, 'watched').write_text('initial')
    result = run(reader, "try { " + source + " } catch (error) { console.log('caught'); } "
        "require('node:fs').unwatchFile('watched');")
    assert result.status == 'fail' and f'unsupported owned unref resource: {kind}' in result.stderr
    assert reader.js.broken


def test_native_valid_dormant_udp_is_explicitly_unsupported_not_a_false_pass(reader):
    source = "const inactive = require('node:dgram').createSocket('udp4');"
    native = Path(reader.cwd, 'native.cjs')
    native.write_text(source)
    oracle = subprocess.run(['node', str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0 and oracle.stdout == '' and oracle.stderr == ''
    # Temporary partial-support limitation, not native-equivalent execution.
    result = run(reader, source)
    assert result.status == 'fail' and 'unsupported owned dormant resource: UDP' in result.stderr
    assert reader.js.broken and reader.js.proc.poll() is not None
    assert 'refusing to restart' in run(reader, "console.log('bad');").detail


@pytest.mark.parametrize('source,kind', [
    ("require('node:child_process').spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], "
     "{stdio:'ignore'}).unref();", 'ChildProcess'),
    ("new (require('node:worker_threads').Worker)('setInterval(() => {}, 1000)', {eval:true}).unref();", 'Worker'),
])
def test_unsupported_owned_unref_children_and_workers_fail_before_pass(reader, source, kind):
    result = run(reader, source)
    assert result.status == 'fail' and f'unsupported owned unref resource: {kind}' in result.stderr
    assert reader.js.broken and reader.js.proc.poll() is not None


@pytest.mark.parametrize("declaration,module_source", [
    ("import { missing } from './exports.mjs';", "export const present = 42;"),
    ("import { missing as unused } from './exports.mjs';", "export const present = 42;"),
    ("import absent from './exports.mjs';", "export const present = 42;"),
    ("import absent, * as namespace from './exports.mjs';", "export const present = 42;"),
    ("import { toString } from './exports.mjs';", "export const present = 42;"),
    ("import present, { missing } from './exports.mjs';", "export default 42;"),
])
def test_absent_static_exports_fail_like_native_esm_before_reader_effects(reader, declaration, module_source):
    Path(reader.cwd, "exports.mjs").write_text(module_source)
    native = Path(reader.cwd, "native.mjs")
    native.write_text(declaration + "\n(await import('node:fs')).writeFileSync('native-effect', 'bad');")
    oracle = subprocess.run(["node", str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode != 0 and "SyntaxError" in oracle.stderr
    assert not Path(reader.cwd, "native-effect").exists()
    result = run(reader, declaration + "\n(await import('node:fs')).writeFileSync('worker-effect', 'bad');")
    assert result.status == "fail" and result.command_failed
    assert "SyntaxError" in result.stderr and "export" in result.stderr
    assert not Path(reader.cwd, "worker-effect").exists()


def test_existing_undefined_exports_and_namespace_default_are_valid(reader):
    Path(reader.cwd, "exports.mjs").write_text("export const value = undefined; export default undefined;")
    result = run(reader, "import first, { value as named } from './exports.mjs';\n"
                 "import second, * as namespace from './exports.mjs';\n"
                 "console.log(first, named, second, namespace.value);")
    assert result.status == "pass", result.stderr
    assert result.stdout == "undefined undefined undefined undefined\n"


@pytest.mark.parametrize("separator", [" ", "\n", " /* comment */ ", "\n// comment\n"])
def test_leading_dynamic_import_whitespace_matches_native_esm(reader, separator):
    Path(reader.cwd, "plugin.mjs").write_text("export const value = 42;")
    code = "import" + separator + "('./plugin.mjs').then(module => console.log(module.value));"
    native = Path(reader.cwd, "native.mjs")
    native.write_text(code)
    oracle = subprocess.run(["node", str(native)], cwd=reader.cwd, capture_output=True, text=True, timeout=5)
    assert oracle.returncode == 0 and oracle.stdout == "42\n", oracle.stderr
    result = run(reader, code)
    assert result.status == "pass", result.stderr
    assert result.stdout == oracle.stdout
