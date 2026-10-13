import { test } from 'node:test';
import assert from 'node:assert/strict';
import { principal, assertProposal, assertCompletedPublication, assertAppliedAudit } from './publication-contract.mjs';

const join = { proposalId: 'proposal-1', operationInstanceId: 'operation-1', correlationId: 'correlation-1', auditId: 'audit-1', itemId: 'item-1', versionId: 'version-1', contentHash: 'hash-1' };
const owner = { actor: 'owner-subject', tenant: 'tenant-a' };
const approver = { actor: 'approver-subject', tenant: 'tenant-a' };
const proposal = { proposalId: join.proposalId, requestedBy: owner.actor, status: 'AwaitingApproval' };
const handle = { ...join, status: 'Completed', auditId: 'audit-1', resourceIds: { itemId: join.itemId, versionId: join.versionId, publicationId: 'publication-1' } };
const publication = { requestId: 'publication-1', itemId: join.itemId, versionId: join.versionId, status: 'accepted' };
const version = { itemId: join.itemId, versionId: join.versionId, contentHash: join.contentHash };
const audit = { action: 'operation.applied', resource_id: join.proposalId, actor: approver.actor, correlation_id: join.correlationId, audit_id: 18, entry_hash: 'durable-hash', details: JSON.stringify({ status: 'Succeeded' }) };

test('real principal claims are singular and cannot silently select another subject', () => {
  const proof = { principals: [{ user: 'approver-a', claims: [{ type: 'sub', value: approver.actor }, { type: 'tenant_id', value: approver.tenant }] }] };
  assert.deepEqual(principal(proof, 'approver-a'), approver);
  assert.throws(() => principal(proof, 'other'));
  proof.principals[0].claims.push({ type: 'sub', value: owner.actor });
  assert.throws(() => principal(proof, 'approver-a'));
});

test('denied approval must leave the exact same unresolved proposal', () => {
  assertProposal(proposal, join, owner, 'AwaitingApproval');
  for (const change of [{ proposalId: 'other' }, { requestedBy: approver.actor }, { status: 'Succeeded' }, { resolvedBy: approver.actor }, { resolvedAt: '2026-10-12T00:00:00Z' }]) {
    assert.throws(() => assertProposal({ ...proposal, ...change }, join, owner, 'AwaitingApproval'));
  }
});

test('successful publication joins the canonical handle, resource, immutable version and hash', () => {
  assertCompletedPublication(handle, join, publication, version);
  for (const change of [{ status: 'RequiresApproval' }, { status: 'Indeterminate' }, { status: 'unknown' }, { auditId: null }, { proposalId: 'other' }, { correlationId: 'other' }, { operationInstanceId: 'other' }]) {
    assert.throws(() => assertCompletedPublication({ ...handle, ...change }, join, publication, version));
  }
  assert.throws(() => assertCompletedPublication(handle, join, { ...publication, requestId: 'other' }, version));
  assert.throws(() => assertCompletedPublication(handle, join, { ...publication, status: 'pending' }, version));
  assert.throws(() => assertCompletedPublication(handle, join, publication, { ...version, contentHash: 'other' }));
});

test('one durable applied audit must identify the separate approver and original correlation', () => {
  assert.equal(assertAppliedAudit([audit], join, approver), '18');
  for (const rows of [[], [audit, audit], [{ ...audit, actor: owner.actor }], [{ ...audit, correlation_id: 'other' }], [{ ...audit, resource_id: 'other' }], [{ ...audit, entry_hash: null }], [{ ...audit, details: JSON.stringify({ status: 'Failed' }) }]]) {
    assert.throws(() => assertAppliedAudit(rows, join, approver));
  }
});
