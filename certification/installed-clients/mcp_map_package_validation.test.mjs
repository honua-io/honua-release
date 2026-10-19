import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createMapPackageValidation } from "./mcp_map_package_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py", "fixture.v1.json"];
const sha256 = Object.fromEntries(names.map((name) => [name, createHash("sha256").update(readFileSync(join(original, name))).digest("hex")]));
const candidateSourceSha = "b".repeat(40), candidateImage = `ghcr.io/honua-io/honua-server@sha256:${"c".repeat(64)}`;
const operations = ["tools/call:create_map_package", "resources/read:Map package"];

function setup() {
  const directory = mkdtempSync(join(tmpdir(), "release-map-package-controls-"));
  const built = spawnSync("python3", [join(original, "mcp_map_package_validation_controls.py"), "--write-controls", directory],
    { encoding: "utf8", timeout: 10_000 });
  assert.equal(built.status, 0, built.stderr);
  const context = { directory: original, sourceSha: "a".repeat(40), sha256, bootstrapPath: join(directory, "bootstrap.json") };
  const responses = JSON.parse(readFileSync(join(directory, "responses.json"))), reads = JSON.parse(readFileSync(join(directory, "reads.json")));
  const previousPath = process.env.PATH;
  process.env.PATH = directory + delimiter + previousPath;
  return { directory, context, responses, reads, restore: () => { process.env.PATH = previousPath; rmSync(directory, { recursive: true, force: true }); } };
}

for (const mode of ["valid", "ignored-view", "wrong-readback", "negative-accepted", "anonymous-accepted", "tool-transport", "read-transport", "runtime-replaced"]) {
  test(`release-owned map composition and fresh-resource callbacks: ${mode}`, async () => {
    const { directory, context, responses, reads, restore } = setup();
    try {
      const api = createMapPackageValidation(context), tools = [], resourceCalls = [], checks = {};
      const results = join(directory, "result.json");
      await api.mapPackageScenarios(async (tool, arguments_) => {
        const index = tools.length; tools.push({ tool, arguments_ });
        if (mode === "tool-transport" && index === 2) throw new Error("controlled actual-client transport failure");
        const result = structuredClone(responses[index]);
        if (index === 0 && mode === "ignored-view") {
          result.structuredContent.package.initialView.bbox = [0, 0, 1, 1];
          result.content[0].text = JSON.stringify(result.structuredContent);
        }
        if (index === 1 && mode === "negative-accepted") result.isError = false;
        return result;
      }, async (uri, { authenticated }) => {
        const index = resourceCalls.length; resourceCalls.push({ uri, authenticated });
        if (mode === "read-transport" && index === 1) throw new Error("controlled fresh client read failure");
        const result = structuredClone(reads[index]);
        if (index === 0 && mode === "wrong-readback") {
          const value = JSON.parse(result.contents[0].text); value.packageId = "map_other"; result.contents[0].text = JSON.stringify(value);
        }
        if (index === 2 && mode === "anonymous-accepted") result.error.data.requiresReauthentication = false;
        if (index === 0 && mode === "runtime-replaced") {
          const path = join(directory, "runtime-control.json"), state = JSON.parse(readFileSync(path));
          state.imageReadback.sourceSha = "f".repeat(40); writeFileSync(path, JSON.stringify(state));
        }
        return result;
      }, async (name, run) => {
        try { checks[name] = { result: "pass", value: await run() }; }
        catch (error) { checks[name] = { result: "fail", error: String(error) }; }
      }, { candidateSourceSha, candidateImage, results });
      assert.deepEqual(Object.keys(checks), operations);
      assert.equal(tools.length, 3);
      assert.ok(tools.every(({ tool }) => tool === "honua_create_map_package"));
      assert.deepEqual(tools[0].arguments_.initialView, { bbox: [-90, 0, -45, 60], crs: 4326 });
      assert.equal(tools[0].arguments_.sourceBindings[0].layerId, "3");
      const raw = JSON.parse(readFileSync(`${results}.map-package.raw.json`));
      assert.equal(raw.cases.length, mode === "tool-transport" ? 2 : 3);
      assert.equal(raw.reads.length, mode === "tool-transport" ? 0 : mode === "read-transport" ? 1 : 3);
      if (mode === "valid") {
        assert.deepEqual(resourceCalls, [
          { uri: "honua://map-packages/map_controlled", authenticated: true },
          { uri: "honua://map-packages/map_controlled-uncreated", authenticated: true },
          { uri: "honua://map-packages/map_controlled", authenticated: false }]);
        for (const operation of operations) {
          const proof = checks[operation].value;
          assert.equal(checks[operation].result, "pass");
          assert.equal(api.verifyMapPackageProof(proof, candidateSourceSha, candidateImage, operation), true);
          for (const observation of [undefined, null, false, 0, "", [], {}]) {
            assert.equal(api.verifyMapPackageProof({ ...proof, observations: observation }, candidateSourceSha, candidateImage, operation), false);
          }
          assert.equal(api.verifyMapPackageProof(proof, "f".repeat(40), candidateImage, operation), false);
          assert.equal(api.verifyMapPackageProof(proof, candidateSourceSha, candidateImage.replace(/c/g, "d"), operation), false);
          assert.equal(api.verifyMapPackageProof(proof, candidateSourceSha, candidateImage, "tools/list"), false);
        }
      } else {
        assert.ok(operations.every((operation) => checks[operation].result === "fail"));
        if (mode === "runtime-replaced") {
          assert.equal(raw.runtimeAfter.imageReadback.sourceSha, "f".repeat(40));
          assert.equal(raw.runtimeAfter.inspectionRefused, true);
        }
      }
    } finally { restore(); }
  });
}

test("valid proof before dependency mutation refuses rejudge and prevents any callbacks", async () => {
  for (const changed of names) {
    const { directory, context, responses, reads, restore } = setup();
    try {
      for (const name of names) copyFileSync(join(original, name), join(directory, name));
      const api = createMapPackageValidation({ ...context, directory });
      let call = 0, read = 0, proof;
      await api.mapPackageScenarios(async () => structuredClone(responses[call++]), async () => structuredClone(reads[read++]),
        async (_name, run) => { proof = await run(); }, { candidateSourceSha, candidateImage, results: join(directory, "before.json") });
      assert.equal(api.verifyMapPackageProof(proof, candidateSourceSha, candidateImage, operations[0]), true);
      writeFileSync(join(directory, changed), "untrusted changed helper/fixture bytes\n");
      assert.equal(api.verifyMapPackageProof(proof, candidateSourceSha, candidateImage, operations[0]), false, changed);
      call = 0; read = 0;
      const errors = [];
      await api.mapPackageScenarios(async () => { call += 1; }, async () => { read += 1; },
        async (_name, run) => { try { await run(); } catch (error) { errors.push(String(error)); } },
        { candidateSourceSha, candidateImage, results: join(directory, "after.json") });
      assert.equal(call, 0); assert.equal(read, 0);
      assert.match(errors[0], /dependency bytes differ from reviewed pins/);
    } finally { restore(); }
  }
});
