// Private browser runtime. Credentials arrive only on stdin, never in events.
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { observeStudioRender, renderContract } from './agent_e2e_studio_render.mjs';

const require = (value) => { if (!value) throw Error('Invalid private browser input.'); };
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const input = lines[Symbol.asyncIterator]();
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
let browser, context, sequence = 0;
const deadline = setTimeout(async () => {
  send({ type: 'failed', code: 'browser-deadline' });
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  process.exit(1);
}, 120000);
deadline.unref();

async function read(maximum) {
  const row = await input.next();
  require(!row.done && Buffer.byteLength(row.value) <= maximum);
  return JSON.parse(row.value);
}

async function authorize() {
  const id = ++sequence;
  send({ type: 'authorize', id });
  const answer = await read(1024);
  require(Object.keys(answer).length === 2 && answer.type === 'authorized' && answer.id === id);
}

try {
  const request = await read(2 * 1024 * 1024);
  require(Object.keys(request).sort().join(',') === 'contractJson,runtime,storageState'
    && typeof request.contractJson === 'string');
  const contract = JSON.parse(request.contractJson);
  renderContract(contract);
  const state = request.storageState;
  require(state && Object.keys(state).sort().join(',') === 'cookies,origins'
    && Array.isArray(state.cookies) && state.cookies.length > 0 && state.cookies.length <= 100
    && Array.isArray(state.origins) && state.origins.length <= 20);
  const runtime = request.runtime;
  require(runtime && Object.keys(runtime).sort().join(',') === 'chromium,playwright'
    && /^\d+\.\d+\.\d+$/.test(runtime.playwright) && /^\d+(?:\.\d+){3}$/.test(runtime.chromium));
  const modulePath = process.env.HONUA_RENDER_PLAYWRIGHT;
  require(typeof modulePath === 'string' && modulePath.startsWith('/') && modulePath.endsWith('/index.mjs'));
  const moduleUrl = pathToFileURL(modulePath);
  const packageMetadata = JSON.parse(await readFile(new URL('./package.json', moduleUrl), 'utf8'));
  require(['playwright', 'playwright-core'].includes(packageMetadata.name) && packageMetadata.version === runtime.playwright);
  const { chromium } = await import(moduleUrl.href);
  await authorize();
  browser = await chromium.launch({ headless: true,
    ...(process.env.HONUA_RENDER_CHROMIUM ? { executablePath: process.env.HONUA_RENDER_CHROMIUM } : {}),
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  require(browser.version() === runtime.chromium);
  context = await browser.newContext({ storageState: state, viewport: {
    width: contract.width, height: contract.height }, deviceScaleFactor: 1,
    acceptDownloads: false, serviceWorkers: 'block' });
  const page = await context.newPage();
  let refused = false;
  const bootRoute = async routed => {
    const request = routed.request(), url = new URL(request.url());
    let allowed = url.origin === requestOrigin && ['GET', 'HEAD'].includes(request.method());
    if (url.origin === requestOrigin && request.method() === 'POST') {
      // Blazor transport negotiation and the human-only, server-enforced
      // catalog reader restore the host. No model/lifecycle/data writer is
      // allowed even while that read-only host context starts.
      allowed = url.pathname === '/_blazor/negotiate';
      if (url.pathname === '/api/studio/catalog-mcp') {
        try {
          const body = JSON.parse(request.postData());
          allowed = ['initialize', 'notifications/initialized'].includes(body.method)
            || body.method === 'tools/call' && ['honua_list_layers', 'honua_get_style', 'honua_describe_layer'].includes(body.params?.name);
        } catch { allowed = false; }
      }
    }
    if (!allowed) { refused = true; await routed.abort('blockedbyclient'); }
    else await routed.continue();
  };
  const requestOrigin = contract.endpoint;
  await page.route('**/*', bootRoute);
  // Opening the maintained Console workspace restores the actual signed-in
  // host/catalog. No prompt, command, draft, source or fabricated cookie is
  // injected. The observer below then fences all traffic to GET/HEAD only.
  await authorize();
  await page.goto(contract.endpoint + '/studio', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForFunction(() => Array.isArray(document.querySelector('honua-studio-app')?.sourceCatalog),
    undefined, { timeout: 30000 });
  require(!refused);
  await page.unroute('**/*', bootRoute);
  await observeStudioRender({ page, contract, contractJson: request.contractJson, authorize, retain: async report => {
    report.browserRuntime = { playwright: packageMetadata.version, chromium: browser.version() };
    const event = { type: 'report', report };
    require(Buffer.byteLength(JSON.stringify(event)) <= 8 * 1024 * 1024);
    send(event);
  } });
  send({ type: 'complete' });
} catch {
  // Browser/transport exceptions can embed private URLs or cookie material.
  send({ type: 'failed', code: 'browser-observation-failed' });
  process.exitCode = 1;
} finally {
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  clearTimeout(deadline);
  lines.close();
}
