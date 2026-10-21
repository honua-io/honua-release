// Fixed published-native launcher over the canonical SDK transport. Only the
// private read-only census socket is implemented here; no signal/command API.
import { randomBytes, createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, readlink, lstat, realpath } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { observeProcess, parseStat, terminalObservation } from './devops_process_observer.mjs';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SCHEMA = 'honua.owned-process-scope/v1';
const MAX = 64 * 1024 * 1024;
const contexts = new WeakMap(), witnesses = new WeakMap();
const fail = code => { throw Error(code); };
const hash = raw => createHash('sha256').update(raw).digest('hex');
const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
const raw64 = text => {
  if (typeof text !== 'string') fail('scope-original-encoding');
  const raw = Buffer.from(text, 'base64');
  if (raw.toString('base64') !== text) fail('scope-original-encoding');
  return raw;
};
const key = row => row.pid + ':' + row.startTicks;

function scopeStat(row) {
  const raw = raw64(row.statBase64), value = parseStat(raw, row.pid);
  const fields = raw.toString('utf8').slice(raw.toString('utf8').lastIndexOf(')') + 1).trim().split(/\s+/);
  if (![fields[2], fields[3]].every(text => /^\d+$/.test(text))) fail('scope-stat-session');
  return { ...value, processGroup: Number(fields[2]), sessionId: Number(fields[3]) };
}

function admission(value, expected) {
  if (value?.schema !== 'honua.owned-process-observation/v1' || value.status !== 'observed'
      || value.bootId !== expected.bootId || value.pid !== expected.pid) fail('scope-admission-identity');
  terminalObservation(value, { ...expected.identity, bootId: expected.bootId });
  const parsed = scopeStat(value);
  if (parsed.startTicks !== expected.identity.startTicks || value.identity.cmdlineSha256 !== expected.identity.cmdlineSha256)
    fail('scope-admission-birth');
  return parsed;
}

export function verifyScopeOriginal(original, { launcherAdmission, nativeAdmission = null, nonce, launchBinding, pidNamespace }) {
  if (original?.schema !== SCHEMA || original.nonce !== nonce || original.subreaperReadback !== 1
      || canonical(original.launchBinding) !== canonical(launchBinding)
      || original.pidNamespace !== pidNamespace
      || original.bootId !== launcherAdmission.bootId || !/^[1-9]\d*$/.test(original.monotonicNs ?? '')
      || !Number.isFinite(Date.parse(original.capturedAt))) fail('scope-original-context');
  const launcher = admission(original.launcherAdmission, launcherAdmission);
  if (launcher.sessionId !== launcher.pid || launcher.processGroup !== launcher.pid
      || original.launcherCwd !== launchBinding.scopeDirectory) fail('scope-session-owner');
  const native = original.nativeAdmission;
  if (nativeAdmission) admission(native, nativeAdmission);
  else {
    if (native?.schema !== 'honua.owned-process-observation/v1' || native.bootId !== original.bootId
        || native.status !== 'observed' || native.pid === launcher.pid) fail('scope-native-admission');
    const parsed = scopeStat(native);
    if (parsed.parentPid !== launcher.pid || parsed.sessionId !== launcher.pid
        || parsed.processGroup !== launcher.pid) fail('scope-native-parent');
    admission(native, native);
  }
  if (JSON.stringify(raw64(native.cmdlineBase64).toString('utf8').split('\0'))
      !== JSON.stringify([...launchBinding.nativeArgv, ''])) fail('scope-fixed-native-argv');
  if (!Array.isArray(original.enumeratedPids) || !original.enumeratedPids.length || original.enumeratedPids.length > 32768
      || !original.enumeratedPids.every(pid => Number.isSafeInteger(pid) && pid > 0)
      || new Set(original.enumeratedPids).size !== original.enumeratedPids.length
      || !Array.isArray(original.observations))
    fail('scope-census-shape');
  const expectedPids = new Set([...original.enumeratedPids, launcher.pid, native.pid]);
  if (original.observations.length !== expectedPids.size) fail('scope-census-missing');
  const rows = new Map(), parsed = new Map();
  for (const row of original.observations) {
    if (!expectedPids.has(row.pid) || rows.has(row.pid)) fail('scope-census-duplicate');
    rows.set(row.pid, row);
    if (row.status === 'absent') {
      if (row.statBase64 !== null || row.cmdlineBase64 !== null) fail('scope-absence-shape');
    } else {
      if (!['observed', 'stat-only'].includes(row.status)) fail('scope-census-unverified');
      parsed.set(row.pid, scopeStat(row));
      if (row.status === 'stat-only' && row.cmdlineBase64 !== null) fail('scope-foreign-argv');
      if (row.status === 'observed') raw64(row.cmdlineBase64);
    }
  }
  const currentLauncher = parsed.get(launcher.pid);
  if (!currentLauncher || key(currentLauncher) !== key(launcher) || currentLauncher.sessionId !== launcher.pid)
    fail('scope-current-launcher');
  const members = new Set();
  for (const [pid, value] of parsed) {
    if (value.sessionId === launcher.pid) members.add(pid);
    let cursor = pid; const seen = new Set();
    while (parsed.has(cursor) && !seen.has(cursor)) {
      if (cursor === launcher.pid) { members.add(pid); break; }
      seen.add(cursor); cursor = parsed.get(cursor).parentPid;
    }
  }
  for (const pid of members) if (rows.get(pid).status !== 'observed') fail('scope-owned-command-missing');
  for (const [pid, row] of rows) if (!members.has(pid) && row.status === 'observed') fail('scope-foreign-argv');
  const before = original.taskChildrenBefore, after = original.taskChildrenAfter;
  if (!Array.isArray(before) || !before.length || before.length > 32768
      || JSON.stringify(before) !== JSON.stringify(after)) fail('scope-children-bracket');
  const tasks = new Set(), seenChildren = new Set();
  for (const record of before) {
    if (!members.has(record.pid) || !Number.isSafeInteger(record.tid) || record.tid < 1
        || tasks.has(record.pid + ':' + record.tid)) fail('scope-task-census');
    tasks.add(record.pid + ':' + record.tid);
    const text = raw64(record.rawBase64).toString('ascii').trim();
    if (text && !/^[1-9]\d*(?:\s+[1-9]\d*)*$/.test(text)) fail('scope-task-children-shape');
    const children = text ? text.split(/\s+/).map(Number) : [];
    if (new Set(children).size !== children.length) fail('scope-task-children-duplicate');
    for (const child of children) {
      if (!members.has(child) || parsed.get(child)?.parentPid !== record.pid || seenChildren.has(child))
        fail('scope-child-ownership');
      seenChildren.add(child);
    }
  }
  for (const pid of members) {
    if (!tasks.has(pid + ':' + pid)) fail('scope-member-task-missing');
    if (pid !== launcher.pid && !seenChildren.has(pid)) fail('scope-member-parent-census');
  }
  const currentNative = rows.get(native.pid);
  const finalNative = original.nativeCurrent;
  if (!finalNative || finalNative.pid !== native.pid || finalNative.status !== currentNative?.status)
    fail('scope-native-current-raced');
  if (currentNative?.status === 'absent') {
    if (finalNative.statBase64 !== null || finalNative.cmdlineBase64 !== null) fail('scope-native-current-raced');
  } else {
    const first = scopeStat(currentNative), last = scopeStat(finalNative);
    if (!['pid', 'parentPid', 'processGroup', 'sessionId', 'startTicks'].every(name => first[name] === last[name])
        || ['Z', 'X'].includes(first.state) !== ['Z', 'X'].includes(last.state)
        || currentNative.cmdlineBase64 !== finalNative.cmdlineBase64) fail('scope-native-current-raced');
  }
  if (!currentNative) fail('scope-native-census-missing');
  let nativeTerminal = currentNative.status === 'absent';
  if (!nativeTerminal) {
    const current = parsed.get(native.pid);
    nativeTerminal = current.startTicks !== native.identity.startTicks || ['Z', 'X'].includes(current.state);
    if (!nativeTerminal && (rows.get(native.pid).cmdlineBase64 !== native.cmdlineBase64
        || current.sessionId !== launcher.pid || current.parentPid !== launcher.pid)) fail('scope-current-native-drift');
  }
  const allowed = new Set([key(launcher), key({ pid: native.pid, startTicks: native.identity.startTicks })]);
  const live = [...members].map(pid => parsed.get(pid)).filter(value => !['Z', 'X'].includes(value.state) && !allowed.has(key(value)));
  return { closed: live.length === 0, liveOwned: live.map(value => ({ pid: value.pid, startTicks: value.startTicks })),
    nativeTerminal, nativeAdmission: native, memberCount: members.size };
}

export async function prepareScopeLaunch(runtime, invocation) {
  const scopeDirectory = join(invocation, 'scope'); await mkdir(scopeDirectory, { mode: 0o700 });
  const intakePath = join(runtime.seal.requestDirectory, 'intake.json'), intake = await readFile(intakePath);
  if (JSON.stringify(JSON.parse(intake)) !== JSON.stringify(runtime.seal)) fail('scope-runtime-intake-drift');
  const member = runtime.seal.members.find(row => row.path === 'Honua.DevOps.Agent');
  if (!member || runtime.seal.expected.rid !== 'linux-x64'
      || runtime.binary !== join(runtime.seal.requestDirectory, 'installed', 'Honua.DevOps.Agent')) fail('scope-runtime-member');
  const script = join(ROOT, 'tools/devops_native_scope.py'), nonce = randomBytes(32).toString('hex');
  const request = { schema: 'honua.published-native-scope-request/v1', nonce, intakePath,
    intakeSha256: hash(intake), scopeDirectory };
  const requestRaw = Buffer.from(JSON.stringify(request) + '\n'), requestPath = join(scopeDirectory, 'request.json');
  await writeFile(requestPath, requestRaw, { mode: 0o600, flag: 'wx' });
  const launchBinding = { schema: 'honua.published-native-scope-binding/v1', requestSha256: hash(requestRaw),
    intakeSha256: request.intakeSha256, nativeSha256: member.sha256, nativePath: runtime.binary,
    nativeArgv: [runtime.binary, '--mcp'], sourceSha: runtime.seal.expected.sourceSha, rid: 'linux-x64',
    scopeDirectory, launcherSha256: hash(await readFile(script)),
    interpreterPath: await realpath('/usr/bin/python3'), interpreterSha256: hash(await readFile('/usr/bin/python3')) };
  const result = Object.freeze({ command: '/usr/bin/python3', args: Object.freeze([script, '--request', requestPath]) });
  contexts.set(result, { nonce, launchBinding, scopeDirectory, invocation, pidNamespace: await readlink('/proc/self/ns/pid'),
    ordinal: 0, launcherAdmission: null, nativeAdmission: null });
  return result;
}

export async function readScopeControlOriginal(path, query, signal) {
  return new Promise((accept, reject) => {
    const socket = createConnection(path), chunks = []; let size = 0, ended = false;
    const refuse = error => { if (!ended) { ended = true; signal?.removeEventListener('abort', abort); socket.destroy(); reject(error); } };
    const abort = () => refuse(Error('scope-observer-expired-native-handle-retained'));
    if (signal?.aborted) { abort(); return; }
    signal?.addEventListener('abort', abort, { once: true });
    socket.once('connect', () => socket.write(JSON.stringify(query) + '\n'));
    socket.on('error', refuse);
    socket.on('data', block => {
      size += block.length;
      if (size > MAX || chunks.length >= 32768) { refuse(Error('scope-control-response-bound')); return; }
      chunks.push(block);
      if (block.includes(10)) {
        const raw = Buffer.concat(chunks);
        if (raw[raw.length - 1] !== 10 || raw.subarray(0, -1).includes(10)) { refuse(Error('scope-control-framing')); return; }
        try {
          const value = JSON.parse(raw); ended = true; socket.end();
          signal?.removeEventListener('abort', abort); accept({ raw, value });
        } catch { refuse(Error('scope-control-json')); }
      }
    });
    socket.once('close', () => { if (!ended) refuse(Error('scope-control-closed-unverified')); });
  });
}

async function actualResponse(context, launcherPid, action, signal) {
  try {
    return await readScopeControlOriginal('/proc/' + launcherPid + '/cwd/control.sock',
      { schema: SCHEMA, nonce: context.nonce, action }, signal);
  } catch (error) {
    if (signal?.aborted) throw error;
    const current = await observeProcess(launcherPid);
    if (!terminalObservation(current, { ...context.launcherAdmission.identity,
      bootId: context.launcherAdmission.bootId }).terminal) throw error;
    // Only actual original-launcher cessation permits this fixed terminal
    // sidecar fallback. A live/unreadable observer never becomes terminal.
    const path = join(context.scopeDirectory, 'scope-terminal.json'), before = await lstat(path);
    if (!before.isFile() || before.isSymbolicLink() || before.uid !== process.getuid()
        || before.nlink !== 1 || before.mode & 0o077 || before.size > MAX || await realpath(path) !== path)
      fail('scope-terminal-original-owner');
    const raw = await readFile(path), after = await lstat(path);
    if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs)
      fail('scope-terminal-original-raced');
    if (raw.length > MAX) fail('scope-terminal-original-bound');
    const value = JSON.parse(raw);
    if (value.original?.waitpidNoChildren !== true) fail('scope-terminal-no-children-missing');
    return { raw, value, launcherTerminal: current };
  }
}

export async function observeScope(launch, launcherPid, { requireClosed = false, signal } = {}) {
  const context = contexts.get(launch);
  if (!context) fail('scope-launch-handle');
  if (!context.launcherAdmission) {
    context.launcherAdmission = await observeProcess(launcherPid);
    if (context.launcherAdmission.status !== 'observed'
        || await readlink('/proc/' + launcherPid + '/cwd') !== context.scopeDirectory) fail('scope-launcher-admission');
  }
  const response = await actualResponse(context, launcherPid, 'census', signal);
  const path = join(context.invocation, 'scope-observation-' + context.ordinal++ + '.json');
  await writeFile(path, response.raw, { mode: 0o600, flag: 'wx' });
  if (response.value.status !== 'observed') fail('scope-control-unverified');
  const original = response.value.original;
  const result = verifyScopeOriginal(original, context);
  if (response.launcherTerminal && (!result.nativeTerminal || !result.closed)) fail('scope-terminal-owned-live');
  if (!context.nativeAdmission) context.nativeAdmission = structuredClone(result.nativeAdmission);
  const witness = { original, originalPath: path, originalSha256: hash(response.raw),
    nativeAdmission: structuredClone(context.nativeAdmission), launcherAdmission: structuredClone(context.launcherAdmission),
    closed: result.closed, nativeTerminal: result.nativeTerminal,
    launcherTerminal: response.launcherTerminal ?? null };
  if (requireClosed && !result.closed) throw Object.assign(Error('scope-owned-descendant-live'), { originalPath: path });
  if (result.closed) witnesses.set(witness, { snapshot: hash(Buffer.from(JSON.stringify(witness))),
    native: key({ pid: context.nativeAdmission.pid, startTicks: context.nativeAdmission.identity.startTicks }),
    bootId: context.nativeAdmission.bootId });
  return witness;
}

export async function drainScope(launch, launcherPid, witness, { signal } = {}) {
  const context = contexts.get(launch);
  if (!context || !isScopeWitness(witness, context.nativeAdmission)) fail('scope-drain-witness');
  const response = await actualResponse(context, launcherPid, 'drain', signal);
  const path = join(context.invocation, 'scope-drain-' + context.ordinal++ + '.json');
  await writeFile(path, response.raw, { mode: 0o600, flag: 'wx' });
  if (response.launcherTerminal) return observeScope(launch, launcherPid, { requireClosed: true, signal });
  if (response.value.status !== 'draining'
      || !verifyScopeOriginal(response.value.original, context).closed) fail('scope-drain-unverified');
  // The launcher forwards native stdin EOF only after its fresh owned census.
  // Wait for actual normal launcher completion, never for the SDK's 2s killer.
  while (true) {
    if (signal?.aborted) fail('scope-observer-expired-native-handle-retained');
    const current = await observeProcess(launcherPid);
    if (terminalObservation(current, { ...context.launcherAdmission.identity,
      bootId: context.launcherAdmission.bootId }).terminal)
      return observeScope(launch, launcherPid, { requireClosed: true, signal });
    await new Promise(accept => setTimeout(accept, 50));
  }
}

export function isScopeWitness(value, nativeAdmission) {
  const bound = value && witnesses.get(value);
  return !!bound && value.closed === true && bound.snapshot === hash(Buffer.from(JSON.stringify(value)))
    && bound.bootId === nativeAdmission?.bootId
    && bound.native === key({ pid: nativeAdmission?.pid, startTicks: nativeAdmission?.identity?.startTicks });
}
