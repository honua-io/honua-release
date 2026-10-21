// Actual local process observations only; no native/cloud qualification.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, readFile, rm, open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { promisify } from 'node:util';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { observeProcess, terminalObservation, bindExecutorToNative, isTerminalWitness } from './devops_process_observer.mjs';
import { verifyReconciliationFacts } from './devops_owned_tool_calls.mjs';
const OP = 'urn:honua:provisioning:' + '1'.repeat(32);
function claim(root) {
  return { operationId: OP, claimDirectory: join(root, 'cleanup-obligations',
    createHash('sha256').update(OP).digest('hex'), 'claim'), planPath: join(root, 'honua.tfplan'), action: 'apply' };
}
const claimArgs = value => ['--claim-dir', value.claimDirectory, '--plan', value.planPath, '--action', value.action];
const delay = () => new Promise(resolve => setTimeout(resolve, 20));

test('raw live stat/cmdline/boot originals cannot be counted as terminal or foreign birth', async () => {
  const raw = await observeProcess(process.pid);
  const expected = { ...raw.identity, bootId: raw.bootId };
  assert.equal(terminalObservation(raw, expected).terminal, false);
  const changed = structuredClone(raw); changed.identity.startTicks = '1';
  assert.throws(() => terminalObservation(changed, expected), /raw-join/);
  assert.throws(() => terminalObservation({ ...raw, bootId: 'foreign' }, expected), /identity/);
  const malformed = structuredClone(raw); malformed.statBase64 += '!';
  assert.throws(() => terminalObservation(malformed, expected), /encoding/);
  assert.equal(isTerminalWitness({ terminal: true }, raw), false);
});

test('wrapper exit cannot release ownership while an actually tracked child survives', async () => {
  const root = await mkdtemp(join(tmpdir(), 'process-descendant-source-'));
  const scripts = join(root, 'scripts'); await mkdir(scripts);
  const script = join(scripts, 'terraform-exact-apply.sh'), childScript = join(root, 'child.sh');
  const fifo = join(root, 'owned-child-input'), pidFile = join(root, 'owned-child-pid');
  await promisify(execFile)('/usr/bin/mkfifo', ['-m', '600', fifo]);
  await writeFile(childScript, 'read value < "$1"\n', { mode: 0o600 });
  await writeFile(script, '/bin/bash "$SOURCE_CHILD_SCRIPT" "$SOURCE_FIFO" &\nprintf "%s\\n" "$!" > "$SOURCE_PID_FILE"\nread value\n', { mode: 0o600 });
  const context = claim(root);
  const child = spawn('/bin/bash', [script, ...claimArgs(context)], { stdio: ['pipe', 'ignore', 'pipe'],
    env: { PATH: '/usr/bin:/bin', SOURCE_CHILD_SCRIPT: childScript, SOURCE_FIFO: fifo, SOURCE_PID_FILE: pidFile } });
  const exited = once(child, 'exit');
  let descendantPid;
  async function finishDescendant() {
    // Owned FIFO only. NONBLOCK prevents a failed fixture from hanging cleanup
    // if the original child has already ceased; no PID signal is ever sent.
    try {
      const file = await open(fifo, constants.O_WRONLY | constants.O_NONBLOCK);
      try { await file.write('owned-normal-child-exit\n'); } finally { await file.close(); }
    } catch (error) { if (error.code !== 'ENXIO') throw error; }
  }
  try {
    await once(child, 'spawn');
    for (let attempt = 0; attempt < 100; attempt++) {
      try { descendantPid = Number((await readFile(pidFile, 'ascii')).trim()); break; }
      catch (error) { if (error.code !== 'ENOENT') throw error; await delay(); }
    }
    assert.ok(Number.isSafeInteger(descendantPid) && descendantPid > 0);
    const native = await observeProcess(process.pid);
    const lease = await bindExecutorToNative(child.pid, native, script, context);
    assert.ok(lease.observations().some(entry => entry.admission.pid === descendantPid));
    child.stdin.end('owned-wrapper-exit\n'); await exited;
    await assert.rejects(lease.observeTerminal(), /descendant-still-live/);
    await finishDescendant();
    let witness;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { witness = await lease.observeTerminal(); break; }
      catch (error) { if (!/still-live/.test(error.message)) throw error; await delay(); }
    }
    assert.ok(witness);
    assert.equal(isTerminalWitness(witness, native), true);
    assert.ok(witness.executorTree.some(entry => entry.admission.pid === descendantPid && entry.terminal));
  } finally {
    if (!child.stdin.destroyed) child.stdin.end();
    await exited;
    await finishDescendant();
    await rm(root, { recursive: true, force: true });
  }
});

test('actual owned bash ancestry, live refusal and normal exit produce a nonforgeable terminal witness', async () => {
  const root = await mkdtemp(join(tmpdir(), 'process-source-'));
  const scripts = join(root, 'scripts'); await mkdir(scripts);
  const script = join(scripts, 'terraform-exact-apply.sh');
  // Source control only: a shell builtin read, no Terraform/AWS or grandchildren.
  await writeFile(script, 'read value\n', { mode: 0o600 });
  const context = claim(root);
  const child = spawn('/bin/bash', [script, ...claimArgs(context)], { stdio: ['pipe', 'ignore', 'pipe'] });
  const exited = once(child, 'exit');
  try {
    await once(child, 'spawn');
    const native = await observeProcess(process.pid);
    const admittedMonotonicNs = process.hrtime.bigint().toString();
    const lease = await bindExecutorToNative(child.pid, native, script, context);
    await assert.rejects(lease.observeTerminal(), /still-live/);
    await assert.rejects(bindExecutorToNative(child.pid, native, '/foreign/scripts/terraform-exact-apply.sh', context), /script-binding/);
    await assert.rejects(bindExecutorToNative(process.pid, native, script, context), /script-context/);
    await assert.rejects(bindExecutorToNative(child.pid, native, script,
      { ...context, planPath: '/foreign/plan' }), /claim-argv/);
    child.stdin.end('owned-normal-exit\n');
    await exited;
    const witness = await lease.observeTerminal();
    assert.equal(isTerminalWitness(witness, native), true);
    const entry = { mutating: true, operationId: context.operationId, action: context.action, admittedMonotonicNs };
    const original = { monotonicNs: process.hrtime.bigint().toString() };
    assert.equal(verifyReconciliationFacts(entry, witness, original), true);
    for (const changed of [
      { ...entry, operationId: 'urn:honua:provisioning:' + '2'.repeat(32) },
      { ...entry, action: 'destroy' },
      { ...entry, admittedMonotonicNs: (BigInt(witness.capturedMonotonicNs) + 1n).toString() }
    ]) assert.throws(() => verifyReconciliationFacts(changed, witness, original));
    assert.throws(() => verifyReconciliationFacts(entry, witness,
      { monotonicNs: (BigInt(witness.capturedMonotonicNs) - 1n).toString() }), /chronology/);
    assert.equal(isTerminalWitness(structuredClone(witness), native), false);
    assert.equal(isTerminalWitness(witness, { ...native, pid: native.pid + 1 }), false);
    witness.terminal.status = 'forged';
    assert.equal(isTerminalWitness(witness, native), false);
  } finally {
    // Only complete the fixture's owned stdin; no PID lookup or signals.
    if (!child.stdin.destroyed) child.stdin.end();
    await exited;
    await rm(root, { recursive: true, force: true });
  }
});
