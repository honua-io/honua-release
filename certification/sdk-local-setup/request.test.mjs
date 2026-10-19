import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { requestJson } from './request.mjs';

const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${server.address().port}`)));
const close = server => new Promise(resolve => server.close(resolve));

test('owned requests retain credentials and foreign redirects receive no request', async () => {
  let foreignRequests = 0;
  const foreign = createServer((_, response) => { foreignRequests++; response.end('{}'); });
  const foreignUrl = await listen(foreign);
  const owned = createServer((request, response) => {
    assert.equal(request.headers['x-api-key'], 'private-test-key');
    if (request.url === '/redirect') {
      response.writeHead(307, { Location: foreignUrl });
    } else {
      response.writeHead(201, { 'Content-Type': 'application/json' });
    }
    response.end('{}');
  });
  const ownedUrl = await listen(owned);
  try {
    const result = await requestJson(ownedUrl, 'private-test-key', '/owned', {});
    assert.equal(result.status, 201);
    await result.json();
    await assert.rejects(requestJson(ownedUrl, 'private-test-key', '/redirect', {}));
    await assert.rejects(requestJson(ownedUrl, 'private-test-key', foreignUrl, {}), /origin mismatch/);
    assert.equal(foreignRequests, 0);
  } finally {
    await Promise.all([close(owned), close(foreign)]);
  }
});
