import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

export function assertIntentSchema(schema, projected = false) {
  assert.equal(schema?.type, 'object', 'Published client lacks typed publication intent');
  if (!projected) assert.equal(schema.additionalProperties, false, 'Publication intent must be closed');
  assert.equal(schema.properties?.route?.type, 'string');
  assert.equal(schema.properties?.visibility?.type, 'string');
  if (!projected) assert.deepEqual(schema.properties.visibility.enum, ['personal', 'team', 'organization', 'public']);
  assert.deepEqual(Object.keys(schema.properties).sort(), ['route', 'visibility']);
}

export function routeUrl(base, intent) {
  assert.equal(intent?.visibility, 'personal', 'Replay uses only a private owned route');
  assert.match(intent?.route ?? '', /^\/sdk1424\/[a-f0-9-]{36}$/);
  return new URL(`/api/v1/studio/published${intent.route}`, base);
}

export function assertRouteIdentity(handle, publication, version, join, intent, base) {
  const expected = routeUrl(base, intent);
  assert.equal(handle.resourceIds.activeUrl, expected.pathname);
  assert.equal(publication.publicationUrl, expected.pathname);
  assert.deepEqual(publication.intent, intent);
  assert.deepEqual(version.envelope.publicationIntent, intent);
  assert.equal(version.itemId, join.itemId);
  return expected;
}

export function assertRouteArtifact(value, publication, version, join, intent) {
  assert.equal(value.route, intent.route);
  assert.equal(value.visibility, intent.visibility);
  assert.equal(value.publicationId, publication.requestId);
  for (const key of ['itemId', 'versionId', 'contentHash']) assert.equal(value[key], join[key]);
  assert.deepEqual(value.envelope, version.envelope, 'Returned route must serve the exact approved immutable envelope');
}

export async function readRoute(url, base, token, expectedStatus, fetcher = fetch) {
  const target = new URL(url);
  assert.equal(target.origin, new URL(base).origin, 'Refusing credential forwarding to a foreign route');
  assert(!target.username && !target.password && !target.search && !target.hash);
  assert.match(target.pathname, /^\/api\/v1\/studio\/published\/sdk1424\/[a-f0-9-]{36}$/);
  const response = await fetcher(target, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    redirect: 'error', signal: AbortSignal.timeout(30000),
  });
  assert.equal(response.status, expectedStatus);
  assert(response.body, 'Published response has no body');
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    assert(size <= 2 * 1024 * 1024, 'Published response exceeded bounded fixture size');
    chunks.push(chunk);
  }
  const bytes = Buffer.concat(chunks);
  return { url: target.href, status: response.status, sha256: createHash('sha256').update(bytes).digest('hex'), body: JSON.parse(bytes.toString('utf8')) };
}
