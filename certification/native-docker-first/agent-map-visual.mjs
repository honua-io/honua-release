#!/usr/bin/env node
// Copyright (c) Honua. All rights reserved.
// Actual paired-human Console UI actions for the external client's already attached map.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { readFileSync, writeFileSync, openSync, closeSync } from 'node:fs';

const required = name => {
  const value = process.env[name]?.trim();
  assert(value, `${name} is required`);
  return value;
};
const consoleRoot = required('HONUA_AGENT_MAP_CONSOLE_ROOT');
const requireConsole = createRequire(join(consoleRoot, 'e2e/playwright/package.json'));
const { chromium } = requireConsole('@playwright/test');
const handoff = JSON.parse(readFileSync(required('HONUA_AGENT_MAP_HANDOFF'), 'utf8'));
const origin = required('HONUA_AGENT_MAP_ORIGIN');
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ baseURL: origin, storageState: required('HONUA_AGENT_MAP_OPERATOR_STATE') });
  const page = await context.newPage();
  let session;
  const read = async path => {
    const headers = session ? { 'X-Honua-Environment': session.environmentId,
      'X-Honua-Session-Partition': session.principalId } : {};
    const response = await page.request.get(path, { headers });
    assert.equal(response.status(), 200, `authorized ${path} read`);
    const body = await response.json();
    return body.data ?? body;
  };
  await page.goto('/studio');
  session = await read('/api/agent-workspace/session');
  assert.equal(session.authenticated, true);
  assert.equal(session.nativeAgentAvailable, false, 'external-only human turn must not invoke a second model');
  const conversation = page.locator('[data-agent-workspace]');
  if (!await conversation.evaluate(element => element.open)) await conversation.locator(':scope > summary').click();
  const workspaceInput = conversation.locator('[data-workspace-id]');
  await workspaceInput.fill(handoff.workspaceId);
  await conversation.locator('[data-workspace-connect]').click();
  await page.locator('honua-studio-app').waitFor();
  await page.waitForFunction(expected => {
    const app = document.querySelector('honua-studio-app');
    return app?.workspaceDraftIdentity?.draftId === expected;
  }, handoff.reopenedDraftId, { timeout: 60000 });
  const draftPath = id => `/api/studio/v1/studio/package-drafts/${encodeURIComponent(id)}`;
  const before = await read(draftPath(handoff.reopenedDraftId));
  assert.equal(before.itemId, handoff.itemId);
  assert.equal(before.packageKey, handoff.packageKey);
  assert.equal(before.envelope.body.layers?.find(layer => layer.id === handoff.layerId)?.sourceId, handoff.sourceId);
  const source = handoff.service;
  const servicePath = source.serviceName.split('/').map(encodeURIComponent).join('/');
  const featurePath = `/rest/services/${servicePath}/FeatureServer/${source.layerId}`;
  const metadata = await read(`/api/studio/data${featurePath}?f=json`);
  const sample = await read(`/api/studio/data${featurePath}/query?where=1%3D1&outFields=*&returnGeometry=false&resultRecordCount=20&f=json`);
  const candidates = (metadata.fields || []).map(field => ({
    name: field.name,
    values: [...new Set((sample.features || []).map(feature => feature.attributes?.[field.name])
      .filter(value => typeof value === 'string' || typeof value === 'number' && Number.isFinite(value)))],
  }));
  const thematic = candidates.find(candidate => candidate.values.length >= 2);
  assert(thematic, 'actual authorized source needs two observed thematic values');
  await page.getByRole('button', { name: 'Edit map style', exact: true }).click();
  const editor = page.getByRole('region', { name: 'Map style editor', exact: true });
  await editor.waitFor();
  await editor.frameLocator('iframe[title="Maputnik visual map style editor"]').locator('.maputnik-layout').waitFor();
  const initialized = await read(draftPath(handoff.reopenedDraftId));
  const rendered = initialized.envelope.body.mapSpec?.layers?.find(layer => layer.id === handoff.layerId);
  assert(rendered && ['circle', 'fill', 'line', 'symbol'].includes(rendered.type), 'real initialized map render layer required');
  const prefix = rendered.type === 'symbol' ? 'text' : rendered.type;
  const colorProperty = `${prefix}-color`, opacityProperty = `${prefix}-opacity`;
  const expression = ['match', ['get', thematic.name], thematic.values[0], '#eff3ff', '#c6dbef'];
  await editor.getByRole('combobox', { name: 'Map layer for styling', exact: true }).selectOption(handoff.layerId);
  await editor.getByRole('combobox', { name: 'Actual source field', exact: true }).selectOption(thematic.name);
  await editor.getByRole('combobox', { name: 'Thematic method', exact: true }).selectOption('categories');
  await editor.getByRole('textbox', { name: 'Explicit thematic values as JSON array', exact: true })
    .fill(JSON.stringify([thematic.values[0]]));
  await editor.getByRole('button', { name: 'Preview thematic colors', exact: true }).click();
  await page.waitForFunction(({ layerId, property, expected }) =>
    JSON.stringify(document.querySelector('honua-studio-canvas')?.mapView?.map?.getPaintProperty(layerId, property))
      === JSON.stringify(expected), { layerId: handoff.layerId, property: colorProperty, expected: expression });
  await editor.getByRole('button', { name: 'Save map style', exact: true }).click();
  let after;
  for (let attempt = 0; attempt < 60; attempt++) {
    after = await read(draftPath(handoff.reopenedDraftId));
    if (JSON.stringify(after.envelope.body.mapSpec?.layers?.find(layer => layer.id === handoff.layerId)
      ?.paint?.[colorProperty]) === JSON.stringify(expression)) break;
    await page.waitForTimeout(500);
  }
  assert(after.generation > before.generation, 'visual save must advance the exact draft');
  assert.deepEqual(after.envelope.body.mapSpec.layers.find(layer => layer.id === handoff.layerId).paint[colorProperty], expression);
  await page.getByRole('button', { name: 'Return to map', exact: true }).click();
  await page.waitForFunction(layerId => {
    const map = document.querySelector('honua-studio-canvas')?.mapView?.map;
    return map?.getLayer(layerId) && map.queryRenderedFeatures({ layers: [layerId] }).length > 0;
  }, handoff.layerId, { timeout: 60000 });
  const prompt = `In current map ${handoff.packageKey}, change only render layer ${handoff.layerId} ` +
    `paint property ${opacityProperty} to 0.55. Preserve its existing ${colorProperty} thematic expression ` +
    `over field ${thematic.name}, all sources and all other layers. Save and reopen this same map, then ` +
    `propose publication of its exact final version at ${handoff.route}; do not approve it.`;
  await conversation.getByRole('textbox', { name: 'Message', exact: true }).fill(prompt);
  await conversation.getByRole('button', { name: 'Send', exact: true }).click();
  await conversation.locator('[data-workspace-status]').filter({ hasText: 'Message saved. Native agent session is not configured' }).waitFor();
  const workspace = await page.evaluate(() => window.honuaAgentWorkspace?.workspaceId);
  assert.equal(workspace, handoff.workspaceId);
  const history = await read(`/api/agent-workspace/${encodeURIComponent(workspace)}/conversation`);
  const matches = (history.messages || []).filter(message => message.role === 'user' && message.content === prompt);
  assert.equal(matches.length, 1, 'fresh exact human prompt must be unique');
  const turnId = matches[0].turnId;
  assert(turnId && turnId !== handoff.turnId, 'fresh actual human turn required');
  const viewId = await page.evaluate(() => window.honuaAgentWorkspace?.viewId);
  assert(viewId, 'actual Console view required');
  const current = await read(`/api/agent-workspace/${encodeURIComponent(workspace)}?viewId=${encodeURIComponent(viewId)}`);
  assert.equal(current.task?.kind, 'studio-composition');
  assert.equal(current.task.id, handoff.reopenedDraftId);
  assert.equal(current.task.revision, after.generation);
  const receipt = { status: 'human-visual-edit-and-turn-observed', workspaceId: workspace,
    turnId, draftId: handoff.reopenedDraftId, itemId: handoff.itemId, packageKey: handoff.packageKey,
    generation: after.generation, layerId: handoff.layerId, sourceId: handoff.sourceId,
    body: after.envelope.body,
    field: thematic.name, observedValue: thematic.values[0], colorProperty, colorExpression: expression,
    opacityProperty, prompt, principalId: session.principalId, environmentId: session.environmentId };
  const path = required('HONUA_AGENT_MAP_VISUAL_RECEIPT');
  const fd = openSync(path, 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(receipt, null, 2) + '\n'); } finally { closeSync(fd); }
} finally { await browser.close(); }
