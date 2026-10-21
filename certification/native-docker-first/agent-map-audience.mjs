#!/usr/bin/env node
// Copyright (c) Honua. All rights reserved.
// Read-only witness for the external agent's exact approved map and public data.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { readFileSync, openSync, writeFileSync, closeSync } from 'node:fs';

const required = name => { const value = process.env[name]?.trim(); assert(value, `${name} required`); return value; };
const requireConsole = createRequire(join(required('HONUA_AGENT_MAP_CONSOLE_ROOT'), 'e2e/playwright/package.json'));
const { chromium, request } = requireConsole('@playwright/test');
const handoff = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_FINAL_HANDOFF'), 'utf8'));
const visual = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_VISUAL_RECEIPT'), 'utf8'));
const initial = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_INITIAL_HANDOFF'), 'utf8'));
const consoleOrigin = required('HONUA_AGENT_MAP_ORIGIN'), serverOrigin = required('HONUA_AGENT_MAP_SERVER_ORIGIN');
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ baseURL: consoleOrigin,
    storageState: required('HONUA_AGENT_MAP_OPERATOR_STATE') });
  const page = await context.newPage();
  await page.goto('/studio');
  const sessionResponse = await page.request.get('/api/agent-workspace/session');
  assert.equal(sessionResponse.status(), 200);
  const session = await sessionResponse.json();
  assert.equal(session.authenticated, true);
  assert.equal(session.nativeAgentAvailable, false);
  assert.equal(session.principalId, visual.principalId);
  assert.equal(session.environmentId, visual.environmentId);
  const headers = { 'X-Honua-Environment': session.environmentId,
    'X-Honua-Session-Partition': session.principalId };
  const read = async path => {
    const response = await page.request.get(path, { headers });
    assert.equal(response.status(), 200, `authorized ${path} read`);
    const wire = await response.json();
    return wire.data ?? wire;
  };
  const proposal = await read(`/api/processes/admin/proposals/${encodeURIComponent(handoff.proposalId)}`);
  assert.equal(proposal.proposalId, handoff.proposalId);
  assert.equal(proposal.status?.toLowerCase(), 'succeeded');
  assert(proposal.requestedBy && proposal.resolvedBy);
  assert.notEqual(proposal.requestedBy, proposal.resolvedBy, 'proposer cannot approve');
  const agentRead = await request.newContext({ baseURL: serverOrigin,
    extraHTTPHeaders: { 'X-API-Key': required('HONUA_AGENT_MAP_AGENT_KEY'), Accept: 'application/json' } });
  let operation;
  try {
    const response = await agentRead.get(`/api/v1/operations/handles/${encodeURIComponent(handoff.operationInstanceId)}`);
    assert.equal(response.status(), 200);
    const wire = await response.json();
    operation = wire.data ?? wire;
  } finally { await agentRead.dispose(); }
  assert.equal(operation.operationInstanceId, handoff.operationInstanceId);
  assert.equal(operation.operationId, 'studio.content.create-publication-request');
  assert.equal(operation.proposalId, handoff.proposalId);
  const requestId = operation.resourceIds?.requestId;
  assert(requestId, 'operation must seal deterministic publication request ID');
  assert.equal(operation.resourceIds.itemId, handoff.itemId);
  assert.equal(operation.resourceIds.versionId, handoff.versionId);
  const version = await read(`/api/studio/v1/studio/content-items/${encodeURIComponent(handoff.itemId)}` +
    `/versions/${encodeURIComponent(handoff.versionId)}`);
  assert.equal(version.contentHash, handoff.contentHash);
  assert.equal(createHash('sha256').update(JSON.stringify(version.envelope.body)).digest('hex'), handoff.bodySha256);
  const publication = await read(`/api/studio/v1/studio/content-items/${encodeURIComponent(handoff.itemId)}` +
    `/versions/${encodeURIComponent(handoff.versionId)}/publish-requests/${encodeURIComponent(requestId)}`);
  assert.equal(publication.requestId, requestId);
  assert.equal(publication.itemId, handoff.itemId);
  assert.equal(publication.versionId, handoff.versionId);
  assert.equal(publication.status?.toLowerCase(), 'accepted');
  assert.equal(publication.intent?.route, handoff.route);
  assert.equal(publication.intent?.visibility, 'public');
  const conversation = page.locator('[data-agent-workspace]');
  if (!await conversation.evaluate(element => element.open)) await conversation.locator(':scope > summary').click();
  await conversation.locator('[data-workspace-id]').fill(handoff.workspaceId);
  await conversation.locator('[data-workspace-connect]').click();
  await page.waitForFunction(expected => document.querySelector('honua-studio-app')?.workspaceDraftIdentity?.draftId
    === expected, handoff.reopenedDraftId, { timeout: 60000 });
  const draft = await read(`/api/studio/v1/studio/package-drafts/${encodeURIComponent(handoff.reopenedDraftId)}`);
  assert.equal(draft.itemId, handoff.itemId);
  assert.deepEqual(draft.envelope.body, version.envelope.body);
  const render = draft.envelope.body.mapSpec?.layers?.find(layer => layer.id === initial.layerId);
  assert(render, 'exact source render layer required');
  assert.deepEqual(render.paint?.[visual.colorProperty], visual.colorExpression);
  assert.equal(render.paint?.[visual.opacityProperty], 0.55);
  await page.waitForFunction(layerId => {
    const map = document.querySelector('honua-studio-canvas')?.mapView?.map;
    return map?.getLayer(layerId) && map.queryRenderedFeatures({ layers: [layerId] }).length > 0;
  }, initial.layerId, { timeout: 60000 });
  const renderedFeatureCount = await page.locator('honua-studio-canvas').evaluate((element, layerId) =>
    element.mapView.map.queryRenderedFeatures({ layers: [layerId] }).length, initial.layerId);
  const anonymous = await request.newContext({ baseURL: serverOrigin,
    extraHTTPHeaders: { Accept: 'application/json' } });
  let artifact, serviceCount;
  try {
    const route = await anonymous.get(`/api/v1/studio/published${handoff.route}`);
    assert.equal(route.status(), 200, 'unauthenticated public map route');
    const routeWire = await route.json();
    artifact = routeWire.data ?? routeWire;
    for (const [key, value] of Object.entries({itemId:handoff.itemId, publicationId:requestId,
      versionId:handoff.versionId,contentHash:handoff.contentHash,route:handoff.route,visibility:'public'}))
      assert.equal(artifact[key], value, `public artifact ${key}`);
    const service = initial.service.serviceName.split('/').map(encodeURIComponent).join('/');
    const layer = initial.service.layerId;
    const metadata = await anonymous.get(`/rest/services/${service}/FeatureServer/${layer}?f=json`);
    assert.equal(metadata.status(), 200, 'unauthenticated public layer metadata');
    const metadataJson = await metadata.json();
    assert(!metadataJson.error && Array.isArray(metadataJson.fields));
    const query = await anonymous.get(`/rest/services/${service}/FeatureServer/${layer}` +
      '/query?where=1%3D1&returnCountOnly=true&f=json');
    assert.equal(query.status(), 200, 'unauthenticated public feature query');
    const data = await query.json();
    serviceCount = data.count;
    assert.equal(serviceCount, initial.service.featureCount, 'public audience sees the original physical features');
  } finally { await anonymous.dispose(); }
  const receipt = { status:'pass', proposalId:handoff.proposalId,
    operationInstanceId:handoff.operationInstanceId, requestId,
    requestedBy:proposal.requestedBy, resolvedBy:proposal.resolvedBy,
    originConsolePartition:visual.principalId, itemId:handoff.itemId, versionId:handoff.versionId,
    contentHash:handoff.contentHash, route:handoff.route, serviceId:initial.service.serviceId,
    serviceName:initial.service.serviceName, sourceId:initial.sourceId,
    publicFeatureCount:serviceCount, renderedFeatureCount,
    publicationUrl:serverOrigin + `/api/v1/studio/published${handoff.route}` };
  const fd = openSync(required('HONUA_AGENT_MAP_AUDIENCE_RECEIPT'), 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(receipt, null, 2) + '\n'); }
  finally { closeSync(fd); }
} finally { await browser.close(); }
