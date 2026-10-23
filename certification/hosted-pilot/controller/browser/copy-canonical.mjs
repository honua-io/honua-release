// Copy the exact browser directories produced by the same versioned upstream
// canonical installer. The OCI digest transports them without a mutable cache.
import fs from 'node:fs';
import crypto from 'node:crypto';
const root = '/opt/honua-render';
const metadataBytes = fs.readFileSync(root + '/node_modules/playwright-core/browsers.json');
const metadata = JSON.parse(metadataBytes);
const pkg = JSON.parse(fs.readFileSync(root + '/node_modules/playwright-core/package.json'));
if (pkg.name !== 'playwright-core' || pkg.version !== '1.61.1' || process.version !== 'v22.23.3') throw Error('Pinned browser package/runtime differs.');
const names = ['chromium', 'chromium-headless-shell', 'ffmpeg'];
fs.mkdirSync(root + '/browsers');
const installed = [];
for (const name of names) {
  const rows = metadata.browsers.filter(row => row.name === name);
  if (rows.length !== 1 || !/^\d+$/.test(rows[0].revision)) throw Error('Canonical browser metadata differs.');
  const row = rows[0], folder = name.replaceAll('-', '_') + '-' + row.revision;
  const source = '/canonical-browsers/' + folder;
  if (!fs.statSync(source).isDirectory() || !fs.existsSync(source + '/INSTALLATION_COMPLETE')) throw Error('Official canonical installation is missing.');
  fs.cpSync(source, root + '/browsers/' + folder, { recursive: true, verbatimSymlinks: true, preserveTimestamps: true });
  installed.push({ name, revision: row.revision, browserVersion: row.browserVersion ?? null, directory: folder });
}
const chrome = installed.find(row => row.name === 'chromium');
if (!/^\d+(?:\.\d+){3}$/.test(chrome.browserVersion)
    || installed.find(row => row.name === 'chromium-headless-shell').browserVersion !== chrome.browserVersion) throw Error('Canonical Chromium versions differ.');
fs.writeFileSync(root + '/runtime.json', JSON.stringify({ playwright: pkg.version, chromium: chrome.browserVersion }) + '\n');
fs.writeFileSync(root + '/browser-source.json', JSON.stringify({ schema: 'honua.canonical-browser-source/v1',
  image: 'mcr.microsoft.com/playwright:v1.61.1-noble@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48',
  upstreamInstaller: 'https://github.com/microsoft/playwright/blob/v1.61.1/utils/docker/Dockerfile.noble',
  metadataSha256: crypto.createHash('sha256').update(metadataBytes).digest('hex'), browsers: installed }) + '\n');
