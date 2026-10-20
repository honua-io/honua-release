// Explicit synthetic vectors exercise the real bridge/verifier. No installed
// package execution, product runtime, or candidate qualification is claimed.
import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createOpsEventsValidation, opsEventsDependencies } from "./mcp_ops_events_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup(inspector = false) {
  const directory = mkdtempSync(join(tmpdir(), "release-ops-events-controls-"));
  try {
    // These are offline controls' support files. Only the exported production
    // dependency list is admitted into the source-bound bridge context.
    for (const entry of readdirSync(original, { withFileTypes: true })) {
      if (entry.isFile()) copyFileSync(join(original, entry.name), join(directory, entry.name));
    }
    const generated = spawnSync("python3", ["-B", "-c", `
import base64,json,yaml
from mcp_ops_events_journey_controls import journey,judge,scenario_inputs,configuration_manifest,bootstrap
observed=journey(inspector=${inspector ? "True" : "False"})
setup,declaration=scenario_inputs()
manifest=configuration_manifest()
manifest['candidate']['ref']=setup['runtimeBinding']['candidateSourceSha']
manifest['components']['honua-server'].update(sha=setup['runtimeBinding']['candidateSourceSha'],digest='sha256:'+'c'*64)
raw=yaml.safe_dump(manifest).encode()
print(json.dumps({'observed':observed,'judged':judge(observed),'setup':setup,'bootstrapBytesBase64':base64.b64encode(json.dumps(bootstrap()).encode()).decode(),'declaration':declaration,'manifestBytesBase64':base64.b64encode(raw).decode()}))
`], { cwd: directory, encoding: "utf8", timeout: 30_000, maxBuffer: 16 * 1024 * 1024 });
    assert.equal(generated.status, 0, generated.stderr);
    const fixture = JSON.parse(generated.stdout);
    const bootstrapPath = join(directory, "bootstrap.json"), setupPath = join(directory, "setup.json");
    const platformManifestPath = join(directory, "manifest.yaml");
    // Keep the original Python JSON number spellings: rewriting through Node
    // would change e.g. 0.0 into 0 and invalidate the authored bootstrap hash.
    writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBytesBase64, "base64"));
    writeFileSync(setupPath, JSON.stringify(fixture.setup));
    writeFileSync(platformManifestPath, Buffer.from(fixture.manifestBytesBase64, "base64"));
    const context = { directory, sourceSha: "a".repeat(40), bootstrapPath, setupPath, platformManifestPath,
      platformManifestSha256: hash(readFileSync(platformManifestPath)), manifestSourceSha: fixture.declaration.releaseSourceSha,
      sha256: Object.fromEntries(opsEventsDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
    const proof = { ...fixture.judged, schema: "honua.mcp-ops-events-proof/v1", helperSourceSha: context.sourceSha,
      helperSha256: `sha256:${context.sha256["mcp_ops_events_validation.py"]}`, observations: fixture.observed };
    const api = createOpsEventsValidation(context);
    return { directory, context, fixture, proof, api,
      verify(value = proof, operation = proof.operations[0]) {
        return api.verifyOpsEventsProof(value, proof.candidateSourceSha, proof.candidateImage, operation, proof.canonicalClient, proof.clientVersion);
      },
      close() { rmSync(directory, { recursive: true, force: true }); } };
  } catch (error) {
    rmSync(directory, { recursive: true, force: true });
    throw error;
  }
}

for (const inspector of [false, true]) test(`full synthetic ${inspector ? "Inspector" : "SDK"} bridge rejudges all four primary contracts`, () => {
  const control = setup(inspector);
  try {
    for (const operation of control.proof.operations) assert.equal(control.verify(control.proof, operation), true);
    for (const facet of ["managedIdentityJoined", "limitedDataReadPositive", "persistedRestart", "actualCursorPaging", "positive"]) {
      const changed = structuredClone(control.proof); changed[facet] = false;
      assert.equal(control.verify(changed), false, facet);
    }
    const missing = structuredClone(control.proof); delete missing.observations.limitedRead;
    assert.equal(control.verify(missing), false);
    const caller = structuredClone(control.proof);
    caller.observations.limitedRead.credentialFingerprint = caller.observations.identityFixtures.full.identity.credentialFingerprint;
    assert.equal(control.verify(caller), false);
    const stale = structuredClone(control.proof);
    stale.observations.calls.at(-4).result = structuredClone(stale.observations.calls[0].result);
    assert.equal(control.verify(stale), false);
    assert.equal(control.verify(control.proof, "tools/call:unrelated"), false);
  } finally { control.close(); }
});

for (const name of opsEventsDependencies) test(`fixed Ops source context refuses changed dependency ${name}`, () => {
  const control = setup();
  try {
    const target = join(control.directory, name);
    writeFileSync(target, Buffer.concat([readFileSync(target), Buffer.from("\nchanged offline source\n")]));
    assert.throws(() => createOpsEventsValidation(control.context), /dependency changed/);
    assert.equal(control.verify(), false);
  } finally { control.close(); }
});

test("original setup, bootstrap, and governed manifest cannot change after bridge creation", () => {
  for (const key of ["setupPath", "bootstrapPath", "platformManifestPath"]) {
    const control = setup();
    try {
      writeFileSync(control.context[key], Buffer.concat([readFileSync(control.context[key]), Buffer.from("\n")]));
      assert.equal(control.verify(), false, key);
    } finally { control.close(); }
  }
});

test("producer requires the explicit disposable candidate boundary before invoking any observer", async () => {
  const control = setup();
  try {
    const checks = [];
    await control.api.opsEventsScenarios(async (operation, run) => {
      try { await run(); checks.push({ operation, passed: true }); }
      catch { checks.push({ operation, passed: false }); }
    }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion,
      credential: "controlled-fixture-admin", installationRoot: control.directory });
    assert.deepEqual(checks.map(check => check.operation), control.proof.operations);
    assert.equal(checks.every(check => check.passed === false), true);
  } finally { control.close(); }
});
