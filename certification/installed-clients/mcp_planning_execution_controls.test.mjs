// Actual factory/fixed runner/wire/oracle with controlled HTTP and Redis reads.
// The installed SDK is a controlled package fixture. No published client,
// Honua image, real Redis, model or candidate receives qualification credit.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { fork, spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createPlanningDisclosure, planningDependencies, planningOperation } from "./mcp_planning_disclosure.mjs";
import { prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const source = dirname(fileURLToPath(import.meta.url));
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const nonce = "0123456789abcdef01234567";

function setup(mode) {
  const directory = mkdtempSync(join(tmpdir(), "planning-producer-offline-"));
  try {
    for (const entry of readdirSync(source, { withFileTypes: true })) if (entry.isFile())
      copyFileSync(join(source, entry.name), join(directory, entry.name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
    const generated = spawnSync("python3", ["-B", join(directory, "mcp_planning_disclosure_controls.py"), "--emit-offline-vector"],
      { encoding: "utf8", timeout: 60_000, maxBuffer: 8 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const vector = JSON.parse(generated.stdout), raw = vector.sdk.observations;
    const bootstrapPath = join(directory, "original-bootstrap.json");
    writeFileSync(bootstrapPath, Buffer.from(vector.bootstrapBase64, "base64"));
    const context = { directory, sourceSha: "1".repeat(40), bootstrapPath,
      sha256: Object.fromEntries(planningDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
    const statePath = join(directory, "controlled-state.json"), tracePath = join(directory, "controlled-processes.ndjson");
    writeFileSync(statePath, JSON.stringify({ raw, runtime: raw.runtimeBefore, keys: {}, api: [], seen: [] }));
    writeFileSync(tracePath, "");
    const realPython = spawnSync("python3", ["-c", "import sys;print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
    assert.ok(realPython.startsWith("/"));
    const proxy = [
      "#!" + realPython,
      "import base64,json,os,runpy,sys",
      "from pathlib import Path",
      "state=Path(" + JSON.stringify(statePath) + ")",
      "trace=Path(" + JSON.stringify(tracePath) + ")",
      "real=" + JSON.stringify(realPython),
      "mode=" + JSON.stringify(mode),
      "args=sys.argv[1:]",
      "script=next((arg for arg in args if arg.endswith('.py')),None)",
      "name=Path(script).name if script else ''",
      "if name=='mcp_planning_disclosure.py' and '--nonce' in args:",
      "    args[args.index('--nonce')+1]=" + JSON.stringify(nonce),
      "if name not in ('mcp_managed_identity_fixture.py','mcp_planning_state.py'): os.execv(real,[real,*args])",
      "sys.path.insert(0,str(Path(script).parent))",
      "import mcp_execute_plan_state as store",
      "def execute(arguments):",
      "    v=json.loads(state.read_text())",
      "    if arguments[0]=='GET':",
      "        identifier=arguments[1].rsplit(':',1)[-1]",
      "        item=v['keys'].get(identifier)",
      "        return item['rawRecord'].encode() if item else None",
      "    if arguments[0]=='EVAL_RO' and arguments[2]=='2':",
      "        identifier=arguments[-1]; item=v['keys'].get(identifier)",
      "        live=item and item['record']['RevokedAt'] is None",
      "        expected=base64.b64decode(item['record']['KeyHash']).hex() if item else ''",
      "        live=live and arguments[3].endswith(':'+expected)",
      "        return [identifier.encode() if live else b'', b'1' if live else b'0']",
      "    if arguments[0]=='EVAL_RO' and arguments[2]=='0':",
      "        if mode=='dirty-job-store' and len(v['seen'])==7:",
      "            record={'operationId':'controlled-extra-job','status':0,'createdAt':'2026-10-20T00:00:00Z','spec':{'parameters':{}},'audit':{}}",
      "            return [[b'jobs',b'controlled-extra-job',json.dumps(record).encode()]]",
      "        return []",
      "    raise ValueError('unreviewed controlled Redis command')",
      "store.redis_command=execute",
      "store.snapshot.__kwdefaults__['execute']=execute",
      "if name=='mcp_managed_identity_fixture.py':",
      "    action=args[args.index(script)+1]",
      "    profile=args[args.index('--profile')+1]",
      "    private=args[args.index('--private-path')+1]",
      "    with trace.open('a') as output: output.write(json.dumps({'action':action,'profile':profile,'private':private})+'\\n')",
      "sys.argv=args[args.index(script):]",
      "runpy.run_path(script,run_name='__main__')",
    ];
    writeFileSync(join(directory, "python3"), proxy.join("\n") + "\n", { mode: 0o755 });
    const docker = [
      "#!" + realPython,
      "import json,sys",
      "from pathlib import Path",
      "v=json.loads(Path(" + JSON.stringify(statePath) + ").read_text())['runtime']",
      "if sys.argv[1]=='image': print(json.dumps(v['imageReadback']))",
      "else: print(json.dumps({key:v[key] for key in ('running','imageId','imageReference','startedAt','containerId')}))",
    ];
    writeFileSync(join(directory, "docker"), docker.join("\n") + "\n", { mode: 0o755 });
    return { directory, raw, context, installationRoot, statePath, tracePath,
      api: createPlanningDisclosure(context), close: () => rmSync(directory, { recursive: true, force: true }) };
  } catch (error) { rmSync(directory, { recursive: true, force: true }); throw error; }
}

async function waitForExit(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
    child.once("exit", () => { clearTimeout(timer); resolve(); });
  });
}

async function start(control, mode) {
  const child = fork(join(source, "mcp_planning_candidate_controls.mjs"), [control.statePath, mode],
    { execArgv: [], stdio: ["ignore", "pipe", "pipe", "ipc"] });
  let stderr = "";
  child.stderr.on("data", data => { stderr += data.toString().slice(0, 4096); });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("controlled planning startup timed out")), 10_000);
      child.once("message", value => {
        clearTimeout(timer);
        value === "ready" ? resolve() : reject(new Error("controlled planning startup refused"));
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`controlled planning exited ${code}: ${stderr}`)); });
      child.once("error", () => { clearTimeout(timer); reject(new Error("controlled planning process launch refused")); });
    });
  } catch (error) { child.kill("SIGTERM"); await waitForExit(child); throw error; }
  return { async close() { if (child.connected) child.send("stop"); await waitForExit(child); } };
}

for (const mode of ["valid", "wrong-model", "extra-step", "reused-session", "dirty-job-store", "cleanup-failure"]) {
  test("offline complete planning producer: " + mode, async () => {
    const control = setup(mode), priorPath = process.env.PATH;
    let candidate;
    try {
      candidate = await start(control, mode);
      process.env.PATH = control.directory + delimiter + priorPath;
      const checks = {}, results = join(control.directory, "controlled-results.json");
      await control.api.planningScenarios(async (operation, run) => {
        try { checks[operation] = { passed: true, proof: await run() }; }
        catch (error) { checks[operation] = { passed: false, error: String(error) }; }
      }, { candidateSourceSha: control.raw.candidateSourceSha, candidateImage: control.raw.candidateImage,
        canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1",
        installationRoot: control.installationRoot, credential: "controlled-fixture-admin",
        ownedDisposableCandidate: true, results });
      const raw = JSON.parse(readFileSync(results + ".planning.raw.json"));
      const state = JSON.parse(readFileSync(control.statePath));
      assert.equal(state.seen.length, 7);
      assert.equal(raw.calls.length, 7);
      assert.equal(raw.snapshots.length, 2);
      assert.equal(state.api.filter(item => item.action === "create").length, 2);
      assert.equal(state.api.filter(item => item.action === "revoke").length, 2);
      const traces = readFileSync(control.tracePath, "utf8").trim().split("\n").map(JSON.parse);
      for (const profile of ["admin", "limited-read"]) {
        assert.equal(traces.filter(row => row.profile === profile && row.action === "create").length, 1);
        assert.equal(traces.filter(row => row.profile === profile && row.action === "revoke").length, 1);
      }
      if (mode === "valid") {
        assert.equal(checks[planningOperation].passed, true, checks[planningOperation].error);
        assert.equal(control.api.verifyPlanningProof(checks[planningOperation].proof, raw.candidateSourceSha,
          raw.candidateImage, planningOperation, "Official MCP TypeScript SDK", "1.32.1"), true);
        assert.equal(checks[planningOperation].proof.liveModelProven, false);
        assert.equal(checks[planningOperation].proof.executionProven, false);
      } else assert.equal(checks[planningOperation].passed, false);
      for (const row of traces.filter(item => item.action === "revoke")) {
        if (mode === "cleanup-failure" && row.profile === "limited-read") assert.equal(existsSync(row.private), true);
        else assert.equal(existsSync(row.private), false);
      }
    } finally {
      process.env.PATH = priorPath;
      if (candidate) await candidate.close();
      // Remove only private journals this exact test's real helper created.
      const traces = readFileSync(control.tracePath, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse);
      for (const privateDirectory of new Set(traces.map(row => dirname(row.private)))) {
        assert.ok(privateDirectory.startsWith(join(tmpdir(), "mcp-planning-private-")));
        rmSync(privateDirectory, { recursive: true, force: true });
      }
      control.close();
    }
  });
}
