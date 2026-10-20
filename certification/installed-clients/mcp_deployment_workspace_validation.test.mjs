import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createDeploymentWorkspaceValidation, deploymentWorkspaceDependencies } from "./mcp_deployment_workspace_validation.mjs";
import { startControlledCandidate, prepareControlledInstallation, controlCredential } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const support = ["mcp_deployment_workspace_validation_controls.py", "mcp_map_package_validation_controls.py", "mcp_render_validation_controls.py",
  "mcp_render_bootstrap.py", "regression.py", "oracles.py"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup(live = false, selector = "sdk") {
  const directory = mkdtempSync(join(tmpdir(), "release-lifecycle-controls-"));
  for (const name of [...deploymentWorkspaceDependencies, ...support]) copyFileSync(join(original, name), join(directory, name));
  const installationRoot = live ? prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json")) : null;
  const generated = spawnSync("python3", ["-B", "-c",
    `import json,base64; from mcp_deployment_workspace_validation_controls import controls,bootstrap; print(json.dumps({'observations':controls(${JSON.stringify(selector)}),'bootstrapBytesBase64':base64.b64encode(json.dumps(bootstrap()).encode()).decode()}))`],
    { cwd: directory, encoding: "utf8", timeout: 60_000, maxBuffer: 16 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout), bootstrapPath = join(directory, "bootstrap.json");
  writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBytesBase64, "base64"));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(deploymentWorkspaceDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observations = join(directory, "observations.json"); writeFileSync(observations, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_deployment_workspace_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observations], { encoding: "utf8", timeout: 20_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-deployment-workspace-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_deployment_workspace_validation.py"]}`, observations: fixture.observations };
  const api = createDeploymentWorkspaceValidation(context);
  const verify = (value = proof) => api.verifyDeploymentWorkspaceProof(value, proof.candidateSourceSha, proof.candidateImage,
    "tools/call:deploy_operations", proof.canonicalClient, proof.clientVersion);
  return { directory, context, proof, api, verify, fixture, installationRoot, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("complete explicit offline SDK/Inspector proof reaches all four primary semantics with no actuation credit", () => {
  for (const selector of ["sdk", "inspector"]) {
    const control = setup(false, selector);
    try {
      for (const operation of control.proof.operations) assert.equal(control.api.verifyDeploymentWorkspaceProof(control.proof, control.proof.candidateSourceSha,
        control.proof.candidateImage, operation, control.proof.canonicalClient, control.proof.clientVersion), true);
      for (const field of ["observations", "persistedAfterRestart", "independentStores", "approvalRequiredForwardProposal", "anonymousInvocationRefusal"]) {
        const changed = structuredClone(control.proof); delete changed[field]; assert.equal(control.verify(changed), false);
      }
      for (const field of ["deploymentActuationProven", "runtimeHealthProven"]) {
        const changed = structuredClone(control.proof); changed[field] = true; assert.equal(control.verify(changed), false);
      }
      assert.equal(control.api.verifyDeploymentWorkspaceProof(control.proof, "f".repeat(40), control.proof.candidateImage,
        "tools/call:deploy_operations", control.proof.canonicalClient, control.proof.clientVersion), false);
    } finally { control.close(); }
  }
});

for (const name of deploymentWorkspaceDependencies) test(`offline complete lifecycle refuses altered protected dependency ${name}`, () => {
  const control = setup();
  try {
    assert.equal(control.verify(), true);
    writeFileSync(join(control.directory, name), "unreviewed changed bytes");
    assert.equal(control.verify(), false);
    assert.throws(() => createDeploymentWorkspaceValidation(control.context), /dependency changed/);
  } finally { control.close(); }
});

test("disposable ownership and configured target are required before any setup/client command", async () => {
  const control = setup();
  try {
    const checks = [];
    await control.api.deploymentWorkspaceScenarios(async (operation, run) => {
      try { checks.push([operation, await run()]); } catch (error) { checks.push([operation, error.message]); }
    }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion,
      credential: controlCredential, foreignCredential: "controlled-foreign-key", installationRoot: "/absent", ownedDisposableCandidate: false, targetId: "owned-target" });
    assert.equal(checks.length, 4);
    assert.match(checks[0][1], /explicitly owned candidate/);
    for (const [, result] of checks.slice(1)) assert.match(result, /complete primary lifecycle proof absent/);
  } finally { control.close(); }
});

// Explicit OFFLINE process/HTTP/store fixture. Alternate package and Docker/SQL
// programs below are never canonical runtime evidence and earn no client credit.
for (const mode of ["valid", "wrong-publication-pointer", "lost-workspace-after-restart", "unexpected-actuation", "reused-session"]) {
  test(`offline HTTP/fixed process/complete persisted lifecycle producer: ${mode}`, async () => {
    const control = setup(true), priorPath = process.env.PATH;
    const raw = control.fixture.observations;
    const runtimePath = join(control.directory, "offline-runtime.json"), seenPath = join(control.directory, "offline-seen.json");
    const setupPath = join(control.directory, "offline-gp-setup.json"), commandsPath = join(control.directory, "offline-state-commands.json");
    writeFileSync(runtimePath, JSON.stringify(raw.runtimeBefore)); writeFileSync(seenPath, "[]"); writeFileSync(commandsPath, "[]");
    const realPython = spawnSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
    assert.ok(realPython.startsWith("/"));
    writeFileSync(join(control.directory, "python3"), `#!${realPython}
import json,os,sys
from pathlib import Path
from datetime import datetime,timezone
if any(arg.endswith('/mcp_deployment_workspace_state.py') or arg.endswith('/mcp_deployment_workspace_prerequisites.py') for arg in sys.argv[1:]):
    sys.path.insert(0,${JSON.stringify(control.directory)})
    from mcp_deployment_workspace_validation_controls import prerequisite,stores,proposal_state
    commands=Path(${JSON.stringify(commandsPath)}); seen=json.loads(commands.read_text()); seen.append(sys.argv[1:]); commands.write_text(json.dumps(seen))
    requests=json.loads(Path(${JSON.stringify(seenPath)}).read_text())
    label=next(x['params']['arguments']['packageKey'] for x in requests if x['params'].get('name')=='honua_studio_create_draft')
    authored={'packageKey':label,'workspaceLabel':label,'route':'/maps/'+label}
    runtime=Path(${JSON.stringify(runtimePath)})
    if '--restart' in sys.argv:
        value=json.loads(runtime.read_text()); value['startedAt']=datetime.now(timezone.utc).isoformat(); runtime.write_text(json.dumps(value)); print(json.dumps(value))
    elif any(arg.endswith('/mcp_deployment_workspace_prerequisites.py') for arg in sys.argv):
        value=prerequisite(authored); Path(${JSON.stringify(setupPath)}).write_text(json.dumps(value)); print(json.dumps(value))
    elif '--proposal-id' in sys.argv:
        value=proposal_state(label+'-rollback')
        if ${JSON.stringify(mode)}=='unexpected-actuation': value['record']['executionOperationId']='forbidden-actuation'
        print(json.dumps(value))
    else:
        pub=next(x['params']['arguments'] for x in requests if x['params'].get('name')=='honua_studio_propose_publication')
        body=next(x['params']['arguments']['body'] for x in requests if x['params'].get('name')=='honua_studio_update_draft')
        expected={'deploymentId':'dep-'+'6'*32,'studioItemId':pub['itemId'],'studioVersionId':pub['versionId'],'studioContentHash':pub['contentHash'],'route':pub['route'],'mapBody':body}
        value=stores(authored,expected,json.loads(Path(${JSON.stringify(setupPath)}).read_text()),40)
        value['startedAt']=datetime.now(timezone.utc).isoformat(); value['completedAt']=datetime.now(timezone.utc).isoformat()
        if ${JSON.stringify(mode)}=='wrong-publication-pointer': value['stores']['studioItem']['publishedVersionId']='55555555-5555-4555-8555-555555555555'
        if ${JSON.stringify(mode)}=='lost-workspace-after-restart' and any('--restart' in item for item in seen): value['stores']['workspace']=None
        print(json.dumps(value))
else:
    os.execv(${JSON.stringify(realPython)},[${JSON.stringify(realPython)},*sys.argv[1:]])
`, { mode: 0o755 });
    writeFileSync(join(control.directory, "docker"), `#!${realPython}
import json,sys
from pathlib import Path
value=json.loads(Path(${JSON.stringify(runtimePath)}).read_text())
print(json.dumps(value['imageReadback'] if sys.argv[1]=='image' else {key:value[key] for key in ['running','imageId','imageReference','startedAt','containerId']}))
`, { mode: 0o755 });
    const responses = raw.calls.filter(call => call.method === "tools/call").map(call => structuredClone(call.result));
    const reads = [0, 1, 2].map(() => structuredClone(raw.calls.find(call => call.id === "deployment").result));
    let candidate;
    candidate = await startControlledCandidate(responses, reads, {
      sessionPrefix: "offline-persisted-lifecycle-session", reuseSessions: mode === "reused-session",
      onRead: () => {
        const request = candidate.seen.at(-1), family = request.params.uri.startsWith("honua://workspaces/") ? "workspace" : "deployment";
        const actualId = raw.calls[candidate.seen.length - 1]?.id;
        for (const [index, id] of [[0, actualId?.startsWith("foreign-") ? `foreign-${family}` : family], [1, `unknown-${family}`], [2, `anonymous-${family}`]]) reads[index] = structuredClone(raw.calls.find(call => call.id === id).result);
      },
      responseMutation: frame => {
        writeFileSync(seenPath, JSON.stringify(candidate.seen));
        const request = candidate.seen.at(-1), args = request.params.arguments;
        if (frame.result?.structuredContent) {
          const value = frame.result.structuredContent;
          if (value.status === "Published") value.shareUrl = "/api/v1/studio/published" + args.route;
          if (value.outcome === "ProposalCreated") {
            const material = `17:controlled-tenant:Deploy:control-plane.deploy:${args.idempotencyKey}`;
            value.proposalId = "proposal-" + hash(Buffer.from(material)).slice(0, 32);
            value.resourceUri = "honua://proposals/" + value.proposalId;
          }
          if (value.items) {
            value.evidencePosture.status = "unavailable";
            value.evidencePosture.sources[0].completeness = "unavailable";
            value.evidencePosture.sources[0].reasonCodes = ["stale"];
          }
          frame.result.content[0].text = value.items && JSON.stringify(value).length >= 1024 ? "Deploy operations returned in structuredContent." : JSON.stringify(value);
        } else if (frame.result?.contents?.[0]?.text) {
          const item = frame.result.contents[0], value = JSON.parse(item.text);
          const label = candidate.seen.find(call => call.params.name === "honua_studio_create_draft").params.arguments.packageKey;
          if (value.deploymentId) value.routePrefix = "/api/v1/studio/published/maps/" + label;
          if (value.workspaceId) value.label = label;
          item.text = JSON.stringify(value);
        }
        return frame;
      },
    });
    process.env.PATH = control.directory + delimiter + priorPath;
    try {
      const checks = {}, results = join(control.directory, "offline-results.json");
      await control.api.deploymentWorkspaceScenarios(async (operation, run) => {
        try { checks[operation] = { result: "pass", value: await run() }; }
        catch (error) { checks[operation] = { result: "fail", error: String(error) }; }
      }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
        canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion,
        credential: controlCredential, foreignCredential: "controlled-foreign-key", installationRoot: control.installationRoot, ownedDisposableCandidate: true, targetId: "owned-target", results });
      assert.deepEqual(Object.keys(checks), control.proof.operations);
      const observed = JSON.parse(readFileSync(`${results}.deployment-workspace.raw.json`));
      assert.ok(observed.calls.length >= 1);
      if (mode === "valid") {
        const diagnostic = checks[control.proof.operations[0]].result === "pass" ? "" : spawnSync(realPython, ["-B", "-c",
          `import json;from pathlib import Path;from mcp_deployment_workspace_validation_controls import FIXTURE,bootstrap;from mcp_deployment_workspace_validation import judge;judge(FIXTURE,bootstrap(),json.loads(Path(${JSON.stringify(results + ".deployment-workspace.raw.json")}).read_text()))`],
          { cwd: control.directory, encoding: "utf8", timeout: 20_000 });
        // This is an explicit offline fixture: retain the actual oracle refusal
        // rather than hiding a valid-path regression behind a generic wrapper.
        for (const operation of control.proof.operations) assert.equal(checks[operation].result, "pass", JSON.stringify(checks) + String(diagnostic.stderr ?? ""));
        assert.equal(control.verify(checks[control.proof.operations[0]].value), true);
        assert.equal(observed.calls.length, 24);
        const commands = JSON.parse(readFileSync(commandsPath));
        assert.equal(commands.filter(args => args.includes("--restart")).length, 1);
        assert.equal(checks[control.proof.operations[0]].value.deploymentActuationProven, false);
      } else for (const operation of control.proof.operations) assert.equal(checks[operation].result, "fail", mode);
    } finally { process.env.PATH = priorPath; await candidate.close(); control.close(); }
  });
}
