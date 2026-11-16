import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  workers: 1,
  retries: 0,
  timeout: 25 * 60 * 1000,
  expect: { timeout: 30_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.OUTCOME_BASE_URL,
    ignoreHTTPSErrors: true,
    locale: 'en-US',
    trace: 'off',
    screenshot: 'off',
    viewport: { width: 1280, height: 900 },
  },
  projects: [{ name: 'desktop' }],
});
