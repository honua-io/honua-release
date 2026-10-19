import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createIngestPublishValidation } from "./mcp_ingest_publish_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["mcp_ingest_publish_validation.py", "mcp_ingest_publish_state.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs",
  "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operations = ["tools/call:ingest_dataset", "tools/call:publish_service", "resources/read:Published service"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "release-ingest-judge-controls-"));
  const generated = spawnSync("python3", ["-B", "-c", "import json; from mcp_ingest_publish_validation_controls import controls, bootstrap; print(json.dumps({'observations':controls(),'bootstrap':bootstrap()}))"],
    { cwd: original, encoding: "utf8", timeout: 15_000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout);
  for (const name of names) copyFileSync(join(original, name), join(directory, name));
  const bootstrapPath = join(directory, "bootstrap.json"); writeFileSync(bootstrapPath, JSON.stringify(fixture.bootstrap));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(names.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observed = join(directory, "controlled.json"); writeFileSync(observed, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_ingest_publish_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observed], { encoding: "utf8", timeout: 15_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-ingest-publish-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_ingest_publish_validation.py"]}`, observations: fixture.observations };
  const api = createIngestPublishValidation(context);
  const verify = (value = proof, source = proof.candidateSourceSha, image = proof.candidateImage, operation = operations[0]) =>
    api.verifyIngestPublishProof(value, source, image, operation, proof.canonicalClient, proof.clientVersion);
  return { directory, context, proof, api, verify, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("all three scoped proofs require actual authored persisted/restart observations", () => {
  const control = setup();
  try {
    for (const operation of operations) assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, operation), true);
    const changed = structuredClone(control.proof); changed.observations.snapshots.after.rows[0].geometry.coordinates.reverse();
    assert.equal(control.verify(changed), false);
    assert.equal(control.verify(control.proof, "f".repeat(40)), false);
    assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, "tools/call:publish_result"), false);
    for (const value of [null, [], {}, { ...control.proof, observations: null }, { ...control.proof, restartReadback: false }, { ...control.proof, queuedPublishExercised: true }]) assert.equal(control.verify(value), false);
  } finally { control.close(); }
});

for (const name of ["mcp_ingest_publish_validation.py", "mcp_ingest_publish_state.py", "fixture.v1.json", "mcp_fixed_client_launcher.mjs"]) {
  test(`valid captured proof cannot rejudge after dependency mutation: ${name}`, () => {
    const control = setup();
    try {
      assert.equal(control.verify(), true);
      writeFileSync(join(control.directory, name), "untrusted changed bytes");
      assert.equal(control.verify(), false);
      assert.throws(() => createIngestPublishValidation(control.context), /dependency changed/);
    } finally { control.close(); }
  });
}

test("execution refuses absent disposable ownership before any installed client launch", async () => {
  const control = setup();
  try {
    const entries = [];
    const record = async (operation, run) => { try { await run(); entries.push([operation, "pass"]); } catch (error) { entries.push([operation, error.message]); } };
    await control.api.ingestPublishScenarios(record, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion, installationRoot: "/nonexistent",
      credential: "controlled-private-credential", ownedDisposableCandidate: false });
    assert.equal(entries.length, 3);
    assert.match(entries[0][1], /explicitly owned disposable candidate/);
    assert.ok(entries.every(([, status]) => status !== "pass"));
  } finally { control.close(); }
});
