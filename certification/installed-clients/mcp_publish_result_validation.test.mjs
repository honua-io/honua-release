import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createPublishResultValidation } from "./mcp_publish_result_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_execute_plan_state.py", "mcp_execute_plan_validation.mjs", "mcp_execute_plan_validation.py",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_client_manifest.v1.json", "mcp_fixed_sdk_runner.mjs", "mcp_ingest_publish_state.py",
  "mcp_ingest_publish_validation.mjs", "mcp_ingest_publish_validation.py", "mcp_map_package_validation.mjs", "mcp_map_package_validation.py",
  "mcp_plan_validation.py", "mcp_render_bootstrap.py", "mcp_render_validation.mjs", "mcp_render_validation.py", "mcp_style_state.py",
  "mcp_style_validation.mjs", "mcp_style_validation.py", "oracles.py", "regression.py", "render_png.py",
  "mcp_publish_result_validation.mjs", "mcp_publish_result_validation.py", "mcp_publish_result_state.py", "mcp_publish_result_setup.py"];
const operations = ["tools/call:publish_result"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "release-publish-result-judge-controls-"));
  const generated = spawnSync("python3", ["-B", "-c", "import json; from mcp_publish_result_validation_controls import controls, bootstrap; print(json.dumps({'observations':controls(),'bootstrap':bootstrap()}))"],
    { cwd: original, encoding: "utf8", timeout: 15_000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout);
  for (const name of names) copyFileSync(join(original, name), join(directory, name));
  const bootstrapPath = join(directory, "bootstrap.json"); writeFileSync(bootstrapPath, JSON.stringify(fixture.bootstrap));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(names.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observed = join(directory, "controlled.json"); writeFileSync(observed, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_publish_result_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observed], { encoding: "utf8", timeout: 15_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-canonical-publish-result-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_publish_result_validation.py"]}`, observations: fixture.observations };
  const api = createPublishResultValidation(context);
  const verify = (value = proof, source = proof.candidateSourceSha, image = proof.candidateImage, operation = operations[0]) =>
    api.verifyPublishResultProof(value, source, image, operation, proof.canonicalClient, proof.clientVersion);
  return { directory, context, proof, api, verify, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("publication requires complete authored genuine worker/durable/geometry/restart observations", () => {
  const control = setup();
  try {
    for (const operation of operations) assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, operation), true);
    const changed = structuredClone(control.proof); changed.observations.snapshots.anonymous.census.jobs.push({id:"hidden-mutation",sha256:"sha256:"+"a".repeat(64)});
    assert.equal(control.verify(changed), false);
    assert.equal(control.verify(control.proof, "f".repeat(40)), false);
    assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, "tools/call:execute_plan"), false);
    for (const value of [null, [], {}, { ...control.proof, observations: null }, { ...control.proof, restartReadback: false }, { ...control.proof, geometricResult: false }]) assert.equal(control.verify(value), false);
  } finally { control.close(); }
});

for (const name of names) test(`valid captured publication proof refuses changed protected dependency: ${name}`, () => {
  const control = setup();
  try {
    assert.equal(control.verify(), true);
    writeFileSync(join(control.directory, name), "untrusted changed bytes");
    assert.equal(control.verify(), false);
    assert.throws(() => createPublishResultValidation(control.context), /dependency changed/);
  } finally { control.close(); }
});

test("publication refuses absent disposable ownership before setup/client launch", async () => {
  const control = setup();
  try {
    const entries = [];
    const record = async (operation, run) => { try { await run(); entries.push([operation, "pass"]); } catch (error) { entries.push([operation, error.message]); } };
    await control.api.publishResultScenarios(record, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion, installationRoot: "/nonexistent",
      credential: "controlled-private-credential", ownedDisposableCandidate: false });
    assert.equal(entries.length, 1);
    assert.match(entries[0][1], /explicitly owned disposable candidate/);
    assert.ok(entries.every(([, status]) => status !== "pass"));
  } finally { control.close(); }
});
