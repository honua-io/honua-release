#!/usr/bin/env node
// Preparation only until a reviewed config supplies actual published identities.
// Execute under the shared Node 22 build lock with an outer 480-second timeout.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { fileURLToPath, pathToFileURL } from 'node:url';

const hash = (bytes, algorithm = 'sha256', encoding = 'hex') => createHash(algorithm).update(bytes).digest(encoding);
const json = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const configBytes = fs.readFileSync(process.argv[2]);
const config = JSON.parse(configBytes);
assert.match(process.version, /^v22\./);
assert.match(config.producerRevision, /^[a-f0-9]{40}$/);
assert.ok(['vanilla-ts', 'react-ts'].includes(config.template));
const root = fs.realpathSync(config.repoRoot);
assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(), config.producerRevision);
const output = path.resolve(config.outputDirectory);
fs.mkdirSync(output, { recursive: false });
fs.writeFileSync(path.join(output, 'execution-config.json'), configBytes);
const harnessBytes = fs.readFileSync(fileURLToPath(import.meta.url));
fs.writeFileSync(path.join(output, 'harness.mjs'), harnessBytes);
const helperPaths = ['scripts/lib/create-honua-app-browser.mjs', 'scripts/lib/create-honua-app-evidence.mjs'];
const helpers = helperPaths.map((relative) => {
  const actual = fs.readFileSync(path.join(root, relative));
  const committed = execFileSync('git', ['show', `${config.producerRevision}:${relative}`], { cwd: root, maxBuffer: 8 * 1024 * 1024 });
  assert.deepEqual(actual, committed, `${relative} differs from the producer revision`);
  fs.writeFileSync(path.join(output, path.basename(relative)), actual);
  return { path: relative, sha256: hash(actual) };
});
const { probeFirstMap } = await import(pathToFileURL(path.join(root, helperPaths[0])).href);
const { verifyRegistryInstall } = await import(pathToFileURL(path.join(root, helperPaths[1])).href);

function publication(kind, name) {
  const identity = config[kind];
  assert.match(identity.version, /^\d+\.\d+\.\d+$/);
  assert.match(identity.sourceRevision, /^[a-f0-9]{40}$/);
  assert.match(identity.receiptSha256, /^[a-f0-9]{64}$/);
  const bytes = fs.readFileSync(identity.receiptPath);
  assert.equal(hash(bytes), identity.receiptSha256, `${kind} receipt hash`);
  const receipt = JSON.parse(bytes);
  assert.equal(receipt.format, 'honua.sdk.published-release-verification.v1');
  assert.equal(receipt.repository, 'honua-io/honua-sdk-js');
  assert.equal(receipt.status, 'verified');
  const matches = receipt.artifacts.filter((row) => row.npmName === name);
  assert.equal(matches.length, 1);
  const artifact = matches[0];
  assert.equal(artifact.version, identity.version);
  assert.equal(artifact.sourceRevision, identity.sourceRevision);
  assert.equal(artifact.status, 'verified');
  for (const id of ['registry-version', 'dist-integrity', 'publish-attestation', 'provenance-attestation', 'provenance-subject', 'provenance-builder', 'source-revision']) {
    assert.equal(artifact.checks.filter((check) => check.id === id && check.status === 'pass').length, 1, `${kind}: ${id}`);
  }
  fs.writeFileSync(path.join(output, `${kind}-publication-receipt.json`), bytes);
  return { artifact, receiptSha256: hash(bytes) };
}
const publications = { scaffold: publication('scaffold', 'create-honua-app'), sdk: publication('sdk', '@honua/sdk-js') };
const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk2101-public-default-'));
const consumer = path.join(workspace, 'consumer');
fs.mkdirSync(consumer);
fs.writeFileSync(path.join(consumer, 'package.json'), JSON.stringify({ name: 'honua-public-default-proof', private: true, type: 'module' }));
const userConfig = path.join(workspace, 'empty-user.npmrc');
const globalConfig = path.join(workspace, 'empty-global.npmrc');
fs.writeFileSync(userConfig, ''); fs.writeFileSync(globalConfig, '');
const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^npm_config_/i.test(key) && !/^VITE_/i.test(key)));
Object.assign(environment, {
  CI: '1', npm_config_userconfig: userConfig, npm_config_globalconfig: globalConfig,
  npm_config_cache: path.join(workspace, 'fresh-cache'), npm_config_registry: 'https://registry.npmjs.org/',
  'npm_config_@honua:registry': 'https://registry.npmjs.org/', npm_config_legacy_peer_deps: 'false',
  npm_config_force: 'false', npm_config_strict_ssl: 'true', npm_config_audit: 'false', npm_config_fund: 'false',
});
const report = {
  format: 'honua.sdk.published-default-cli-proof.v1', status: 'failed', requestedSelection: 'default',
  observedDefaultSdkSelection: false, observedAt: new Date().toISOString(), template: config.template,
  producerRevision: config.producerRevision, node: process.version, harnessSha256: hash(harnessBytes),
  executionConfigSha256: hash(configBytes), helpers, publications, workspace,
  defaultPeerResolution: true, publicRegistry: 'https://registry.npmjs.org/', commands: [],
  measurement: { budgetMs: 120000, elapsedMs: 0, withinBudget: false, stages: [] },
  journey: { mapMounted: false, canvasPresent: false, renderedFeatureCount: 0, dataLane: 'fixture', consoleErrors: [], externalRequests: [] },
};
let timerStart;
let stageStart;
let preview;
let commandNumber = 0;
const stage = (name) => {
  const now = performance.now();
  report.measurement.stages.push({ name, elapsedMs: Math.round(now - stageStart) });
  stageStart = now;
};
function npm(args, cwd = consumer) {
  const logPath = path.join(output, `command-${++commandNumber}.log`);
  report.commands.push({ command: 'npm', arguments: args, cwd, log: path.basename(logPath) });
  try {
    const stdout = execFileSync('npm', args, { cwd, env: environment, encoding: 'utf8', timeout: 180000, maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
    fs.writeFileSync(logPath, stdout);
    return stdout;
  } catch (error) {
    fs.writeFileSync(logPath, `${error.stdout ?? ''}\n${error.stderr ?? ''}`);
    throw new Error(`npm ${args.join(' ')} failed; retained ${path.basename(logPath)}: ${error.code ?? error.status}`);
  }
}
async function registry(kind) {
  const artifact = publications[kind].artifact;
  const metadataText = npm(['view', `${artifact.npmName}@${artifact.version}`, '--json']);
  fs.writeFileSync(path.join(output, `${kind}-registry-metadata.json`), metadataText);
  const metadata = JSON.parse(metadataText);
  assert.equal(metadata.name, artifact.npmName); assert.equal(metadata.version, artifact.version);
  assert.equal(metadata.gitHead, artifact.sourceRevision);
  assert.equal(metadata.dist.integrity, artifact.integrity); assert.equal(metadata.dist.tarball, artifact.tarball);
  const url = new URL(artifact.tarball);
  assert.equal(url.origin, 'https://registry.npmjs.org');
  assert.equal(url.username + url.password + url.search + url.hash, '');
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(30000) });
  assert.equal(response.status, 200);
  const bytes = Buffer.from(await response.arrayBuffer());
  assert.equal(`sha512-${hash(bytes, 'sha512', 'base64')}`, artifact.integrity);
  fs.writeFileSync(path.join(output, `${kind}.tgz`), bytes);
  report[`${kind}Tarball`] = { sha256: hash(bytes), bytes: bytes.length, integrity: artifact.integrity };
  return metadata;
}
function installed(kind, directory, metadata) {
  const artifact = publications[kind].artifact;
  const result = verifyRegistryInstall({ metadata, packageName: artifact.npmName, version: artifact.version,
    installed: json(path.join(directory, 'node_modules', artifact.npmName, 'package.json')),
    lock: json(path.join(directory, 'package-lock.json')) });
  fs.copyFileSync(path.join(directory, 'package-lock.json'), path.join(output, `${kind}-package-lock.json`));
  return result;
}
function closePreview() {
  if (!preview) return;
  try { process.kill(-preview.pid, 'SIGTERM'); } catch { preview.kill('SIGTERM'); }
  preview = undefined;
}
async function serve(directory) {
  preview = spawn('npm', ['run', 'preview', '--', '--host', '127.0.0.1', '--port', '4173'], { cwd: directory, env: environment, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  return await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Preview did not report a URL within 45 seconds')), 45000);
    let buffered = '';
    const inspect = (chunk) => {
      fs.appendFileSync(path.join(output, 'preview.log'), chunk);
      buffered += String(chunk).replace(/\x1b\[[0-9;]*m/g, '');
      const match = buffered.match(/http:\/\/(?:localhost|127\.0\.0\.1):(\d+)\//);
      if (match) { clearTimeout(timeout); resolve(`http://127.0.0.1:${match[1]}/`); }
    };
    preview.stdout.on('data', inspect); preview.stderr.on('data', inspect);
    preview.once('error', (error) => { clearTimeout(timeout); reject(error); });
    preview.once('exit', (code) => { clearTimeout(timeout); reject(new Error(`Preview exited: ${code}`)); });
  });
}
try {
  report.npm = npm(['--version']).trim();
  const npmConfiguration = JSON.parse(npm(['config', 'list', '--json']));
  for (const [key, expected] of Object.entries({ registry: 'https://registry.npmjs.org/', '@honua:registry': 'https://registry.npmjs.org/', 'legacy-peer-deps': false, force: false, 'strict-ssl': true, userconfig: userConfig, globalconfig: globalConfig, cache: path.join(workspace, 'fresh-cache') })) {
    assert.equal(npmConfiguration[key], expected, `Effective npm configuration: ${key}`);
  }
  report.verifiedNpmConfiguration = { registry: npmConfiguration.registry, scopedRegistry: npmConfiguration['@honua:registry'], legacyPeerDeps: npmConfiguration['legacy-peer-deps'], force: npmConfiguration.force, strictSsl: npmConfiguration['strict-ssl'], freshCache: true, emptyUserAndGlobalConfig: true };
  timerStart = performance.now(); stageStart = timerStart;
  const scaffoldMetadata = await registry('scaffold');
  const latestText = npm(['view', 'create-honua-app@latest', '--json']);
  fs.writeFileSync(path.join(output, 'scaffold-latest-registry-metadata.json'), latestText);
  const latest = JSON.parse(latestText);
  assert.equal(latest.name, scaffoldMetadata.name);
  assert.equal(latest.version, config.scaffold.version, 'Public create-honua-app@latest must name the selected final scaffold');
  assert.equal(latest.gitHead, scaffoldMetadata.gitHead);
  assert.equal(latest.dist.integrity, scaffoldMetadata.dist.integrity);
  assert.equal(latest.dist.tarball, scaffoldMetadata.dist.tarball);
  report.observedScaffoldDistTag = { tag: 'latest', version: latest.version, sourceRevision: latest.gitHead, integrity: latest.dist.integrity, metadataSha256: hash(Buffer.from(latestText)) };
  const sdkMetadata = await registry('sdk');
  stage('verify-registry');
  npm(['install', '--save-exact', `create-honua-app@${config.scaffold.version}`]);
  report.installedScaffold = installed('scaffold', consumer, scaffoldMetadata);
  const cliRoot = path.join(consumer, 'node_modules/create-honua-app');
  const cliPackage = json(path.join(cliRoot, 'package.json'));
  assert.equal(cliPackage.bin['create-honua-app'], 'bin/create-honua-app.mjs');
  const cli = fs.realpathSync(path.join(consumer, 'node_modules/.bin/create-honua-app'));
  assert.equal(cli, fs.realpathSync(path.join(cliRoot, cliPackage.bin['create-honua-app'])));
  report.cli = { path: cli, version: cliPackage.version, sha256: hash(fs.readFileSync(cli)) };
  stage('install-cli');
  // npm exec must resolve the installed public CLI. No SDK selection override.
  npm(['exec', '--no', '--', 'create-honua-app', 'app', '--template', config.template]);
  const project = path.join(consumer, 'app');
  const generated = json(path.join(project, 'package.json'));
  assert.equal(generated.dependencies['@honua/sdk-js'], config.sdk.version);
  report.observedDefaultSdkSelection = true;
  fs.copyFileSync(path.join(project, 'package.json'), path.join(output, 'generated-package.json'));
  stage('scaffold');
  npm(['install'], project);
  report.installedSdk = installed('sdk', project, sdkMetadata);
  stage('install-app');
  npm(['run', 'build'], project); stage('build');
  const url = await serve(project); stage('serve');
  await probeFirstMap(url, report.journey, { timeoutMs: 45000 }); stage('map');
  assert.equal(report.journey.consoleErrors.length, 0); assert.equal(report.journey.externalRequests.length, 0);
  assert.ok(report.journey.mapMounted && report.journey.canvasPresent && report.journey.renderedFeatureCount > 0);
  assert.ok(performance.now() - timerStart <= 120000, 'Published CLI to rendered map exceeds the unchanged 120-second budget');
  report.status = 'passed';
} catch (error) {
  report.failure = { message: error.message, stack: error.stack };
  process.exitCode = 1;
} finally {
  closePreview();
  report.measurement.elapsedMs = timerStart === undefined ? 0 : Math.round(performance.now() - timerStart);
  report.measurement.withinBudget = timerStart !== undefined && report.measurement.elapsedMs <= 120000;
  if (report.status === 'passed' && !report.measurement.withinBudget) {
    report.status = 'failed'; report.failure = { message: 'Final measurement exceeded the 120-second budget' }; process.exitCode = 1;
  }
  fs.writeFileSync(path.join(output, 'receipt.json'), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${report.status}: ${path.join(output, 'receipt.json')}\n`);
}
