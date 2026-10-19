import assert from 'node:assert/strict';

export function principal(proof, user) {
  const entry = proof.principals.find(item => item.user === user);
  assert(entry, `Missing authenticated principal ${user}`);
  const claim = type => {
    const values = entry.claims.filter(item => item.type === type).map(item => item.value);
    assert.equal(values.length, 1, `Ambiguous ${type}`);
    assert(values[0]);
    return values[0];
  };
  // The /admin/packages path uses ConsolePrincipal.ResolveActorId and creates
  // a proposal without Evidence. Its approval branch preserves that raw subject,
  // unlike the canonical scheme/issuer actor on generic OperationsEndpoints.
  return { actor: claim('sub'), tenant: claim('tenant_id') };
}

export function assertProposal(value, join, owner, status) {
  assert.equal(value.proposalId, join.proposalId);
  assert.equal(value.requestedBy, owner.actor);
  assert.equal(value.status, status);
  if (status === 'AwaitingApproval') assert(!value.resolvedBy && !value.resolvedAt);
}

export function assertCompletedPublication(handle, join, publication, version) {
  assert.equal(handle.operationInstanceId, join.operationInstanceId);
  assert.equal(handle.proposalId, join.proposalId);
  assert.equal(handle.correlationId, join.correlationId);
  assert(join.auditId, 'Missing original durable operation audit identity');
  assert.equal(handle.auditId, join.auditId);
  assert.equal(handle.status, 'Completed');
  assert.equal(handle.resourceIds.itemId, join.itemId);
  assert.equal(handle.resourceIds.versionId, join.versionId);
  assert.equal(publication.requestId, handle.resourceIds.publicationId);
  assert.equal(publication.itemId, join.itemId);
  assert.equal(publication.versionId, join.versionId);
  assert.equal(publication.status, 'accepted');
  assert.equal(version.itemId, join.itemId);
  assert.equal(version.versionId, join.versionId);
  assert.equal(version.contentHash, join.contentHash);
}

export function assertAppliedAudit(rows, join, approver) {
  const applied = rows.filter(row => row.action === 'operation.applied' && row.resource_id === join.proposalId);
  assert.equal(applied.length, 1, 'Approval must have exactly one durable applied audit');
  assert.equal(applied[0].actor, approver.actor);
  assert.equal(applied[0].correlation_id, join.correlationId);
  assert(applied[0].audit_id && applied[0].entry_hash, 'Applied audit is not durably hashed');
  assert.equal(JSON.parse(applied[0].details).status, 'Succeeded');
  return String(applied[0].audit_id);
}
