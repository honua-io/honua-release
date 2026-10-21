#!/usr/bin/env node
// Copyright (c) Honua. All rights reserved.
// Actual paired-human Console turn, after separate approval of the exact map.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { readFileSync, openSync, writeFileSync, closeSync } from 'node:fs';

const required = name => { const value = process.env[name]?.trim(); assert(value, `${name} required`); return value; };
const requireConsole = createRequire(join(required('HONUA_AGENT_MAP_CONSOLE_ROOT'), 'e2e/playwright/package.json'));
const { chromium, request } = requireConsole('@playwright/test');
const final = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_FINAL_HANDOFF'), 'utf8'));
const first = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_INITIAL_HANDOFF'), 'utf8'));
const visual = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_VISUAL_RECEIPT'), 'utf8'));
const prompt = required('HONUA_AGENT_MAP_SHARE_PROMPT');
const browser = await chromium.launch();
try {
  const context = await browser.newContext({baseURL: required('HONUA_AGENT_MAP_ORIGIN'),
    storageState: required('HONUA_AGENT_MAP_OPERATOR_STATE')});
  const page = await context.newPage();
  await page.goto('/studio');
  const sessionResponse = await page.request.get('/api/agent-workspace/session');
  assert.equal(sessionResponse.status(), 200);
  const session = await sessionResponse.json();
  assert.equal(session.authenticated, true);
  assert.equal(session.nativeAgentAvailable, false);
  assert.equal(session.principalId, visual.principalId);
  assert.equal(session.environmentId, visual.environmentId);
  const headers = {'X-Honua-Environment': session.environmentId,
    'X-Honua-Session-Partition': session.principalId};
  const read = async path => {
    const response = await page.request.get(path, {headers});
    assert.equal(response.status(), 200, `authorized ${path} read`);
    const wire = await response.json();
    return wire.data ?? wire;
  };
  const proposal = await read(`/api/processes/admin/proposals/${encodeURIComponent(final.proposalId)}`);
  assert.equal(proposal.proposalId, final.proposalId);
  assert.equal(proposal.status?.toLowerCase(), 'succeeded', 'separate map approval required before source sharing');
  assert.equal(proposal.requestedBy, final.expectedRequestedBy);
  assert(proposal.resolvedBy && proposal.resolvedBy !== proposal.requestedBy);
  const agentRead = await request.newContext({baseURL: required('HONUA_AGENT_MAP_SERVER_ORIGIN'),
    extraHTTPHeaders:{'X-API-Key':required('HONUA_AGENT_MAP_AGENT_KEY'), Accept:'application/json'}});
  let operation;
  try {
    const response = await agentRead.get(
      `/api/v1/operations/handles/${encodeURIComponent(final.operationInstanceId)}`);
    assert.equal(response.status(), 200);
    const wire = await response.json();
    operation = wire.data ?? wire;
  } finally { await agentRead.dispose(); }
  assert.equal(operation.operationInstanceId, final.operationInstanceId);
  assert.equal(operation.operationId, 'studio.content.create-publication-request');
  assert.equal(operation.proposalId, final.proposalId);
  assert.equal(operation.status, 'Completed');
  const requestId = operation.resourceIds?.requestId;
  assert(requestId, 'approved map must have a real publication request');
  const publication = await read(`/api/studio/v1/studio/content-items/${encodeURIComponent(final.itemId)}` +
    `/versions/${encodeURIComponent(final.versionId)}/publish-requests/${encodeURIComponent(requestId)}`);
  assert.equal(publication.requestId, requestId);
  assert.equal(publication.status?.toLowerCase(), 'accepted');
  assert.equal(publication.intent?.route, final.route);
  assert.equal(publication.intent?.visibility, 'public');
  const conversation = page.locator('[data-agent-workspace]');
  if (!await conversation.evaluate(element => element.open)) await conversation.locator(':scope > summary').click();
  await conversation.locator('[data-workspace-id]').fill(final.workspaceId);
  await conversation.locator('[data-workspace-connect]').click();
  await page.waitForFunction(expected =>
    document.querySelector('honua-studio-app')?.workspaceDraftIdentity?.draftId === expected,
    final.reopenedDraftId, {timeout: 60000});
  const draft = await read(`/api/studio/v1/studio/package-drafts/${encodeURIComponent(final.reopenedDraftId)}`);
  assert.equal(draft.itemId, final.itemId);
  assert.deepEqual(draft.envelope.body, final.body);
  assert.equal(first.service.serviceName, required('HONUA_AGENT_MAP_SERVICE_NAME'));
  await conversation.getByRole('textbox', {name: 'Message', exact: true}).fill(prompt);
  await conversation.getByRole('button', {name: 'Send', exact: true}).click();
  await conversation.locator('[data-workspace-status]').filter({hasText:
    'Message saved. Native agent session is not configured'}).waitFor();
  const workspaceId = await page.evaluate(() => window.honuaAgentWorkspace?.workspaceId);
  assert.equal(workspaceId, final.workspaceId);
  const history = await read(`/api/agent-workspace/${encodeURIComponent(workspaceId)}/conversation`);
  const matches = (history.messages || []).filter(message => message.role === 'user' && message.content === prompt);
  assert.equal(matches.length, 1, 'fresh explicit human sharing request required');
  const turnId = matches[0].turnId;
  assert(turnId && turnId !== final.turnId && turnId !== visual.turnId && turnId !== first.turnId);
  const viewId = await page.evaluate(() => window.honuaAgentWorkspace?.viewId);
  assert(viewId);
  const current = await read(`/api/agent-workspace/${encodeURIComponent(workspaceId)}?viewId=${encodeURIComponent(viewId)}`);
  assert.equal(current.task?.kind, 'studio-composition');
  assert.equal(current.task.id, final.reopenedDraftId);
  assert.equal(current.task.revision, draft.generation);
  const receipt = {status:'human-sharing-turn-observed', workspaceId, turnId, viewId,
    prompt, principalId:session.principalId, environmentId:session.environmentId,
    task:current.task, serviceId:first.service.serviceId, serviceName:first.service.serviceName,
    layerId:first.service.layerId, mapProposalId:final.proposalId,
    mapOperationInstanceId:final.operationInstanceId, mapItemId:final.itemId,
    mapVersionId:final.versionId, mapContentHash:final.contentHash,
    mapApproverId:proposal.resolvedBy, mapPublicationRequestId:requestId};
  const fd = openSync(required('HONUA_AGENT_MAP_SHARE_REQUEST_RECEIPT'), 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(receipt, null, 2) + '\n'); } finally { closeSync(fd); }
} finally { await browser.close(); }
