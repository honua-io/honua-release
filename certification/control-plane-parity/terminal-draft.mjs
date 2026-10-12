import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

export function terminalDraft({ consumer, run, baseUrl, token, identity, input, dryRun = false }) {
  const env = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'LANG'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  Object.assign(env, { HONUA_BASE_URL: baseUrl, HONUA_CONFIG_HOME: `${run}/terminal-draft-config` });
  if (token) env.HONUA_BEARER_TOKEN = token;
  const args = [`${consumer}/node_modules/@honua/sdk-js/dist/src/cli/bin.js`, 'studio', 'draft', 'save-version', input.draftId, '--generation', String(input.generation), '--json', dryRun ? '--dry-run' : '--yes'];
  if (identity) args.push('--actor', identity.actor, '--tenant', identity.tenantId);
  const result = spawnSync(process.execPath, args, { env, encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 });
  assert(!result.error && result.status !== null, 'Terminal draft invocation failed to start or timed out; never retry mutation');
  for (const output of [result.stdout, result.stderr]) if (token) assert(!output.includes(token), 'Terminal leaked bearer');
  return { exitCode: result.status, value: JSON.parse((result.stdout || result.stderr).trim()) };
}

export function assertTenantParity(results, surfaces) {
  assert.deepEqual(results.map(item => item.surface).sort(), [...surfaces].sort());
  for (const item of results) {
    assert(!item.unexpectedSuccess && item.error, 'Cross-tenant operation unexpectedly succeeded');
    assert.equal(item.error.kind, 'honua.command.error.v1');
    assert.equal(item.error.commandId, 'studio.draft.saveVersion');
    assert([403, 404].includes(item.error.statusCode));
    assert.equal(item.error.errorKind, item.error.statusCode === 403 ? 'authorization' : 'transport');
    assert(item.error.correlationId && item.error.idempotencyKey);
    if (item.surface === 'cli') assert.notEqual(item.exitCode, 0);
    if (item.surface === 'mcp') assert.equal(item.isError, true);
  }
  for (const key of ['commandId', 'statusCode', 'errorKind', 'correlationId', 'idempotencyKey']) {
    assert.equal(new Set(results.map(item => item.error[key])).size, 1, `Cross-tenant ${key} differs`);
  }
  return results[0].error.correlationId;
}

export function operatorAuditActor(token, subject) {
  // Test oracle for the canonical S4 operator bearer, already accepted by real
  // authenticated probes. Decoding here grants no authority and retains no token.
  // OidcAuthenticationExtensions.OperatorBearerScheme + CanonicalSecurityActor.Resolve.
  const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
  assert.equal(claims.iss, 'honua-operator-bearer');
  assert.equal(claims.sub, subject);
  assert.match(subject, /^[a-f0-9-]{36}$/);
  return `operatorbearer:subject:honua-operator-bearer:${subject}`;
}

export function assertTenantAudit(rows, draftId, correlationId, actor, count) {
  const denials = rows.filter(row => row.action === 'studio.read_draft');
  assert.equal(denials.length, count, 'One canonical authorization audit is required for each adapter pre-read');
  assert.equal(new Set(denials.map(row => String(row.audit_id))).size, count);
  for (const row of denials) {
    assert.equal(row.correlation_id, correlationId);
    assert.equal(row.actor, actor);
    assert.equal(row.resource_type, 'studio-package-draft');
    assert.equal(row.resource_id, draftId);
    assert.equal(row.outcome, 'Denied');
    assert.equal(JSON.parse(row.details).code, 'studio_authorization/cross_tenant_denied');
    assert(row.audit_id && row.entry_hash, 'Authorization audit must be durably hashed');
  }
  return denials.map(row => String(row.audit_id));
}

export function assertDraftOwnership(draft, expected, persisted) {
  assert.equal(draft.draftId, expected.draftId);
  assert(typeof draft.ownerId === 'string' && draft.ownerId.length > 0, 'Public draft owner must be present');
  assert.equal(draft.ownerId, expected.actor);
  if (persisted !== undefined) {
    assert.equal(persisted.length, 1, 'Exactly one persisted draft ownership row is required');
    assert.equal(persisted[0].draft_id, expected.draftId);
    assert.equal(persisted[0].owner_id, expected.actor);
    assert(typeof persisted[0].tenant_id === 'string' && persisted[0].tenant_id.length > 0, 'Persisted tenant binding must be present');
    assert.equal(persisted[0].tenant_id, expected.tenantId);
  }
}
