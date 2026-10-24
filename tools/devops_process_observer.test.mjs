// Actual local process observations only; no native/cloud qualification.
import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, readFile, rm, open } from 'node:fs/promises';
import { constants, promises as fsPromises } from 'node:fs';
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

async function retainObservation(name, value) {
  const directory = join(process.cwd(), 'artifacts', 'devops-source-controls');
  await mkdir(directory, { recursive: true });
  const original = await mkdtemp(join(directory, name + '-'));
  await writeFile(join(original, 'observations.json'), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
}

test('actual exit between stat and cmdline requires a fresh absence observation', async () => {
  const child = spawn('/bin/bash', ['-c', 'read value'], { stdio: ['pipe', 'ignore', 'pipe'] });
  const exited = once(child, 'exit');
  let hook;
  try {
    await once(child, 'spawn');
    const admission = await observeProcess(child.pid), reads = [];
    const originalOpen = fsPromises.open;
    let triggered = false;
    hook = mock.method(fsPromises, 'open', async (path, ...args) => {
      reads.push(path);
      if (path === `/proc/${child.pid}/cmdline` && !triggered) {
        triggered = true;
        child.stdin.end('owned-normal-exit\n'); await exited;
      }
      return originalOpen(path, ...args);
    });
    const observed = await observeProcess(child.pid);
    assert.equal(triggered, true);
    assert.equal(observed.status, 'absent');
    assert.equal(reads.filter(path => path === `/proc/${child.pid}/stat`).length, 2);
    assert.equal(terminalObservation(observed, { ...admission.identity, bootId: admission.bootId }).terminal, true);
    assert.equal(isTerminalWitness(observed, admission), false);
    await retainObservation('exit-between-reads', { admission, observed, reads });
  } finally {
    hook?.mock.restore();
    if (!child.stdin.destroyed) child.stdin.end();
    await exited;
  }
});

test('missing mid-read data has a finite retry bound and access errors never mean absence', async () => {
  const admission = await observeProcess(process.pid), originalOpen = fsPromises.open;
  const outcomes = [];
  for (const code of ['ENOENT', 'ESRCH', 'EACCES', 'EIO']) {
    let attempts = 0;
    const hook = mock.method(fsPromises, 'open', async (path, ...args) => {
      if (path === `/proc/${process.pid}/cmdline`) {
        attempts += 1;
        throw Object.assign(new Error('controlled read fault'), { code });
      }
      return originalOpen(path, ...args);
    });
    try {
      await assert.rejects(observeProcess(process.pid), /process-observation-(raced|unverified)/);
      assert.equal(attempts, ['ENOENT', 'ESRCH'].includes(code) ? 3 : 1);
      outcomes.push({ code, attempts, verdict: 'refused' });
    } finally { hook.mock.restore(); }
  }
  const current = await observeProcess(process.pid);
  assert.equal(terminalObservation(current, { ...admission.identity, bootId: admission.bootId }).terminal, false);
  await retainObservation('read-fault-refusals', { admission, current, outcomes });
});

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
  let observationHook;
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
    // End the real wrapper between its already-admitted child's stat/cmdline
    // reads. No process bytes are mocked, and the child remains blocked on its
    // owned FIFO until the later explicit normal completion.
    const originalOpen = fsPromises.open;
    let reparented = false;
    observationHook = mock.method(fsPromises, 'open', async (path, ...args) => {
      if (path === `/proc/${descendantPid}/cmdline` && !reparented) {
        reparented = true;
        child.stdin.end('owned-wrapper-exit\n'); await exited;
      }
      return originalOpen(path, ...args);
    });
    await assert.rejects(lease.observeTerminal(), /descendant-still-live/);
    assert.equal(reparented, true);
    observationHook.mock.restore(); observationHook = null;
    const stillLive = await observeProcess(descendantPid);
    const originalChild = lease.observations().find(entry => entry.admission.pid === descendantPid);
    assert.equal(originalChild.parentBirth.pid, child.pid);
    assert.notEqual(stillLive.identity.parentPid, child.pid);
    assert.equal(terminalObservation(stillLive, { ...originalChild.admission.identity,
      bootId: originalChild.admission.bootId }).terminal, false);
    await finishDescendant();
    let witness;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { witness = await lease.observeTerminal(); break; }
      catch (error) { if (!/still-live/.test(error.message)) throw error; await delay(); }
    }
    assert.ok(witness);
    assert.equal(isTerminalWitness(witness, native), true);
    assert.ok(witness.executorTree.some(entry => entry.admission.pid === descendantPid && entry.terminal));
    await retainObservation('tracked-child-reparenting', { native, originalChild, stillLive, witness });
  } finally {
    observationHook?.mock.restore();
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
