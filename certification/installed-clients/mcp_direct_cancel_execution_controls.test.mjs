// Complete offline factory/wire/managed-key/Redis controls. No Honua image,
// published client, candidate job or native cancellation is executed here.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { fork, spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createDirectCancelValidation, directCancelDependencies, directCancelOperation } from "./mcp_direct_cancel_validation.mjs";
import { prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const source = process.env.HONUA_DIRECT_CANCEL_SOURCE_DIR ?? dirname(fileURLToPath(import.meta.url));
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const nonce = "0123456789abcdef01234567";

function prepareControlledInspector(directory, manifestPath) {
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const selected = manifest.clients.inspector;
  assert.equal(selected.entry, "clients/launcher/build/index.js");
  assert.equal(Object.keys(selected.files).length, 27);
  const root = join(directory, "node_modules", ...selected.name.split("/"));
  const cli = [
    "const args=process.argv.slice(2);",
    "const value=name=>args[args.indexOf(name)+1];",
    "const headers={'content-type':'application/json'};",
    "for(let i=0;i<args.length;i++)if(args[i]==='--header'){const entry=args[++i],at=entry.indexOf(':');headers[entry.slice(0,at).toLowerCase()]=entry.slice(at+1).trim();}",
    "const request=async(id,method,params,session)=>{const response=await fetch(value('--cli'),{method:'POST',headers:{...headers,...(session?{'mcp-session-id':session}:{})},body:JSON.stringify({jsonrpc:'2.0',id,method,params})});return {frame:await response.json(),session:response.headers.get('mcp-session-id')};};",
    "const init=await request(0,'initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'inspector-cli',version:'2.10.1'}});",
    "const method=value('--method');const params=method==='resources/read'?{uri:value('--uri')}:{name:value('--tool-name'),arguments:{}};",
    "for(let i=0;i<args.length;i++)if(args[i]==='--tool-arg')for(i++;i<args.length&&!args[i].startsWith('--');i++){const at=args[i].indexOf('=');params.arguments[args[i].slice(0,at)]=JSON.parse(args[i].slice(at+1));}",
    "const {frame}=await request(1,method,params,init.session);",
    "if(frame.error){const auth=frame.error.data?.code==='unauthenticated';console.error(JSON.stringify({error:{code:auth?'auth_required':'error',message:frame.error.message}}));process.exitCode=auth?3:1;}",
    "else{console.log(JSON.stringify({result:frame.result}));process.exitCode=frame.result?.isError===true?5:0;}",
  ].join("\n") + "\n";
  const files = {};
  for (const name of Object.keys(selected.files).sort()) {
    const content = name === "package.json"
      ? JSON.stringify({ name: selected.name, version: selected.version, type: "module" }) + "\n"
      : name === selected.entry ? cli : `controlled Inspector fixture ${name}\n`;
    const path = join(root, name);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, content);
    files[name] = hash(content);
  }
  selected.files = files;
  selected.integrity = "offline-control-not-published";
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
}

function setup(mode) {
  const directory = mkdtempSync(join(tmpdir(), "direct-cancel-producer-offline-"));
  try {
    for (const entry of readdirSync(source, { withFileTypes: true })) if (entry.isFile())
      copyFileSync(join(source, entry.name), join(directory, entry.name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
    if (mode === "valid-inspector") prepareControlledInspector(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
    const generated = spawnSync("python3", ["-B", join(directory, "mcp_direct_cancel_validation_controls.py"), "--emit-offline-vector"],
      { encoding: "utf8", timeout: 90_000, maxBuffer: 64 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const vector = JSON.parse(generated.stdout);
    const raw = (mode === "valid-inspector" ? vector.inspector : vector.sdk).observations;
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
      "with trace.open('a') as output: output.write(json.dumps({'action':'python','name':name})+'\\n')",
      "if name in ('mcp_direct_cancel_validation.py','mcp_direct_cancel_config.py') and '--nonce' in args:",
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
      "    return {'operationId':job,'status':status,'createdAt':call['result']['structuredContent']['createdAt'],'updatedAt':'2026-10-19T22:01:50Z' if status==5 else '2026-10-19T22:01:05Z','completedAt':'2026-10-19T22:01:50Z' if status==5 else None,'audit':{'requestedBy':actor,'tenantId':None,'idempotencyKey':plan['planId'],'submitterSecurityContext':{'tenantId':v['raw']['tenantId'],'principalId':actor,'ownerActorId':actor,'workspaceOwnerId':actor.removeprefix('ApiKey:api-key:')}},'spec':{'parameters':params},'artifactReferences':[],'committedEffectReferences':[]}",
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

for (const mode of ["valid", "valid-inspector", "wrong-actor", "early-success", "no-terminal", "cleanup-failure"]) {
  test("offline complete direct cancel producer: " + mode, async () => {
    const control = setup(mode), priorPath = process.env.PATH;
    let candidate;
    try {
      candidate = await start(control, mode);
      process.env.PATH = control.directory + delimiter + priorPath;
      const checks = {}, results = join(control.directory, "controlled-results.json");
      const canonicalClient = mode === "valid-inspector" ? "MCP Inspector" : "Official MCP TypeScript SDK";
      const clientVersion = mode === "valid-inspector" ? "2.10.1" : "1.32.1";
      await control.api.directCancelScenarios(async (operation, run) => {
        try { checks[operation] = { passed: true, proof: await run() }; }
        catch (error) { checks[operation] = { passed: false, error: String(error) }; }
      }, { candidateSourceSha: control.raw.candidateSourceSha, candidateImage: control.raw.candidateImage,
        canonicalClient, clientVersion,
        installationRoot: control.installationRoot, credential: "controlled-fixture-admin",
        ownedDisposableCandidate: true, results });
      const raw = JSON.parse(readFileSync(results + ".direct-cancel.raw.json"));
      const state = JSON.parse(readFileSync(control.statePath));
      const traces = readFileSync(control.tracePath, "utf8").trim().split("\n").map(JSON.parse);
      assert.equal(state.api.filter(item => item.action === "create").length, 2,
        JSON.stringify({ error: checks[directCancelOperation]?.error,
          launched: traces.filter(item => item.action === "python").map(item => item.name).slice(-8) }));
      assert.equal(state.api.filter(item => item.action === "revoke").length, 2);
      for (const profile of ["admin", "limited-read"]) {
        assert.equal(traces.filter(row => row.profile === profile && row.action === "create").length, 1);
        assert.equal(traces.filter(row => row.profile === profile && row.action === "revoke").length, 1);
      }
      if (mode === "valid" || mode === "valid-inspector") {
        assert.equal(checks[directCancelOperation].passed, true, checks[directCancelOperation].error);
        assert.equal(state.seen.length, 16);
        assert.equal(raw.calls.length, 16);
        assert.equal(Object.keys(raw.snapshots).length, 11);
        assert.equal(control.api.verifyDirectCancelProof(checks[directCancelOperation].proof, raw.candidateSourceSha,
          raw.candidateImage, directCancelOperation, canonicalClient, clientVersion), true);
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
      for (const privateDirectory of new Set(traces.filter(row => typeof row.private === "string")
        .map(row => dirname(row.private)))) {
        assert.ok(privateDirectory.startsWith(join(tmpdir(), "mcp-direct-cancel-private-")));
        rmSync(privateDirectory, { recursive: true, force: true });
      }
      control.close();
    }
  });
}
