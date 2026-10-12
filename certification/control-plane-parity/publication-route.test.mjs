import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { assertIntentSchema, routeUrl, assertRouteIdentity, assertRouteArtifact, readRoute } from './publication-route.mjs';

const intent = { route: '/sdk1424/00000000-0000-4000-8000-000000000001', visibility: 'personal' };
const join = { itemId: 'item', versionId: 'version', contentHash: 'immutable-hash' };
const version = { ...join, envelope: { family: 'map', publicationIntent: intent, body: { sources: [] } } };
const publication = { ...join, requestId: 'publication', intent, publicationUrl: routeUrl('http://localhost', intent).pathname };
const handle = { resourceIds: { activeUrl: publication.publicationUrl } };
const artifact = { ...join, publicationId: publication.requestId, route: intent.route, visibility: intent.visibility, envelope: version.envelope };
const schema = { type: 'object', additionalProperties: false, properties: { route: { type: 'string' }, visibility: { type: 'string', enum: ['personal', 'team', 'organization', 'public'] } } };

test('typed intent admission refuses missing or incomplete published contracts', () => {
  assertIntentSchema(schema);
  assertIntentSchema({ ...schema, additionalProperties: true, properties: { ...schema.properties, visibility: { type: 'string' } } }, true);
  for (const value of [undefined, {}, { ...schema, additionalProperties: true }, { ...schema, properties: { route: { type: 'string' } } }, { ...schema, properties: { ...schema.properties, approvedBy: { type: 'string' } } }]) {
    assert.throws(() => assertIntentSchema(value));
  }
});

test('the returned route binds both server URLs and the sealed requested audience', () => {
  assert.equal(assertRouteIdentity(handle, publication, version, join, intent, 'http://localhost').pathname, publication.publicationUrl);
  assert.throws(() => assertRouteIdentity({ resourceIds: { activeUrl: 'https://foreign.test' } }, publication, version, join, intent, 'http://localhost'));
  assert.throws(() => assertRouteIdentity(handle, { ...publication, intent: { ...intent, visibility: 'public' } }, version, join, intent, 'http://localhost'));
  assert.throws(() => assertRouteIdentity(handle, publication, { ...version, envelope: {} }, join, intent, 'http://localhost'));
  assert.throws(() => routeUrl('http://localhost', { ...intent, route: '//foreign.test' }));
});

test('readback cannot substitute another item, version, publication, hash or envelope', () => {
  assertRouteArtifact(artifact, publication, version, join, intent);
  for (const key of ['itemId', 'versionId', 'contentHash', 'publicationId', 'route', 'visibility']) {
    assert.throws(() => assertRouteArtifact({ ...artifact, [key]: 'wrong' }, publication, version, join, intent));
  }
  assert.throws(() => assertRouteArtifact({ ...artifact, envelope: { ...version.envelope, body: {} } }, publication, version, join, intent));
});

test('route read stops when a response exceeds the bounded fixture size', async () => {
  const response = new Response(new Uint8Array(2 * 1024 * 1024 + 1));
  await assert.rejects(readRoute(routeUrl('http://localhost', intent), 'http://localhost', null, 200, async () => response), /bounded fixture size/);
});

test('actual HTTP read refuses redirect and foreign origin without leaking the bearer', async () => {
  const observed = [];
  const foreign = createServer((request, response) => { observed.push(request.headers.authorization); response.end('{}'); });
  foreign.listen(0, '127.0.0.1'); await once(foreign, 'listening');
  const foreignBase = `http://127.0.0.1:${foreign.address().port}`;
  let mode = 'read';
  const target = createServer((request, response) => {
    if (mode === 'redirect') { response.writeHead(302, { location: `${foreignBase}${request.url}` }); response.end(); return; }
    assert.equal(request.headers.authorization, mode === 'anonymous' ? undefined : 'Bearer private-test-token');
    response.writeHead(mode === 'anonymous' ? 401 : 200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ data: artifact }));
  });
  target.listen(0, '127.0.0.1'); await once(target, 'listening');
  const base = `http://127.0.0.1:${target.address().port}`;
  try {
    const read = await readRoute(routeUrl(base, intent), base, 'private-test-token', 200);
    assertRouteArtifact(read.body.data, publication, version, join, intent);
    assert.match(read.sha256, /^[a-f0-9]{64}$/);
    mode = 'anonymous'; await readRoute(routeUrl(base, intent), base, null, 401);
    mode = 'redirect'; await assert.rejects(readRoute(routeUrl(base, intent), base, 'private-test-token', 200));
    await assert.rejects(readRoute(routeUrl(foreignBase, intent), base, 'private-test-token', 200));
    assert.deepEqual(observed, []);
  } finally {
    target.closeAllConnections(); foreign.closeAllConnections();
    await Promise.all([new Promise(resolve => target.close(resolve)), new Promise(resolve => foreign.close(resolve))]);
  }
});
