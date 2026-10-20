// Complete EXPLICIT OFFLINE process/HTTP/normal-helper cohort orchestration.
// Alternate package files are control fixtures, never published client proof.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash, randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync, mkdirSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createDeploymentWorkspaceCohort, deploymentWorkspaceCohortDependencies } from "./mcp_deployment_workspace_cohort.mjs";
import { prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const support = ["mcp_deployment_workspace_full_controls.py", "mcp_deployment_workspace_factory_fixture.py",
  "mcp_deployment_workspace_validation_controls.py", "mcp_deployment_identity_role_controls.py", "mcp_managed_identity_fixture_controls.py",
  "mcp_map_package_validation_controls.py", "mcp_render_validation_controls.py", "mcp_render_bootstrap.py", "regression.py", "oracles.py"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");

function inspectorInstallation(directory, root, manifestPath) {
  const manifest = JSON.parse(readFileSync(manifestPath)), selected = manifest.clients.inspector;
  const packageRoot = join(root, ...selected.name.split("/"));
  const entry = `// Explicit offline CLI grammar and HTTP control, never the published package.
const args=process.argv.slice(2), at=key=>args[args.indexOf(key)+1], endpoint=at('--cli');
const headers={'content-type':'application/json'};const h=args.indexOf('--header');
if(h>=0)for(let i=h+1;i<args.length&&!args[i].startsWith('--');i++){const n=args[i].indexOf(':');if(n<1)throw Error('bad offline header');headers[args[i].slice(0,n)]=args[i].slice(n+1).trim();}
const method=at('--method');let params;
if(method==='resources/read')params={uri:at('--uri')};else{const values={},i=args.indexOf('--tool-arg');if(i>=0)for(let n=i+1;n<args.length&&!args[n].startsWith('--');n++){const k=args[n].indexOf('=');values[args[n].slice(0,k)]=JSON.parse(args[n].slice(k+1));}params={name:at('--tool-name'),arguments:values};}
const initial=await fetch(endpoint,{method:'POST',headers,body:JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-06-18',capabilities:{},clientInfo:${JSON.stringify(selected.clientInfo)}}})});await initial.json();
const response=await fetch(endpoint,{method:'POST',headers:{...headers,'mcp-session-id':initial.headers.get('mcp-session-id')},body:JSON.stringify({jsonrpc:'2.0',id:2,method,params})});const frame=await response.json();
if(frame.error){const auth=frame.error.data?.code==='unauthenticated';console.error(JSON.stringify({error:{code:auth?'auth_required':'error',message:frame.error.message}}));process.exitCode=auth?3:1;}else{console.log(JSON.stringify(frame.result));process.exitCode=frame.result.isError?5:0;}
`;
  const sources = { "package.json": JSON.stringify({ name: selected.name, version: selected.version, type: "module" }), [selected.entry]: entry };
  for (const [name, bytes] of Object.entries(sources)) { const path = join(packageRoot, name); mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, bytes); }
  selected.files = Object.fromEntries(Object.entries(sources).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([name, bytes]) => [name, hash(bytes)]));
  selected.integrity = "explicit-offline-not-published"; writeFileSync(manifestPath, JSON.stringify(manifest));
}

test("actual JS two-client factory completes unchanged full oracles through offline HTTP and normal fixture implementations", async () => {
  const directory = mkdtempSync(join(tmpdir(), "offline-full-deployment-factory-"));
  const originalDate = globalThis.Date, originalPath = process.env.PATH;
  const oldEnvironment = Object.fromEntries(["OFFLINE_ORIGINAL_PYTHON", "OFFLINE_COHORT_DIRECTORY"].map(key => [key, process.env[key]]));
  let server;
  try {
    for (const name of new Set([...deploymentWorkspaceCohortDependencies, ...support])) copyFileSync(join(original, name), join(directory, name));
    const python = spawnSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }); assert.equal(python.status, 0);
    const realPython = python.stdout.trim();
    const generated = spawnSync(realPython, ["-B", "-c", "import json; from mcp_render_validation_controls import bootstrap; print(json.dumps(bootstrap()))"], { cwd: directory, encoding: "utf8" });
    assert.equal(generated.status, 0, generated.stderr);
    const bootstrap = JSON.parse(generated.stdout), bootstrapPath = join(directory, "bootstrap.json"); writeFileSync(bootstrapPath, generated.stdout);
    const manifestPath = join(directory, "mcp_fixed_client_manifest.v1.json"), root = prepareControlledInstallation(directory, manifestPath);
    inspectorInstallation(directory, root, manifestPath);
    const manifest = JSON.parse(readFileSync(manifestPath));
    const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
      sha256: Object.fromEntries(deploymentWorkspaceCohortDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
    const clockPath = join(directory, "offline-clock.json"), vectorPath = join(directory, "offline-full-cohort.json");
    writeFileSync(clockPath, JSON.stringify("2026-10-20T00:00:00+00:00"));
    // Logical clock belongs ONLY to this explicit offline adapter. It makes
    // original provisioning, actual child calls, restart and cleanup comparable.
    globalThis.Date = class extends originalDate {
      constructor(...args) { super(...(args.length ? args : [JSON.parse(readFileSync(clockPath))])); }
      static now() { return originalDate.parse(JSON.parse(readFileSync(clockPath))); }
    };
    const shim = join(directory, "python3");
    writeFileSync(shim, `#!${realPython}\nimport runpy\nrunpy.run_path(${JSON.stringify(join(directory, "mcp_deployment_workspace_factory_fixture.py"))},run_name='__main__')\n`, { mode: 0o700 });
    process.env.PATH = `${directory}${delimiter}${originalPath}`;
    process.env.OFFLINE_ORIGINAL_PYTHON = realPython; process.env.OFFLINE_COHORT_DIRECTORY = directory;
    const observed = [], sessions = new Set(); let cursor = 0;
    server = createServer(async (request, response) => {
      try {
        const chunks = []; for await (const chunk of request) chunks.push(chunk);
        const message = JSON.parse(Buffer.concat(chunks));
        if (message.method === "initialize") {
          const session = randomUUID(); sessions.add(session);
          response.writeHead(200, { "content-type": "application/json", "mcp-session-id": session });
          response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "explicit-offline-full-cohort", version: "0.0.0" } } })); return;
        }
        assert.equal(sessions.has(request.headers["mcp-session-id"]), true);
        const vector = JSON.parse(readFileSync(vectorPath));
        const calls = [...vector.members.sdk.calls, ...vector.members.inspector.calls], expected = calls[cursor];
        assert.ok(expected, "unexpected extra actual client invocation");
        assert.equal(message.method, expected.method); assert.deepEqual(message.params, expected.params);
        const selector = cursor < vector.members.sdk.calls.length ? "sdk" : "inspector";
        const index = (selector === "sdk" ? 0 : 2) + (expected.credentialRole === "foreign" ? 2 : 1);
        const credential = `hnua_${Buffer.alloc(32, index).toString("base64url")}`;
        assert.equal(request.headers["x-api-key"], expected.authenticated ? credential : undefined);
        assert.equal(request.headers["x-honua-tenant"], expected.wire.expectedTenantId);
        const old = JSON.parse(readFileSync(clockPath));
        if (originalDate.parse(expected.completedAt) > originalDate.parse(old)) writeFileSync(clockPath, JSON.stringify(expected.completedAt));
        observed.push({ selector, id: expected.id, session: request.headers["mcp-session-id"] }); cursor++;
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, ...(expected.result.error ? expected.result : { result: expected.result }) }));
      } catch (error) { observed.push({ error: error.message }); response.writeHead(400).end(JSON.stringify({ error: "explicit offline fixture mismatch" })); }
    });
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(8080, "127.0.0.1", resolve); });
    const api = createDeploymentWorkspaceCohort(context), records = [];
    const record = selector => async (operation, execute) => {
      try { records.push({ selector, operation, proof: await execute() }); }
      catch (error) { records.push({ selector, operation, error: error.message }); }
    };
    const proofs = await api.deploymentWorkspaceCohortScenarios({ sdk: record("sdk"), inspector: record("inspector") }, {
      candidateSourceSha: bootstrap.candidateSourceSha, candidateImage: bootstrap.candidateImage,
      adminCredential: "explicit-offline-normal-fixture-admin", targetId: "owned-target", ownedDisposableCandidate: true,
      results: join(directory, "actual-offline-factory-results"),
      clients: Object.fromEntries(["sdk", "inspector"].map(selector => [selector, { clientVersion: manifest.clients[selector].version, installationRoot: root }])) });
    assert.deepEqual(observed.filter(value => value.error), []);
    const recovery = readdirSync(directory).filter(name => name.endsWith('.recovery.json'))
      .map(name => JSON.parse(readFileSync(join(directory,name))));
    const helperCommands = readFileSync(join(directory, "offline-helper-commands.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
    const diagnostic = JSON.stringify({ recovery, helpers: helperCommands.map(value => ({ helper:value.helper, phase:value.args[3] })), observedCount:observed.length });
    assert.equal(records.length, 8); assert.deepEqual(records.filter(value => value.error), [], diagnostic);
    for (const selector of ["sdk", "inspector"]) {
      assert.ok(proofs[selector]); assert.equal(records.filter(value => value.selector === selector).length, 4);
      for (const value of records.filter(value => value.selector === selector)) assert.equal(api.verifyDeploymentWorkspaceProof(value.proof,
        bootstrap.candidateSourceSha, bootstrap.candidateImage, value.operation, value.proof.canonicalClient, manifest.clients[selector].version), true);
    }
    assert.equal(new Set(observed.map(value => value.session)).size, observed.length);
    const commands = readFileSync(join(directory, "offline-helper-commands.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
    assert.equal(commands.filter(value => value.helper === "mcp_deployment_identity_fixture.py" && value.args[3] === "prepare").length, 1);
    assert.equal(commands.filter(value => value.helper === "mcp_deployment_identity_fixture.py" && value.args[3] === "close").length, 1);
    assert.equal(commands.filter(value => value.args.includes("--restart")).length, 2);
  } finally {
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
    globalThis.Date = originalDate; process.env.PATH = originalPath;
    for (const [key, value] of Object.entries(oldEnvironment)) if (value === undefined) delete process.env[key]; else process.env[key] = value;
    rmSync(directory, { recursive: true, force: true });
  }
});
