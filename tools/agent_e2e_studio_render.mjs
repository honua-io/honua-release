// Read-only observation using the installed Console's deployed Studio renderer.
// The host owns the real signed-in Page, lease, private credentials and retention.
import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';

const require = (condition, reason) => { if (!condition) throw Error(reason); };
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const canonical = value => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const guid = value => typeof value === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);
const MAX_BODY = 2 * 1024 * 1024;

export function renderContract(contract) {
  require(contract?.schemaVersion === 1 && /^sha256:[0-9a-f]{64}$/.test(contract.candidateKey)
    && /^[0-9a-f]{40}$/.test(contract.consoleCommit), 'render requires frozen candidate and Console source');
  const origin = new URL(contract.endpoint);
  require(origin.origin === contract.endpoint && !origin.username && !origin.password
    && (origin.protocol === 'https:' || origin.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)),
  'render requires an exact TLS or loopback Console origin');
  const scope = contract.scope;
  require(scope && ['itemId', 'draftId', 'versionId'].every(key => guid(scope[key]))
    && /^[a-zA-Z0-9_-]+$/.test(scope.namespace) && typeof scope.ownerPrincipalId === 'string' && scope.ownerPrincipalId
    && /^[0-9a-f]{64}$/.test(scope.documentSha256), 'render lacks exact saved native item/owner/workspace scope');
  require(typeof contract.session?.principalId === 'string' && contract.session.principalId
    && typeof contract.session.environmentId === 'string' && contract.session.environmentId, 'render requires actual signed-in session binding');
  require(Array.isArray(contract.runtimeAssets) && contract.runtimeAssets.length >= 3
    && new Set(contract.runtimeAssets.map(row => row.path)).size === contract.runtimeAssets.length
    && contract.runtimeAssets.every(row => /^\/_studio\/[A-Za-z0-9_.\/-]+\.js$/.test(row.path)
      && !row.path.includes('..') && /^[0-9a-f]{64}$/.test(row.sha256))
    && contract.runtimeAssets.some(row => row.path === '/_studio/console.js')
    && contract.runtimeAssets.some(row => row.path === contract.mapLibreModule), 'render requires pinned deployed runtime assets');
  require(Number.isInteger(contract.width) && Number.isInteger(contract.height) && contract.width >= 256 && contract.height >= 256
    && contract.width <= 2048 && contract.height <= 2048, 'render viewport exceeds independent pixel bound');
  require(Array.isArray(contract.layers) && contract.layers.length > 0 && contract.layers.length <= 100
    && new Set(contract.layers.map(row => row.id)).size === contract.layers.length
    && contract.layers.every(row => typeof row.id === 'string' && row.id && typeof row.sourceIdProperty === 'string'
      && row.sourceIdProperty && Array.isArray(row.featureIds) && row.featureIds.length > 0
      && row.featureIds.every(id => typeof id === 'string') && row.paint && row.layout), 'render lacks frozen feature/style predicates');
  const color = value => Array.isArray(value) && value.length === 4 && value.every(channel => Number.isInteger(channel) && channel >= 0 && channel <= 255);
  require(color(contract.background) && Number.isInteger(contract.backgroundTolerance) && contract.backgroundTolerance >= 0
    && contract.backgroundTolerance <= 255 && Array.isArray(contract.regions) && contract.regions.length > 0
    && new Set(contract.regions.map(row => row.id)).size === contract.regions.length
    && contract.regions.every(row => typeof row.id === 'string' && row.id && color(row.rgba)
      && Number.isInteger(row.tolerance) && row.tolerance >= 0 && row.tolerance <= 255
      && Array.isArray(row.rect) && row.rect.length === 4 && row.rect.every(Number.isInteger)
      && row.rect[0] >= 0 && row.rect[1] >= 0 && row.rect[2] > 0 && row.rect[3] > 0
      && row.rect[0] + row.rect[2] <= contract.width && row.rect[1] + row.rect[3] <= contract.height),
  'render requires independently frozen background and geometry/style pixel regions');
  return contract;
}

/** No lifecycle calls, prompts, draft attachment, save or source writes occur. */
export async function observeStudioRender({ page, contract, contractJson, authorize, retain }) {
  renderContract(contract);
  require(contractJson === undefined || typeof contractJson === 'string'
    && Buffer.byteLength(contractJson) <= MAX_BODY && isDeepStrictEqual(JSON.parse(contractJson), contract),
  'render contract bytes differ from its frozen declaration');
  require(typeof authorize === 'function' && typeof retain === 'function', 'render requires fenced private custody');
  await authorize();
  require(new URL(page.url()).origin === contract.endpoint, 'browser is not on the exact signed-in installed Console');
  const report = { schema: 'honua.studio-browser-render/v1', candidateKey: contract.candidateKey,
    contractDigest: 'sha256:' + sha(contractJson ?? canonical(contract)), startedAt: new Date().toISOString(), native: {}, runtimeAssets: [],
    network: [], errors: [], png: null, render: null, session: null };
  const pending = new Set();
  const recordError = (kind, value) => { report.errors.push({ kind, sha256: sha(String(value)) }); };
  const pageError = error => recordError('pageerror', error.message);
  const consoleError = message => { if (message.type() === 'error') recordError('console', message.text()); };
  const failed = request => recordError('requestfailed', request.failure()?.errorText ?? 'unknown');
  const route = async routed => {
    const request = routed.request(), url = new URL(request.url());
    // Renderer traffic has no writer capability or cross-origin credential path.
    if (url.origin !== contract.endpoint || !['GET', 'HEAD'].includes(request.method())) {
      recordError('forbidden-request', request.method() + ' ' + url.pathname);
      await routed.abort('blockedbyclient');
    } else await routed.continue();
  };
  const response = reply => {
    const operation = (async () => {
      const url = new URL(reply.url());
      if (url.origin !== contract.endpoint) return;
      const row = { method: reply.request().method(), path: url.pathname + url.search, status: reply.status() };
      if (reply.status() >= 400) recordError('http-error', row.status + ' ' + url.pathname);
      if (url.pathname.startsWith('/api/studio/data/')) {
        const declared = Number(reply.headers()['content-length'] ?? 0);
        require(declared <= MAX_BODY, 'render feature response exceeds bound');
        const raw = await reply.body();
        require(raw.length <= MAX_BODY, 'render feature response exceeds bound');
        row.sha256 = sha(raw); row.bytes = raw.length;
      }
      report.network.push(row);
    })();
    pending.add(operation);
    operation.catch(() => recordError('network-observation', 'incomplete')).finally(() => pending.delete(operation));
  };
  await page.route('**/*', route);
  page.on('pageerror', pageError); page.on('console', consoleError);
  page.on('requestfailed', failed); page.on('response', response);
  const headers = { 'X-Honua-Environment': contract.session.environmentId,
    'X-Honua-Session-Partition': contract.session.principalId };
  const read = async path => {
    await authorize();
    require(path.startsWith('/') && !path.startsWith('//') && !path.includes('\\'), 'render native read path escaped origin');
    const reply = await page.request.get(contract.endpoint + path, { headers, maxRedirects: 0, timeout: 30000 });
    require(reply.status() === 200 && Number(reply.headers()['content-length'] ?? 0) <= MAX_BODY, 'render independent read failed');
    const raw = await reply.body();
    require(raw.length <= MAX_BODY, 'render independent read exceeds bound');
    const parsed = JSON.parse(raw.toString('utf8'));
    require(parsed && parsed.success !== false && !('error' in parsed), 'render independent read reports product failure');
    const original = { path, status: reply.status(), sha256: sha(raw), bytes: raw.length, bodyBase64: raw.toString('base64') };
    const value = parsed.data ?? parsed;
    return { original, value };
  };
  const paths = {
    draft: '/api/studio/v1/studio/package-drafts/' + contract.scope.draftId,
    version: '/api/studio/v1/studio/content-items/' + contract.scope.itemId + '/versions/' + contract.scope.versionId,
    current: '/api/studio/v1/studio/content-items?' + new URLSearchParams({ family: 'map', workspaceId: contract.scope.namespace,
      owner: contract.scope.ownerPrincipalId, limit: '100' }),
  };
  const inspect = async () => {
    const values = {};
    for (const [name, path] of Object.entries(paths)) values[name] = await read(path);
    const scope = contract.scope, draft = values.draft.value, version = values.version.value, current = values.current.value;
    for (const value of [draft, version]) require(value.itemId === scope.itemId && value.ownerId === scope.ownerPrincipalId
      && value.workspaceId === scope.namespace && value.packageKey === scope.namespace && value.envelope?.family === 'map'
      && value.envelope?.schemaVersion === '1.0', 'renderer native resource escaped exact authored scope');
    require(draft.draftId === scope.draftId && version.versionId === scope.versionId && version.sourceDraftId === draft.draftId
      && isDeepStrictEqual(draft.envelope, version.envelope), 'render source differs from the original immutable save');
    // Python independently hashes the retained original body against the frozen
    // expectation. Re-serializing parsed JS numbers here loses e.g. 1.0 vs 1.
    require(current.total === 1 && current.nextCursor == null && current.items?.length === 1
      && current.items[0].itemId === scope.itemId && current.items[0].currentVersionId === scope.versionId,
    'renderer current pointer changed from the selected model save');
    return { originals: Object.fromEntries(Object.entries(values).map(([name, row]) => [name, row.original])), version };
  };
  try {
    const sessionReply = await page.request.get(contract.endpoint + '/api/agent-workspace/session',
      { maxRedirects: 0, timeout: 30000 });
    require(sessionReply.status() === 200, 'actual signed-in browser session is unavailable');
    // The session also holds private CSRF material. Retain only the observed
    // principal/target identity below; never hash or export the session body.
    const session = await sessionReply.json();
    require(session.authenticated === true && session.principalId === contract.session.principalId
      && session.environmentId === contract.session.environmentId, 'render browser lacks its actual signed-in principal/target');
    report.session = { principalId: session.principalId, environmentId: session.environmentId, authenticated: true };
    const consoleVersion = await read('/version.json');
    require(consoleVersion.value.commit === contract.consoleCommit, 'installed Console source differs from frozen render runtime');
    report.consoleVersion = consoleVersion.original;
    for (const asset of contract.runtimeAssets) {
      const reply = await page.request.get(contract.endpoint + asset.path, { maxRedirects: 0, timeout: 30000 });
      require(reply.status() === 200, 'deployed renderer asset absent');
      const raw = await reply.body();
      require(raw.length <= 16 * 1024 * 1024 && sha(raw) === asset.sha256, 'deployed renderer asset differs from source-frozen bytes');
      report.runtimeAssets.push({ ...asset, bytes: raw.length, status: 200 });
    }
    const before = await inspect(); report.native.before = before.originals;
    await authorize();
    // Only the independently read candidate body enters the supported read-only
    // renderer. Expected body/style/geometry/pixel values are never constructed
    // from candidate output. The normal corpus oracle checks those separately.
    report.render = await page.evaluate(async ({ contract, body }) => {
      const app = document.querySelector('honua-studio-app');
      if (!app || !Array.isArray(app.sourceCatalog)) throw Error('Actual current-reader Studio catalog is unavailable.');
      const used = new Set(body.layers.map(layer => layer.sourceId));
      // The editor catalog advertises native/legacy locators. A current-reader
      // canvas must receive the same resource through the Console's authorized
      // data BFF, rather than sending those aliases into its anonymous boundary.
      // Only transport paths change; no source identity or document is authored.
      const catalog = app.sourceCatalog.filter(source => used.has(source.id) || used.has(source.locator?.serviceId)).map(source => {
        const locator = source.locator, raw = locator?.url;
        if (source.protocol !== 'geoservices-feature-service' || typeof raw !== 'string' || /[%\\]/.test(raw)
          || !Number.isInteger(locator.layerId) || locator.layerId < 0) throw Error('Unsupported current-reader source locator.');
        const original = new URL(raw, location.origin);
        if (original.origin !== location.origin || original.username || original.password || original.search || original.hash)
          throw Error('Source catalog escaped the actual Console reader origin.');
        const native = original.pathname.replace(/^\/api\/studio\/data(?=\/rest\/)/, '').replace(/^\/api(?=\/rest\/)/, '');
        if (!/^\/rest\/services\/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*\/)+FeatureServer$/.test(native)
          || native.split('/').some(part => part === '.' || part === '..')) throw Error('Unrecognized native reader resource path.');
        return { ...structuredClone(source), locator: { ...structuredClone(locator), url: location.origin + '/api/studio/data' + native } };
      });
      const { createReadOnlyMapCanvas } = await import('/_studio/console.js');
      const maplibre = await import(contract.mapLibreModule);
      if (typeof createReadOnlyMapCanvas !== 'function' || typeof maplibre.Map !== 'function') throw Error('Installed read-only renderer is unavailable.');
      if (document.getElementById('honua-agent-independent-render') || window.__honuaIndependentRender)
        throw Error('An existing render observation remains unresolved.');
      const container = document.createElement('div');
      container.id = 'honua-agent-independent-render';
      container.style.cssText = `position:fixed;left:0;top:0;width:${contract.width}px;height:${contract.height}px;z-index:2147483647;background:transparent`;
      document.body.appendChild(container);
      let nativeMap, canvas, expired = false, timer;
      const deadline = new Promise((_resolve, reject) => {
        timer = setTimeout(() => { expired = true; reject(Error('Native read-only map exceeded its observation deadline.')); }, 30000);
      });
      const previousOn = maplibre.Map.prototype.on;
      // Observe the real default factory's Map without replacing that factory,
      // its source protocols, style composition or credential guards.
      maplibre.Map.prototype.on = function (...args) {
        try { if (this.getContainer() === container) nativeMap = this; } catch { /* constructor has not bound its container yet */ }
        return Reflect.apply(previousOn, this, args);
      };
      const transport = { getAccessToken: async () => undefined, fetchRequest: async (url, options = {}) => {
        const target = new URL(url, document.baseURI);
        if (target.origin !== location.origin || !target.pathname.startsWith('/api/studio/data/') || (options.method ?? 'GET') !== 'GET')
          throw Error('Read-only renderer refused source transport.');
        return fetch(target, { ...options, method: 'GET', credentials: 'same-origin', redirect: 'error',
          headers: { Accept: 'application/geo+json, application/json', 'X-Honua-Environment': contract.session.environmentId,
            'X-Honua-Session-Partition': contract.session.principalId } });
      } };
      try {
        const creation = createReadOnlyMapCanvas({ container, body, projection: { catalog, baseUrl: '/api/studio/data' },
          sourceAuthBaseUrl: '/api/studio/data', sourceCatalogBaseUrl: '/api/studio/data', getAuth: () => transport });
        creation.then(value => { if (expired) value.dispose(); }, () => {});
        canvas = await Promise.race([creation, deadline]);
        await Promise.race([canvas.settled(), deadline]);
        if (nativeMap && !nativeMap.loaded()) await Promise.race([new Promise((resolve, reject) => {
          const ready = () => { if (nativeMap.loaded()) { nativeMap.off('idle', ready); nativeMap.off('error', broken); resolve(); } };
          const broken = () => { nativeMap.off('idle', ready); nativeMap.off('error', broken); reject(Error('Native source map failed to paint.')); };
          nativeMap.on('idle', ready); nativeMap.on('error', broken); ready();
        }), deadline]);
        if (!(nativeMap instanceof maplibre.Map) || nativeMap.getContainer() !== container
          || canvas.status !== 'ready' || canvas.projection?.unresolved?.length) throw Error('Actual native map did not render every authored source.');
        const style = nativeMap.getStyle();
        const layers = contract.layers.map(layer => {
          const visuals = style.layers.filter(row => row.id === layer.id || row.metadata?.['honua:compositionLayerId'] === layer.id);
          return { id: layer.id, styles: visuals,
          features: nativeMap.queryRenderedFeatures(undefined, { layers: visuals.map(row => row.id) }).map(row => ({
            layerId: row.layer.id, type: row.layer.type, source: row.source, id: row.id ?? null,
            geometry: row.geometry, properties: row.properties,
          })),
        }; });
        window.__honuaIndependentRender = { container, canvas };
        return { status: canvas.status, projection: canvas.projection, renderedFeatureCount: canvas.renderedFeatureCount, layers };
      } catch (error) {
        expired = true; canvas?.dispose(); if (!canvas) nativeMap?.remove(); container.remove(); throw error;
      } finally { clearTimeout(timer); maplibre.Map.prototype.on = previousOn; }
    }, { contract, body: before.version.envelope.body });
    await authorize();
    const png = await page.locator('#honua-agent-independent-render canvas.maplibregl-canvas').screenshot({ type: 'png', timeout: 30000 });
    require(png.length > 0 && png.length <= 16 * 1024 * 1024, 'render PNG exceeds bound');
    report.png = { sha256: sha(png), bytes: png.length, base64: png.toString('base64') };
    const after = await inspect(); report.native.after = after.originals;
    require(isDeepStrictEqual(before.originals, after.originals), 'native draft/version/current pointer changed during rendering');
    await Promise.allSettled([...pending]);
    require(report.network.some(row => row.path.startsWith('/api/studio/data/') && row.status === 200 && row.bytes > 0),
      'map has no original authorized source network observation');
    require(report.errors.length === 0, 'browser render contains source, network or runtime failures');
    return report;
  } finally {
    report.completedAt = new Date().toISOString();
    await page.evaluate(() => {
      const view = window.__honuaIndependentRender;
      view?.canvas.dispose(); view?.container.remove(); delete window.__honuaIndependentRender;
    }).catch(() => recordError('observer-cleanup', 'failed'));
    page.off('pageerror', pageError); page.off('console', consoleError);
    page.off('requestfailed', failed); page.off('response', response);
    await page.unroute('**/*', route);
    await authorize();
    // Keep failed originals too; the private host must reject secret disclosure
    // and persist this before any partial observation can be reported.
    await retain(report);
  }
}
