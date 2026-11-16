import { expect, test, type Browser, type Page } from '@playwright/test';
import { captureDiagnostics, screenshot, writeJson } from './evidence';
import { Recorder, type Verdict } from './recorder';
import { openMenu, outputDirectory, signIn, trialName } from './session';

const CONTROLS = [
  { label: 'Legend', kind: 'legend' },
  { label: 'Layer list', kind: 'toc' },
];
const HASH = /^[0-9a-f]{64}$/;
const DENIED = [401, 403];
const TIME_LIMIT_MINUTES = 20;

interface Composition {
  key: string;
  layer: string;
  draftId: string;
  version: { versionNumber?: number; contentHash?: string; envelope?: { family?: string; body?: any } };
  saveStatus: string;
}

const layerPattern = () => new RegExp(process.env.OUTCOME_LAYER_PATTERN ?? '^Add Outcome points .+ to map$');
const studioStatus = (page: Page) => page.locator('[data-studio-status]').first();

function studioResponse(page: Page, method: string, path: RegExp) {
  return page.waitForResponse(response =>
    response.request().method() === method && path.test(new URL(response.url()).pathname));
}

async function openNewMap(recorder: Recorder, key: string): Promise<string> {
  const page = recorder.page;
  await openMenu(recorder, 'Studio', 'Maps');
  await expect(studioStatus(page)).toContainText('Connected to', { timeout: 60_000 });
  await recorder.fill(page.getByRole('textbox', { name: 'New map key' }), 'studio.mapKey', key);
  const created = studioResponse(page, 'POST', /\/package-drafts$/);
  await recorder.click(page.getByRole('button', { name: 'New map', exact: true }), 'New map');
  await page.getByText('Shared server draft opened.').waitFor({ timeout: 60_000 });
  return (await (await created).json()).data.draftId;
}

async function addPublishedLayer(recorder: Recorder): Promise<string> {
  const page = recorder.page;
  await recorder.click(page.getByRole('button', { name: 'Choose published layer', exact: true }), 'Choose published layer');
  const add = page.getByRole('button', { name: layerPattern() }).first();
  await add.waitFor();
  const layer = (await add.innerText()).replace(/^Add (.+) to map$/, '$1').trim();
  await recorder.click(add, 'Add <layer> to map');
  await expect(studioStatus(page)).toContainText(layer);
  return layer;
}

async function addControls(recorder: Recorder) {
  const page = recorder.page;
  await recorder.click(page.getByTestId('manual-elements-toggle'), 'Add widgets and controls');
  const panel = page.locator('[aria-label="Add widgets and controls"]').last();
  const kinds = panel.locator('label').filter({ hasText: /^Element/ }).locator('select');
  for (const control of CONTROLS) {
    await recorder.select(kinds, `element.kind.${control.kind}`, { label: control.label });
    const stored = studioResponse(page, 'PUT', /\/package-drafts\//);
    await recorder.click(panel.getByRole('button', { name: 'Add element', exact: true }), `Add element (${control.label})`);
    await panel.locator('[role=status]').filter({ hasText: /Saved\. Use Undo/ }).waitFor();
    await stored;
  }
}

async function saveVersion(recorder: Recorder) {
  const page = recorder.page;
  const saved = studioResponse(page, 'POST', /\/content-versions$/);
  await recorder.click(page.getByRole('button', { name: 'Save version', exact: true }), 'Save version');
  await expect(studioStatus(page)).toContainText(/Saved version|Could not|error/i, { timeout: 60_000 });
  const version = (await (await saved).json().catch(() => null))?.data ?? {};
  return { version, saveStatus: await studioStatus(page).innerText() };
}

async function compose(recorder: Recorder, key: string): Promise<Composition> {
  const draftId = await openNewMap(recorder, key);
  const layer = await addPublishedLayer(recorder);
  await addControls(recorder);
  return { key, layer, draftId, ...await saveVersion(recorder) };
}

async function anonymousStatus(browser: Browser, url: string): Promise<number> {
  const visitor = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    return (await visitor.request.get(url, { headers: { accept: 'application/json' } })).status();
  } finally {
    await visitor.close();
  }
}

/** Names the first unmet part of the contract. Content is judged on saved version 1, not on the mutable draft. */
function unmetContract(composition: Composition, state: any): string | undefined {
  const draft = state.draft?.data ?? {};
  const saved = composition.version.envelope ?? {};
  const layers = saved.body?.layers ?? [];
  const controls = (saved.body?.widgets ?? []).map((widget: any) => widget.kind).sort();
  const checks: [boolean, string][] = [
    [/Saved version 1/.test(composition.saveStatus), `the version was not saved: ${composition.saveStatus}`],
    [composition.version.versionNumber === 1 && HASH.test(composition.version.contentHash ?? ''), 'no hashed content version 1'],
    [state.draftStatus === 200 && draft.packageKey === composition.key, 'the owner cannot read the saved draft'],
    [layers.length === 1 && layers[0].title === composition.layer && Boolean(layers[0].sourceId), 'version 1 does not hold exactly the supplied layer'],
    [controls.join() === CONTROLS.map(control => control.kind).sort().join(), 'version 1 does not hold exactly the two controls'],
    [saved.family === 'app', `capability: the saved package family is "${saved.family}", not "app"`],
    [state.canvasArea > 0, 'the composed page shows no map canvas'],
    [DENIED.includes(state.anonymousStatus), `a signed-out visitor is not refused the private draft (${state.anonymousStatus})`],
  ];
  return checks.find(([met]) => !met)?.[1];
}

async function visibleCanvasArea(page: Page): Promise<number> {
  const canvas = page.locator('canvas').first();
  const box = await canvas.isVisible() ? await canvas.boundingBox() : null;
  return box ? box.width * box.height : 0;
}

async function readBack(page: Page, browser: Browser, composition: Composition): Promise<Verdict> {
  const path = `/api/v1/studio/package-drafts/${composition.draftId}`;
  const response = await page.request.get(path, { headers: { accept: 'application/json' } });
  const state = {
    draftStatus: response.status(),
    draft: await response.json().catch(() => null),
    canvasArea: await visibleCanvasArea(page),
    anonymousStatus: await anonymousStatus(browser, new URL(path, page.url()).href),
  };
  const failure = unmetContract(composition, state);
  writeJson(`${outputDirectory()}/readback.json`, { composition, ...state, postconditionsPassed: !failure, failure });
  return { readback: 'saved version 1 is a private app with the supplied layer, a legend and a layer list', failure };
}

test('build a private app from one layer and two controls', async ({ page, browser }) => {
  page.setDefaultTimeout(30_000);
  const directory = outputDirectory();
  const recorder = new Recorder(page, directory);
  captureDiagnostics(page, `${directory}/diagnostics.log`);
  try {
    const verdict = await recorder.trial(TIME_LIMIT_MINUTES, async () => {
      await signIn(recorder);
      const composition = await compose(recorder, trialName('oe-app-'));
      recorder.observer('server readback of the saved app');
      return readBack(page, browser, composition);
    });
    expect(verdict.failure).toBeUndefined();
  } finally {
    await screenshot(page, `${directory}/final.png`);
  }
});
