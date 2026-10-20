// Installed official SDK transport. It reports acknowledgements, never cessation.
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const clientRoot = new URL('../certification/sources/geospatial-mcp/', import.meta.url);
const require = createRequire(new URL('package.json', clientRoot));
const { Client } = await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/index.js')));
const { StreamableHTTPClientTransport } = await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/streamableHttp.js')));
const LOCK = '5e6b28f4c29b82b799190e9e92083c7aca38b879afc7e242bee4320a711e195f';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

function outputs(result) {
  if (result.isError !== undefined && result.isError !== false) throw Error('tool-error');
  const values = [result.structuredContent, ...(result.content ?? []).filter(x => x.type === 'text').map(x => JSON.parse(x.text))].filter(x => x !== undefined);
  if (!values.length || values.some(x => !x || typeof x !== 'object' || Array.isArray(x))) throw Error('missing-output');
  if (values.some(x => JSON.stringify(x) !== JSON.stringify(values[0]))) throw Error('contradictory-output');
  return values[0];
}

export async function lifecycle(baseUrl, mode, argument, apiKey, { loopback = false } = {}) {
  const origin = new URL(baseUrl);
  if (!(origin.protocol === 'https:' || loopback && origin.protocol === 'http:' && origin.hostname === '127.0.0.1')
    || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash || !apiKey) throw Error('invalid-origin');
  const bytes = await readFile(new URL('package-lock.json', clientRoot));
  const locked = JSON.parse(bytes).packages['node_modules/@modelcontextprotocol/sdk'];
  if (hash(bytes) !== LOCK || locked?.version !== '1.32.1') throw Error('client-lock');
  let root = dirname(require.resolve('@modelcontextprotocol/sdk/client/index.js'));
  let installed;
  for (let i = 0; i < 6; i++, root = dirname(root)) {
    try { const doc = JSON.parse(await readFile(resolve(root, 'package.json'))); if (doc.name === '@modelcontextprotocol/sdk') { installed = doc; break; } }
    catch (e) { if (e.code !== 'ENOENT') throw e; }
  }
  if (installed?.version !== locked.version) throw Error('installed-client-version');
  const tool = mode === 'submit' ? 'honua_execute_plan' : mode === 'cancel' ? 'honua_cancel_job' : null;
  if (!tool) throw Error('unsupported-mode');
  if (mode === 'cancel' && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(argument.jobId)) throw Error('job-identity');
  const client = new Client({ name: 'honua-aws-native-lifecycle', version: '1' });
  const budget = AbortSignal.timeout(45000);
  const transport = new StreamableHTTPClientTransport(new URL('/mcp', origin), {
    requestInit: { headers: { 'X-API-Key': apiKey } },
    fetch: (url, options) => fetch(url, { ...options, redirect: 'error', signal: AbortSignal.any([budget, ...(options?.signal ? [options.signal] : [])]) }),
  });
  try {
    await client.connect(transport);
    let cursor, found = false;
    const seen = new Set();
    for (let page = 0; page < 100; page++) {
      const listed = await client.listTools(cursor ? { cursor } : {}, { timeout: 5000 });
      found ||= listed.tools.some(x => x.name === tool && x.inputSchema?.type === 'object');
      cursor = listed.nextCursor;
      if (!cursor) break;
      if (page === 99 || seen.has(cursor)) throw Error('incomplete-discovery');
      seen.add(cursor);
    }
    if (!found) throw Error('missing-tool');
    const responses = [];
    for (let i = 0; i < (mode === 'cancel' ? 2 : 1); i++) {
      const value = outputs(await client.callTool({ name: tool, arguments: argument }, undefined, { timeout: 15000 }));
      if (mode === 'cancel' && (value.jobId !== argument.jobId || value.status !== 'cancellation_requested'
        || value.cancellationRequested !== true || Object.keys(value).length !== 3)) throw Error('invalid-acknowledgement');
      if (mode === 'submit' && (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value.jobId)
        || !['Queued', 'Running'].includes(value.status) || value.resourceUri !== `honua://jobs/${value.jobId}`
        || Object.keys(value).length !== 4 || typeof value.createdAt !== 'string' || !Number.isFinite(Date.parse(value.createdAt)))) throw Error('invalid-submission');
      responses.push(value);
    }
    return { schema: 'honua.gp-native-mcp/v1', clientLockSha256: LOCK, sdkVersion: installed.version,
      adapterSha256: hash(await readFile(new URL(import.meta.url))), executionCessationProven: false,
      ...(mode === 'submit' ? responses[0] : { jobId: argument.jobId, responses }) };
  } finally { await client.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const input = []; for await (const chunk of process.stdin) input.push(chunk);
    const raw = Buffer.concat(input); if (raw.length > 16 * 1024 * 1024) throw Error('input-bound');
    process.stdout.write(JSON.stringify(await lifecycle(process.argv[2], process.argv[3], JSON.parse(raw), process.env.HONUA_GP_MCP_API_KEY)) + '\n');
  } catch { process.stderr.write('Installed MCP native lifecycle operation refused.\n'); process.exitCode = 1; }
}
