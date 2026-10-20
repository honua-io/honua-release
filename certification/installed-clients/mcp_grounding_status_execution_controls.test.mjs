// Real producer/fixed-client/wire/judge orchestration against controlled inputs.
// All assertions are offline. They do not prove an installed published client
// or selected Honua candidate.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { fork, spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const source = resolve(process.env.HONUA_GROUNDING_SOURCE_DIR ?? dirname(fileURLToPath(import.meta.url)));
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

function executable(path, lines) { writeFileSync(path, lines.join("\n") + "\n", { mode: 0o755 }); }

async function setup(mode) {
  const directory = mkdtempSync(join(tmpdir(), "grounding-producer-offline-"));
  let candidate;
  try {
    const { prepareControlledInstallation } =
      await import(pathToFileURL(join(source, "mcp_map_package_wire_controls.mjs")).href);
    const { createGroundingStatusValidation, groundingStatusDependencies } =
      await import(pathToFileURL(join(source, "mcp_grounding_status_validation.mjs")).href);
    for (const entry of readdirSync(source, { withFileTypes: true }))
      if (entry.isFile()) copyFileSync(join(source, entry.name), join(directory, entry.name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
    const generated = spawnSync("python3", ["-B", "-c",
      "import base64,json; from mcp_grounding_status_journey_controls import vector; " +
      "from mcp_render_validation_controls import bootstrap; " +
      "b,m,d,v=vector(); print(json.dumps({'bootstrap':base64.b64encode(json.dumps(b).encode()).decode()," +
      "'manifest':base64.b64encode(m).decode(),'declaration':d,'observed':v}))"],
      { cwd: directory, encoding: "utf8", timeout: 30_000, maxBuffer: 16 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const fixture = JSON.parse(generated.stdout);
    const bootstrapPath = join(directory, "bootstrap.json");
    const platformManifestPath = join(directory, "manifest.yaml");
    writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrap, "base64"));
    writeFileSync(platformManifestPath, Buffer.from(fixture.manifest, "base64"));
    const context = { directory, sourceSha: "a".repeat(40), bootstrapPath, platformManifestPath,
      platformManifestSha256: digest(readFileSync(platformManifestPath)),
      manifestSourceSha: fixture.declaration.releaseSourceSha,
      sha256: Object.fromEntries(groundingStatusDependencies.map(name => [name,
        digest(readFileSync(join(directory, name)))])) };
    const statePath = join(directory, "offline-state.json"), seenPath = join(directory, "candidate-seen.ndjson");
    writeFileSync(statePath, JSON.stringify(fixture.observed));
    const realPython = spawnSync("python3", ["-c", "import sys;print(sys.executable)"],
      { encoding: "utf8" }).stdout.trim();
    assert.ok(realPython.startsWith("/"));
    executable(join(directory, "python3"), [
      "#!" + realPython,
      "import copy,hashlib,json,os,sys",
      "from pathlib import Path",
      "real=" + JSON.stringify(realPython),
      "mode=" + JSON.stringify(mode),
      "state=json.loads(Path(" + JSON.stringify(statePath) + ").read_text())",
      "args=sys.argv[1:]",
      "script=next((arg for arg in args if arg.endswith('.py')),None)",
      "name=Path(script).name if script else ''",
      "def get(flag): return args[args.index(flag)+1]",
      "if name=='mcp_managed_identity_fixture.py':",
      "    action=args[args.index(script)+1]",
      "    profile=get('--profile')",
      "    private=Path(get('--private-path')); receipt=Path(get('--receipt-path'))",
      "    salt=get('--credential-salt')",
      "    key='controlled-limited-read-key' if profile=='limited-read' else 'controlled-full-key'",
      "    fingerprint='scrypt:'+salt+':'+hashlib.scrypt(key.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()",
      "    field='limitedIdentityFixture' if profile=='limited-read' else 'identityFixture'",
      "    item=copy.deepcopy(state[field]); item['credentialSalt']=salt",
      "    item['identity']['credentialFingerprint']=fingerprint",
      "    item['revocation']['credentialFingerprint']=fingerprint",
      "    if action=='create':",
      "        private.write_text(json.dumps({'key':key}))",
      "        private.chmod(0o600)",
      "        Path(" + JSON.stringify(directory) + ", 'managed-path-'+profile+'.txt').write_text(str(private))",
      "        item['cleanupComplete']=False",
      "        for field in ('revocation','revokedKeyIds','revokedEffectivePermissions'): item.pop(field,None)",
      "    elif mode=='cleanup-failure' and profile=='limited-read': sys.exit(1)",
      "    else: private.unlink()",
      "    receipt.write_text(json.dumps(item))",
      "    if action=='revoke': print(json.dumps(item)); sys.exit(0)",
      "    if profile=='admin':",
      "        for name in ('preflightBefore','preflightAfter'): state[name]['credentialFingerprint']=fingerprint",
      "    Path(" + JSON.stringify(statePath) + ").write_text(json.dumps(state))",
      "    print(json.dumps(item)); sys.exit(0)",
      "if name=='mcp_render_validation.py' and '--inspect-runtime' in args:",
      "    print(json.dumps(state['runtimeBefore'])); sys.exit(0)",
      "if name=='mcp_grounding_status_state.py':",
      "    key='preflightBefore' if '--admin-preflight' in args else 'graphObservation'",
      "    print(json.dumps(state[key])); sys.exit(0)",
      "os.execv(real,[real,*args])",
    ]);
    candidate = fork(join(dirname(fileURLToPath(import.meta.url)), "mcp_grounding_status_controlled_candidate.mjs"),
      [statePath, seenPath, mode], { execArgv: [], stdio: ["ignore", "pipe", "pipe", "ipc"] });
    let stderr = "";
    candidate.stderr.on("data", chunk => { stderr += chunk.toString().slice(0, 4096); });
    await new Promise((accept, reject) => {
      const timer = setTimeout(() => reject(new Error("controlled candidate startup timed out")), 10_000);
      candidate.once("message", value => {
        clearTimeout(timer);
        if (value === "ready") accept(); else reject(new Error("controlled candidate startup refused"));
      });
      candidate.once("exit", code => { clearTimeout(timer); reject(new Error(`candidate exited ${code}: ${stderr}`)); });
    });
    return { directory, fixture, context, installationRoot, candidate, seenPath,
      api: createGroundingStatusValidation(context), close: async () => {
        candidate.send("stop");
        await new Promise(accept => { candidate.once("exit", accept);
          setTimeout(() => { candidate.kill("SIGKILL"); accept(); }, 5000).unref(); });
        rmSync(directory, { recursive: true, force: true });
      } };
  } catch (error) {
    if (candidate?.exitCode === null && !candidate.killed) candidate.kill("SIGTERM");
    rmSync(directory, { recursive: true, force: true });
    throw error;
  }
}

for (const mode of ["valid", "wrong-status", "reused-session", "cleanup-failure"]) {
  test(`offline full grounding/status producer ${mode}`, async () => {
    const control = await setup(mode), oldPath = process.env.PATH;
    process.env.PATH = control.directory + delimiter + oldPath;
    try {
      const checks = {}, results = join(control.directory, "results.json");
      await control.api.groundingStatusScenarios(async (operation, run) => {
        try { checks[operation] = { pass: true, value: await run() }; }
        catch (error) { checks[operation] = { pass: false, error: String(error) }; }
      }, { candidateSourceSha: control.fixture.observed.candidateSourceSha,
        candidateImage: control.fixture.observed.candidateImage,
        canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1",
        installationRoot: control.installationRoot, credential: "controlled-full-key",
        ownedDisposableCandidate: true, results });
      const seen = readFileSync(control.seenPath, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse);
      assert.equal(seen.length, 13);
      if (mode === "valid") {
        for (const operation of control.fixture.observed.operations) {
          assert.equal(checks[operation].pass, true, checks[operation].error);
          assert.equal(control.api.verifyGroundingStatusProof(checks[operation].value,
            control.fixture.observed.candidateSourceSha, control.fixture.observed.candidateImage,
            operation, "Official MCP TypeScript SDK", "1.32.1"), true);
        }
        const raw = JSON.parse(readFileSync(results + ".grounding-status.raw.json"));
        assert.equal(raw.calls[3].params.arguments.intentId,
          raw.calls[2].result.structuredContent.draftIntent.intentId);
        assert.ok(raw.calls.every(call => call.client.processId !== process.pid));
      } else {
        assert.ok(Object.values(checks).every(check => check.pass === false));
        if (mode === "cleanup-failure") {
          const admin = readFileSync(join(control.directory, "managed-path-admin.txt"), "utf8");
          const limited = readFileSync(join(control.directory, "managed-path-limited-read.txt"), "utf8");
          assert.equal(existsSync(admin), false);
          assert.equal(existsSync(limited), true);
          assert.ok(dirname(limited).startsWith(join(tmpdir(), "mcp-grounding-private-")));
        }
      }
    } finally {
      process.env.PATH = oldPath;
      const pathFile = join(control.directory, "managed-path-limited-read.txt");
      if (existsSync(pathFile)) {
        const retained = readFileSync(pathFile, "utf8");
        if (dirname(retained).startsWith(join(tmpdir(), "mcp-grounding-private-")))
          rmSync(dirname(retained), { recursive: true, force: true });
      }
      await control.close();
    }
  });
}
