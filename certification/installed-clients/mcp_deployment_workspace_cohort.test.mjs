// Explicit offline original-byte vectors. These never certify a candidate.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync, mkdirSync, readdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createDeploymentWorkspaceCohort, deploymentWorkspaceCohortDependencies } from "./mcp_deployment_workspace_cohort.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const support = ["mcp_deployment_workspace_full_controls.py", "mcp_deployment_workspace_validation_controls.py",
  "mcp_deployment_identity_role_controls.py", "mcp_managed_identity_fixture_controls.py",
  "mcp_map_package_validation_controls.py", "mcp_render_validation_controls.py", "mcp_render_bootstrap.py", "regression.py", "oracles.py"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "offline-original-deployment-cohort-"));
  try {
    for (const name of new Set([...deploymentWorkspaceCohortDependencies, ...support])) copyFileSync(join(original, name), join(directory, name));
    const generated = spawnSync("python3", ["-B", "-c", `import json; from mcp_deployment_workspace_full_controls import full_controls,bootstrap; print(json.dumps({'vector':full_controls(),'bootstrap':bootstrap()}))`],
      { cwd: directory, encoding: "utf8", timeout: 90_000, maxBuffer: 16 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const { vector, bootstrap } = JSON.parse(generated.stdout), bootstrapPath = join(directory, "bootstrap.json");
    writeFileSync(bootstrapPath, JSON.stringify(bootstrap));
    const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
      sha256: Object.fromEntries(deploymentWorkspaceCohortDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
    const proofs = {};
    const cohortPath = join(directory, "cohort.json"); writeFileSync(cohortPath, JSON.stringify(vector.terminal));
    for (const selector of ["sdk", "inspector"]) {
      const observed = join(directory, `${selector}.json`); writeFileSync(observed, JSON.stringify(vector.members[selector]));
      const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_deployment_workspace_cohort.py"),
        "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, "--observations", observed, "--cohort", cohortPath],
        { encoding: "utf8", timeout: 30_000, maxBuffer: 16 * 1024 * 1024 });
      assert.equal(judged.status, 0, judged.stderr);
      proofs[selector] = { ...JSON.parse(judged.stdout), schema: "honua.mcp-deployment-workspace-proof/v2", helperSourceSha: context.sourceSha,
        helperSha256: `sha256:${context.sha256["mcp_deployment_workspace_cohort.py"]}`, observations: vector.members[selector], identityCohort: vector.terminal };
    }
    const api = createDeploymentWorkspaceCohort(context);
    const verify = proof => api.verifyDeploymentWorkspaceProof(proof, proof.candidateSourceSha, proof.candidateImage,
      "tools/call:deploy_operations", proof.canonicalClient, proof.clientVersion);
    return { directory, context, api, proofs, verify, close: () => rmSync(directory, { recursive: true, force: true }) };
  } catch (error) { rmSync(directory, { recursive: true, force: true }); throw error; }
}

test("both complete original offline clients pass the full semantic and managed authority join", () => {
  const control = setup();
  try {
    for (const proof of Object.values(control.proofs)) {
      assert.equal(control.verify(proof), true);
      for (const operation of proof.operations) assert.equal(control.api.verifyDeploymentWorkspaceProof(proof,
        proof.candidateSourceSha, proof.candidateImage, operation, proof.canonicalClient, proof.clientVersion), true);
      for (const field of ["normalManagedIdentityJoined", "tenantOwnerIsolation", "sameTenantPersonalOwnerRefusal",
        "limitedDataReadPositive", "originalTwoClientCohortJoined", "identityCohort"]) {
        const changed = structuredClone(proof); delete changed[field]; assert.equal(control.verify(changed), false);
      }
      const semanticOnly = structuredClone(proof); semanticOnly.semanticOnly = true;
      assert.equal(control.verify(semanticOnly), false);
      const missingOther = structuredClone(proof); missingOther.identityCohort.roleReceipt.cohort.journeys.pop();
      assert.equal(control.verify(missingOther), false);
    }
  } finally { control.close(); }
});

test("full original cohort rechecks every protected dependency and the literal bootstrap", () => {
  const control = setup();
  try {
    assert.equal(control.verify(control.proofs.sdk), true);
    for (const name of deploymentWorkspaceCohortDependencies) {
      const path = join(control.directory, name), bytes = readFileSync(path);
      writeFileSync(path, "changed protected bytes");
      assert.equal(control.verify(control.proofs.sdk), false, name);
      writeFileSync(path, bytes);
    }
    writeFileSync(control.context.bootstrapPath, "{}");
    assert.equal(control.verify(control.proofs.sdk), false);
  } finally { control.close(); }
});

for (const mode of ["prepare-fails", "members-fail", "cleanup-fails"]) {
  test(`actual JS cohort factory retains partial owned state and refuses all eight records: ${mode}`, async () => {
    const control = setup(), originalPath = process.env.PATH, calls = [];
    let privateRoot;
    try {
      const bin = join(control.directory, "offline-bin"), commands = join(control.directory, "offline-commands.jsonl"); mkdirSync(bin);
      const preparationPath = join(control.directory,"offline-public-preparation.json");
      const preparation = structuredClone(control.proofs.sdk.observations.identityPreparation);
      preparation.keys = { sdk:{}, inspector:{} };
      writeFileSync(preparationPath,JSON.stringify(preparation));
      // Only fixture CLI failure responses are controlled here. No successful
      // semantic/auth judge is stubbed and no candidate row can pass.
      const interpreter = spawnSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" });
      assert.equal(interpreter.status, 0);
      const shim = `#!${interpreter.stdout.trim()}\nimport json,pathlib,sys\na=sys.argv[1:]\nassert a[:2]==['-I','-B']\nassert pathlib.Path(a[2]).name=='mcp_deployment_identity_fixture.py'\nphase=a[3]\nroot=pathlib.Path(a[a.index('--private-root')+1])\nwith pathlib.Path(${JSON.stringify(commands)}).open('a') as stream: stream.write(json.dumps({'phase':phase,'args':a,'privateRoot':str(root)})+'\\n')\nif phase=='prepare':\n (root/'offline-owned-recovery.private.json').write_text('explicit offline recovery marker')\n if ${JSON.stringify(mode)}=='prepare-fails':sys.exit(17)\n print(pathlib.Path(${JSON.stringify(preparationPath)}).read_text())\nelif phase=='close':\n if ${JSON.stringify(mode)}=='cleanup-fails':sys.exit(23)\n print(json.dumps({'cleanupComplete':False,'failures':['retained explicit offline setup failure']}))\nelse:raise AssertionError('unexpected mutation/helper')\n`;
      writeFileSync(join(bin, "python3"), shim, { mode: 0o700 }); process.env.PATH = `${bin}${delimiter}${originalPath}`;
      const proof = control.proofs.sdk, manifest = JSON.parse(readFileSync(join(control.directory, "mcp_fixed_client_manifest.v1.json")));
      const record = selector => async (operation, execute) => {
        try { calls.push({ selector, operation, passed: true, proof: await execute() }); }
        catch (error) { calls.push({ selector, operation, passed: false, error: error.message }); }
      };
      await control.api.deploymentWorkspaceCohortScenarios({ sdk: record("sdk"), inspector: record("inspector") }, {
        candidateSourceSha: proof.candidateSourceSha, candidateImage: proof.candidateImage, adminCredential: "explicit-offline-private-admin",
        targetId: "owned-target", ownedDisposableCandidate: true, results: join(control.directory, "cohort-results"),
        clients: Object.fromEntries(["sdk", "inspector"].map(selector => [selector, { clientVersion: manifest.clients[selector].version, installationRoot: "/explicit-offline-unused" }])) });
      const observed = readFileSync(commands, "utf8").trim().split("\n").map(JSON.parse);
      assert.deepEqual(observed.map(value => value.phase), ["prepare", "close"]);
      privateRoot = observed[0].privateRoot;
      assert.equal(observed[1].privateRoot, privateRoot);
      assert.equal(existsSync(join(privateRoot, "offline-owned-recovery.private.json")), true);
      assert.equal(calls.length, 8); assert.equal(calls.every(value => !value.passed), true);
      for (const selector of ["sdk", "inspector"]) assert.equal(calls.filter(value => value.selector === selector).length, 4);
      if (mode !== "prepare-fails") {
        assert.equal(observed[1].args.includes("--sdk-journey"), true);
        assert.equal(observed[1].args.includes("--inspector-journey"), true);
      }
      const recovery = readdirSync(control.directory).filter(name => name.endsWith(".recovery.json")); assert.equal(recovery.length, 1);
      assert.equal(JSON.parse(readFileSync(join(control.directory, recovery[0]))).privateRootRetained, true);
    } finally {
      process.env.PATH = originalPath;
      if (privateRoot) rmSync(privateRoot, { recursive: true, force: true });
      control.close();
    }
  });
}
