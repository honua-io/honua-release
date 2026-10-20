// Linux read-only birth-bound observations for the owned native/executor tree.
// No signal, command, retry, reclaim or process mutation is provided here.
import { open } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const fail = code => { throw Error(code); };
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const pidValue = value => Number.isSafeInteger(value) && value > 0;
const terminalWitnesses = new WeakMap();

async function bounded(path, limit) {
  const file = await open(path, 'r');
  try {
    const buffer = Buffer.alloc(limit + 1);
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
    if (bytesRead > limit) fail('process-observation-bound');
    return buffer.subarray(0, bytesRead);
  } finally { await file.close(); }
}

export function parseStat(raw, expectedPid) {
  if (!Buffer.isBuffer(raw) || raw.length > 65536 || !pidValue(expectedPid)) fail('process-stat-input');
  const value = raw.toString('utf8'), close = value.lastIndexOf(')');
  if (!value.startsWith(expectedPid + ' (') || close < 0) fail('process-stat-identity');
  const fields = value.slice(close + 1).trim().split(/\s+/);
  if (fields.length < 20 || !/^[A-Za-z]$/.test(fields[0]) || !/^\d+$/.test(fields[1])
      || !/^[1-9]\d*$/.test(fields[19])) fail('process-stat-shape');
  const parentPid = Number(fields[1]), startTicks = fields[19];
  if (!Number.isSafeInteger(parentPid) || parentPid < 0) fail('process-stat-parent');
  return { pid: expectedPid, parentPid, startTicks, state: fields[0] };
}

export async function observeProcess(pid) {
  if (process.platform !== 'linux' || !pidValue(pid)) fail('process-observation-host-pid');
  const bootRaw = await bounded('/proc/sys/kernel/random/boot_id', 128);
  const bootId = bootRaw.toString('ascii').trim();
  if (!/^[a-f0-9-]{36}$/.test(bootId)) fail('process-observation-boot');
  let first;
  try { first = await bounded('/proc/' + pid + '/stat', 65536); }
  catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ESRCH')
      return { schema: 'honua.owned-process-observation/v1', bootId, pid, status: 'absent',
        statBase64: null, cmdlineBase64: null, identity: null };
    // AccessDenied/I/O failures are not absence.
    fail('process-observation-unverified');
  }
  const identity = parseStat(first, pid);
  let command, last;
  try {
    command = await bounded('/proc/' + pid + '/cmdline', 2 * 1024 * 1024);
    last = await bounded('/proc/' + pid + '/stat', 65536);
  } catch { fail('process-observation-raced'); }
  const after = parseStat(last, pid);
  if (identity.startTicks !== after.startTicks || identity.parentPid !== after.parentPid)
    fail('process-observation-raced');
  return { schema: 'honua.owned-process-observation/v1', bootId, pid, status: 'observed',
    statBase64: last.toString('base64'), cmdlineBase64: command.toString('base64'),
    identity: { ...after, cmdlineSha256: sha(command) } };
}

export function terminalObservation(original, expected) {
  // Expected is independently captured at admission from the owned native
  // transport/claim and its ancestry, not inferred from this terminal record.
  if (!expected || !pidValue(expected.pid) || typeof expected.startTicks !== 'string'
      || !/^[1-9]\d*$/.test(expected.startTicks) || typeof expected.bootId !== 'string'
      || original?.schema !== 'honua.owned-process-observation/v1'
      || original.pid !== expected.pid || original.bootId !== expected.bootId)
    fail('process-terminal-identity');
  if (original.status === 'absent') {
    if (original.identity !== null || original.statBase64 !== null || original.cmdlineBase64 !== null)
      fail('process-terminal-absence-shape');
    return { terminal: true, reason: 'original-pid-absent' };
  }
  if (original.status !== 'observed' || typeof original.statBase64 !== 'string'
      || typeof original.cmdlineBase64 !== 'string') fail('process-terminal-unverified');
  const raw = Buffer.from(original.statBase64, 'base64'), parsed = parseStat(raw, expected.pid);
  if (raw.toString('base64') !== original.statBase64
      || Buffer.from(original.cmdlineBase64, 'base64').toString('base64') !== original.cmdlineBase64)
    fail('process-terminal-encoding');
  if (JSON.stringify(parsed) !== JSON.stringify({ pid: original.identity?.pid,
    parentPid: original.identity?.parentPid, startTicks: original.identity?.startTicks,
    state: original.identity?.state }) || sha(Buffer.from(original.cmdlineBase64, 'base64')) !== original.identity?.cmdlineSha256)
    fail('process-terminal-raw-join');
  if (parsed.startTicks !== expected.startTicks) return { terminal: true, reason: 'original-birth-no-longer-present' };
  return { terminal: parsed.state === 'Z' || parsed.state === 'X',
    reason: parsed.state === 'Z' || parsed.state === 'X' ? 'original-exited' : 'original-still-live' };
}

export function isTerminalWitness(witness, nativeAdmission) {
  const bound = witness && terminalWitnesses.get(witness);
  return !!bound && bound.snapshot === sha(Buffer.from(JSON.stringify(witness)))
    && bound.pid === nativeAdmission?.pid && bound.bootId === nativeAdmission?.bootId
    && bound.startTicks === nativeAdmission?.identity?.startTicks;
}

export async function bindExecutorToNative(executorPid, nativeAdmission, expectedApplyScript, claimContext) {
  if (!nativeAdmission || nativeAdmission.status !== 'observed') fail('process-native-admission');
  if (executorPid === nativeAdmission.pid || typeof expectedApplyScript !== 'string'
      || !expectedApplyScript.startsWith('/') || !expectedApplyScript.endsWith('/scripts/terraform-exact-apply.sh'))
    fail('process-executor-script-context');
  const executor = await observeProcess(executorPid);
  if (executor.status !== 'observed' || ['Z', 'X'].includes(executor.identity.state)) fail('process-executor-not-live');
  const argv = Buffer.from(executor.cmdlineBase64, 'base64').toString('utf8').split('\0');
  if (!['bash', '/bin/bash', '/usr/bin/bash'].includes(argv[0]) || argv[1] !== expectedApplyScript)
    fail('process-executor-script-binding');
  if (!claimContext || !/^urn:honua:provisioning:[a-f0-9]{32}$/.test(claimContext.operationId ?? '')
      || typeof claimContext.claimDirectory !== 'string' || !claimContext.claimDirectory.endsWith(
        '/cleanup-obligations/' + sha(Buffer.from(claimContext.operationId)) + '/claim')
      || typeof claimContext.planPath !== 'string' || !claimContext.planPath.startsWith('/')
      || !['apply', 'destroy'].includes(claimContext.action)) fail('process-executor-claim-context');
  for (const [flag, expected] of [['--claim-dir', claimContext.claimDirectory],
    ['--plan', claimContext.planPath], ['--action', claimContext.action]]) {
    if (argv.filter(value => value === flag).length !== 1 || argv[argv.indexOf(flag) + 1] !== expected)
      fail('process-executor-claim-argv');
  }
  const ancestry = [executor];
  let current = executor;
  for (let depth = 0; depth < 32; depth++) {
    if (current.pid === nativeAdmission.pid) {
      if (current.bootId !== nativeAdmission.bootId
          || current.identity.startTicks !== nativeAdmission.identity.startTicks
          || current.identity.cmdlineSha256 !== nativeAdmission.identity.cmdlineSha256)
        fail('process-native-birth-substitution');
      const native = structuredClone(nativeAdmission), admitted = structuredClone(executor);
      const tree = new Map();
      const key = row => row.pid + ':' + row.identity.startTicks;
      tree.set(key(admitted), { admission: admitted, parentBirth: null, terminal: null });
      let invalid = false, complete = false, scanTail = Promise.resolve();
      async function scan() {
        if (invalid) fail('process-tree-observation-invalid');
        // Retain each discovered birth and its actual parent edge. Continue to
        // follow known children after wrapper exit, including newly forked
        // descendants of those still-live children. No process is signalled.
        try {
          const queue = [...tree.values()];
          for (let index = 0; index < queue.length; index++) {
            const entry = queue[index];
            const observed = await observeProcess(entry.admission.pid);
            const result = terminalObservation(observed, { ...entry.admission.identity, bootId: entry.admission.bootId });
            if (result.terminal) { entry.terminal = observed; continue; }
            entry.terminal = null;
            let rawChildren;
            try { rawChildren = await bounded('/proc/' + observed.pid + '/task/' + observed.pid + '/children', 65536); }
            catch (error) {
              if (error.code === 'ENOENT' || error.code === 'ESRCH') {
                const ended = await observeProcess(observed.pid);
                if (terminalObservation(ended, { ...entry.admission.identity, bootId: entry.admission.bootId }).terminal) {
                  entry.terminal = ended; continue;
                }
              }
              fail('process-tree-children-unverified');
            }
            const text = rawChildren.toString('ascii').trim();
            if (text && !/^[1-9]\d*(?:\s+[1-9]\d*)*$/.test(text)) fail('process-tree-children-shape');
            const children = text ? text.split(/\s+/).map(Number) : [];
            if (new Set(children).size !== children.length) fail('process-tree-children-duplicate');
            for (const pid of children) {
              if (!pidValue(pid)) fail('process-tree-child-pid');
              const child = await observeProcess(pid);
              if (child.status === 'absent') continue; // This observed PID already ceased.
              const parentAfter = await observeProcess(observed.pid);
              if (parentAfter.status !== 'observed'
                  || parentAfter.identity.startTicks !== observed.identity.startTicks
                  || child.identity.parentPid !== observed.pid) fail('process-tree-parent-race');
              const childKey = key(child);
              if (!tree.has(childKey)) {
                if (tree.size >= 512) fail('process-tree-bound');
                const childEntry = { admission: child, parentBirth: {
                  pid: observed.pid, startTicks: observed.identity.startTicks, bootId: observed.bootId }, terminal: null };
                tree.set(childKey, childEntry); queue.push(childEntry);
              }
            }
          }
          complete = [...tree.values()].every(entry => entry.terminal !== null);
        } catch (error) { invalid = true; throw error; }
      }
      function scanSerialized() {
        const current = scanTail.then(scan);
        scanTail = current.catch(() => {});
        return current;
      }
      await scanSerialized();
      // Observation cadence only, NOT an executor deadline/cancellation timer.
      // Keep this owned monitor alive until actual tracked births are terminal.
      const monitor = async () => {
        while (!complete && !invalid) {
          await new Promise(resolve => setTimeout(resolve, 100));
          try { await scanSerialized(); } catch { return; }
        }
      };
      void monitor();
      return { executor, ancestry,
        observations() { return structuredClone([...tree.values()]); },
        async observeTerminal() {
          await scanSerialized();
          if (!complete) fail('process-executor-or-descendant-still-live');
          const observed = tree.get(key(admitted)).terminal;
          const result = terminalObservation(observed, { ...admitted.identity, bootId: admitted.bootId });
          const witness = Object.freeze({ nativeAdmission: native, admission: admitted,
            terminal: observed, result, capturedMonotonicNs: process.hrtime.bigint().toString(),
            ancestry: structuredClone(ancestry),
            claimContext: structuredClone(claimContext), executorTree: structuredClone([...tree.values()]) });
          terminalWitnesses.set(witness, { snapshot: sha(Buffer.from(JSON.stringify(witness))),
            pid: native.pid, bootId: native.bootId, startTicks: native.identity.startTicks });
          return witness;
        } };
    }
    const parent = current.identity.parentPid;
    if (!pidValue(parent) || ancestry.some(row => row.pid === parent)) fail('process-executor-not-owned');
    current = await observeProcess(parent);
    if (current.status !== 'observed') fail('process-ancestry-unverified');
    ancestry.push(current);
  }
  fail('process-ancestry-bound');
}
