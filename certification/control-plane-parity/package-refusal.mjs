import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

export const packageTables = ['studio_content_items', 'studio_package_drafts', 'studio_content_versions',
  'saved_map_checkpoint_versions', 'studio_content_version_dependencies', 'studio_publication_requests', 'studio_rollback_requests'];

function docker(args) {
  const result = spawnSync('docker', args, { encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  assert.equal(result.status, 0, 'durable refusal-state readback failed');
  return result.stdout.trim();
}

export function snapshotPackageState() {
  // Read every fixture row, including creations under unexpected IDs. Retain only counts and hashes.
  const selects = packageTables.map(table => `select '${table}' as name, count(*) as count, encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text,E'\\n' order by to_jsonb(t)::text),''),'UTF8')),'hex') as sha256 from honua.${table} t`);
  const query = `select json_agg(s order by name) from (${selects.join(' union all ')}) s`;
  const tables = JSON.parse(docker(['exec', 'honua-sdk1424-fixed-db-1', 'psql', '-U', 'honua', '-d', 'honua', '-Atc', query]));
  const redis = (...args) => docker(['exec', 'honua-sdk1424-fixed-redis-1', 'redis-cli', '--raw', ...args]);
  const keys = [...new Set(redis('--scan', '--pattern', 'controlplane:proposal:*').split('\n').filter(key => /^controlplane:proposal:[^:]+$/.test(key) && !key.endsWith(':active')))].sort();
  const proposals = keys.map(key => {
    const raw = redis('GET', key), record = JSON.parse(raw);
    assert.equal(key, `controlplane:proposal:${record.proposalId}`);
    return { id: record.proposalId, sha256: createHash('sha256').update(raw).digest('hex') };
  });
  const activeProposalIds = redis('SMEMBERS', 'controlplane:proposal:active').split('\n').filter(Boolean).sort();
  return { tables, proposals, activeProposalIds };
}

export function assertNoPackageMutation(before, after, audit) {
  assert.deepEqual(before.tables.map(table => table.name).sort(), [...packageTables].sort());
  assert(before.tables.every(table => Number.isInteger(table.count) && table.count >= 0 && /^[a-f0-9]{64}$/.test(table.sha256)));
  assert(before.proposals.length > 0, 'positive admin control must have durably created a proposal first');
  assert.deepEqual(after, before, 'scope refusal changed durable package or proposal state');
  assert(!audit.some(row => ['operation.accepted', 'operation.submitted', 'operation.proposed', 'operation.applied'].includes(row.action)), 'scope refusal accepted or actuated a mutation');
}
