import assert from 'node:assert/strict';

const route = '/api/v1/admin/import/upload-url';

export function importExecutionRow(rows, join, originalActor, tenant) {
  const canonical = rows.filter(row => row.action.startsWith('operation.'));
  assert.equal(canonical.filter(row => row.action === 'operation.accepted').length, 1);
  assert(canonical.filter(row => row.action === 'operation.retry').length >= 3);
  assert(canonical.every(row => row.actor === originalActor && row.resource_id === join.operationInstanceId && row.correlation_id === join.correlationId));
  const posts = rows.filter(row => row.action === 'admin.post');
  const external = posts.filter(row => row.actor === originalActor);
  assert.equal(external.length, 4, 'each public adapter must retain its original actor');
  const internal = posts.filter(row => row.actor !== originalActor);
  assert.equal(internal.length, 1, 'exactly one scoped execution request is permitted');
  for (const row of posts) {
    assert.equal(row.resource_id, route);
    assert.equal(row.correlation_id, join.correlationId);
    assert.equal(row.outcome, 'Success');
    const details = JSON.parse(row.details);
    assert.equal(details.tenantId, tenant);
    assert.equal(details.method, 'POST');
    assert.equal(details.status, 202);
  }
  const row = internal[0], details = JSON.parse(row.details);
  assert.equal(details.operationInstanceId, join.operationInstanceId);
  assert.equal(details.acceptedAuditId, join.auditId);
  const match = /^apikey:api-key:([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/.exec(row.actor);
  assert(match, 'internal request must name a stable API-key identity');
  return { row, credentialId: match[1] };
}

export function assertExecutionCredential(record, activeMembership, execution, join, originalActor, tenant) {
  assert.equal(record.Id, execution.credentialId);
  assert.equal(record.Name, `import-operation:${join.operationInstanceId}`);
  assert.equal(record.CreatedBy, originalActor);
  assert.deepEqual([...record.Permissions].sort(), [
    `admin:operation:POST:${route}`, `admin:operation:tenant:${tenant}`,
  ].sort(), 'credential must authorize only the accepted route and tenant');
  assert.equal(activeMembership, 0, 'revoked execution key remains indexed for authentication');
  const created = Date.parse(record.CreatedAt), revoked = Date.parse(record.RevokedAt), expires = Date.parse(record.ExpiresAt);
  assert(Number.isFinite(created) && Number.isFinite(revoked) && Number.isFinite(expires));
  assert(revoked >= created && expires > created && expires - created <= 300000, 'execution credential lifetime must be bounded to five minutes');
  // Explicit allowlist excludes credential hash, prefix, or key material from retained evidence.
  return { id: record.Id, name: record.Name, createdBy: record.CreatedBy, permissions: record.Permissions,
    createdAt: record.CreatedAt, revokedAt: record.RevokedAt, expiresAt: record.ExpiresAt,
    activeAuthenticationIndexMembership: activeMembership, auditId: execution.row.audit_id };
}
