// Console witness half of the focused read/approve receipt (driven by run.py).
//
// The browser reaches the pinned Console through the trusted edge proxy, which asserts the
// operator identity without any forwardable credential. The operator signs in to honua-server
// through the real IdP, and the Console exchanges that server session for its own operator bearer. It then reads the
// exact proposals the scoped API key resolved. Credentials arrive only through the
// environment and are never written to observations or page captures.
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const env = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const origin = new URL(env('RECEIPT_CONSOLE_ORIGIN')).origin;
const out = env('RECEIPT_OUT');
const proposals = JSON.parse(env('RECEIPT_PROPOSALS'));
const idpHost = env('RECEIPT_IDP_HOST');
const observations = { proposals: {} };
let activePage;

const { chromium } = await import(pathToFileURL(join(env('RECEIPT_PLAYWRIGHT'), 'index.mjs')).href);
const browser = await chromium.launch({
  headless: true,
  // One issuer URL for the server container and the browser: the IdP alias resolves to loopback here.
  args: ['--host-resolver-rules=MAP host.docker.internal 127.0.0.1'],
});

async function capture(page, name) {
  writeFileSync(join(out, `${name}.html`), await page.content());
}

async function readProposal(page, label, id) {
  await page.goto(`${origin}/approvals?proposalId=${encodeURIComponent(id)}`);
  const panel = page.locator(`section[data-proposal-id="${id}"]`);
  try {
    await panel.waitFor({ state: 'visible', timeout: 45_000 });
  } catch (error) {
    const detail = (await page.locator('.approval-inbox-detail').first().innerText().catch(() => ''))
      .replace(/\s+/g, ' ').trim().slice(0, 400);
    observations.proposals[label] = { proposalId: id, rendered: false, detail };
    throw new Error(`the Console did not render ${label} proposal ${id}: ${detail}`);
  }
  const status = await panel.locator('[data-proposal-status]').getAttribute('data-proposal-status');
  observations.proposals[label] = {
    proposalId: await panel.getAttribute('data-proposal-id'),
    status,
    approveControlRendered: (await panel.locator('[data-proposal-approve]').count()) > 0,
  };
  await capture(page, `proposal-${label}`);
}

try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  activePage = page;

  // Before server sign-in the operator has no bearer and the Console has no admin key:
  // the exact approved proposal must not render.
  await page.goto(`${origin}/approvals?proposalId=${encodeURIComponent(proposals.approve)}`);
  await page.locator('[data-inbox-error], .console-state-error, [data-operate-status], section.console-panel')
    .first().waitFor({ state: 'visible', timeout: 45_000 }).catch(() => undefined);
  await page.waitForTimeout(3_000);
  const beforeUrl = new URL(page.url());
  observations.beforeServerSignIn = {
    page: beforeUrl.pathname,
    proposalStatusRendered: (await page.locator('[data-proposal-status]').count()) > 0,
    detail: ((await page.locator('.approval-inbox-detail, [data-inbox-error]').first().innerText()
      .catch(() => '')) || '').replace(/\s+/g, ' ').trim().slice(0, 400),
  };
  if (beforeUrl.pathname !== '/approvals') {
    throw new Error(`the operator was not admitted to the Console (landed on ${beforeUrl.pathname})`);
  }
  await capture(page, 'before-server-sign-in');
  if (observations.beforeServerSignIn.proposalStatusRendered) {
    throw new Error('the Console rendered a proposal before the operator signed in to honua-server');
  }

  // Real OIDC sign-in through the Console's shared-origin server-session bridge.
  await page.goto(`${origin}/auth/server/login?profileId=local-dev&returnTo=%2Fapprovals`);
  await page.waitForURL((url) => url.host === idpHost, { timeout: 60_000 });
  await page.locator('#username').fill(env('RECEIPT_OPERATOR_USER'));
  await page.locator('#password').fill(env('RECEIPT_OPERATOR_PASSWORD'));
  await page.locator('#kc-login').click();
  await page.waitForURL((url) => url.origin === origin && url.pathname === '/approvals', { timeout: 60_000 });
  observations.serverSignIn = { returnedTo: '/approvals', identityProviderHost: 'host.docker.internal' };

  await page.locator('[data-proposal-id], [data-inbox-error], .approval-inbox-summary').first()
    .waitFor({ state: 'visible', timeout: 45_000 }).catch(() => undefined);
  await page.waitForTimeout(2_000);
  observations.inbox = {
    proposalIds: await page.locator('[data-proposal-id]')
      .evaluateAll((nodes) => [...new Set(nodes.map((node) => node.getAttribute('data-proposal-id')))]),
    text: (await page.locator('main').first().innerText().catch(() => '')).replace(/\s+/g, ' ').trim().slice(0, 600),
  };
  await capture(page, 'inbox-after-sign-in');

  for (const [label, id] of Object.entries(proposals)) {
    await readProposal(page, label, id);
  }
  await context.close();
  writeFileSync(join(out, 'observations.json'), JSON.stringify({ observations }, null, 2));
} catch (error) {
  if (activePage) {
    // Location without query or fragment: IdP and callback URLs carry one-time state.
    const failedAt = new URL(activePage.url());
    observations.failedAt = `${failedAt.host}${failedAt.pathname}`;
    await capture(activePage, 'failure').catch(() => undefined);
  }
  writeFileSync(join(out, 'observations.json'), JSON.stringify({
    observations,
    error: error instanceof Error ? error.message.split('\n')[0] : 'unknown browser failure',
  }, null, 2));
  process.exitCode = 1;
} finally {
  await browser.close();
}
