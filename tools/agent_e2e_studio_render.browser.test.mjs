// Genuine local Chromium + deployed Studio SDK controls, never candidate qualification.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, realpath } from 'node:fs/promises';
import { resolve, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { observeStudioRender, renderContract } from './agent_e2e_studio_render.mjs';

const sha = raw => createHash('sha256').update(raw).digest('hex');
const canonical = value => JSON.stringify(value, (_key, row) => row && typeof row === 'object' && !Array.isArray(row)
  ? Object.fromEntries(Object.keys(row).sort().map(key => [key, row[key]])) : row);
function declaration() {
  return { schemaVersion: 1, candidateKey: 'sha256:'+'a'.repeat(64), consoleCommit: 'b'.repeat(40), endpoint: 'http://127.0.0.1:1234',
    session: { principalId: 'test-actual-browser-human', environmentId: 'test-native-target' },
    scope: { itemId: '11111111-1111-1111-1111-111111111111', draftId: '22222222-2222-2222-2222-222222222222',
      versionId: '33333333-3333-3333-3333-333333333333', namespace: 'ae_render_local', ownerPrincipalId: 'test-agent-owner', documentSha256: 'd'.repeat(64) },
    runtimeAssets: [ { path: '/_studio/console.js', sha256: 'e'.repeat(64) }, { path: '/_studio/map.js', sha256: 'f'.repeat(64) },
      { path: '/_studio/worker.js', sha256: '1'.repeat(64) } ], mapLibreModule: '/_studio/map.js',
    width: 256, height: 256, background: [255,255,255,255], backgroundTolerance: 1,
    layers: [{ id: 'shelters', sourceIdProperty: 'source_id', featureIds: ['s1'], geometryType: 'Point',
      properties: { s1: { name: 'First shelter' } }, paint: { 'circle-color': '#ef2020' }, layout: {} }],
    regions: [{ id: 'shelter', rect: [120,120,16,16], rgba: [239,32,32,255], tolerance: 2 }] };
}

test('renderer refuses unpinned runtime, absent pixel geometry and unbound source before browser effects', () => {
  const value = declaration(); renderContract(value);
  for (const mutate of [row => row.runtimeAssets.pop(), row => row.regions = [], row => row.scope.documentSha256 = null,
    row => row.session.principalId = '', row => row.layers = [], row => row.endpoint = 'https://user:secret@example.com']) {
    const changed = structuredClone(value); mutate(changed);
    assert.throws(() => renderContract(changed));
  }
});

const enabled = process.env.HONUA_AGENT_RENDER_BROWSER_TESTS === '1';
test('real deployed read-only Studio SDK paints native HTTP source without any writer request', { skip: !enabled, timeout: 90000 }, async testContext => {
  const root = await realpath(process.env.HONUA_AGENT_RENDER_ASSETS);
  const playwright = await import(pathToFileURL(process.env.HONUA_AGENT_RENDER_PLAYWRIGHT).href);
  const contract = declaration(), retained = [], seen = [];
  const source = { type: 'FeatureCollection', features: [{ type: 'Feature', id: 's1',
    properties: { source_id: 's1', name: 'First shelter' }, geometry: { type: 'Point', coordinates: [0,0] } }] };
  const metadata = { id: 1, name: 'Shelters', objectIdField: 'OBJECTID', geometryType: 'esriGeometryPoint',
    fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, { name: 'source_id', type: 'esriFieldTypeString' },
      { name: 'name', type: 'esriFieldTypeString' }], spatialReference: { wkid: 4326 },
    capabilities: 'Query', maxRecordCount: 2000, advancedQueryCapabilities: { supportsPagination: true } };
  const body = { layers: [{ id: 'shelters', sourceId: 'service/1', type: 'circle', visible: true }],
    view: { center: [0,0], zoom: 2 }, mapSpec: { version: 8, sources: { 'service/1': { type: 'geojson', data: { type: 'FeatureCollection', features: [] } } },
      layers: [{ id: 'shelters', type: 'circle', source: 'service/1', paint: { 'circle-color': '#ef2020', 'circle-radius': 18 } }] } };
  contract.scope.documentSha256 = sha(canonical(body));
  const base = { itemId: contract.scope.itemId, ownerId: contract.scope.ownerPrincipalId,
    workspaceId: contract.scope.namespace, packageKey: contract.scope.namespace,
    envelope: { family: 'map', schemaVersion: '1.0', body } };
  const draft = { ...base, draftId: contract.scope.draftId, generation: 3 };
  const version = { ...base, sourceDraftId: contract.scope.draftId, versionId: contract.scope.versionId, versionNumber: 1 };
  const catalog = [{ id: 'service/1', title: 'Shelters', protocol: 'geoservices-feature-service', geometryType: 'Point',
    locator: { serviceId: 'service', layerId: 1, url: '/rest/services/shelters/FeatureServer' } }];
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    seen.push({ method: request.method, path, headers: request.headers });
    try {
      if (request.method !== 'GET') { response.writeHead(405).end(); return; }
      if (path.startsWith('/_studio/')) {
        const file = resolve(root, path.slice('/_studio/'.length));
        const actual = await realpath(file);
        assert.ok(!relative(root, actual).startsWith('..'+sep));
        response.writeHead(200, { 'Content-Type': 'text/javascript' }).end(await readFile(actual)); return;
      }
      let value;
      if (path === '/version.json') value = { commit: contract.consoleCommit };
      else if (path === '/api/agent-workspace/session') value = { ...contract.session, authenticated: true, csrfToken: 'private-test-csrf-value' };
      else if (path.startsWith('/api/studio/data/')) {
        assert.equal(request.headers['x-honua-environment'], contract.session.environmentId);
        assert.equal(request.headers['x-honua-session-partition'], contract.session.principalId);
        value = path.endsWith('/query') ? { objectIdFieldName: 'OBJECTID', geometryType: metadata.geometryType,
          spatialReference: metadata.spatialReference, fields: metadata.fields, exceededTransferLimit: false,
          features: source.features.map((row, index) => ({ attributes: { OBJECTID: index+1, ...row.properties },
            geometry: { x: row.geometry.coordinates[0], y: row.geometry.coordinates[1] } })) } : metadata;
      } else if (path.includes('/package-drafts/')) value = draft;
      else if (path.includes('/versions/')) value = version;
      else if (path === '/api/studio/v1/studio/content-items') value = { total: 1, nextCursor: null, items: [{ itemId: base.itemId, currentVersionId: version.versionId }] };
      else if (path === '/') {
        response.writeHead(200, { 'Content-Type': 'text/html' }).end('<!doctype html><honua-studio-app></honua-studio-app><script>document.querySelector("honua-studio-app").sourceCatalog='+JSON.stringify(catalog)+'</script>'); return;
      } else { response.writeHead(404).end(); return; }
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(value));
    } catch { response.writeHead(500).end(); }
  });
  testContext.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  contract.endpoint = 'http://127.0.0.1:'+server.address().port;
  const sourceConsole = await readFile(resolve(root, 'console.js'), 'utf8');
  const mapName = sourceConsole.match(/import\("\.\/(maplibre-gl-[^"]+\.js)"\)/)?.[1];
  const workerName = sourceConsole.match(/import\("\.\/(maplibre-gl-worker-[^"]+\.js)"\)/)?.[1];
  assert.ok(mapName && workerName, 'Actual deployed SDK must bind native MapLibre and worker assets.');
  contract.mapLibreModule = '/_studio/'+mapName;
  contract.runtimeAssets = [];
  for (const name of ['console.js', mapName, workerName]) contract.runtimeAssets.push({ path: '/_studio/'+name, sha256: sha(await readFile(resolve(root, name))) });
  const browser = await playwright.chromium.launch({ executablePath: process.env.HONUA_AGENT_RENDER_CHROMIUM,
    headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const context = await browser.newContext({ viewport: { width: 512, height: 512 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    await page.goto(contract.endpoint);
    // Local control diagnostics only: the server above has no live credentials
    // or user data. Production observation retains bounded error digests.
    await page.evaluate(async modulePath => {
      const module = await import(modulePath), fire = module.Map.prototype.fire;
      window.__localRenderControlErrors = [];
      module.Map.prototype.fire = function (event, ...rest) {
        if (event?.type === 'error' || event === 'error')
          window.__localRenderControlErrors.push(String(event?.error?.message ?? rest[0]?.error?.message ?? 'unknown'));
        return Reflect.apply(fire, this, [event, ...rest]);
      };
    }, contract.mapLibreModule);
    let report;
    try {
      report = await observeStudioRender({ page, contract, authorize: async () => {}, retain: async report => retained.push(structuredClone(report)) });
    } catch (error) {
      testContext.diagnostic(JSON.stringify(await page.evaluate(() => window.__localRenderControlErrors)));
      throw error;
    }
    assert.equal(report.render.status, 'ready');
    assert.ok(report.render.renderedFeatureCount > 0);
    assert.equal(report.render.layers[0].features[0].properties.source_id, 's1');
    assert.deepEqual(report.errors, []);
    assert.equal(await page.locator('#honua-agent-independent-render').count(), 0);
    assert.equal(retained.length, 1);
    assert.ok(seen.every(row => row.method === 'GET'));
    assert.ok(!JSON.stringify(retained).includes('private-test-csrf-value'));
    if (process.env.HONUA_AGENT_RENDER_CONTROL_OUTPUT) {
      const { writeFile } = await import('node:fs/promises');
      await writeFile(process.env.HONUA_AGENT_RENDER_CONTROL_OUTPUT, JSON.stringify({ report: retained[0], contract })+'\n', { mode: 0o600 });
    }
    await context.close();
  } finally { await browser.close(); }
});
