// Fixed image startup control: no credentials, HTTP, candidate or model input.
import { readFile } from 'node:fs/promises';
import { chromium } from '/opt/honua-render/node_modules/playwright-core/index.mjs';
const runtime = JSON.parse(await readFile('/opt/honua-render/runtime.json', 'utf8'));
const browser = await chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  if (process.version !== 'v22.23.3' || runtime.playwright !== '1.61.1' || browser.version() !== runtime.chromium) throw Error('Runtime differs.');
  const context = await browser.newContext({ viewport: { width: 32, height: 32 }, serviceWorkers: 'block' });
  await context.route('**/*', route => route.abort());
  const page = await context.newPage();
  await page.setContent('<html><body style="margin:0;background:rgb(0,128,0)"></body></html>');
  const png = await page.screenshot();
  if (png.readUInt32BE(0) !== 0x89504e47 || png.length < 100) throw Error('Browser did not paint.');
  await context.close();
  process.stdout.write(JSON.stringify({ status: 'runtime-started-and-painted', runtime }) + '\n');
} finally {
  await browser.close();
}
