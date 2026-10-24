// Source adapter controls only. Constructed transport/results are NOT native,
// AWS, SDK callTool, six-cell, installed-client or candidate evidence.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { createOwnedToolCalls } from './devops_owned_tool_calls.mjs';

const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
const OP = 'urn:honua:provisioning:' + '1'.repeat(32);
function args(action = 'plan') {
  return { stack: 'aws-ecs', size: 'small', action, variablesJson: '{}', confirmed: action === 'apply',
    confirmation: action === 'apply' ? 'source-vector-only' : '',
    approvalReceiptJson: action === 'apply' ? '{"sourceControlOnly":true}' : '',
    idempotencyKey: 'source-control', provisioningOperationId: action === 'apply' ? OP : '' };
}
async function fixture() {
  const invocation = await mkdtemp(join(tmpdir(), 'owned-call-source-'));
  const forwarded = [], sent = [];
  const transport = { send: async message => { sent.push(message); }, onmessage: message => forwarded.push(message) };
  const tools = ['provision_infrastructure', 'install_handoff', 'verify_install_handoff'].map(name => ({ name,
    inputSchema: { type: 'object', properties: Object.fromEntries(Object.keys(args()).map(name => [name, {}])) } }));
  const expectedTools = Object.fromEntries(tools.map(tool => [tool.name, hash(tool)]));
  const adapter = createOwnedToolCalls({ transport, tools, expectedTools, invocation, privateHome: invocation,
    nativeAdmission: { pid: process.pid }, CallToolResultSchema: { parse: value => {
      if (!value || !Array.isArray(value.content)) throw Error('source-schema-refusal'); return value;
    } } });
  return { invocation, transport, forwarded, sent, adapter,
    async cleanup() { await rm(invocation, { recursive: true, force: true }); } };
}

test('owned public protocol resolver retains decoded originals and leaves Client response IDs alone', async () => {
  const f = await fixture();
  try {
    const call = await f.adapter.provision(args());
    assert.equal(f.sent[0].id, call.requestId);
    const own = { jsonrpc: '2.0', id: call.requestId, result: { content: [{ type: 'text', text: '{}' }] } };
    f.transport.onmessage({ jsonrpc: '2.0', id: 0, result: {} });
    f.transport.onmessage({ jsonrpc: '2.0', id: 'foreign', result: {} });
    f.transport.onmessage(own);
    const result = await call.completion;
    assert.equal(f.forwarded.length, 2);
    assert.deepEqual(JSON.parse(await readFile(join(f.invocation, result.original.original))), own);
    assert.equal(f.adapter.pendingIds().length, 0);
    assert.equal(f.adapter.unreconciledIds().length, 0);
  } finally { await f.cleanup(); }
});

test('remote response never clears actual executor ownership and raw JSON cannot mint terminal witness', async () => {
  const f = await fixture();
  try {
    const call = await f.adapter.provision(args('apply'));
    f.transport.onmessage({ jsonrpc: '2.0', id: call.requestId, result: { content: [] } });
    await call.completion;
    assert.deepEqual(f.adapter.unreconciledIds(), [call.requestId]);
    await assert.rejects(f.adapter.provision(args()), /unreconciled/);
    assert.throws(() => f.adapter.reconcileExecutor(call.requestId, { terminal: true }), /terminal-witness/);
  } finally { await f.cleanup(); }
});

test('matching cancellation is awaited and wrong IDs produce no notification', async () => {
  const f = await fixture();
  try {
    const call = await f.adapter.provision(args('apply'));
    await assert.rejects(f.adapter.cancel('wrong'), /cancel-id/);
    assert.equal(f.sent.length, 1);
    let release;
    f.transport.send = async message => {
      f.sent.push(message); await new Promise(resolve => { release = resolve; });
    };
    let completed = false;
    const cancel = f.adapter.cancel(call.requestId).then(result => { completed = true; return result; });
    await Promise.resolve();
    assert.equal(completed, false);
    release(); await cancel;
    assert.deepEqual(f.sent[1], { jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: call.requestId } });
    assert.deepEqual(f.adapter.unreconciledIds(), [call.requestId]);
    f.transport.onmessage({ jsonrpc: '2.0', id: call.requestId, error: { code: -32800, message: 'source-vector' } });
    await assert.rejects(call.completion, /remote-error/);
  } finally { await f.cleanup(); }
});

for (const defect of ['missing-approval', 'arbitrary-tool-field', 'arbitrary-action', 'secret-env-field']) {
  test('typed request refuses ' + defect + ' before any send', async () => {
    const f = await fixture();
    try {
      const value = args('apply');
      if (defect === 'missing-approval') value.approvalReceiptJson = '';
      if (defect === 'arbitrary-tool-field') value.command = 'not-allowed';
      if (defect === 'arbitrary-action') value.action = 'exec';
      if (defect === 'secret-env-field') value.env = { AWS_SECRET_ACCESS_KEY: 'source-vector' };
      assert.throws(() => f.adapter.provision(value));
      assert.equal(f.sent.length, 0);
    } finally { await f.cleanup(); }
  });
}

test('synchronous reservation survives deferred evidence write and blocks concurrent begin/drain', async () => {
  const f = await fixture();
  try {
    // writeFile is asynchronous: no event-loop yield occurs before both probes.
    const first = f.adapter.provision(args());
    assert.equal(f.adapter.pendingIds().length, 1);
    assert.equal(f.sent.length, 0);
    await assert.rejects(f.adapter.provision(args()), /overlap-or-unreconciled/);
    assert.throws(() => f.adapter.beginDrain(null), /drain-reservation/);
    const call = await first;
    f.transport.onmessage({ jsonrpc: '2.0', id: call.requestId, result: { content: [] } });
    await call.completion;
    assert.deepEqual(f.adapter.pendingIds(), []);
  } finally { await f.cleanup(); }
});
