import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createRenderValidation } from "./mcp_render_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["mcp_render_validation.py", "render_png.py", "fixture.v1.json"];
const sha256 = Object.fromEntries(names.map((name) => [name, createHash("sha256").update(readFileSync(join(original, name))).digest("hex")]));
const candidateSourceSha = "b".repeat(40), candidateImage = `ghcr.io/honua-io/honua-server@sha256:${"c".repeat(64)}`;

function setup() {
  const directory = mkdtempSync(join(tmpdir(), "release-render-execution-"));
  const built = spawnSync("python3", [join(original, "mcp_render_validation_controls.py"), "--write-controls", directory], { encoding: "utf8", timeout: 10_000 });
  assert.equal(built.status, 0, built.stderr);
  const bootstrapPath = join(directory, "bootstrap.json");
  const context = { directory: original, sourceSha: "a".repeat(40), sha256, bootstrapPath };
  const responses = JSON.parse(readFileSync(join(directory, "responses.json")));
  return { directory, context, responses };
}

for (const mode of ["valid", "conflicting-image", "wrong-mime", "wrong-layer", "negative-accepted", "wrong-error", "transport-failure"]) {
  test(`release-owned actual-client callback and retained proof: ${mode}`, async () => {
    const { directory, context, responses } = setup();
    try {
      const api = createRenderValidation(context), calls = [];
      let proof, error;
      const results = join(directory, "result.json");
      await api.renderScenarios(async (tool, arguments_) => {
        const index = calls.length;
        calls.push({ tool, arguments_: structuredClone(arguments_) });
        if (mode === "transport-failure" && index === 2) throw new Error("controlled transport failure");
        const result = structuredClone(responses[index]);
        if (index === 0 && mode === "conflicting-image") result.content[1].data = "AA==";
        if (index === 0 && mode === "wrong-mime") result.content[1].mimeType = "image/jpeg";
        if (index === 0 && mode === "wrong-layer") result.structuredContent.layers[0].layerId += 1;
        if (index === 1 && mode === "negative-accepted") result.isError = false;
        if (index === 2 && mode === "wrong-error") result.structuredContent.code = "internal";
        return result;
      }, async (name, run) => {
        assert.equal(name, "tools/call:render_map");
        try { proof = await run(); } catch (failure) { error = failure; }
      }, { candidateSourceSha, candidateImage, results });
      assert.equal(calls.length, 3);
      assert.ok(calls.every(({ tool }) => tool === "honua_render_map"));
      assert.deepEqual(calls[0].arguments_.layers, [{ serviceId: "allocated-service-id", layerId: 3 }]);
      assert.equal(calls[1].arguments_.bbox[0], calls[0].arguments_.bbox[2]);
      assert.equal(calls[2].arguments_.layers[0].layerId, 2147483647);
      const raw = JSON.parse(readFileSync(`${results}.render-validation.raw.json`));
      assert.equal(raw.cases.length, mode === "transport-failure" ? 2 : 3);
      assert.equal(Boolean(error), mode !== "valid");
      if (mode === "valid") {
        assert.equal(api.verifyRenderProof(proof, candidateSourceSha, candidateImage), true);
        for (const value of [undefined, null, false, 0, "", [], {}]) {
          const missing = { ...proof, observations: value };
          assert.equal(api.verifyRenderProof(missing, candidateSourceSha, candidateImage), false);
        }
        for (const value of [undefined, null, "", [candidateSourceSha], "B".repeat(40)]) {
          assert.equal(api.verifyRenderProof({ ...proof, candidateSourceSha: value }, value, candidateImage), false);
        }
        assert.equal(api.verifyRenderProof(proof, "d".repeat(40), candidateImage), false);
        assert.equal(api.verifyRenderProof(proof, candidateSourceSha, candidateImage.replace(/c/g, "d")), false);
        const changed = structuredClone(proof);
        changed.observations.cases[2].arguments.layers[0].layerId -= 1;
        assert.equal(api.verifyRenderProof(changed, candidateSourceSha, candidateImage), false);
        const bootstrap = JSON.parse(readFileSync(context.bootstrapPath));
        bootstrap.published.area.layerId += 1;
        writeFileSync(context.bootstrapPath, JSON.stringify(bootstrap));
        assert.equal(api.verifyRenderProof(proof, candidateSourceSha, candidateImage), false);
      }
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

test("valid proof then source mutation refuses before callbacks or Python execution", async () => {
  for (const name of names) {
    const { directory, context, responses } = setup();
    try {
      for (const file of names) copyFileSync(join(original, file), join(directory, file));
      const api = createRenderValidation({ ...context, directory });
      let proof, count = 0;
      await api.renderScenarios(async () => structuredClone(responses[count++]), async (_name, run) => { proof = await run(); },
        { candidateSourceSha, candidateImage, results: join(directory, "before.json") });
      assert.equal(api.verifyRenderProof(proof, candidateSourceSha, candidateImage), true);
      writeFileSync(join(directory, name), "untrusted changed source\n");
      assert.equal(api.verifyRenderProof(proof, candidateSourceSha, candidateImage), false, name);
      count = 0;
      let error;
      await api.renderScenarios(async () => { count += 1; }, async (_name, run) => { try { await run(); } catch (value) { error = value; } },
        { candidateSourceSha, candidateImage, results: join(directory, "after.json") });
      assert.match(String(error), /bytes differ from reviewed pins/);
      assert.equal(count, 0);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  }
});
