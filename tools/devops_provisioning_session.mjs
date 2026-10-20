// Real published-native session preparation for original129. No source-built
// executable, shell fallback, issuer, cloud command or generic tool entrypoint.
import { createHash } from 'node:crypto';
import { lstat, realpath, mkdir, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { preparePublishedRuntime, recheckPublishedRuntime, validateTool } from './devops_native_mcp.mjs';
import { createLifecycleTransport } from './devops_transport_lifecycle.mjs';
import { createOwnedToolCalls } from './devops_owned_tool_calls.mjs';
import { prepareScopeLaunch, observeScope, drainScope } from './devops_process_scope.mjs';

const fail = code => { throw Error(code); };
const digest = value => createHash('sha256').update(value).digest('hex');
const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
const TOOL_NAMES = Object.freeze(['provision_infrastructure', 'install_handoff', 'verify_install_handoff']);

// Exact finite normal runtime declaration. This list grants no values or keys;
// expectedEnvironmentSha256 is authenticated outside this module from the
// normal owned run request. Private values never enter the public journal.
const REQUIRED = Object.freeze(['PATH', 'AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
  'HONUA_DEVOPS_EXECUTION_MODE', 'HONUA_DEVOPS_EXECUTION_TIER', 'HONUA_DEVOPS_APPROVAL_MODE',
  'HONUA_DEVOPS_ALLOWED_ENVIRONMENTS', 'HONUA_DEVOPS_TERRAFORM_LOCAL_PATH',
  'HONUA_DEVOPS_PROVISION_APPROVAL_SIGNING_MODE', 'HONUA_DEVOPS_PROVISION_APPROVAL_ISSUER_KEY_ARNS',
  'HONUA_DEVOPS_CANDIDATE_REFERENCE', 'HONUA_DEVOPS_MCP_PROXY_PACKAGE', 'HONUA_DEVOPS_MCP_PROXY_INTEGRITY']);
const OPTIONAL = Object.freeze(['AWS_DEFAULT_REGION', 'AWS_CA_BUNDLE', 'SSL_CERT_FILE',
  'HONUA_DEVOPS_TERRAFORM_BIN', 'HONUA_IAC_TERRAFORM_BIN', 'HONUA_IAC_PUBLIC_KEYS',
  'HONUA_IAC_BACKEND_PUBLIC_KEYS', 'HONUA_IAC_IDENTITY_ISSUER', 'HONUA_IAC_SESSION_EXPIRY',
  'HONUA_DEVOPS_DEPLOY_TARGET_ID', 'HONUA_DEVOPS_HONUA_API_BASE_URL', 'HONUA_DEVOPS_OTEL_BASE_URL']);

export function validateRuntimeDeclaration(environment, expectedEnvironmentSha256, declaredTerraformVariables) {
  if (!environment || Object.getPrototypeOf(environment) !== Object.prototype
      || !Array.isArray(declaredTerraformVariables)
      || new Set(declaredTerraformVariables).size !== declaredTerraformVariables.length
      || !declaredTerraformVariables.every(name => /^[a-z][a-z0-9_]*$/.test(name))) fail('provision-runtime-shape');
  const allowed = new Set([...REQUIRED, ...OPTIONAL, ...declaredTerraformVariables.map(name => 'TF_VAR_' + name)]);
  if (!REQUIRED.every(name => Object.hasOwn(environment, name))
      || !Object.entries(environment).every(([name, value]) => allowed.has(name)
        && typeof value === 'string' && value.length > 0 && value.length <= 65536 && !value.includes('\0')))
    fail('provision-runtime-closure');
  if (!/^[a-f0-9]{64}$/.test(expectedEnvironmentSha256 ?? '')
      || digest(Buffer.from(canonical(environment))) !== expectedEnvironmentSha256) fail('provision-runtime-original');
  if (environment.HONUA_DEVOPS_EXECUTION_MODE !== 'execute'
      || !['execute-lower-env', 'break-glass'].includes(environment.HONUA_DEVOPS_EXECUTION_TIER)
      || environment.HONUA_DEVOPS_APPROVAL_MODE !== 'direct-allowed'
      || environment.HONUA_DEVOPS_PROVISION_APPROVAL_SIGNING_MODE !== 'kms-mac'
      || !['dev', 'staging', 'it'].includes(environment.HONUA_DEVOPS_ALLOWED_ENVIRONMENTS))
    fail('provision-runtime-normal-authority');
  // Offline/test fixture, local HMAC, command injection and ambient credential
  // selectors are not members of this launch declaration.
  return { ...environment };
}

async function privateDirectory(path) {
  const absolute = resolve(path), info = await lstat(absolute);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid()
      || info.mode & 0o077 || await realpath(absolute) !== absolute) fail('provision-private-directory');
  return absolute;
}

export async function prepareProvisioningSession({ expectedFile, outputDirectory, privateHome,
  environment, expectedEnvironmentSha256, declaredTerraformVariables, expectedTools }) {
  if (process.platform !== 'linux' || process.arch !== 'x64') fail('provision-native-host');
  const root = await privateDirectory(outputDirectory), home = await privateDirectory(privateHome);
  const normal = validateRuntimeDeclaration(environment, expectedEnvironmentSha256, declaredTerraformVariables);
  if (!expectedTools || Object.keys(expectedTools).sort().join() !== [...TOOL_NAMES].sort().join()
      || !Object.values(expectedTools).every(value => /^[a-f0-9]{64}$/.test(value))) fail('provision-tool-closure');
  const invocation = join(root, 'native-session-' + process.pid + '-' + Date.now());
  await mkdir(invocation, { mode: 0o700 });
  const runtime = await preparePublishedRuntime(expectedFile, invocation);
  const cwd = join(invocation, 'cwd'); await mkdir(cwd, { mode: 0o700 });
  // The same independently owned HOME is retained over native restarts; using
  // a new empty home would conceal durable cleanup/one-use behavior.
  const env = { ...normal, HOME: home, XDG_DATA_HOME: join(home, '.local/share'), TMPDIR: join(home, 'tmp'),
    DOTNET_BUNDLE_EXTRACT_BASE_DIR: join(home, 'bundle') };
  await mkdir(env.TMPDIR, { mode: 0o700, recursive: true });
  const launch = await prepareScopeLaunch(runtime, invocation);
  const client = new runtime.sdk.Client({ name: 'honua-release-owned-provisioning', version: '1' });
  const transport = new runtime.sdk.StdioClientTransport({ command: launch.command, args: [...launch.args], cwd, env, stderr: 'pipe' });
  const lifecycle = createLifecycleTransport(transport);
  const observations = [];
  let stderrBytes = 0;
  const stderr = [];
  transport.stderr?.on('data', bytes => {
    stderrBytes += bytes.length;
    if (stderrBytes <= 128 * 1024) stderr.push(bytes);
  });
  let started = false;
  try {
    await client.connect(lifecycle.transport, { timeout: 15000 }); started = true;
    const tools = [], seen = new Set(); let cursor;
    for (let page = 0; page < 100; page++) {
      const response = await client.listTools(cursor ? { cursor } : {}, { timeout: 5000 });
      if (!Array.isArray(response.tools)) fail('provision-tool-discovery');
      for (const tool of response.tools) {
        if (typeof tool?.name !== 'string' || seen.has('tool:' + tool.name)) fail('provision-tool-discovery');
        seen.add('tool:' + tool.name); tools.push(tool);
      }
      cursor = response.nextCursor;
      if (!cursor) break;
      if (typeof cursor !== 'string' || seen.has('cursor:' + cursor) || page === 99) fail('provision-tool-discovery');
      seen.add('cursor:' + cursor);
    }
    for (const name of TOOL_NAMES) {
      // Metadata binds the actual published tool contract before any call.
      validateTool(tools.find(tool => tool.name === name), expectedTools[name], { name, arguments: {} });
    }
    const serverInfo = client.getServerVersion();
    const initialScope = await observeScope(launch, lifecycle.transport.pid);
    const nativeAdmission = initialScope.nativeAdmission;
    await writeFile(join(invocation, 'native-admission.json'), JSON.stringify(nativeAdmission) + '\n',
      { mode: 0o600, flag: 'wx' });
    const calls = createOwnedToolCalls({ transport: lifecycle.transport, tools, expectedTools,
      CallToolResultSchema: runtime.sdk.CallToolResultSchema, invocation, privateHome: home, nativeAdmission });
    // Return an owned live handle, not a JSON authorization receipt. The actual
    // long-call adapter is deliberately separate from this preparation layer.
    return { runtime, invocation, home, tools, serverInfo, observations, nativeAdmission, calls,
      scope: { async observe(options) { return observeScope(launch, lifecycle.transport.pid, options); } },
      async closeQuiescent({ signal } = {}) {
        if (calls.pendingIds().length || calls.unreconciledIds().length) fail('provision-native-executor-unreconciled');
        const witness = await observeScope(launch, lifecycle.transport.pid, { requireClosed: true, signal });
        calls.beginDrain(witness);
        const terminal = await drainScope(launch, lifecycle.transport.pid, witness, { signal });
        // Stock SDK close has a 2s signal fallback. Invoke it only AFTER the
        // supervised launcher actually exited normally with no owned children.
        await lifecycle.closeAfterTerminal(terminal, nativeAdmission);
        await client.close();
        return terminal;
      },
      async retain() {
        await writeFile(join(invocation, 'native.stderr'), Buffer.concat(stderr), { mode: 0o600, flag: 'wx' });
        if (stderrBytes > 128 * 1024) fail('provision-native-stderr-bound');
      },
      async recheck() { return recheckPublishedRuntime(runtime, invocation); } };
  } catch (error) {
    let retentionError = null;
    try { await writeFile(join(invocation, 'native.stderr'), Buffer.concat(stderr), { mode: 0o600, flag: 'wx' }); }
    catch (failure) { retentionError = failure.code ?? 'stderr-retention-failed'; }
    // Initialization failure is not proof that all descendants ceased. Retain
    // an owned handle; no finally/SDK-close path can signal live native work.
    throw Object.assign(error, { ownedNativeHandle: {
      invocation, started, retentionError,
      deferredCloseRequests: () => lifecycle.deferredCloseRequests(),
      async observe(options) { return observeScope(launch, lifecycle.transport.pid, options); },
      async finishQuiescent({ signal } = {}) {
        const witness = await observeScope(launch, lifecycle.transport.pid, { requireClosed: true, signal });
        const terminal = await drainScope(launch, lifecycle.transport.pid, witness, { signal });
        await lifecycle.closeAfterTerminal(terminal, terminal.nativeAdmission);
        await client.close(); return terminal;
      }
    } });
  }
}
