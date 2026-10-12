// Harness-only fixture setup. SDK imports resolve from the verified installed consumer.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { requestJson } from './request.mjs';
import { McpClient } from '@honua/sdk-js/studio-agent';

const [stage, root] = process.argv.slice(2);
try {
  assert.ok(['fixture', 'capture'].includes(stage) && root);
  const state = JSON.parse(await readFile(path.join(root, 'state.json'), 'utf8'));
  const config = JSON.parse(await readFile(path.join(root, 'stack/.mcp.json'), 'utf8')).mcpServers.honua.env;
  const baseUrl = `http://127.0.0.1:${state.port}`;
  assert.equal(config.HONUA_MCP_REMOTE_URL, `${baseUrl}/mcp`);
  assert.equal(typeof config.HONUA_ADMIN_KEY, 'string');
  const client = new McpClient({ baseUrl, apiKey: config.HONUA_ADMIN_KEY, workflowView: 'setup' });
  await client.listAllTools({ view: 'setup' });
  const call = async (name, args) => {
    const result = await client.callTool(name, args);
    assert.notEqual(result.isError, true);
    assert.ok(result.structuredContent);
    return result.structuredContent;
  };
  const save = (name, body) => writeFile(path.join(root, name), `${JSON.stringify(body, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
  if (stage === 'capture') {
    const receipt = JSON.parse(await readFile(path.join(root, 'qualification.json'), 'utf8'));
    assert.equal(receipt.pass, true);
    const result = await client.readResource(receipt.styleRender.uri);
    const blob = result.contents?.find(value => value.uri === receipt.styleRender.uri && typeof value.blob === 'string');
    assert.ok(blob && blob.mimeType === 'image/png');
    const png = Buffer.from(blob.blob, 'base64');
    assert.equal(createHash('sha256').update(png).digest('hex'), receipt.styleRender.imageSha256);
    assert.equal(png.length, receipt.styleRender.byteLength);
    await writeFile(path.join(root, 'rendered.png'), png, { mode: 0o600, flag: 'wx' });
  } else {
    const request = async (route, body, expectedStatus, extraHeaders = {}) => {
      const response = await requestJson(baseUrl, config.HONUA_ADMIN_KEY, route, body, extraHeaders);
      assert.equal(response.status, expectedStatus);
      return response.json();
    };
    const created = await request('/api/v1/admin/connections', {
      name: 'installer-qualified-fixture', host: 'postgres', port: 5432, databaseName: 'honua', username: 'honua',
      secretReference: 'env:HONUA_LOCAL_DB_PASSWORD', secretType: 'environment', sslRequired: false, sslMode: 'Disable',
    }, 201);
    const connectionId = created.data.connectionId;
    assert.equal(typeof connectionId, 'string');
    const tested = await request(`/api/v1/admin/connections/${encodeURIComponent(connectionId)}/test`, {}, 200);
    assert.equal(tested.data.isHealthy, true);
    const published = await request(`/api/v1/admin/connections/${encodeURIComponent(connectionId)}/layers`, {
      schema: 'public', table: 'zero_to_map_parcels', layerName: 'Installer qualification parcels',
      serviceName: 'installer-qualified-fixture', geometryColumn: 'geometry', geometryType: 'Polygon',
      srid: 4326, primaryKey: 'parcel_id', enabled: true,
    }, 201);
    assert.ok(Number.isInteger(published.data.layerId));
    const rows = [];
    let offset = 0;
    for (let page = 0; page < 20; page++) {
      const listing = await call('honua_list_layers', { filter: 'installer-qualified-fixture', limit: 100, offset });
      assert.ok(Array.isArray(listing.layers));
      rows.push(...listing.layers);
      if (listing.hasMore === false) break;
      assert.ok(Number.isInteger(listing.nextOffset) && listing.nextOffset > offset && page < 19);
      offset = listing.nextOffset;
    }
    const matches = rows.filter(row => row.layerId === published.data.layerId && row.serviceName === 'installer-qualified-fixture');
    assert.equal(matches.length, 1);
    const target = { serviceId: matches[0].serviceId, layerId: matches[0].layerId };
    assert.equal(typeof target.serviceId, 'string');
    const styleId = 'installer-qualified-parcels';
    const style = await request('/ogc/styles', {
      version: 8, name: 'Installer qualification polygon fixture',
      sources: { parcels: { type: 'vector', url: `${baseUrl}/ogc/tiles/collections/${target.layerId}` } },
      layers: [
        { id: 'parcel-fill', type: 'fill', source: 'parcels', 'source-layer': String(target.layerId), paint: { 'fill-color': '#e45826', 'fill-opacity': 0.8 } },
        { id: 'parcel-outline', type: 'line', source: 'parcels', 'source-layer': String(target.layerId), paint: { 'line-color': '#213547', 'line-width': 3 } },
      ],
    }, 201, { 'X-Style-Id': styleId });
    assert.equal(style.id, styleId);
    const preview = await call('honua_apply_style_preset', { ...target, styleId, dryRun: true });
    assert.equal(preview.applied, false);
    const applied = await call('honua_apply_style_preset', { ...target, styleId });
    assert.equal(applied.applied, true);
    const confirmed = await call('honua_get_style', { ...target, includeStylesheet: true });
    assert.equal(confirmed.styleId, styleId);
    assert.ok(Number.isInteger(confirmed.styleVersion) && confirmed.styleVersion >= 1);
    await save('fixture.json', { scope: 'harness-owned installer seed publication', ...target, styleId,
      bbox: [-157.865, 21.295, -157.845, 21.315], connectionHealthy: true,
      sourceTable: 'public.zero_to_map_parcels', dryRunApplied: preview.applied, baselineStyleVersion: confirmed.styleVersion });
  }
  console.log(`${stage}: pass`);
} catch {
  // Request errors can include private payloads; this private log stays generic too.
  console.error(`${stage}: failed; no fallback`);
  process.exitCode = 1;
}
