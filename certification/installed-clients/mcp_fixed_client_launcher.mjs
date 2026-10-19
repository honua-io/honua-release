// Trusted release producer boundary: fixed package verification and child launch.
// Retained JSON records this producer's observation; it is not process attestation.
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { readFileSync, realpathSync, lstatSync, mkdtempSync, rmSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL("./", import.meta.url));
const hash = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
const own = (name) => hash(readFileSync(join(here, name)));
const manifestName = "mcp_fixed_client_manifest.v1.json";

export function verifyInstalledClient(installationRoot, selector) {
  const manifest = JSON.parse(readFileSync(join(here, manifestName), "utf8"));
  if (manifest.schema !== "honua.mcp-fixed-client-manifest/v1" || !["sdk", "inspector"].includes(selector)) throw new Error("fixed canonical client selection refused");
  const selected = manifest.clients[selector];
  const root = realpathSync(join(resolve(installationRoot), ...selected.name.split("/")));
  const metadata = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  if (metadata.name !== selected.name || metadata.version !== selected.version) throw new Error("installed canonical package identity differs from protected manifest");
  const files = selected.files;
  if (!files || !Object.hasOwn(files, "package.json") || !Object.hasOwn(files, selected.entry)) throw new Error("fixed canonical package manifest incomplete");
  try { for (const [name, digest] of Object.entries(files)) {
    const path = resolve(root, name);
    if (!path.startsWith(root + sep) || !/^[a-f0-9]{64}$/.test(digest) || !lstatSync(path).isFile()
        || realpathSync(path) !== path || hash(readFileSync(path)) !== `sha256:${digest}`) throw new Error("installed canonical package bytes differ from protected manifest");
  }
  } catch { throw new Error("installed canonical package bytes differ from protected manifest"); }
  return { root, selected, package: { name: selected.name, version: selected.version, integrity: selected.integrity,
    fileCount: Object.keys(files).length, filesSha256: hash(JSON.stringify(files)),
    packageJsonSha256: `sha256:${files["package.json"]}`, entry: selected.entry, entrySha256: `sha256:${files[selected.entry]}` } };
}

export function inspectorToolArguments(name, arguments_) {
  const pairs = Object.entries(arguments_).map(([key, value]) => `${key}=${JSON.stringify(value)}`);
  // The public CLI requires at least one pair after --tool-arg; otherwise it
  // consumes the following option (such as --metadata) as an invalid pair.
  return ["--tool-name", name, ...(pairs.length ? ["--tool-arg", ...pairs] : [])];
}

export async function launchCanonicalClient({ installationRoot, selector, endpoint, method, params, authenticated, credential }) {
  const url = new URL(endpoint);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.pathname !== "/mcp"
      || url.username || url.password || url.search || url.hash || typeof authenticated !== "boolean"
      || typeof credential !== "string" || !credential || !["tools/call", "resources/read"].includes(method)) throw new Error("fixed client request refused");
  const installed = verifyInstalledClient(installationRoot, selector);
  const directory = mkdtempSync(join(tmpdir(), "release-mcp-installed-client-"));
  const entry = selector === "sdk" ? join(here, "mcp_fixed_sdk_runner.mjs") : join(installed.root, installed.selected.entry);
  const args = [entry];
  if (selector === "inspector") {
    args.push("--cli", url.href, "--transport", "http", "--method", method, "--format", "json", "--stored-auth-only");
    if (method === "tools/call") {
      args.push(...inspectorToolArguments(params.name, params.arguments));
      if (authenticated) args.push("--metadata", 'honua.io/workflow-view="full"');
      else if (["honua_ingest_dataset", "honua_publish_service", "honua_get_style", "honua_apply_style_preset", "honua_execute_plan", "honua_publish_result"].includes(params.name)) {
        // These operations are explicitly advertised by the selected server's
        // public setup view. Discovery narrows the catalog; it grants no auth.
        args.push("--metadata", 'honua.io/workflow-view="setup"');
      }
    } else args.push("--uri", params.uri);
    if (authenticated) args.push("--header", `X-API-Key: ${credential}`);
  }
  const binding = { schema: "honua.mcp-fixed-client-launch/v1", selector, package: installed.package,
    manifestSha256: own(manifestName), launcherSha256: own("mcp_fixed_client_launcher.mjs"), sdkRunnerSha256: own("mcp_fixed_sdk_runner.mjs"),
    request: { endpoint: url.href, method, params, authenticated } };
  try {
    // Async is essential: the transparent observer runs in this event loop.
    const outcome = await new Promise((resolveOutcome, reject) => {
      const child = spawn(process.execPath, args, { env: { PATH: process.env.PATH, HOME: directory, XDG_CONFIG_HOME: directory,
        MCP_AUTO_OPEN_ENABLED: "false", MCP_STORAGE_DIR: directory, MCP_CATALOG_PATH: join(directory, "catalog.json"),
        MCP_INSPECTOR_SECRET_STORE: "file", MCP_INSPECTOR_SECRET_FILE: join(directory, "secrets.json") }, stdio: ["pipe", "pipe", "pipe"] });
      let stdout = "", stderr = "", timedOut = false, oversized = false, forceStop;
      const stop = () => { child.kill("SIGTERM"); forceStop ??= setTimeout(() => child.kill("SIGKILL"), 1_000); };
      const timer = setTimeout(() => { timedOut = true; stop(); }, 30_000);
      const append = (stream, chunk) => {
        if (Buffer.byteLength(stdout) + Buffer.byteLength(stderr) + Buffer.byteLength(chunk) > 2 * 1024 * 1024) { oversized = true; stop(); return; }
        if (stream === "stdout") stdout += chunk; else stderr += chunk;
      };
      child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk) => append("stdout", chunk)); child.stderr.on("data", (chunk) => append("stderr", chunk));
      child.stdin.on("error", () => {}); // Early CLI refusal can close stdin before input.
      child.once("error", () => { clearTimeout(timer); clearTimeout(forceStop); reject(new Error("fixed canonical client spawn failed")); });
      child.once("close", (exitCode, exitSignal) => {
        clearTimeout(timer); clearTimeout(forceStop);
        if (oversized || stdout.includes(credential) || stderr.includes(credential)) { reject(new Error("fixed client diagnostics unsafe or oversized")); return; }
        resolveOutcome({ processId: child.pid, exitCode, exitSignal, timedOut, stdout, stderr });
      });
      child.stdin.end(selector === "sdk" ? JSON.stringify({ packageRoot: installed.root, endpoint: url.href, method, params, authenticated, credential }) : "");
    });
    // Refuse a package changed while its child was running as well as before it.
    verifyInstalledClient(installationRoot, selector);
    const launch = { ...binding, processId: outcome.processId, exitCode: outcome.exitCode, exitSignal: outcome.exitSignal,
      timedOut: outcome.timedOut, stdoutSha256: hash(outcome.stdout), stderrSha256: hash(outcome.stderr) };
    const client = { kind: selector === "sdk" ? "sdk" : "inspector-cli", canonicalClient: installed.selected.canonicalClient,
      version: installed.selected.version, requestUrl: url.href, ...outcome, launch };
    if (selector === "sdk" && outcome.exitCode === 0 && !outcome.exitSignal && !outcome.timedOut) {
      const output = JSON.parse(outcome.stdout);
      if (output.processId !== outcome.processId) throw new Error("fixed SDK child process identity differs");
      client.sessionSha256 = output.sessionSha256; client.output = output.output;
    }
    return client;
  } finally { rmSync(directory, { recursive: true, force: true }); }
}
