import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { terminalDraft } from './terminal-draft.mjs';

const run = process.env.HONUA_PARITY_RUN, consumer = process.env.HONUA_PARITY_CONSUMER;
const provenance = JSON.parse(await readFile(`${consumer}/provenance.json`, 'utf8'));
assert.equal(provenance.kind, 'public-npm-default-resolution');
// A dry run needs no credential or live draft. The unreachable target cannot mutate the fixture.
const result = terminalDraft({ consumer, run, baseUrl: 'http://127.0.0.1:1', input: { draftId: '00000000-0000-4000-8000-000000000001', generation: 0 }, dryRun: true });
assert.equal(result.exitCode, 0, 'Published terminal lacks the shared draft save-version verb');
assert.equal(result.value.commandId, 'studio.draft.saveVersion');
assert.equal(result.value.status, 'dry-run');
await writeFile(`${run}/terminal-draft-admission.json`, JSON.stringify({ kind: 'verified-published-terminal-draft-admission', publishedClients: provenance, dryRun: result, scope: 'Actual installed executable accepts the shared command; no live draft or mutation proof' }, null, 2) + '\n', { mode: 0o600 });
