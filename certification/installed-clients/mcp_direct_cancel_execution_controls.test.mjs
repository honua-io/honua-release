// Complete offline factory/wire/managed-key/Redis controls. No Honua image,
// published client, candidate job or native cancellation is executed here.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { fork, spawnSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDirectCancelValidation, directCancelDependencies, directCancelOperation } from "./mcp_direct_cancel_validation.mjs";
import { prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const source = process.env.HONUA_DIRECT_CANCEL_SOURCE_DIR ?? dirname(fileURLToPath(import.meta.url));
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const nonce = "0123456789abcdef01234567";

function setup(mode) {
  const directory = mkdtempSync(join(tmpdir(), "direct-cancel-producer-offline-"));
  try {
    for (const entry of readdirSync(source, { withFileTypes: true })) if (entry.isFile())
      copyFileSync(join(source, entry.name), join(directory, entry.name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
    const generated = spawnSync("python3", ["-B", join(directory, "mcp_direct_cancel_validation_controls.py"), "--emit-offline-vector"],
      { encoding: "utf8", timeout: 90_000, maxBuffer: 64 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const vector = JSON.parse(generated.stdout), raw = vector.sdk.observations;
    if (mode === "early-success") {
      const body = JSON.parse(raw.calls[6].result.contents[0].text);
      body.status = "Succeeded";
      raw.calls[6].result.contents[0].text = JSON.stringify(body);
    }
    if (mode === "no-terminal") {
      const body = JSON.parse(raw.calls[12].result.contents[0].text);
      body.status = "Running";
      raw.calls[12].result.contents[0].text = JSON.stringify(body);
    }
    const bootstrapPath = join(directory, "original-bootstrap.json");
    writeFileSync(bootstrapPath, Buffer.from(vector.bootstrapBase64, "base64"));
    const context = { directory, sourceSha: "1".repeat(40), bootstrapPath,
      sha256: Object.fromEntries(directCancelDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
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
      "if name=='mcp_direct_cancel_validation.py' and '--nonce' in args:",
      "    args[args.index('--nonce')+1]=" + JSON.stringify(nonce),
      "if name not in ('mcp_managed_identity_fixture.py','mcp_direct_cancel_state.py'): os.execv(real,[real,*args])",
      "sys.path.insert(0,str(Path(script).parent))",
      "import mcp_execute_plan_state as store",
      "def job_record(v):",
      "    count=len(v['seen'])",
      "    if count<6: return None",
      "    call=v['raw']['calls'][5]",
      "    job=call['result']['structuredContent']['jobId']",
      "    plan=call['params']['arguments']['plan']",
      "    full=next((k for k in v['keys'].values() if k['record']['Permissions']==['admin']),None)",
      "    if not full: raise ValueError('controlled managed full key missing')",
      "    status=0 if count==6 else 2 if count<13 else 5",
      "    if mode=='wrong-actor': actor='ApiKey:api-key:33333333-3333-4333-8333-333333333333'",
      "    else: actor='ApiKey:api-key:'+full['record']['Id']",
      "    params={'honua.geoprocessing.plan_id':plan['planId'],'honua.geoprocessing.process_definitions':'analytics.buffer-aggregate-managed','honua.geoprocessing.output_artifact_kinds':'FeatureLayer'}",
      "    params.update({'honua.geoprocessing.step.0.'+k:x for k,x in plan['steps'][0]['inputs'].items()})",
      "    return {'operationId':job,'status':status,'createdAt':call['result']['structuredContent']['createdAt'],'updatedAt':'2026-10-19T22:01:50Z' if status==5 else '2026-10-19T22:01:05Z','completedAt':'2026-10-19T22:01:50Z' if status==5 else None,'audit':{'requestedBy':actor,'tenantId':v['raw']['tenantId'],'idempotencyKey':plan['planId']},'spec':{'parameters':params},'artifactReferences':[],'committedEffectReferences':[]}",
      "def execute(arguments):",
      "    v=json.loads(state.read_text())",
      "    if arguments[0]=='GET' and arguments[1].startswith('controlplane:job:'):",
      "        item=job_record(v)",
      "        return json.dumps(item).encode() if item and arguments[1]=='controlplane:job:'+item['operationId'] else None",
      "    if arguments[0]=='GET':",
      "        identifier=arguments[1].rsplit(':',1)[-1]",
      "        item=v['keys'].get(identifier)",
      "        return item['rawRecord'].encode() if item else None",
      "    if arguments[0]=='EVAL_RO' and arguments[2]=='2':",
      "        identifier=arguments[-1]; item=v['keys'].get(identifier)",
      "        live=item and item['record']['RevokedAt'] is None",
      "        expected=base64.b64decode(item['record']['KeyHash']).hex() if item else ''",
      "        live=live and arguments[3].endswith(':'+expected)",
      "        return [identifier.encode() if live else b'',b'1' if live else b'0']",
      "    if arguments[0]=='EVAL_RO' and arguments[2]=='0':",
      "        item=job_record(v)",
      "        if not item: return []",
      "        job=item['operationId']",
      "        rows=[[b'jobs',job.encode(),json.dumps(item).encode()]]",
      "        if len(v['seen'])>=14:",
      "            result={'resultPackageId':job+':v3','status':7,'artifacts':[],'errors':[]}",
      "            rows.append([b'results',job.encode(),json.dumps(result).encode()])",
      "        return rows",
      "    raise ValueError('unreviewed controlled Redis command')",
      "store.redis_command=execute",
      "store.snapshot.__kwdefaults__['execute']=execute",
      "if name=='mcp_direct_cancel_state.py':",
      "    import mcp_direct_cancel_state as cancel",
      "    cancel.observe.__kwdefaults__['read']=execute",
      "    cancel.observe.__kwdefaults__['census']=store.snapshot",
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
      "state=json.loads(Path(" + JSON.stringify(statePath) + ").read_text())",
      "v=state['runtime']",
      "if sys.argv[1]=='image': print(json.dumps(v['imageReadback']))",
      "elif any('selectedScope' in arg for arg in sys.argv):",
      "    for key,value in state['raw']['configuration']['declaration']['environment'].items():",
      "        print(json.dumps({'selectedScope':True}))",
      "        print(json.dumps(key+'='+value))",
      "else: print(json.dumps({key:v[key] for key in ('running','imageId','imageReference','startedAt','containerId')}))",
    ];
    writeFileSync(join(directory, "docker"), docker.join("\n") + "\n", { mode: 0o755 });
    return { directory, raw, context, installationRoot, statePath, tracePath,
      api: createDirectCancelValidation(context), close: () => rmSync(directory, { recursive: true, force: true }) };
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
      const timer = setTimeout(() => reject(new Error("controlled direct cancel startup timed out")), 10_000);
      child.once("message", value => {
        clearTimeout(timer);
        value === "ready" ? resolve() : reject(new Error("controlled direct cancel startup refused"));
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`controlled direct cancel exited ${code}: ${stderr}`)); });
      child.once("error", () => { clearTimeout(timer); reject(new Error("controlled direct cancel process launch refused")); });
    });
  } catch (error) { child.kill("SIGTERM"); await waitForExit(child); throw error; }
  return { async close() { if (child.connected) child.send("stop"); await waitForExit(child); } };
}

for (const mode of ["valid", "wrong-actor", "early-success", "no-terminal", "cleanup-failure"]) {
  test("offline complete direct cancel producer: " + mode, async () => {
    const control = setup(mode), priorPath = process.env.PATH;
    let candidate;
    try {
      candidate = await start(control, mode);
      process.env.PATH = control.directory + delimiter + priorPath;
      const checks = {}, results = join(control.directory, "controlled-results.json");
      await control.api.directCancelScenarios(async (operation, run) => {
        try { checks[operation] = { passed: true, proof: await run() }; }
        catch (error) { checks[operation] = { passed: false, error: String(error) }; }
      }, { candidateSourceSha: control.raw.candidateSourceSha, candidateImage: control.raw.candidateImage,
        canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1",
        installationRoot: control.installationRoot, credential: "controlled-fixture-admin",
        ownedDisposableCandidate: true, results });
      const raw = JSON.parse(readFileSync(results + ".direct-cancel.raw.json"));
      const state = JSON.parse(readFileSync(control.statePath));
      assert.equal(state.api.filter(item => item.action === "create").length, 2);
      assert.equal(state.api.filter(item => item.action === "revoke").length, 2);
      const traces = readFileSync(control.tracePath, "utf8").trim().split("\n").map(JSON.parse);
      for (const profile of ["admin", "limited-read"]) {
        assert.equal(traces.filter(row => row.profile === profile && row.action === "create").length, 1);
        assert.equal(traces.filter(row => row.profile === profile && row.action === "revoke").length, 1);
      }
      if (mode === "valid") {
        assert.equal(state.seen.length, 16);
        assert.equal(raw.calls.length, 16);
        assert.equal(Object.keys(raw.snapshots).length, 11);
        assert.equal(checks[directCancelOperation].passed, true, checks[directCancelOperation].error);
        assert.equal(control.api.verifyDirectCancelProof(checks[directCancelOperation].proof, raw.candidateSourceSha,
          raw.candidateImage, directCancelOperation, "Official MCP TypeScript SDK", "1.32.1"), true);
        assert.equal(checks[directCancelOperation].proof.nativeChildCessationProven, false);
      } else assert.equal(checks[directCancelOperation].passed, false);
      for (const row of traces.filter(item => item.action === "revoke")) {
        if (mode === "cleanup-failure" && row.profile === "limited-read") assert.equal(existsSync(row.private), true);
        else assert.equal(existsSync(row.private), false);
      }
    } finally {
      process.env.PATH = priorPath;
      if (candidate) await candidate.close();
      const traces = readFileSync(control.tracePath, "utf8").trim().split("\n").filter(Boolean).map(JSON.parse);
      for (const privateDirectory of new Set(traces.map(row => dirname(row.private)))) {
        assert.ok(privateDirectory.startsWith(join(tmpdir(), "mcp-direct-cancel-private-")));
        rmSync(privateDirectory, { recursive: true, force: true });
      }
      control.close();
    }
  });
}
