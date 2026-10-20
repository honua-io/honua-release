import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createLocationValidation, locationDependencies, locationOperations } from "./mcp_location_validation.mjs";
import { startControlledCandidate, controlCredential, prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const candidateSourceSha = "b".repeat(40), candidateImage = `ghcr.io/honua-io/honua-server@sha256:${"c".repeat(64)}`;
const identity = { candidateSourceSha, candidateImage, canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1", credential: controlCredential };
const verify = (api, proof, operation) => api.verifyLocationProof(proof, candidateSourceSha, candidateImage, operation, identity.canonicalClient, identity.clientVersion);

async function setup(options = {}) {
  const directory = mkdtempSync(join(tmpdir(), "release-location-controls-"));
  try {
    const built = spawnSync("python3", [join(original, "mcp_location_validation_controls.py"), "--write-controls", directory],
      { encoding: "utf8", timeout: 15_000 });
    assert.equal(built.status, 0, built.stderr);
    for (const name of locationDependencies) copyFileSync(join(original, name), join(directory, name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"), options);
    const context = { directory, sourceSha: "a".repeat(40), sha256: Object.fromEntries(locationDependencies.map(name => [name,
      createHash("sha256").update(readFileSync(join(directory, name))).digest("hex")])), bootstrapPath: join(directory, "bootstrap.json") };
    const byOperation = JSON.parse(readFileSync(join(directory, "location-responses.json")));
    const responses = locationOperations.flatMap(operation => byOperation[operation]);
    const previousPath = process.env.PATH;
    process.env.PATH = directory + delimiter + previousPath;
    let candidate;
    try { candidate = await startControlledCandidate(responses, [], options); }
    catch (error) { process.env.PATH = previousPath; throw error; }
    return { directory, installationRoot, context, candidate, restore: async () => {
      await candidate.close(); process.env.PATH = previousPath; rmSync(directory, { recursive: true, force: true });
    } };
  } catch (error) { rmSync(directory, { recursive: true, force: true }); throw error; }
}

async function execute(api, directory, installationRoot) {
  const checks = {};
  await api.locationScenarios(async (operation, run) => {
    try { checks[operation] = { result: "pass", value: await run() }; }
    catch (error) { checks[operation] = { result: "fail", error: String(error) }; }
  }, { ...identity, installationRoot, results: join(directory, "results.json") });
  return checks;
}

test("actual offline HTTP/controlled installed process executes and independently rejudges each location path", async () => {
  const { directory, context, installationRoot, candidate, restore } = await setup();
  try {
    const api = createLocationValidation(context), checks = await execute(api, directory, installationRoot);
    for (const operation of locationOperations) {
      const check = checks[operation]; assert.equal(check.result, "pass", check.error);
      const proof = check.value;
      assert.equal(verify(api, proof, operation), true);
      assert.equal(verify(api, { ...proof, observations: null }, operation), false);
      for (const flag of ["positive", "negative", "mediaSchema", "coordinateSemantics", "limitSemantics", "anonymousInvocationRefusal"])
        assert.equal(verify(api, { ...proof, [flag]: false }, operation), false);
      assert.equal(verify(api, { ...proof, locationFixtureSha256: "sha256:" + "f".repeat(64) }, operation), false);
      assert.equal(api.verifyLocationProof(proof, "f".repeat(40), candidateImage, operation, identity.canonicalClient, identity.clientVersion), false);
      assert.equal(api.verifyLocationProof(proof, candidateSourceSha, candidateImage.replace(/c/g, "d"), operation, identity.canonicalClient, identity.clientVersion), false);
      assert.equal(api.verifyLocationProof(proof, candidateSourceSha, candidateImage, operation, "MCP Inspector", "2.10.1"), false);
      assert.equal(verify(api, proof, "tools/list"), false);
      const raw = JSON.parse(readFileSync(`${join(directory, "results.json")}.location-${operation.split(":")[1]}.raw.json`));
      assert.equal(raw.operation, operation);
      assert.equal(raw.cases.at(-1).authenticated, false);
      assert.ok(raw.cases.every(item => item.client.processId !== process.pid));
    }
    assert.equal(candidate.seen.length, Object.values(checks).reduce((sum, c) => sum + c.value.observations.cases.length, 0));
    // Every dependency is checked after valid proof and before further child
    // requests. Controlled temporary copies are never protected source files.
    for (const name of locationDependencies) {
      const path = join(directory, name), originalBytes = readFileSync(path), before = candidate.seen.length;
      writeFileSync(path, "controlled changed dependency\n");
      assert.equal(verify(api, checks[locationOperations[0]].value, locationOperations[0]), false, name);
      const refused = await execute(api, directory, installationRoot);
      assert.ok(Object.values(refused).every(check => check.result === "fail" && /dependency bytes differ/.test(check.error)));
      assert.equal(candidate.seen.length, before);
      writeFileSync(path, originalBytes);
    }
  } finally { await restore(); }
});

for (const mode of ["reused-session", "forwarded-anonymous-auth", "transport-failure"]) {
  test(`actual observer/process location refusal retains evidence: ${mode}`, async () => {
    const { directory, context, installationRoot, restore } = await setup({ reuseSessions: mode === "reused-session",
      forwardedAnonymousAuth: mode === "forwarded-anonymous-auth", toolTransport: mode === "transport-failure" });
    try {
      const checks = await execute(createLocationValidation(context), directory, installationRoot);
      assert.ok(Object.values(checks).every(check => check.result === "fail"), JSON.stringify(checks));
      for (const operation of locationOperations) {
        const raw = JSON.parse(readFileSync(`${join(directory, "results.json")}.location-${operation.split(":")[1]}.raw.json`));
        assert.equal(raw.operation, operation); assert.ok(raw.cases.length > 0);
      }
    } finally { await restore(); }
  });
}

test("protected real installed-client manifest rejects alternate controlled package before any request", async () => {
  const { directory, context, installationRoot, candidate, restore } = await setup();
  try {
    const name = "mcp_fixed_client_manifest.v1.json";
    copyFileSync(join(original, name), join(directory, name));
    context.sha256[name] = createHash("sha256").update(readFileSync(join(directory, name))).digest("hex");
    const checks = await execute(createLocationValidation(context), directory, installationRoot);
    assert.ok(Object.values(checks).every(check => check.result === "fail" && /package bytes differ/.test(check.error)));
    assert.equal(candidate.seen.length, 0);
  } finally { await restore(); }
});
