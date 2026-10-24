// Actual published native stdio transport. No provisioning authority or admission.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, writeFile, mkdir, lstat, realpath, readdir } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { dirname, resolve, join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const LOCK = '5e6b28f4c29b82b799190e9e92083c7aca38b879afc7e242bee4320a711e195f';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
async function fileHash(path) { const result = createHash('sha256'); for await (const bytes of createReadStream(path)) result.update(bytes); return result.digest('hex'); }
const fail = category => { throw Error(category); };
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join() === [...keys].sort().join();
const canonical = value => JSON.stringify(value, (_, entry) => entry && typeof entry === 'object' && !Array.isArray(entry)
  ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);

export function typedReadRequest(name, operationId) {
  if (name === 'describe_environment' && operationId === undefined) return { name, arguments: {} };
  if (name === 'inspect-cleanup' && typeof operationId === 'string' && /^urn:honua:provisioning:[0-9A-Fa-f]{32}$/.test(operationId)) {
    return { name: 'provision_infrastructure', arguments: { stack: 'aws-ecs', size: 'small', action: 'inspect-cleanup',
      variablesJson: '{}', confirmed: false, confirmation: '', approvalReceiptJson: '',
      idempotencyKey: '', provisioningOperationId: operationId } };
  }
  // The full authority/one-use-plan/role/backend join is still original129/183
  // work. A caller boolean, role name or source recipe cannot enable it here.
  fail('native-actuation-authority-unqualified');
}

export function validateTool(tool, expectedDigest, request) {
  if (!tool || tool.name !== request.name || !tool.inputSchema || tool.inputSchema.type !== 'object'
      || !/^[a-f0-9]{64}$/.test(expectedDigest ?? '') || hash(Buffer.from(canonical(tool))) !== expectedDigest) fail('native-tool-schema-binding');
  const props = tool.inputSchema.properties;
  if (request.name === 'provision_infrastructure' && (!props || 'cancellationToken' in props
      || !Object.keys(request.arguments).every(key => key in props))) fail('native-provision-schema');
}

export async function validateSdkLocation(module, expectedRoot) {
  if (await realpath(expectedRoot) !== expectedRoot || !module.startsWith(expectedRoot + '/dist/')
      || await realpath(module) !== module) fail('native-sdk-resolution');
}

export async function prepareInstalledSdk(artifactDirectory) {
  const root = resolve(ROOT, 'certification/sources/geospatial-mcp');
  const bytes = await readFile(join(root, 'package-lock.json'));
  const locked = JSON.parse(bytes).packages['node_modules/@modelcontextprotocol/sdk'];
  if (hash(bytes) !== LOCK || locked?.version !== '1.32.1' || !locked.integrity?.startsWith('sha512-')) fail('native-sdk-lock');
  const verification = await finiteProcess('python3', [join(ROOT, 'tools/devops_native_artifact.py'), '--verify-sdk', '--output', artifactDirectory],
    ROOT, join(artifactDirectory, 'sdk-verification.stdout.json'), join(artifactDirectory, 'sdk-verification.stderr'));
  const require = createRequire(join(root, 'package.json'));
  const module = require.resolve('@modelcontextprotocol/sdk/client/index.js');
  await validateSdkLocation(module, join(root, 'node_modules/@modelcontextprotocol/sdk'));
  const typesModule = require.resolve('@modelcontextprotocol/sdk/types.js');
  await validateSdkLocation(typesModule, join(root, 'node_modules/@modelcontextprotocol/sdk'));
  let path = dirname(module), installed;
  for (let i = 0; i < 6; i++, path = dirname(path)) {
    try { const value = JSON.parse(await readFile(join(path, 'package.json'))); if (value.name === '@modelcontextprotocol/sdk') { installed = value; break; } }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  if (installed?.version !== locked.version) fail('native-sdk-installed-version');
  return { Client: (await import(pathToFileURL(module))).Client,
    StdioClientTransport: (await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/stdio.js')))).StdioClientTransport,
    CallToolResultSchema: (await import(pathToFileURL(typesModule))).CallToolResultSchema,
    lockSha256: LOCK, version: installed.version, integrity: locked.integrity, verification };
}

async function finiteProcess(command, args, cwd, stdoutFile, stderrFile) {
  // Only fixed intake helper is called here, never a caller-provided command.
  const child = spawn(command, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
  const stdout = [], stderr = []; let out = 0, err = 0, exceeded = false;
  child.stdout.on('data', bytes => { out += bytes.length; if (out <= 2 * 1024 * 1024) stdout.push(bytes); else exceeded = true; });
  child.stderr.on('data', bytes => { err += bytes.length; if (err <= 128 * 1024) stderr.push(bytes); else exceeded = true; });
  // Child's own finite gh timeouts govern the action. Do not kill an admitted
  // action because the parent waiter timed out. This helper only reads assets.
  const exit = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', (code, signal) => accept({ code, signal })); });
  const raw = Buffer.concat(stdout);
  await writeFile(stdoutFile, raw, { mode: 0o600, flag: 'wx' });
  await writeFile(stderrFile, Buffer.concat(stderr), { mode: 0o600, flag: 'wx' });
  if (exceeded || exit.code !== 0 || exit.signal) fail('native-intake-refused');
  return JSON.parse(raw);
}

async function verifyMembers(seal) {
  if (seal?.schema !== 'honua.devops-native-intake/v1' || seal.sourceQualified !== false || seal.candidateQualified !== false
      || !Array.isArray(seal.members) || !seal.members.length) fail('native-intake-shape');
  const root = resolve(seal.requestDirectory, 'installed');
  if (await realpath(root) !== root) fail('native-installed-path');
  const actual = [];
  async function walk(directory, prefix = '') {
    for (const name of await readdir(directory)) {
      const relative = prefix ? prefix + '/' + name : name;
      const path = join(directory, name), info = await lstat(path);
      if (info.isSymbolicLink() || info.uid !== process.getuid() || info.mode & 0o077) fail('native-installed-ownership');
      if (info.isDirectory()) await walk(path, relative);
      else if (info.isFile() && info.nlink === 1) actual.push({ path: relative, sha256: await fileHash(path), bytes: info.size });
      else fail('native-installed-member');
    }
  }
  await walk(root);
  actual.sort((a, b) => a.path.localeCompare(b.path));
  if (canonical(actual) !== canonical(seal.members)) fail('native-installed-drift');
  const binary = join(root, 'Honua.DevOps.Agent');
  if (resolve(seal.binary) !== binary || seal.expected?.rid !== 'linux-x64') fail('native-execution-rid');
  return binary;
}

// Shared by the bounded original129 coordinator. These helpers authenticate
// normal published bytes and the existing installed SDK; they grant no tool,
// environment, approval, cloud or execution authority. No generic subprocess
// runner is exported. The read-only inspectPublished contract below is retained.
export async function preparePublishedRuntime(expectedFile, invocation) {
  const seal = await finiteProcess('python3', [join(ROOT, 'tools/devops_native_artifact.py'),
    '--expected', resolve(expectedFile), '--output', invocation], ROOT,
  join(invocation, 'intake.stdout.json'), join(invocation, 'intake.stderr'));
  const binary = await verifyMembers(seal);
  const sdk = await prepareInstalledSdk(invocation);
  return { seal, binary, sdk };
}

export async function recheckPublishedRuntime(runtime, invocation) {
  await verifyMembers(runtime.seal);
  const after = await finiteProcess('python3', [join(ROOT, 'tools/devops_native_artifact.py'),
    '--verify-sdk', '--sdk-archive', runtime.sdk.verification.archive, '--output', invocation],
  ROOT, join(invocation, 'sdk-after.stdout.json'), join(invocation, 'sdk-after.stderr'));
  if (after.closureSha256 !== runtime.sdk.verification.closureSha256
      || after.archiveSha256 !== runtime.sdk.verification.archiveSha256) fail('native-sdk-after-drift');
  return after;
}

export async function inspectPublished({ expectedFile, outputDirectory, expectedTools = {}, readRequest = null }) {
  if (process.platform !== 'linux' || process.arch !== 'x64') fail('native-execution-host');
  if (typeof expectedFile !== 'string' || typeof outputDirectory !== 'string'
      || !exact(expectedTools, Object.keys(expectedTools))) fail('native-input-shape');
  const root = resolve(outputDirectory);
  const rootInfo = await lstat(root);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink() || rootInfo.uid !== process.getuid()
      || rootInfo.mode & 0o077 || await realpath(root) !== root) fail('native-output-ownership');
  const invocation = join(root, 'stdio-' + process.pid + '-' + Date.now());
  await mkdir(invocation, { mode: 0o700 });
  let client, transport, seal, result, sdk;
  const stderr = []; let stderrBytes = 0, secretRefused = false;
  const observations = [];
  try {
    // A caller-written "verified":true seal never authorizes executable launch.
    seal = await finiteProcess('python3', [join(ROOT, 'tools/devops_native_artifact.py'), '--expected', resolve(expectedFile), '--output', invocation],
      ROOT, join(invocation, 'intake.stdout.json'), join(invocation, 'intake.stderr'));
    const binary = await verifyMembers(seal);
    sdk = await prepareInstalledSdk(invocation);
    const cwd = join(invocation, 'session'); await mkdir(cwd, { mode: 0o700 });
    // Read-only source prerequisite: no ambient HOME/.env/GH/AWS/credentials,
    // Terraform checkout/executable or operator grants are forwarded.
    const environment = { HOME: cwd, TMPDIR: cwd, DOTNET_BUNDLE_EXTRACT_BASE_DIR: join(cwd, 'bundle'),
      PATH: cwd, HONUA_DEVOPS_EXECUTION_MODE: 'plan', HONUA_DEVOPS_EXECUTION_TIER: 'observe',
      HONUA_DEVOPS_ALLOWED_ENVIRONMENTS: 'dev', HONUA_DEVOPS_HONUA_API_BASE_URL: 'http://127.0.0.1:1',
      HONUA_DEVOPS_OTEL_BASE_URL: 'http://127.0.0.1:1' };
    client = new sdk.Client({ name: 'honua-published-native-source-probe', version: '1' });
    transport = new sdk.StdioClientTransport({ command: binary, args: ['--mcp'], cwd, env: environment, stderr: 'pipe' });
    transport.stderr?.on('data', bytes => { stderrBytes += bytes.length; if (stderrBytes <= 128 * 1024) stderr.push(bytes); });
    const send = transport.send.bind(transport);
    transport.send = async message => {
      if (message.method === 'tools/call') observations.push({ method: message.method, id: message.id,
        name: message.params?.name, argumentsSha256: hash(Buffer.from(canonical(message.params?.arguments))) });
      await send(message);
    };
    await client.connect(transport, { timeout: 15000 });
    const tools = [], seen = new Set(); let cursor;
    for (let page = 0; page < 100; page++) {
      const value = await client.listTools(cursor ? { cursor } : {}, { timeout: 5000 });
      if (!Array.isArray(value.tools)) fail('native-tool-list-shape');
      for (const tool of value.tools) {
        if (typeof tool?.name !== 'string' || seen.has('tool:' + tool.name)) fail('native-tool-duplicates');
        seen.add('tool:' + tool.name); tools.push(tool);
      }
      cursor = value.nextCursor;
      if (!cursor) break;
      if (typeof cursor !== 'string' || seen.has('cursor:' + cursor) || page === 99) fail('native-discovery-incomplete');
      seen.add('cursor:' + cursor);
    }
    if (!tools.length) fail('native-tools-empty');
    let call = null;
    if (readRequest !== null) {
      if (!exact(readRequest, ['name', 'operationId']) && !exact(readRequest, ['name'])) fail('native-read-request');
      const request = typedReadRequest(readRequest.name, readRequest.operationId);
      validateTool(tools.find(tool => tool.name === request.name), expectedTools[request.name], request);
      call = await client.callTool(request, undefined, { timeout: 15000 });
      // Preserve original tool response, including refusal. Transport success
      // is never semantic, handoff, cleanup or candidate qualification.
      if (call.isError !== undefined && typeof call.isError !== 'boolean') fail('native-tool-result');
    }
    result = { schema: 'honua.devops-native-stdio-source/v1', status: 'source-observed',
      sourceQualified: false, candidateQualified: false, artifact: seal, sdk: { version: sdk.version, integrity: sdk.integrity, lockSha256: sdk.lockSha256, verification: sdk.verification },
      serverInfo: client.getServerVersion(), tools, observations, call,
      originalAcceptance: { nativeCohortAdmitted: false, candidateTupleQualified: false,
        roleBackendApprovalJoined: false, sixAwsFaultCellsQualified: false, terminal123Joined: false } };
  } catch (error) {
    result = { schema: 'honua.devops-native-stdio-source/v1', status: 'unqualified',
      category: /^[a-z][a-z0-9-]+$/.test(error.message) ? error.message : error.name,
      sourceQualified: false, candidateQualified: false, observations };
    throw Object.assign(error, { publicResult: result });
  } finally {
    let cleanupError;
    try { if (client) await client.close(); } catch (error) { cleanupError = error; }
    try { if (seal) await verifyMembers(seal); } catch (error) { cleanupError ??= error; }
    if (sdk) {
      try {
        const after = await finiteProcess('python3', [join(ROOT, 'tools/devops_native_artifact.py'), '--verify-sdk', '--sdk-archive', sdk.verification.archive, '--output', invocation],
          ROOT, join(invocation, 'sdk-after.stdout.json'), join(invocation, 'sdk-after.stderr'));
        if (after.closureSha256 !== sdk.verification.closureSha256 || after.archiveSha256 !== sdk.verification.archiveSha256) fail('native-sdk-after-drift');
      } catch (error) { cleanupError ??= error; }
    }
    const bytes = Buffer.concat(stderr);
    // Preserve finite public failure observations before enforcing cleanup/drift
    // refusal. No credentials are forwarded to this read-only native process.
    secretRefused = stderrBytes > 128 * 1024;
    if (cleanupError || secretRefused) result = { ...result, status: 'unqualified',
      category: cleanupError ? 'native-cleanup-or-drift' : 'native-stderr-bound', candidateQualified: false };
    await writeFile(join(invocation, 'native.stderr'), bytes, { mode: 0o600, flag: 'wx' });
    await writeFile(join(invocation, 'result.json'), JSON.stringify({ ...result,
      stderrSha256: hash(bytes), stderrTruncated: secretRefused }) + '\n', { mode: 0o600, flag: 'wx' });
    if (cleanupError) throw Object.assign(cleanupError, { publicResult: result });
    if (secretRefused) throw Object.assign(Error('native-stderr-bound'), { publicResult: result });
  }
  return result;
}

// The public SDK notification method, using an ID observed on THIS owned
// transport. This helper grants no cancellation/cessation or mutation credit.
export async function cancelOwnedRequest(client, observations, requestId) {
  const calls = observations.filter(row => row.method === 'tools/call' && row.id === requestId);
  if (calls.length !== 1) fail('native-cancel-request-identity');
  await client.notification({ method: 'notifications/cancelled', params: { requestId } });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
    if (!exact(config, ['expectedFile', 'outputDirectory', 'expectedTools', 'readRequest'])) fail('native-request-shape');
    process.stdout.write(JSON.stringify(await inspectPublished(config)) + '\n');
  } catch (error) { process.stdout.write(JSON.stringify(error.publicResult ?? { status: 'unqualified', candidateQualified: false,
    category: /^[a-z][a-z0-9-]+$/.test(error.message) ? error.message : error.name }) + '\n'); process.exitCode = 1; }
}
