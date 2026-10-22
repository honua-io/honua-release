// Actual Linux census originals, rejudged by the production pure consumer.
// Source controls only: the fixture is not a published DevOps binary.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { verifyScopeOriginal, isScopeWitness } from './devops_process_scope.mjs';

const clone = value => structuredClone(value);
const context = original => ({ launcherAdmission: original.launcherAdmission,
  nativeAdmission: original.nativeAdmission, nonce: original.nonce,
  pidNamespace: original.pidNamespace, launchBinding: { ...original.launchBinding,
    scopeDirectory: original.launcherCwd,
    nativeArgv: Buffer.from(original.nativeAdmission.cmdlineBase64, 'base64').toString().split('\0').slice(0, -1) } });
function bind(original) {
  const result = clone(original);
  const expected = context(result);
  result.launchBinding = clone(expected.launchBinding);
  return { original: result, expected };
}

test('real adopted setsid child and slow normal drain remain nonclosed until actual exit', async () => {
  const child = spawn('/usr/bin/python3', [fileURLToPath(new URL('./test_devops_native_scope.py', import.meta.url)),
    '--source-control-worker'], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = ''; child.stderr.on('data', data => { stderr += data; });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const receive = async () => {
    const next = await lines.next(); assert.equal(next.done, false, stderr);
    return JSON.parse(next.value);
  };
  const send = value => child.stdin.write(value + '\n');
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  try {
    const live = bind((await receive()).original);
    assert.equal(verifyScopeOriginal(live.original, live.expected).closed, false);
    // Scheduling/accounting fields can change without changing identity or
    // live/terminal classification; preserve raw records but compare ownership.
    const accounting = clone(live.original);
    const raw = Buffer.from(accounting.nativeCurrent.statBase64, 'base64').toString();
    const close = raw.lastIndexOf(')');
    const fields = raw.slice(close + 1).trim().split(/\s+/);
    fields[11] = (BigInt(fields[11]) + 1n).toString();
    accounting.nativeCurrent.statBase64 = Buffer.from(raw.slice(0, close + 1) + ' ' + fields.join(' ') + '\n').toString('base64');
    assert.equal(verifyScopeOriginal(accounting, live.expected).closed, false);
    for (const [label, mutate] of [
      ['contradictory final native', value => { value.nativeCurrent = { pid: value.nativeAdmission.pid, status: 'absent', statBase64: null, cmdlineBase64: null }; }],
      ['missing census row', value => value.observations.pop()],
      ['duplicate PID', value => value.enumeratedPids.push(value.enumeratedPids[0])],
      ['wrong namespace', value => { value.pidNamespace = 'pid:[1]'; }],
      ['wrong native birth', value => { value.nativeAdmission.identity.startTicks += '1'; }],
      ['wrong session owner', value => { value.launcherCwd += '/foreign'; }],
      ['missing child custody', value => { value.taskChildrenAfter = []; }],
      ['wrong source binding', value => { value.launchBinding.sourceControlOnly = false; }]
    ]) {
      const bad = clone(live.original); mutate(bad);
      assert.throws(() => verifyScopeOriginal(bad, live.expected), undefined, label);
    }
    assert.equal(isScopeWitness({ ...live.original, closed: true }, live.original.nativeAdmission), false);
    send('observe');
    const still = bind((await receive()).original);
    assert.equal(verifyScopeOriginal(still.original, still.expected).closed, false);
    send('release-escape');
    const quiet = bind((await receive()).original);
    assert.equal(verifyScopeOriginal(quiet.original, quiet.expected).closed, true);
    // Pure rejudging does not manufacture a branded live ownership witness.
    assert.equal(isScopeWitness(quiet.original, quiet.original.nativeAdmission), false);
    send('drain');
    const slow = bind((await receive()).original);
    assert.equal(verifyScopeOriginal(slow.original, slow.expected).closed, false);
    const terminal = bind((await receive()).original);
    const verdict = verifyScopeOriginal(terminal.original, terminal.expected);
    assert.equal(verdict.closed, true); assert.equal(verdict.nativeTerminal, true);
    assert.ok((await receive()).waitedSeconds > 2);
    child.stdin.end();
    assert.deepEqual(await exited, { code: 0, signal: null }); assert.equal(stderr, '');
  } finally {
    child.stdin.end(); // normal fixture EOF release, never kill on test failure
    await exited;
  }
});
