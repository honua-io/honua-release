import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertTenantParity, assertTenantAudit, operatorAuditActor, assertDraftOwnership } from './terminal-draft.mjs';

const surfaces = ['cli', 'sdk', 'studio', 'mcp'];
const error = { kind: 'honua.command.error.v1', errorKind: 'authorization', commandId: 'studio.draft.saveVersion', correlationId: 'same-correlation', idempotencyKey: 'same-key', statusCode: 403 };
const results = surfaces.map(surface => ({ surface, error, ...(surface === 'cli' ? { exitCode: 2 } : {}), ...(surface === 'mcp' ? { isError: true } : {}) }));
const subject = '00000000-0000-4000-8000-000000000001';
const actor = `operatorbearer:subject:honua-operator-bearer:${subject}`;
const row = { actor, correlation_id: error.correlationId, resource_type: 'studio-package-draft', resource_id: 'owned-draft', action: 'studio.read_draft', outcome: 'Denied', details: JSON.stringify({ code: 'studio_authorization/cross_tenant_denied' }), entry_hash: 'durable-hash' };
const rows = surfaces.map((_, index) => ({ ...row, audit_id: String(index + 1) }));

test('all four real surface labels require the same shared refusal identity', () => {
  assert.equal(assertTenantParity(results, surfaces), error.correlationId);
  assert.throws(() => assertTenantParity(results.slice(1), surfaces));
  for (const change of [{ correlationId: 'other' }, { idempotencyKey: 'other' }, { commandId: 'other' }, { statusCode: 404 }, { errorKind: 'transport' }, { kind: 'untyped' }]) {
    assert.throws(() => assertTenantParity([{ ...results[0], error: { ...error, ...change } }, ...results.slice(1)], surfaces));
  }
  assert.throws(() => assertTenantParity([{ ...results[0], exitCode: 0 }, ...results.slice(1)], surfaces));
  assert.throws(() => assertTenantParity([...results.slice(0, 3), { ...results[3], isError: false }], surfaces));
});

test('durable denial rows must bind every adapter to the same actor, object and policy', () => {
  assert.deepEqual(assertTenantAudit(rows, 'owned-draft', error.correlationId, actor, 4), ['1', '2', '3', '4']);
  assert.throws(() => assertTenantAudit(rows.slice(1), 'owned-draft', error.correlationId, actor, 4));
  for (const change of [{ actor: subject }, { resource_id: 'foreign-draft' }, { correlation_id: 'other' }, { outcome: 'Success' }, { entry_hash: null }, { details: JSON.stringify({ code: 'other' }) }, { audit_id: '2' }]) {
    assert.throws(() => assertTenantAudit([{ ...rows[0], ...change }, ...rows.slice(1)], 'owned-draft', error.correlationId, actor, 4));
  }
});

test('expected canonical actor uses the real probe subject and fixed S4 operator issuer', () => {
  // This unsigned string tests the oracle only; it is never used for authentication.
  const token = claims => `header.${Buffer.from(JSON.stringify(claims)).toString('base64url')}.signature`;
  assert.equal(operatorAuditActor(token({ iss: 'honua-operator-bearer', sub: subject }), subject), actor);
  assert.throws(() => operatorAuditActor(token({ iss: 'foreign', sub: subject }), subject));
  assert.throws(() => operatorAuditActor(token({ iss: 'honua-operator-bearer', sub: 'other' }), subject));
});

test('ownership requires the observed public owner and real persisted tenant binding', () => {
  const draft = { draftId: 'owned-draft', ownerId: subject };
  const expected = { draftId: draft.draftId, actor: subject, tenantId: 'tenant-a' };
  const persisted = [{ draft_id: draft.draftId, owner_id: subject, tenant_id: expected.tenantId }];
  assertDraftOwnership(draft, expected, persisted);
  for (const ownerId of [undefined, null, '', 'another-owner']) assert.throws(() => assertDraftOwnership({ ...draft, ownerId }, expected, persisted));
  for (const change of [{ draft_id: 'other-draft' }, { owner_id: 'other-owner' }, { tenant_id: undefined }, { tenant_id: '' }, { tenant_id: 'tenant-b' }]) assert.throws(() => assertDraftOwnership(draft, expected, [{ ...persisted[0], ...change }]));
  assert.throws(() => assertDraftOwnership(draft, expected, []));
  assert.throws(() => assertDraftOwnership(draft, expected, [...persisted, ...persisted]));
});
