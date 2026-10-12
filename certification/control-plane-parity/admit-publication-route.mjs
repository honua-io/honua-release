// Only verified registry-installed modules are used; this does not select versions.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';
import { assertIntentSchema } from './publication-route.mjs';

const run = process.env.HONUA_PARITY_RUN;
const consumer = process.env.HONUA_PARITY_CONSUMER;
const require = createRequire(`${consumer}/package.json`);
const load = name => import(pathToFileURL(require.resolve(name)).href);
const provenance = JSON.parse(await readFile(`${consumer}/provenance.json`, 'utf8'));
assert.equal(provenance.kind, 'public-npm-default-resolution');
const { HonuaClient } = await load('@honua/sdk-js/honua');
const { mapPackagePublishCommand, createHonuaCommandRuntime } = await load('@honua/sdk-js/control-plane');
assertIntentSchema(mapPackagePublishCommand.inputSchema.properties.intent);
const { createServer } = await load('@honua/mcp-server');
const mcpRequire = createRequire(`${consumer}/node_modules/@honua/mcp-server/package.json`);
const { Client } = await import(pathToFileURL(mcpRequire.resolve('@modelcontextprotocol/sdk/client/index.js')));
const { InMemoryTransport } = await import(pathToFileURL(mcpRequire.resolve('@modelcontextprotocol/sdk/inMemory.js')));
let wireAttempts = 0;
const client = new HonuaClient({ baseUrl: 'http://127.0.0.1:19816', fetchFn: async () => { wireAttempts++; throw new Error('Admission must never contact the server'); } });
const server = createServer(client, { controlPlaneCommands: { runtime: createHonuaCommandRuntime({ client }) } });
const mcp = new Client({ name: 'honua-publication-intent-admission', version: '1.0.0' });
const [ct, st] = InMemoryTransport.createLinkedPair();
try {
  await server.connect(st); await mcp.connect(ct);
  const tools = await mcp.listTools();
  const matches = tools.tools.filter(tool => tool.name === 'honua_command_map_package_publish');
  assert.equal(matches.length, 1);
  const intentSchema = matches[0].inputSchema.properties.input.properties.intent;
  assertIntentSchema(intentSchema, true);
  const intent = { route: `/sdk1424/${randomUUID()}`, visibility: 'personal' };
  const envelope = JSON.parse(await readFile(`${run}/published-map-envelope.json`, 'utf8'));
  const invoke = async input => {
    const result = await mcp.callTool({ name: matches[0].name, arguments: { input, dryRun: true } });
    return JSON.parse(result.content.find(item => item.type === 'text').text);
  };
  assert.equal((await invoke({ package: envelope.body, intent })).status, 'dry-run');
  for (const denied of [{ ...intent, approvedBy: 'self' }, { ...intent, visibility: 'unknown' }, { ...intent, route: 'relative' }]) {
    assert.equal((await invoke({ package: envelope.body, intent: denied })).errorKind, 'validation');
  }
  assert.equal(wireAttempts, 0);
  const receipt = {
    kind: 'verified-published-intent-admission', publishedClients: provenance,
    intent, dryRunControls: { accepted: 1, refused: 3, wireAttempts },
    sdkSchemaSha256: createHash('sha256').update(JSON.stringify(mapPackagePublishCommand.inputSchema)).digest('hex'),
    mcpSchemaSha256: createHash('sha256').update(JSON.stringify(matches[0].inputSchema)).digest('hex'),
    scope: 'Actual installed SDK and MCP schema support; no mutation or publication success claim',
  };
  await writeFile(`${run}/publication-route-admission.json`, JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
} finally { await mcp.close(); await server.close(); }
