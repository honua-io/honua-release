import test from 'node:test';
import assert from 'node:assert/strict';
import { packageTables, assertNoPackageMutation } from './package-refusal.mjs';
const state = () => ({ tables: packageTables.map(name => ({ name, count: 1, sha256: 'a'.repeat(64) })), proposals: [{ id: 'positive-control', sha256: 'b'.repeat(64) }], activeProposalIds: ['positive-control'] });
test('unchanged live state and authorization audit satisfy refusal oracle', () => {
  assertNoPackageMutation(state(), state(), [{ action: 'auth.denied' }]);
});
for (const [name, mutate] of [
  ['new publication', after => { after.tables.find(table => table.name === 'studio_publication_requests').count++; }],
  ['mutated existing package', after => { after.tables[0].sha256 = 'c'.repeat(64); }],
  ['new terminal proposal', after => { after.proposals.push({ id: 'hidden', sha256: 'c'.repeat(64) }); }],
  ['new active proposal', after => { after.activeProposalIds.push('hidden'); }],
]) test(`refuses ${name}`, () => {
  const before = state(), after = state(); mutate(after);
  assert.throws(() => assertNoPackageMutation(before, after, []));
});
for (const action of ['operation.accepted', 'operation.submitted', 'operation.proposed', 'operation.applied']) test(`refuses ${action} despite unchanged final state`, () => {
  assert.throws(() => assertNoPackageMutation(state(), state(), [{ action }]));
});
test('missing positive proposal control fails', () => {
  const before = state(); before.proposals = [];
  assert.throws(() => assertNoPackageMutation(before, before, []));
});
