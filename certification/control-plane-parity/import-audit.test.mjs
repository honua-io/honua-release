import test from 'node:test';
import assert from 'node:assert/strict';
import { importExecutionRow, assertExecutionCredential } from './import-audit.mjs';

const actor = 'operatorbearer:subject:honua-operator-bearer:fixture-subject';
const tenant = 'fixture-tenant';
const id = '12345678-1234-1234-1234-123456789abc';
const join = { operationInstanceId: 'opinst-fixture', auditId: '36', correlationId: 'fixture-correlation' };
const route = '/api/v1/admin/import/upload-url';
function fixture() {
  const post = { action: 'admin.post', actor, correlation_id: join.correlationId, resource_id: route, outcome: 'Success', details: JSON.stringify({ status: 202, method: 'POST', tenantId: tenant }) };
  const rows = ['operation.accepted', 'operation.submitted', ...Array(3).fill('operation.retry')].map(action => ({ action, actor, resource_id: join.operationInstanceId, correlation_id: join.correlationId }));
  rows.push(...Array.from({ length: 4 }, () => structuredClone(post)));
  rows.push({ ...post, actor: `apikey:api-key:${id}`, audit_id: 37, details: JSON.stringify({ ...JSON.parse(post.details), operationInstanceId: join.operationInstanceId, acceptedAuditId: join.auditId }) });
  const record = { Id: id, Name: `import-operation:${join.operationInstanceId}`, CreatedBy: actor, Permissions: [`admin:operation:POST:${route}`, `admin:operation:tenant:${tenant}`], CreatedAt: '2026-10-13T08:00:00Z', RevokedAt: '2026-10-13T08:00:01Z', ExpiresAt: '2026-10-13T08:05:00Z', KeyHash: 'never-retain', KeyPrefix: 'never-retain' };
  return { rows, record, active: 0 };
}
function verify(value) {
  const execution = importExecutionRow(value.rows, join, actor, tenant);
  return assertExecutionCredential(value.record, value.active, execution, join, actor, tenant);
}
test('joins four caller audits and the one revoked narrowly scoped execution key', () => {
  const receipt = verify(fixture());
  assert.equal(receipt.id, id);
  assert(!JSON.stringify(receipt).includes('never-retain'));
});
for (const [name, change] of [
  ['wrong creator', value => { value.record.CreatedBy = 'different-actor'; }],
  ['broader grant', value => { value.record.Permissions.push('admin'); }],
  ['wrong route', value => { value.record.Permissions[0] = 'admin:operation:POST:/api/v1/admin/other'; }],
  ['wrong tenant', value => { value.record.Permissions[1] = 'admin:operation:tenant:other'; }],
  ['unrevoked', value => { value.record.RevokedAt = null; }],
  ['still indexed', value => { value.active = 1; }],
  ['extra internal request', value => { value.rows.push(structuredClone(value.rows.at(-1))); }],
  ['caller actor drift', value => { value.rows[5].actor = 'other-user'; }],
  ['canonical actor drift', value => { value.rows[0].actor = 'other-user'; }],
  ['wrong accepted audit join', value => { const row = value.rows.at(-1); row.details = JSON.stringify({ ...JSON.parse(row.details), acceptedAuditId: 'other' }); }],
  ['different key identity', value => { value.record.Id = 'another-key'; }],
  ['long-lived credential', value => { value.record.ExpiresAt = '2026-10-14T08:05:00Z'; }],
]) test(`refuses ${name}`, () => {
  const value = fixture();
  change(value);
  assert.throws(() => verify(value));
});
