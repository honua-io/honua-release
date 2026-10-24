#!/usr/bin/env node
// Copyright (c) Honua. All rights reserved.
// Read-only witness for the external agent's exact approved map and public data.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { readFileSync, openSync, writeFileSync, closeSync } from 'node:fs';
import { isDistinctHumanApproval } from './publication_identity.mjs';

const required = name => { const value = process.env[name]?.trim(); assert(value, `${name} required`); return value; };
const requireConsole = createRequire(join(required('HONUA_AGENT_MAP_CONSOLE_ROOT'), 'e2e/playwright/package.json'));
const { chromium, request } = requireConsole('@playwright/test');
const handoff = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_FINAL_HANDOFF'), 'utf8'));
const visual = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_VISUAL_RECEIPT'), 'utf8'));
const initial = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_INITIAL_HANDOFF'), 'utf8'));
const share = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_SHARE_RECEIPT'), 'utf8'));
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
  assert(isDistinctHumanApproval(proposal, handoff.expectedRequestedBy, visual.principalId),
    'map approver must differ from the originating human and agent');
  const agentRead = await request.newContext({ baseURL: serverOrigin,
    extraHTTPHeaders: { 'X-API-Key': required('HONUA_AGENT_MAP_AGENT_KEY'), Accept: 'application/json' } });
  let operation, policyOperation, policyProposal = null;
  try {
    const response = await agentRead.get(`/api/v1/operations/handles/${encodeURIComponent(handoff.operationInstanceId)}`);
    assert.equal(response.status(), 200);
    const wire = await response.json();
    operation = wire.data ?? wire;
    const policyResponse = await agentRead.get(
      `/api/v1/operations/handles/${encodeURIComponent(share.operationInstanceId)}`);
    assert.equal(policyResponse.status(), 200, 'read exact original-service policy operation');
    const policyWire = await policyResponse.json();
    policyOperation = policyWire.data ?? policyWire;
  } finally { await agentRead.dispose(); }
  assert.equal(operation.operationInstanceId, handoff.operationInstanceId);
  assert.equal(operation.operationId, 'studio.content.create-publication-request');
  assert.equal(operation.proposalId, handoff.proposalId);
  assert.equal(share.mapProposalId, handoff.proposalId);
  assert.equal(share.serviceId, initial.service.serviceId);
  assert.equal(share.serviceName, initial.service.serviceName);
  assert.equal(share.layerId, initial.service.layerId);
  assert.equal(policyOperation.operationInstanceId, share.operationInstanceId);
  assert.equal(policyOperation.operationId, 'admin.services.access-policy.set');
  assert.equal(policyOperation.status, 'Completed', 'public read policy must complete before anonymous requests');
  assert.equal(policyOperation.proposalId ?? null, share.proposalId);
  if (share.requiresApproval) {
    assert.equal(share.status, 'RequiresApproval');
    assert(share.proposalId, 'pending policy requires its own durable proposal');
    policyProposal = await read(`/api/processes/admin/proposals/${encodeURIComponent(share.proposalId)}`);
    assert.equal(policyProposal.proposalId, share.proposalId);
    assert.equal(policyProposal.status?.toLowerCase(), 'succeeded');
    assert(isDistinctHumanApproval(policyProposal, handoff.expectedRequestedBy, visual.principalId),
      'policy approver must differ from the originating human and agent');
  } else {
    assert.equal(share.status, 'Completed');
    assert.equal(share.proposalId, null);
  }
  const policyResponseBody = JSON.parse(policyOperation.result?.details?.response ?? '{}');
  const policyData = policyResponseBody.data ?? policyResponseBody;
  assert.equal(policyResponseBody.success, true);
  assert.equal(policyData.serviceName, initial.service.serviceName);
  assert.equal(policyData.accessPolicy?.allowAnonymous, true);
  assert.equal(policyData.accessPolicy?.allowAnonymousWrite, false);
  const requestId = operation.resourceIds?.requestId;
  assert(requestId, 'operation must seal deterministic publication request ID');
  assert.equal(operation.resourceIds.itemId, handoff.itemId);
  assert.equal(operation.resourceIds.versionId, handoff.versionId);
  const version = await read(`/api/studio/v1/studio/content-items/${encodeURIComponent(handoff.itemId)}` +
    `/versions/${encodeURIComponent(handoff.versionId)}`);
  assert.equal(version.contentHash, handoff.contentHash);
  assert.deepEqual(version.envelope.body, handoff.body, 'approved version must be the agent-observed final map');
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
  const authenticatedStudioRenderedFeatureCount = await page.locator('honua-studio-canvas').evaluate((element, layerId) =>
    element.mapView.map.queryRenderedFeatures({ layers: [layerId] }).length, initial.layerId);
  assert(authenticatedStudioRenderedFeatureCount > 0, 'human Studio must retain the exact final styled map');
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
  const routeParts = handoff.route.replace(/^\//, '').split('/');
  assert(routeParts.length > 0 && routeParts.every(part => part && part !== '.' && part !== '..'
    && !/[\\%\p{Cc}]/u.test(part)), 'canonical public viewer route required');
  const publicViewerPath = '/maps/published/' + routeParts.map(encodeURIComponent).join('/');
  const definitionPath = '/api/studio/viewer/published/' + routeParts.map(encodeURIComponent).join('/');
  const sourcePath = '/api/studio/viewer/data/rest/services/' +
    initial.service.serviceName.split('/').map(encodeURIComponent).join('/') +
    `/FeatureServer/${initial.service.layerId}`;
  // This context has no operator storageState, bearer, or agent API key.
  const publicContext = await browser.newContext({baseURL: consoleOrigin});
  let anonymousPublishedRenderedFeatureCount, viewerPartition, viewerSourceFeatureCount;
  try {
    const publicPage = await publicContext.newPage();
    const dataReads = [];
    publicPage.on('response', response => {
      if (new URL(response.url()).pathname.startsWith('/api/studio/viewer/data/rest/services/'))
        dataReads.push((async () => ({response, body: await response.json()}))());
    });
    const definitionResponsePromise = publicPage.waitForResponse(response =>
      new URL(response.url()).pathname === definitionPath, {timeout: 60000});
    const sourceResponsePromise = publicPage.waitForResponse(response =>
      new URL(response.url()).pathname === `${sourcePath}/query`, {timeout: 60000});
    const documentResponse = await publicPage.goto(publicViewerPath);
    assert.equal(documentResponse?.status(), 200, 'anonymous published viewer page');
    const definitionResponse = await definitionResponsePromise;
    assert.equal(definitionResponse.status(), 200, 'anonymous viewer loaded published artifact');
    const definitionHeaders = await definitionResponse.request().allHeaders();
    for (const header of ['cookie', 'authorization', 'x-api-key'])
      assert(!definitionHeaders[header], `anonymous viewer sent ${header}`);
    viewerPartition = definitionResponse.headers()['x-honua-viewer-partition'];
    assert.match(viewerPartition ?? '', /^[0-9a-f]{64}$/);
    const definitionWire = await definitionResponse.json();
    assert.equal(definitionWire.success, true);
    const loaded = definitionWire.data;
    for (const [key, value] of Object.entries({itemId:handoff.itemId, publicationId:requestId,
      versionId:handoff.versionId, contentHash:handoff.contentHash, route:handoff.route,
      visibility:'public'})) assert.equal(loaded[key], value, `anonymous viewer artifact ${key}`);
    assert.deepEqual(loaded.envelope?.body, version.envelope.body,
      'anonymous viewer rendered the exact approved version body');
    assert(loaded.sourceCatalog?.some(source => source.id === initial.sourceId
      && source.locator?.layerId === initial.service.layerId
      && new URL(source.locator.url, serverOrigin).pathname.endsWith(
        `/rest/services/${initial.service.serviceName}/FeatureServer`)),
    'viewer source catalog must bind the original published service and layer');
    await sourceResponsePromise;
    await publicPage.waitForFunction(() => document.querySelector('[data-map-status]')?.textContent === 'Map ready.',
      undefined, {timeout: 60000});
    await publicPage.waitForFunction(() => {
      const value = document.querySelector('[data-map-canvas]')?.dataset.renderedFeatureCount;
      return value !== undefined && /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
    }, undefined, {timeout: 60000});
    anonymousPublishedRenderedFeatureCount = Number(await publicPage.locator('[data-map-canvas]')
      .getAttribute('data-rendered-feature-count'));
    const reads = await Promise.all(dataReads);
    assert(reads.length > 0, 'anonymous viewer must request public source data');
    for (const {response} of reads) {
      assert.equal(response.status(), 200, `anonymous source data ${response.url()}`);
      assert.equal(response.headers()['x-honua-viewer-partition'], viewerPartition);
      const requestHeaders = await response.request().allHeaders();
      assert.equal(requestHeaders['x-honua-viewer-partition'], viewerPartition);
      for (const header of ['cookie', 'authorization', 'x-api-key'])
        assert(!requestHeaders[header], `anonymous source data sent ${header}`);
    }
    const sourceRead = reads.find(({response, body}) => new URL(response.url()).pathname === `${sourcePath}/query`
      && body.type === 'FeatureCollection' && Array.isArray(body.features) && body.features.length > 0);
    assert(sourceRead, 'anonymous viewer must load original nonempty feature collection');
    viewerSourceFeatureCount = sourceRead.body.features.length;
  } finally { await publicContext.close(); }
  const receipt = { status:'pass', proposalId:handoff.proposalId,
    policyOperationInstanceId:share.operationInstanceId, policyProposalId:share.proposalId,
    policyStatus:policyOperation.status, policyRequestedBy:policyProposal?.requestedBy ?? null,
    policyResolvedBy:policyProposal?.resolvedBy ?? null,
    operationInstanceId:handoff.operationInstanceId, requestId,
    requestedBy:proposal.requestedBy, resolvedBy:proposal.resolvedBy,
    originConsolePartition:visual.principalId, itemId:handoff.itemId, versionId:handoff.versionId,
    contentHash:handoff.contentHash, route:handoff.route, serviceId:initial.service.serviceId,
    serviceName:initial.service.serviceName, sourceId:initial.sourceId,
    publicFeatureCount:serviceCount, authenticatedStudioRenderedFeatureCount,
    anonymousPublishedRenderedFeatureCount, viewerSourceFeatureCount, viewerPartition,
    publicationUrl:serverOrigin + `/api/v1/studio/published${handoff.route}`,
    publicViewerUrl:consoleOrigin + publicViewerPath };
  const fd = openSync(required('HONUA_AGENT_MAP_AUDIENCE_RECEIPT'), 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(receipt, null, 2) + '\n'); }
  finally { closeSync(fd); }
} finally { await browser.close(); }
