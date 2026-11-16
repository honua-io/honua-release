import type { Page } from '@playwright/test';
import { appendFileSync, writeFileSync } from 'node:fs';

const PRODUCT_API = /^\/(api|mcp|rest|ogc)\//;

export function writeJson(file: string, value: unknown) {
  writeFileSync(file, `${JSON.stringify(value, null, 1)}\n`);
}

/** Keeps page errors, failed requests and API statuses beside the transcript. */
export function captureDiagnostics(page: Page, file: string) {
  const startedAt = Date.now();
  const log = (line: string) => {
    appendFileSync(file, `${((Date.now() - startedAt) / 1000).toFixed(2)} ${line.slice(0, 600)}\n`);
  };
  writeFileSync(file, '');
  page.on('pageerror', error => log(`pageerror ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') log(`console ${message.text()}`);
  });
  page.on('requestfailed', request => {
    log(`requestfailed ${request.method()} ${new URL(request.url()).pathname} ${request.failure()?.errorText}`);
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (response.status() >= 400 || PRODUCT_API.test(path)) {
      log(`http ${response.request().method()} ${path} ${response.status()}`);
    }
  });
}

export async function screenshot(page: Page, file: string) {
  await page.screenshot({ path: file, fullPage: true }).catch(() => undefined);
}
