// Actual published terminal approval and SDK reads. No direct HTTP mutation.
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { principal, assertProposal, assertCompletedPublication, assertAppliedAudit } from './publication-contract.mjs';

const run = process.env.HONUA_PARITY_RUN;
const consumer = process.env.HONUA_PARITY_CONSUMER;
const require = createRequire(`${consumer}/package.json`);
const load = name => import(pathToFileURL(require.resolve(name)).href);
const { HonuaClient } = await load('@honua/sdk-js/honua');
const { HonuaAdminClient } = await load('@honua/sdk-js/control-plane');
const { HonuaStudioLifecycleClient } = await load('@honua/sdk-js/studio');
const baseUrl = 'http://127.0.0.1:19816';
const out = `${run}/parity-publication-lifecycle`;
await mkdir(out, { mode: 0o700 });
const read = async path => JSON.parse(await readFile(`${run}/${path}`, 'utf8'));
const proof = await read('principal-proof.json');
const owner = principal(proof, 'admin-a'), approver = principal(proof, 'approver-a');
assert.notEqual(owner.actor, approver.actor);
assert.equal(owner.tenant, approver.tenant);
const previous = await read('parity-publish/summary.json');
assert.equal(previous.status, 'bounded-parity-passed');
const join = previous.cases.find(item => item.user === 'admin-a').governedJoin;
for (const value of Object.values(join)) assert(typeof value === 'string' && value.length > 0);
const tokens = Object.fromEntries(await Promise.all(['admin-a', 'approver-a', 'admin-b', 'viewer-a', 'admin-read-a'].map(async user => [user, (await read(`${user}-bearer.json`)).accessToken])));
const clients = Object.fromEntries(Object.entries(tokens).map(([user, token]) => [user, new HonuaAdminClient({ baseUrl, headers: { Authorization: `Bearer ${token}` } })]));
const studio = new HonuaStudioLifecycleClient({ client: new HonuaClient({ baseUrl, bearerToken: tokens['admin-a'] }) });
const receipt = { status: 'failed', publishedClients: await read('consumer/provenance.json'), join, owner, approver, observations: [], scope: 'Separate authenticated fixture principal, not a human approval acceptance or four-surface workflow claim', remaining: ['Published typed map intent and final route readback', 'Full release journey and four-adapter operation coverage'] };
const save = async () => {
  const text = JSON.stringify(receipt, null, 2);
  for (const token of Object.values(tokens)) assert(!text.includes(token), 'Bearer in retained evidence');
  await writeFile(`${out}/summary.json`, text + '\n', { mode: 0o600 });
};
function terminal(user, operation) {
  const env = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'LANG'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  Object.assign(env, { HONUA_BASE_URL: baseUrl, HONUA_BEARER_TOKEN: tokens[user], HONUA_CONFIG_HOME: `${out}/empty-config` });
  const result = spawnSync(process.execPath, [`${consumer}/node_modules/@honua/sdk-js/dist/src/cli/bin.js`, 'admin', 'operate', operation, '--path', `id=${join.proposalId}`, '--yes', '--json'], { env, encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 });
  assert(!result.error && result.status !== null, 'Terminal invocation timed out or failed to start; no mutation retry');
  for (const value of [result.stdout, result.stderr]) for (const token of Object.values(tokens)) assert(!value.includes(token), 'Terminal leaked bearer');
  const output = JSON.parse((result.stdout || result.stderr).trim());
  receipt.observations.push({ user, surface: 'terminal', operation, exitCode: result.status, output });
  return { exitCode: result.status, output };
}
async function get(user = 'admin-a') {
  return (await clients[user].call('getOperationProposal', { path: { id: join.proposalId } })).data;
}
try {
  const before = terminal('admin-a', 'getOperationProposal');
  assert.equal(before.exitCode, 0);
  assertProposal(before.output, join, owner, 'AwaitingApproval');
  assertProposal(await get(), join, owner, 'AwaitingApproval');
  // All negative attempts use the real terminal; SDK confirms exact HTTP refusal.
  for (const [user, status] of [['admin-a', 403], ['viewer-a', 403], ['admin-read-a', 403], ['admin-b', 404]]) {
    const denied = terminal(user, 'approveOperationProposal');
    assert.notEqual(denied.exitCode, 0);
    assert.match(denied.output.error, new RegExp(`HTTP ${status}\\b`));
    await assert.rejects(clients[user].call('approveOperationProposal', { path: { id: join.proposalId } }), error => error.statusCode === status);
    assertProposal(await get(), join, owner, 'AwaitingApproval');
    receipt.observations.push({ user, surface: 'sdk', operation: 'approveOperationProposal', refusedHttpStatus: status, proposalUnchanged: true });
    await save();
  }
  // Exactly one positive terminal mutation; no automatic retry on uncertainty.
  const accepted = terminal('approver-a', 'approveOperationProposal');
  assert.equal(accepted.exitCode, 0);
  let resolved;
  for (let index = 0; index < 20; index++) {
    const observation = terminal('admin-a', 'getOperationProposal');
    assert.equal(observation.exitCode, 0);
    resolved = observation.output;
    assert.equal(resolved.proposalId, join.proposalId);
    await save();
    if (resolved.status === 'Succeeded') break;
    assert(['Executing', 'Submitted', 'Reconciling'].includes(resolved.status), 'Unexpected or failed proposal state');
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  assertProposal(resolved, join, owner, 'Succeeded');
  assert.equal(resolved.resolvedBy, approver.actor);
  assertProposal(await get(), join, owner, 'Succeeded');
  const response = await fetch(`${baseUrl}/api/v1/operations/handles/${encodeURIComponent(join.operationInstanceId)}`, { headers: { Authorization: `Bearer ${tokens['admin-a']}` }, redirect: 'error', signal: AbortSignal.timeout(30000) });
  assert.equal(response.status, 200);
  const handle = (await response.json()).data;
  const publication = await studio.publicationRequests.get(join.itemId, join.versionId, handle.resourceIds.publicationId);
  const version = await studio.contentVersions.get(join.itemId, join.versionId);
  assertCompletedPublication(handle, join, publication, version);
  const correlation = join.correlationId.replaceAll("'", "''");
  const sql = `select coalesce(json_agg(t),'[]'::json) from (select audit_id,actor,action,outcome,correlation_id,resource_type,resource_id,details,entry_hash from honua.audit_log where correlation_id='${correlation}' order by audit_id)t`;
  const auditProcess = spawnSync('docker', ['exec', 'honua-sdk1424-fixed-db-1', 'psql', '-U', 'honua', '-d', 'honua', '-Atc', sql], { encoding: 'utf8', timeout: 30000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(auditProcess.status, 0);
  const audit = JSON.parse(auditProcess.stdout);
  receipt.appliedAuditId = assertAppliedAudit(audit, join, approver);
  Object.assign(receipt, { handle, publication, immutableVersion: { itemId: version.itemId, versionId: version.versionId, contentHash: version.contentHash }, durableAudit: audit, status: 'bounded-separate-principal-publication-passed' });
  await save();
} catch (error) {
  receipt.failure = String(error);
  // Error strings can include server diagnostics; credentials may never be retained.
  for (const token of Object.values(tokens)) receipt.failure = receipt.failure.replaceAll(token, '<private-bearer>');
  await save();
  process.exitCode = 1;
}
