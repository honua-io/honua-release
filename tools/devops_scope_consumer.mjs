// Internal data-only bridge for the Python semantic judge. Reuse the canonical
// raw-kernel consumer; never mint its live WeakMap witnesses or execute a tool.
import { verifyScopeOriginal } from './devops_process_scope.mjs';

const MAX = 65 * 1024 * 1024;
try {
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX) throw Error('scope-consumer-input-bound');
    chunks.push(chunk);
  }
  const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!request || Object.getPrototypeOf(request) !== Object.prototype
      || Object.keys(request).sort().join() !== 'expected,original') throw Error('scope-consumer-request');
  const result = verifyScopeOriginal(request.original, request.expected);
  process.stdout.write(JSON.stringify({ status: 'scope-original-rejudged',
    closed: result.closed, nativeTerminal: result.nativeTerminal,
    memberCount: result.memberCount, liveOwned: result.liveOwned,
    candidateQualified: false, sixAwsCellsQualified: false }) + '\n');
} catch (error) {
  const category = /^[a-z0-9-]+$/.test(error?.message ?? '') ? error.message : 'scope-consumer-refused';
  process.stdout.write(JSON.stringify({ status: 'refused', category,
    candidateQualified: false, sixAwsCellsQualified: false }) + '\n');
  process.exitCode = 1;
}
