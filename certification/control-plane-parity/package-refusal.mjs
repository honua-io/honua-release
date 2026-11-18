import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

export const packageTables = ['studio_content_items', 'studio_package_drafts', 'studio_content_versions',
  'saved_map_checkpoint_versions', 'studio_content_version_dependencies', 'studio_publication_requests', 'studio_rollback_requests'];

function docker(args) {
  const result = spawnSync('docker', args, { encoding: 'utf8', timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  assert.equal(result.status, 0, 'durable refusal-state readback failed');
  return result.stdout.trim();
}

// Proposals and package state are durable PostgreSQL rows; read them straight from the database.
export function queryDurableJson(query) {
  return JSON.parse(docker(['exec', 'honua-sdk1424-fixed-db-1', 'psql', '-U', 'honua', '-d', 'honua', '-Atc', query]));
}

export function snapshotPackageState() {
  // Read every fixture row, including creations under unexpected IDs. Retain only counts and hashes.
  const selects = packageTables.map(table => `select '${table}' as name, count(*) as count, encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text,E'\\n' order by to_jsonb(t)::text),''),'UTF8')),'hex') as sha256 from honua.${table} t`);
  const tables = queryDurableJson(`select json_agg(s order by name) from (${selects.join(' union all ')}) s`);
  // Each proposal hash covers the whole row: record, version, active flag and timestamps.
  const proposals = queryDurableJson(`select coalesce(json_agg(json_build_object('id', p.proposal_id, 'sha256', encode(sha256(convert_to(row_to_json(p)::text,'UTF8')),'hex')) order by p.proposal_id), '[]') from honua.operation_proposals p`);
  const activeProposalIds = queryDurableJson(`select coalesce(json_agg(proposal_id order by proposal_id), '[]') from honua.operation_proposals where active`);
  return { tables, proposals, activeProposalIds };
}

export function assertNoPackageMutation(before, after, audit) {
  assert.deepEqual(before.tables.map(table => table.name).sort(), [...packageTables].sort());
  assert(before.tables.every(table => Number.isInteger(table.count) && table.count >= 0 && /^[a-f0-9]{64}$/.test(table.sha256)));
  assert(before.proposals.length > 0, 'positive admin control must have durably created a proposal first');
  assert.deepEqual(after, before, 'scope refusal changed durable package or proposal state');
  assert(!audit.some(row => ['operation.accepted', 'operation.submitted', 'operation.proposed', 'operation.applied'].includes(row.action)), 'scope refusal accepted or actuated a mutation');
}
