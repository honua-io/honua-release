import { expect, test, type Locator } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { captureDiagnostics, screenshot, writeJson } from './evidence';
import { Recorder, type Verdict } from './recorder';
import { apiUrl, openMenu, operatorPassword, outputDirectory, signIn, trialName } from './session';

const FIXTURE = new URL('./fixtures/three-points.geojson', import.meta.url);
const EXPECTED_ROWS = [
  { name: 'City Hall', rank: 1, x: -74.006, y: 40.7128 },
  { name: 'Times Sq', rank: 2, x: -73.9855, y: 40.758 },
  { name: 'Harlem', rank: 3, x: -73.9465, y: 40.8116 },
];
const COORDINATE_TOLERANCE = 1e-6;
const TIME_LIMIT_MINUTES = 20;
const READBACK_TIMEOUT = 30_000;

interface Names {
  table: string;
  service: string;
  layer: string;
}

function trialNames(): Names {
  const name = trialName('oe');
  return { table: `${name}_points`, service: `${name}-points`, layer: `Outcome points ${name}` };
}

async function importFile(recorder: Recorder, table: string) {
  const page = recorder.page;
  await openMenu(recorder, 'Data', 'Add data');
  await recorder.click(page.getByRole('link', { name: 'Choose dataset file' }), 'Choose dataset file');
  const file = { name: 'three-points.geojson', mimeType: 'application/geo+json', buffer: readFileSync(FIXTURE) };
  await recorder.chooseFile(page.locator('input[type=file]').first(), 'import.file', file);
  await recorder.fill(page.locator('input[placeholder="parcels"]'), 'import.table', table);
  await recorder.click(page.getByRole('button', { name: 'Import file' }), 'Import file');
  await page.locator('[data-imported-table], .console-state-danger').first().waitFor({ timeout: 180_000 });
  await expect(page.locator('[data-imported-table]')).toBeVisible();
}

const TABLE_LIST_WAIT = 5_000;

/** The table list loads after a connection is chosen; an absent table is known only once the wait runs out. */
async function offeredOption(tables: Locator, table: string): Promise<string> {
  const option = tables.locator(`option[value*="${table}"]`).first();
  await option.waitFor({ state: 'attached', timeout: TABLE_LIST_WAIT }).catch(() => undefined);
  return await option.count() ? await option.getAttribute('value') ?? '' : '';
}

/** A person reads the connection labels; every connection tried is a counted field action. */
async function chooseImportedTable(recorder: Recorder, table: string) {
  const connections = recorder.page.locator('select[aria-label=Connection]');
  const tables = recorder.page.locator('select[aria-label=Table]');
  await connections.waitFor({ timeout: 60_000 });
  const candidates = await connections.locator('option').evaluateAll(
    options => (options as HTMLOptionElement[]).map(option => option.value).filter(Boolean));
  let option = await offeredOption(tables, table);
  for (const connection of option ? [] : candidates) {
    await recorder.select(connections, 'publish.connection', connection);
    option = await offeredOption(tables, table);
    if (option) break;
  }
  if (!option) throw new Error('the imported table is not offered for publishing');
  if (await tables.inputValue() !== option) await recorder.select(tables, 'publish.table', option);
}

async function publishLayer(recorder: Recorder, names: Names) {
  const page = recorder.page;
  await recorder.click(page.getByRole('link', { name: 'Publish this table' }), 'Publish this table');
  await chooseImportedTable(recorder, names.table);
  await recorder.fill(page.locator('[data-workspace-field=serviceName]'), 'publish.serviceName', names.service);
  await recorder.fill(page.locator('[data-workspace-field=layerName]'), 'publish.layerName', names.layer);
  await recorder.click(page.locator('[data-workspace-publish]'), 'Publish layer');
  const outcome = page.locator('[data-published-service], [data-publish-validation], .console-state-danger');
  await outcome.first().waitFor({ timeout: 120_000 });
  await expect(page.locator('[data-published-service]')).toBeVisible();
}

async function serverJson(path: string) {
  const headers = { 'X-API-Key': operatorPassword(), accept: 'application/json' };
  const response = await fetch(apiUrl() + path, { headers, signal: AbortSignal.timeout(READBACK_TIMEOUT) });
  return { path, status: response.status, body: await response.json().catch(() => null) };
}

function sameRow(served: typeof EXPECTED_ROWS[number] | undefined, expected: typeof EXPECTED_ROWS[number]) {
  return served?.name === expected.name
    && Number(served.rank) === expected.rank
    && Math.abs(served.x - expected.x) < COORDINATE_TOLERANCE
    && Math.abs(served.y - expected.y) < COORDINATE_TOLERANCE;
}

/** Reads the published layer back from the server, outside the browser session. */
async function readBack(names: Names): Promise<Verdict> {
  const service = await serverJson(`/rest/services/${names.service}/FeatureServer?f=json`);
  const layers = service.body?.layers ?? [];
  const query = await serverJson(`/rest/services/${names.service}/FeatureServer/${layers[0]?.id}/query`
    + '?where=1%3D1&outFields=*&outSR=4326&orderByFields=rank&f=json');
  const rows = (query.body?.features ?? []).map((feature: any) => ({
    name: feature.attributes?.name, rank: feature.attributes?.rank, x: feature.geometry?.x, y: feature.geometry?.y,
  }));
  const passed = service.status === 200 && query.status === 200 && layers.length === 1 && layers[0].name === names.layer
    && rows.length === EXPECTED_ROWS.length && EXPECTED_ROWS.every((expected, index) => sameRow(rows[index], expected));
  writeJson(`${outputDirectory()}/readback.json`, { service, query, rows, expected: EXPECTED_ROWS, postconditionsPassed: passed });
  return {
    readback: 'the published feature layer serves the three fixture rows under the given layer name',
    failure: passed ? undefined : 'the published layer does not serve the fixture rows under the given layer name',
  };
}

test('upload a file and publish it as a layer', async ({ page }) => {
  page.setDefaultTimeout(30_000);
  const directory = outputDirectory();
  const recorder = new Recorder(page, directory);
  const names = trialNames();
  captureDiagnostics(page, `${directory}/diagnostics.log`);
  writeJson(`${directory}/context.json`, names);
  try {
    const verdict = await recorder.trial(TIME_LIMIT_MINUTES, async () => {
      await signIn(recorder);
      await importFile(recorder, names.table);
      await publishLayer(recorder, names);
      recorder.observer('server readback of the published layer');
      return readBack(names);
    });
    expect(verdict.failure).toBeUndefined();
  } finally {
    await screenshot(page, `${directory}/final.png`);
  }
});
