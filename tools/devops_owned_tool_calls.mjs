// Explicit public-protocol requests over the installed SDK's STOCK transport.
// Client still owns initialize/discovery and its own response IDs. This adapter
// only resolves its nonce-owned fixed-tool requests. It sets no request timer:
// the unchanged production runner owns its existing 45 minute execution limit.
// An observer expiry must retain this live handle, never close or cancel it.
import { randomBytes, createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { validateTool } from './devops_native_mcp.mjs';
import { isTerminalWitness } from './devops_process_observer.mjs';
import { isScopeWitness } from './devops_process_scope.mjs';

const fail = code => { throw Error(code); };
const exact = (value, names) => value && Object.getPrototypeOf(value) === Object.prototype
  && Object.keys(value).sort().join() === [...names].sort().join();
const operation = value => typeof value === 'string' && /^urn:honua:provisioning:[a-f0-9]{32}$/.test(value);
const hash = raw => createHash('sha256').update(raw).digest('hex');

// Pure intent/time join only; this never creates either live ownership brand.
export function verifyReconciliationFacts(entry, witness, scopeOriginal) {
  const number = value => typeof value === 'string' && /^[1-9]\d*$/.test(value);
  if (!entry?.mutating || witness?.claimContext?.operationId !== entry.operationId
      || witness.claimContext.action !== entry.action) fail('owned-call-terminal-intent');
  if (!number(entry.admittedMonotonicNs) || !number(witness.capturedMonotonicNs)
      || !number(scopeOriginal?.monotonicNs)
      || BigInt(witness.capturedMonotonicNs) < BigInt(entry.admittedMonotonicNs)
      || BigInt(scopeOriginal.monotonicNs) < BigInt(witness.capturedMonotonicNs))
    fail('owned-call-terminal-chronology');
  return true;
}

export function createOwnedToolCalls({ transport, tools, expectedTools, CallToolResultSchema,
  invocation, privateHome, nativeAdmission }) {
  if (typeof transport?.send !== 'function' || typeof transport.onmessage !== 'function'
      || typeof CallToolResultSchema?.parse !== 'function') fail('owned-call-stock-session');
  const sdkMessage = transport.onmessage, sdkClose = transport.onclose;
  const boundNative = structuredClone(nativeAdmission);
  const nonce = randomBytes(16).toString('hex');
  const pending = new Map(), history = new Map();
  let draining = false;
  const settling = new Set();
  const reservations = new Map();
  const unreconciled = new Set();
  let ordinal = 0;

  async function retain(kind, id, message) {
    const raw = Buffer.from(JSON.stringify(message) + '\n');
    if (raw.length > 8 * 1024 * 1024) fail('owned-call-observation-bound');
    const name = 'decoded-' + kind + '-' + id + '.json';
    await writeFile(join(invocation, name), raw, { mode: 0o600, flag: 'wx' });
    return { original: name, sha256: hash(raw) };
  }

  transport.onmessage = message => {
    const response = message && !Object.hasOwn(message, 'method')
      && typeof message.id === 'string' ? pending.get(message.id) : undefined;
    if (!response) return sdkMessage(message); // Never steal Client-managed IDs.
    // Remove only the response resolver. This does NOT mark the underlying
    // executor terminal or authorize native shutdown/restart/cleanup.
    pending.delete(message.id);
    settling.add(message.id);
    void retain('response', message.id, message).then(original => {
      history.get(message.id).response = original;
      settling.delete(message.id);
      if (message.jsonrpc !== '2.0' || (Object.hasOwn(message, 'error') === Object.hasOwn(message, 'result')))
        return response.reject(Error('owned-call-response-shape'));
      if (Object.hasOwn(message, 'error')) {
        // Raw remote error remains private. A remote error is not an invented
        // local OperationCanceledException or proof of executor cessation.
        return response.reject(Object.assign(Error('owned-call-remote-error'), { original }));
      }
      try { response.resolve({ result: CallToolResultSchema.parse(message.result), requestId: message.id, original }); }
      catch { response.reject(Error('owned-call-tool-result-schema')); }
    }).catch(error => { settling.delete(message.id); response.reject(error); });
  };
  transport.onclose = () => {
    for (const entry of pending.values()) entry.reject(Error('owned-call-transport-ended-indeterminate'));
    // Preserve all owned IDs/history for reconciliation. Transport close is not
    // executor terminal, cleanup, readiness or candidate evidence.
    if (sdkClose) sdkClose();
  };

  async function begin(name, args) {
    if (draining) fail('owned-call-session-draining');
    const inspection = name === 'provision_infrastructure' && args.action === 'inspect-cleanup';
    const inFlight = [...reservations.keys(), ...pending.keys(), ...settling];
    if ((!inspection && (inFlight.length || unreconciled.size))
        || (inspection && (inFlight.length > 1 || inFlight.some(id => (history.get(id) ?? reservations.get(id)).inspection))))
      fail('owned-call-overlap-or-unreconciled');
    if (history.size >= 256) fail('owned-call-history-bound');
    const tool = tools.find(value => value.name === name);
    validateTool(tool, expectedTools[name], { name, arguments: args });
    const id = 'honua-release-' + nonce + '-' + ordinal++;
    const request = { jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args } };
    // Reserve synchronously before the first asynchronous evidence write.
    const admittedMonotonicNs = process.hrtime.bigint().toString();
    reservations.set(id, { inspection, admittedMonotonicNs });
    let original;
    try { original = await retain('request', id, request); }
    catch (error) { reservations.delete(id); throw error; }
    let resolve, reject;
    const completion = new Promise((accept, refuse) => { resolve = accept; reject = refuse; });
    // Caller may inspect/process observations before awaiting completion. Keep
    // a handled branch so an early remote failure cannot become unhandled.
    void completion.catch(() => {});
    pending.set(id, { resolve, reject });
    reservations.delete(id);
    const mutating = name === 'provision_infrastructure' && ['apply', 'destroy'].includes(args.action) && args.confirmed;
    history.set(id, { name, inspection, mutating, operationId: args.provisioningOperationId ?? null,
      action: args.action ?? null, admittedMonotonicNs, request: original, response: null, cancellation: null });
    if (mutating)
      unreconciled.add(id);
    try { await transport.send(request); }
    catch {
      // Send failure can occur after admission. Do not close/kill/retry or
      // discard ownership; caller must independently reconcile the executor.
      reject(Error('owned-call-send-indeterminate'));
    }
    return { requestId: id, completion, original };
  }

  function provision(args) {
    if (!exact(args, ['stack', 'size', 'action', 'variablesJson', 'confirmed', 'confirmation',
      'approvalReceiptJson', 'idempotencyKey', 'provisioningOperationId'])
      || args.stack !== 'aws-ecs' || args.size !== 'small'
      || !['plan', 'apply', 'destroy', 'inspect-cleanup'].includes(args.action)
      || typeof args.variablesJson !== 'string' || Buffer.byteLength(args.variablesJson) > 1024 * 1024
      || typeof args.confirmed !== 'boolean' || typeof args.confirmation !== 'string'
      || typeof args.approvalReceiptJson !== 'string' || Buffer.byteLength(args.approvalReceiptJson) > 1024 * 1024
      || typeof args.idempotencyKey !== 'string' || !/^[A-Za-z0-9._:-]{1,160}$/.test(args.idempotencyKey)
      || typeof args.provisioningOperationId !== 'string') fail('owned-call-provision-shape');
    let variables;
    try { variables = JSON.parse(args.variablesJson); } catch { fail('owned-call-variables'); }
    if (!variables || Array.isArray(variables) || typeof variables !== 'object') fail('owned-call-variables');
    const mutating = ['apply', 'destroy'].includes(args.action) && args.confirmed;
    if (mutating && (!operation(args.provisioningOperationId) || !args.approvalReceiptJson
      || !args.confirmation)) fail('owned-call-approval-missing');
    if (!mutating && (args.approvalReceiptJson || args.confirmation || args.confirmed)) fail('owned-call-unrequested-authority');
    if (args.action === 'apply' && !mutating) fail('owned-call-approval-missing');
    if (args.action === 'inspect-cleanup' && !operation(args.provisioningOperationId)) fail('owned-call-operation');
    return begin('provision_infrastructure', args);
  }

  return {
    provision,
    installHandoff(operationId) {
      if (!operation(operationId)) fail('owned-call-operation');
      return begin('install_handoff', { stack: 'aws-ecs', baseUrl: '', adminKeySecretRef: '',
        outputDirectory: join(privateHome, 'handoffs', operationId.slice(-32)), overwrite: false,
        provisioningOperationId: operationId });
    },
    verifyHandoff(operationId) {
      if (!operation(operationId)) fail('owned-call-operation');
      return begin('verify_install_handoff', {
        handoffConfigPath: join(privateHome, 'handoffs', operationId.slice(-32), 'honua-mcp-proxy.handoff.json'),
        overwrite: false });
    },
    async cancel(requestId) {
      if (!pending.has(requestId) || !history.get(requestId)?.mutating
          || history.get(requestId)?.cancellation) fail('owned-call-cancel-id');
      const message = { jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId } };
      await transport.send(message); // AWAITED standard notification, never local token inference.
      const original = await retain('cancel', requestId, message);
      history.get(requestId).cancellation = original;
      return original;
    },
    observations() { return [...history].map(([requestId, value]) => ({ requestId, ...value })); },
    pendingIds() { return [...reservations.keys(), ...pending.keys(), ...settling]; },
    unreconciledIds() { return [...unreconciled]; },
    reconcileExecutor(requestId, witness, scopeWitness) {
      if (!unreconciled.has(requestId) || !isTerminalWitness(witness, boundNative))
        fail('owned-call-terminal-witness');
      verifyReconciliationFacts(history.get(requestId), witness, scopeWitness?.original);
      if (!isScopeWitness(scopeWitness, boundNative)) fail('owned-call-terminal-witness');
      if ((pending.has(requestId) || settling.has(requestId)) && !scopeWitness.nativeTerminal)
        fail('owned-call-request-not-idle');
      unreconciled.delete(requestId);
      // A missing response is reconcilable only once the actual native birth
      // also ceased, with the authoritative whole scope drained. A still-live
      // native request cannot resume after a sampling-only "terminal" verdict.
      const response = pending.get(requestId);
      if (response) {
        pending.delete(requestId);
        response.reject(Error('owned-call-reconciled-without-response'));
      }
    },
    beginDrain(scopeWitness) {
      if (reservations.size) fail('owned-call-drain-reservation');
      if ( pending.size || settling.size || unreconciled.size || !isScopeWitness(scopeWitness, boundNative))
        fail('owned-call-drain-not-quiescent');
      draining = true;
    }
    // Deliberately no close/finalizer, no arbitrary methods and no timer.
  };
}
