// Offline HTTP/process controls only. Alternative client bytes are deliberately
// installed into a separate test manifest; none can qualify a real candidate.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createAppPackageValidation, appPackageDependencies } from "./mcp_app_package_validation.mjs";
import { startControlledCandidate, prepareControlledInstallation, controlCredential } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const support = ["mcp_app_package_validation_controls.py", "mcp_execute_plan_validation_controls.py", "mcp_map_package_validation_controls.py",
  "mcp_render_validation_controls.py", "mcp_render_bootstrap.py", "mcp_execute_plan_state.py", "mcp_style_state.py", "regression.py", "oracles.py"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup(live = false) {
  const directory = mkdtempSync(join(tmpdir(), "release-app-controls-"));
  for (const name of [...appPackageDependencies, ...support]) copyFileSync(join(original, name), join(directory, name));
  let installationRoot;
  if (live) installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"));
  const generated = spawnSync("python3", ["-B", "-c",
    "import json,base64; from mcp_app_package_validation_controls import controls, bootstrap; print(json.dumps({'observations':controls(),'bootstrapBytesBase64':base64.b64encode(json.dumps(bootstrap()).encode()).decode()}))"],
  { cwd: directory, encoding: "utf8", timeout: 20_000, maxBuffer: 16 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout);
  // Preserve the actual Python-authored bootstrap representation. A JS parse /
  // stringify round trip loses integral float types and changes its bound hash.
  const bootstrapPath = join(directory, "bootstrap.json"); writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBytesBase64, "base64"));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(appPackageDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observations = join(directory, "observations.json"); writeFileSync(observations, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_app_package_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observations], { encoding: "utf8", timeout: 20_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-app-package-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_app_package_validation.py"]}`, observations: fixture.observations };
  const api = createAppPackageValidation(context);
  const verify = (value = proof) => api.verifyAppPackageProof(value, proof.candidateSourceSha, proof.candidateImage,
    "tools/call:create_app_package", proof.canonicalClient, proof.clientVersion);
  return { directory, context, fixture, proof, api, verify, installationRoot, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("complete app positive proof joins real map/artifact, metadata and both review tools", () => {
  const control = setup();
  try {
    for (const operation of control.proof.operations) assert.equal(control.api.verifyAppPackageProof(control.proof, control.proof.candidateSourceSha,
      control.proof.candidateImage, operation, control.proof.canonicalClient, control.proof.clientVersion), true);
    for (const field of ["observations", "readOnlyPreview", "realMapAndArtifact", "anonymousInvocationRefusal"]) {
      const value = structuredClone(control.proof); delete value[field]; assert.equal(control.verify(value), false);
    }
    const altered = structuredClone(control.proof); altered.observations.prerequisites.execution.snapshots.completed.results = [];
    assert.equal(control.verify(altered), false);
    const image = structuredClone(control.proof); image.candidateImage = `ghcr.io/honua/server@sha256:${"f".repeat(64)}`; assert.equal(control.verify(image), false);
  } finally { control.close(); }
});

for (const name of appPackageDependencies) test(`app rejects changed protected dependency: ${name}`, () => {
  const control = setup();
  try {
    assert.equal(control.verify(), true);
    writeFileSync(join(control.directory, name), "changed untrusted bytes");
    assert.equal(control.verify(), false); assert.throws(() => createAppPackageValidation(control.context), /dependency changed/);
  } finally { control.close(); }
});

for (const mode of ["valid", "mutated-app-content", "mutating-preview", "reused-session"]) {
  test(`actual offline HTTP observer + fixed client process + full app oracle: ${mode}`, async () => {
    const control = setup(true), priorPath = process.env.PATH;
    const raw = control.fixture.observations;
    const responses = [raw.prerequisites.map.result, ...raw.cases.filter(item => item.authenticated).map(item => item.result),
      ...raw.reviews.filter(item => item.authenticated).map(item => item.result), ...raw.cases.filter(item => !item.authenticated).map(item => item.result),
      ...raw.reviews.filter(item => !item.authenticated).map(item => item.result)];
    if (mode === "mutated-app-content") {
      responses[1].structuredContent.package.runtimeConfig = {};
      responses[1].content[0].text = JSON.stringify(responses[1].structuredContent);
    }
    if (mode === "mutating-preview") {
      responses[6].structuredContent.previewPlan.mayMutatePublishedState = true;
      responses[6].content[0].text = JSON.stringify(responses[6].structuredContent);
    }
    const state = join(control.directory, "runtime-control.json"); writeFileSync(state, JSON.stringify(raw.runtimeBefore));
    writeFileSync(join(control.directory, "docker"), `#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\ns=json.loads(Path(${JSON.stringify(state)}).read_text())\nprint(json.dumps(s['imageReadback'] if sys.argv[1]=='image' else {k:s[k] for k in ['running','imageId','imageReference','startedAt','containerId']}))\n`, { mode: 0o755 });
    process.env.PATH = control.directory + delimiter + priorPath;
    const candidate = await startControlledCandidate(responses, raw.reads.map(item => item.result), {
      sessionPrefix: "offline-app-live-session", reuseSessions: mode === "reused-session" });
    try {
      const checks = {}, results = join(control.directory, "results.json");
      await control.api.appPackageScenarios(async (operation, run) => {
        try { checks[operation] = { result: "pass", value: await run() }; }
        catch (error) { checks[operation] = { result: "fail", error: String(error) }; }
      }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
        canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion, credential: controlCredential,
        installationRoot: control.installationRoot, executionObservations: raw.prerequisites.execution, results });
      assert.deepEqual(Object.keys(checks), control.proof.operations);
      assert.equal(checks[control.proof.operations[0]].result, mode === "valid" ? "pass" : "fail", JSON.stringify(checks));
      const retained = JSON.parse(readFileSync(`${results}.app-package.raw.json`));
      assert.equal(retained.prerequisites.map.wire.method, "tools/call");
      assert.equal(candidate.seen[0].params.name, "honua_create_map_package");
      if (mode === "valid") {
        assert.equal(candidate.seen.length, 14);
        for (const operation of control.proof.operations) assert.equal(checks[operation].result, "pass");
        assert.equal(control.verify(checks[control.proof.operations[0]].value), true);
      }
    } finally { await candidate.close(); process.env.PATH = priorPath; control.close(); }
  });
}
