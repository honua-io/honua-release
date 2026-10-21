import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { lifecycle } from './gp_native_mcp.mjs';

async function run(mode, operation = 'cancel') {
  const calls = [];
  const server = createServer(async (request, response) => {
    if (request.method !== 'POST') { response.writeHead(200).end(); return; }
    const bytes = []; for await (const chunk of request) bytes.push(chunk);
    const body = JSON.parse(Buffer.concat(bytes)); calls.push({ body, key: request.headers['x-api-key'] });
    if (!('id' in body)) { response.writeHead(202).end(); return; }
    let result;
    if (body.method === 'initialize') result = { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'control', version: '1' } };
    else if (body.method === 'tools/list') result = { tools: mode === 'missing-tool' ? [] : ['honua_execute_plan', 'honua_cancel_job'].map(name => ({ name, inputSchema: { type: 'object' } })), ...(mode === 'cursor-loop' ? { nextCursor: 'loop' } : {}) };
    else {
      const value = operation === 'submit' ? { jobId: 'job-1', status: 'Queued', createdAt: '2026-10-18T19:00:00Z', resourceUri: 'honua://jobs/job-1' }
        : { jobId: 'job-1', status: 'cancellation_requested', cancellationRequested: true };
      if (mode === 'wrong-job') value.jobId = 'other';
      if (mode === 'false-ack') value.cancellationRequested = false;
      if (mode === 'completed') value.status = 'Succeeded';
      if (mode === 'readable-result') value.results = { output: 1 };
      result = { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: mode === 'contradictory' ? { ...value, jobId: 'other' } : value,
        ...(mode === 'tool-error' ? { isError: true } : {}) };
      if (mode === 'empty') result = { content: [] };
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const value = await lifecycle(`http://127.0.0.1:${server.address().port}`, operation,
      operation === 'submit' ? { plan: { planId: 'p', steps: [] } } : { jobId: 'job-1' }, 'control', { loopback: true });
    return { value, calls };
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('actual installed SDK submits and repeats cancellation without claiming cessation', async () => {
  const submitted = await run('positive', 'submit');
  assert.equal(submitted.value.jobId, 'job-1');
  assert.equal(submitted.value.sdkVersion, '1.32.1');
  assert.equal(submitted.value.executionCessationProven, false);
  const cancelled = await run('positive');
  assert.equal(cancelled.value.sdkVersion, '1.32.1');
  assert.equal(cancelled.value.responses.length, 2);
  assert.equal(cancelled.calls.filter(x => x.body.method === 'tools/call').length, 2);
  assert.ok(cancelled.calls.every(x => x.key === 'control'));
});
for (const [mode, reason] of [['missing-tool', 'missing-tool'], ['cursor-loop', 'incomplete-discovery'],
  ['wrong-job', 'invalid-acknowledgement'], ['false-ack', 'invalid-acknowledgement'],
  ['completed', 'invalid-acknowledgement'], ['readable-result', 'invalid-acknowledgement'],
  ['contradictory', 'contradictory-output'], ['tool-error', 'tool-error'], ['empty', 'missing-output']]) {
  // An unrelated admission failure (for example a stale client lock) must not
  // make every semantic negative look successful before it reaches the wire.
  test(`SDK refuses ${mode}`, async () => { await assert.rejects(run(mode), { message: reason }); });
}
test('cloud CLI rejects non-TLS target before connecting', async () => {
  await assert.rejects(lifecycle('http://127.0.0.1:9', 'cancel', { jobId: 'job-1' }, 'key'), /invalid-origin/);
});
