// Pure routing/source guards. Actual binary stdio proof is separate and retained.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { typedReadRequest, validateTool, cancelOwnedRequest, validateSdkLocation } from './devops_native_mcp.mjs';
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

test('readonly request is fixed and carries no caller authority', () => {
  assert.deepEqual(typedReadRequest('describe_environment'), { name: 'describe_environment', arguments: {} });
  const request = typedReadRequest('inspect-cleanup', 'urn:honua:provisioning:' + 'a'.repeat(32));
  assert.equal(request.arguments.action, 'inspect-cleanup');
  assert.equal(request.arguments.confirmed, false);
  assert.equal(request.arguments.approvalReceiptJson, '');
});
for (const name of ['plan', 'apply', 'destroy', 'install_handoff', 'verify_install_handoff', 'arbitrary']) {
  test(`missing original authority refuses ${name}`, () => assert.throws(() => typedReadRequest(name), /authority-unqualified/));
}
test('operation identity and exact expected tool binding cannot be bypassed', () => {
  for (const id of ['../foreign', 'owned-operation', 'urn:foreign:provisioning:' + 'a'.repeat(32),
    'urn:honua:provisioning:' + 'a'.repeat(31), 'urn:honua:provisioning:' + 'g'.repeat(32)]) {
    assert.throws(() => typedReadRequest('inspect-cleanup', id), /authority-unqualified/);
  }
  const tool = { inputSchema: { type: 'object' }, name: 'describe_environment' };
  validateTool(tool, hash(tool), typedReadRequest('describe_environment'));
  assert.throws(() => validateTool(tool, '0'.repeat(64), typedReadRequest('describe_environment')), /schema-binding/);
  assert.throws(() => validateTool(tool, undefined, typedReadRequest('describe_environment')), /schema-binding/);
});
test('cancellation uses exactly one observed owned request ID and awaits standard SDK send', async () => {
  const observations = [{ method: 'tools/call', id: 'owned', name: 'provision_infrastructure' }];
  const sent = []; const client = { async notification(message) { await Promise.resolve(); sent.push(message); } };
  await cancelOwnedRequest(client, observations, 'owned');
  assert.deepEqual(sent, [{ method: 'notifications/cancelled', params: { requestId: 'owned' } }]);
  await assert.rejects(cancelOwnedRequest(client, observations, 'wrong'), /request-identity/);
  await assert.rejects(cancelOwnedRequest(client, [...observations, ...observations], 'owned'), /request-identity/);
  await assert.rejects(cancelOwnedRequest({ async notification() { throw Error('send-failed'); } }, observations, 'owned'), /send-failed/);
});


test('SDK resolution cannot fall back to a same-version ancestor or symlink', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'native-sdk-location-'));
  try {
    const owned = join(temp, 'owned'); const foreign = join(temp, 'foreign');
    await mkdir(join(owned, 'dist'), { recursive: true }); await mkdir(join(foreign, 'dist'), { recursive: true });
    await writeFile(join(owned, 'dist/index.js'), ''); await writeFile(join(foreign, 'dist/index.js'), '');
    await validateSdkLocation(join(owned, 'dist/index.js'), owned);
    await assert.rejects(validateSdkLocation(join(foreign, 'dist/index.js'), owned), /sdk-resolution/);
    await symlink(foreign, join(temp, 'alias'));
    await assert.rejects(validateSdkLocation(join(temp, 'alias/dist/index.js'), join(temp, 'alias')), /sdk-resolution/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
