// Offline producer control. It runs the real release factory, fixed SDK runner,
// candidate-wire observer, and pure judge against controlled HTTP/process inputs.
// It does not execute a published client, Honua image, Redis, or Postgres.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { fork, spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const source = resolve(process.env.HONUA_OPS_SOURCE_DIR ?? dirname(fileURLToPath(import.meta.url)));
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const fullKey = "controlled-full-key";
const limitedKey = "controlled-limited-read-key";
const fixedNonce = "abcdef123456";

function writeExecutable(path, lines) {
  writeFileSync(path, lines.join("\n") + "\n", { mode: 0o755 });
}

async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "ops-producer-offline-"));
  const { prepareControlledInstallation } =
    await import(pathToFileURL(join(source, "mcp_map_package_wire_controls.mjs")).href);
  const { createOpsEventsValidation, opsEventsDependencies } =
    await import(pathToFileURL(join(source, "mcp_ops_events_validation.mjs")).href);
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    if (entry.isFile()) copyFileSync(join(source, entry.name), join(directory, entry.name));
  }
  const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
  const generated = spawnSync("python3", ["-B", "-c",
    "import base64,json,yaml; from mcp_ops_events_journey_controls import journey,scenario_inputs,configuration_manifest,bootstrap; " +
    "v=journey(); s,d=scenario_inputs(); m=configuration_manifest(); " +
    "m['candidate']['ref']=s['runtimeBinding']['candidateSourceSha']; " +
    "m['components']['honua-server'].update(sha=s['runtimeBinding']['candidateSourceSha'],digest='sha256:'+'c'*64); " +
    "print(json.dumps({'observed':v,'setup':s,'declaration':d," +
    "'bootstrapBytesBase64':base64.b64encode(json.dumps(bootstrap()).encode()).decode()," +
    "'setupBytesBase64':base64.b64encode(json.dumps(s).encode()).decode()," +
    "'manifest':base64.b64encode(yaml.safe_dump(m).encode()).decode()}))"],
    { cwd: directory, encoding: "utf8", timeout: 30_000, maxBuffer: 16 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout);
  const bootstrapPath = join(directory, "bootstrap.json");
  const setupPath = join(directory, "setup.json");
  const platformManifestPath = join(directory, "manifest.yaml");
  writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBytesBase64, "base64"));
  writeFileSync(setupPath, Buffer.from(fixture.setupBytesBase64, "base64"));
  writeFileSync(platformManifestPath, Buffer.from(fixture.manifest, "base64"));
  const context = {
    directory, sourceSha: "a".repeat(40), bootstrapPath, setupPath, platformManifestPath,
    platformManifestSha256: digest(readFileSync(platformManifestPath)),
    manifestSourceSha: fixture.declaration.releaseSourceSha,
    sha256: Object.fromEntries(opsEventsDependencies.map(name => [name, digest(readFileSync(join(directory, name)))])),
  };
  return { directory, fixture, context, installationRoot, api: createOpsEventsValidation(context),
    close: () => rmSync(directory, { recursive: true, force: true }) };
}

function prepareProcesses(control, mode) {
  const { directory, fixture } = control;
  const raw = fixture.observed;
  const values = {
    raw, runtime: raw.runtimeBefore, declaration: fixture.declaration,
    contexts: raw.calls.flatMap(call => call.before ? [call.before, call.after] : []),
    index: 0, commands: [], paths: {},
  };
  const fixturePath = join(directory, "offline-process-state.json");
  writeFileSync(fixturePath, JSON.stringify(values));
  const realPython = spawnSync("python3", ["-c", "import sys;print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
  assert.ok(realPython.startsWith("/"));
  const python = [
    "#!" + realPython,
    "import copy,hashlib,json,os,sys",
    "from datetime import datetime",
    "from pathlib import Path",
    "path=Path(" + JSON.stringify(fixturePath) + ")",
    "real=" + JSON.stringify(realPython),
    "mode=" + JSON.stringify(mode),
    "args=sys.argv[1:]",
    "script=next((arg for arg in args if arg.endswith('.py')),None)",
    "name=Path(script).name if script else ''",
    "action=args[args.index(script)+1] if script and args.index(script)+1<len(args) else ''",
    "def get(flag): return args[args.index(flag)+1]",
    "v=json.loads(path.read_text())",
    "if name == 'mcp_ops_events_validation.py' and '--nonce' in args:",
    "    args[args.index('--nonce')+1]=" + JSON.stringify(fixedNonce),
    "    os.execv(real,[real,*args])",
    "if name == 'mcp_managed_identity_fixture.py':",
    "    role='limited' if get('--profile')=='limited-read' else 'full'",
    "    v['commands'].append('identity-'+action+'-'+role)",
    "    item=copy.deepcopy(v['raw']['identityFixtures'][role])",
    "    salt=get('--credential-salt')",
    "    key=" + JSON.stringify(fullKey) + " if role=='full' else " + JSON.stringify(limitedKey),
    "    fingerprint='scrypt:'+salt+':'+hashlib.scrypt(key.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()",
    "    item['credentialSalt']=salt",
    "    item['identity']['credentialFingerprint']=fingerprint",
    "    item['revocation']['credentialFingerprint']=fingerprint",
    "    if role=='full':",
    "        for context in v['contexts']:",
    "            context['preflight']['credentialFingerprint']=fingerprint",
    "    private=Path(get('--private-path')); receipt=Path(get('--receipt-path'))",
    "    v['paths'][role]={'private':str(private),'receipt':str(receipt),'directory':str(private.parent)}",
    "    if action=='create':",
    "        private.write_text(json.dumps({'key':key}))",
    "        private.chmod(0o600)",
    "        item['cleanupComplete']=False",
    "        for key in ('revocation','revokedKeyIds','revokedEffectivePermissions'): item.pop(key,None)",
    "    else:",
    "        if mode=='cleanup-failure' and role=='limited':",
    "            path.write_text(json.dumps(v)); sys.exit(1)",
    "        private.unlink()",
    "    receipt.write_text(json.dumps(item))",
    "    path.write_text(json.dumps(v)); print(json.dumps(item)); sys.exit(0)",
    "if name == 'mcp_ops_events_state.py':",
    "    output=Path(get('--output'))",
    "    if '--restart' in args:",
    "        v['runtime']=v['raw']['runtimeRestarted']",
    "        item={'runtimeBinding':v['runtime']}",
    "        v['commands'].append('restart')",
    "    else:",
    "        item=v['contexts'][v['index']]; v['index']+=1",
    "        v['commands'].append('context')",
    "    output.write_text(json.dumps(item))",
    "    path.write_text(json.dumps(v)); sys.exit(0)",
    "if name == 'mcp_ops_events_data_read.py':",
    "    ns={'__name__':'offline_data_read','__file__':script}",
    "    exec(compile(Path(script).read_text(),script,'exec'),ns)",
    "    class Clock:",
    "        values=iter((v['raw']['limitedRead']['startedAt'],v['raw']['limitedRead']['completedAt']))",
    "        @staticmethod",
    "        def now(tz=None): return datetime.fromisoformat(next(Clock.values).replace('Z','+00:00'))",
    "    ns['datetime']=Clock",
    "    ns['read'].__kwdefaults__['clock']=Clock.now",
    "    sys.argv=args[args.index(script):]",
    "    sys.exit(ns['main']())",
    "os.execv(real,[real,*args])",
  ];
  writeExecutable(join(directory, "python3"), python);
  const docker = [
    "#!" + realPython,
    "import json,sys",
    "from pathlib import Path",
    "v=json.loads(Path(" + JSON.stringify(fixturePath) + ").read_text())",
    "args=sys.argv[1:]",
    "if 'selectedScope' in ' '.join(args):",
    "    for k,val in v['declaration']['environment'].items():",
    "        print('{\"selectedScope\":true}'); print(json.dumps(k+'='+val))",
    "elif args[0]=='image': print(json.dumps(v['runtime']['imageReadback']))",
    "else: print(json.dumps({key:v['runtime'][key] for key in ('running','imageId','imageReference','startedAt','containerId')}))",
  ];
  writeExecutable(join(directory, "docker"), docker);
  return { fixturePath };
}

async function startCandidate(control, mode, fixturePath) {
  const seenPath = join(control.directory, "candidate-seen.ndjson");
  const child = fork(join(dirname(fileURLToPath(import.meta.url)), "mcp_ops_events_candidate_server.mjs"),
    [fixturePath, join(control.directory, "fixture.v1.json"), seenPath, mode],
    { execArgv: [], stdio: ["ignore", "pipe", "pipe", "ipc"] });
  let stderr = "";
  child.stderr.on("data", chunk => { stderr += chunk.toString().slice(0, 4096); });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("controlled candidate startup timed out")), 10_000);
      child.once("message", value => {
        clearTimeout(timer);
        if (value === "ready") resolve(); else reject(new Error("controlled candidate startup refused"));
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`controlled candidate exited ${code}: ${stderr}`)); });
    });
  } catch (error) { child.kill("SIGTERM"); throw error; }
  return {
    seen: () => readFileSync(seenPath, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse),
    close: async () => {
      child.send("stop");
      await new Promise(resolve => { child.once("exit", resolve); setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 5000).unref(); });
    },
  };
}

for (const mode of ["valid", "wrong-caller", "reused-session", "cleanup-failure", "zero-read", "missing-read"]) {
  test("offline full Ops producer orchestration: " + mode, async () => {
    const control = await setup(), priorPath = process.env.PATH;
    const { fixturePath } = prepareProcesses(control, mode);
    const candidate = await startCandidate(control, mode, fixturePath);
    process.env.PATH = control.directory + delimiter + priorPath;
    try {
      const checks = {}, results = join(control.directory, "offline-results.json");
      await control.api.opsEventsScenarios(async (operation, run) => {
        try { checks[operation] = { passed: true, value: await run() }; }
        catch (error) { checks[operation] = { passed: false, error: String(error) }; }
      }, { candidateSourceSha: control.fixture.observed.candidateSourceSha,
        candidateImage: control.fixture.observed.candidateImage, canonicalClient: "Official MCP TypeScript SDK",
        clientVersion: "1.32.1", installationRoot: control.installationRoot,
        credential: "controlled-fixture-admin", ownedDisposableCandidate: true, results });
      const observed = JSON.parse(readFileSync(results + ".ops-events.raw.json"));
      const state = JSON.parse(readFileSync(fixturePath));
      const { commands, paths } = state;
      const seen = candidate.seen();
      assert.equal(seen.filter(entry => entry.method === "GET").length, 1);
      const readRefused = mode === "zero-read" || mode === "missing-read";
      assert.equal(seen.filter(entry => entry.method === "tools/call" || entry.method === "resources/read").length,
        readRefused ? 0 : 26);
      assert.equal(commands.filter(value => value === "restart").length, readRefused ? 0 : 1);
      assert.equal(commands.filter(value => value === "identity-create-full").length, 1);
      assert.equal(commands.filter(value => value === "identity-create-limited").length, 1);
      assert.equal(commands.filter(value => value === "identity-revoke-full").length, 1);
      assert.equal(commands.filter(value => value === "identity-revoke-limited").length, 1);
      assert.equal(observed.calls.length, readRefused ? 0 : 26);
      assert.ok(paths.full?.private && paths.limited?.private);
      if (mode === "cleanup-failure") {
        assert.equal(paths.full.directory, paths.limited.directory);
        assert.equal(readFileSync(paths.limited.private, "utf8").includes(limitedKey), true);
        assert.throws(() => readFileSync(paths.full.private));
      } else {
        assert.throws(() => readFileSync(paths.full.private));
        assert.throws(() => readFileSync(paths.limited.private));
      }
      if (mode === "valid") {
        for (const operation of control.fixture.observed.operations) {
          assert.equal(checks[operation].passed, true, checks[operation].error);
          assert.equal(control.api.verifyOpsEventsProof(checks[operation].value,
            observed.candidateSourceSha, observed.candidateImage, operation, "Official MCP TypeScript SDK", "1.32.1"), true);
        }
      } else {
        assert.ok(control.fixture.observed.operations.every(operation => checks[operation].passed === false));
      }
    } finally {
      process.env.PATH = priorPath;
      await candidate.close();
      if (mode === "cleanup-failure") {
        const paths = JSON.parse(readFileSync(fixturePath, "utf8")).paths;
        const directory = paths.limited?.directory;
        assert.ok(directory?.startsWith(join(tmpdir(), "mcp-ops-private-")));
        assert.equal(directory, paths.full?.directory);
        rmSync(directory, { recursive: true, force: true });
      }
      control.close();
    }
  });
}
