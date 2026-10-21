import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createPlanningDisclosure, planningDependencies, planningOperation } from "./mcp_planning_disclosure.mjs";

const root = fileURLToPath(new URL("./", import.meta.url));
const vector = spawnSync("python3", ["-B", join(root, "mcp_planning_disclosure_controls.py"), "--emit-offline-vector"],
  { encoding: "utf8", timeout: 60_000, maxBuffer: 8 * 1024 * 1024 });
assert.equal(vector.status, 0, vector.stderr);
const fixture = JSON.parse(vector.stdout);
const hash = bytes => createHash("sha256").update(bytes).digest("hex");

function isolated(selector = "sdk") {
  const directory = mkdtempSync(join(tmpdir(), "mcp-planning-bridge-control-"));
  const sha256 = Object.fromEntries(planningDependencies.map(name => {
    const bytes = readFileSync(join(root, name));
    copyFileSync(join(root, name), join(directory, name));
    return [name, hash(bytes)];
  }));
  const bootstrapPath = join(directory, "original-bootstrap.json");
  writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBase64, "base64"));
  const factory = createPlanningDisclosure({ directory, sourceSha: "1".repeat(40), sha256, bootstrapPath });
  const value = structuredClone(fixture[selector]);
  const proof = { ...value.judged, observations: value.observations, schema: "honua.mcp-planning-proof/v1",
    helperSourceSha: "1".repeat(40), helperSha256: `sha256:${sha256["mcp_planning_disclosure.py"]}` };
  const verify = () => factory.verifyPlanningProof(proof, proof.candidateSourceSha, proof.candidateImage,
    planningOperation, proof.canonicalClient, proof.clientVersion);
  return { directory, sha256, bootstrapPath, proof, verify, close() { rmSync(directory, { recursive: true, force: true }); } };
}

test("both complete synthetic canonical vectors pass the actual protected-source planning verifier", () => {
  for (const selector of ["sdk", "inspector"]) {
    const control = isolated(selector);
    try {
      assert.equal(control.verify(), true);
      assert.equal(control.proof.liveModelProven, false);
      assert.equal(control.proof.executionProven, false);
    } finally { control.close(); }
  }
});

test("every transitive planning executable/oracle/fixture dependency is rechecked", () => {
  for (const name of planningDependencies) {
    const control = isolated();
    try {
      assert.equal(control.verify(), true);
      writeFileSync(join(control.directory, name), "altered protected bytes");
      assert.equal(control.verify(), false, name);
    } finally { control.close(); }
  }
});

test("planning verifier refuses original context, caller cleanup and raw result replacements", () => {
  const mutations = [
    control => writeFileSync(control.bootstrapPath, "{}"),
    control => { control.proof.helperSourceSha = "2".repeat(40); },
    control => { control.proof.helperSha256 = "sha256:" + "0".repeat(64); },
    control => { control.proof.observations.identityFixtures.limited.cleanupComplete = false; },
    control => { control.proof.observations.calls[1].result.structuredContent.engine = "live"; },
    control => { control.proof.observations.calls.pop(); },
    control => { control.proof.observations.snapshots.pop(); },
    control => { control.proof.liveModelProven = true; },
    control => { control.proof.executionProven = true; },
  ];
  for (const selector of ["sdk", "inspector"]) for (const mutate of mutations) {
    const control = isolated(selector);
    try {
      assert.equal(control.verify(), true);
      mutate(control);
      assert.equal(control.verify(), false);
    } finally { control.close(); }
  }
});

test("planning refuses an incomplete dependency map before loading an observer", () => {
  const control = isolated();
  try {
    const incomplete = { ...control.sha256 };
    delete incomplete["mcp_planning_state.py"];
    assert.throws(() => createPlanningDisclosure({ directory: control.directory, sourceSha: "1".repeat(40),
      sha256: incomplete, bootstrapPath: control.bootstrapPath }), /complete immutable dependency/);
  } finally { control.close(); }
});
