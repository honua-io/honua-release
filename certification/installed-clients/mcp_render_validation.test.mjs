import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createRenderValidation, createRenderBootstrap } from "./mcp_render_validation.mjs";

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
  const previousPath = process.env.PATH;
  process.env.PATH = directory + delimiter + previousPath;
  return { directory, context, responses, restore: () => { process.env.PATH = previousPath; } };
}

for (const mode of ["valid", "conflicting-image", "wrong-mime", "wrong-layer", "negative-accepted", "wrong-error", "transport-failure"]) {
  test(`release-owned actual-client callback and retained proof: ${mode}`, async () => {
    const { directory, context, responses, restore } = setup();
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
    } finally { restore(); rmSync(directory, { recursive: true, force: true }); }
  });
}

test("valid proof then source mutation refuses before callbacks or Python execution", async () => {
  for (const name of names) {
    const { directory, context, responses, restore } = setup();
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
    } finally { restore(); rmSync(directory, { recursive: true, force: true }); }
  }
});

test("bootstrap delegates only after every setup dependency is verified", () => {
  const files = ["mcp_render_bootstrap.py", "regression.py", "oracles.py", "mcp_plan_validation.py", "render_png.py", "fixture.v1.json", "mcp_render_validation.py"];
  for (const changed of files) {
    const directory = mkdtempSync(join(tmpdir(), "release-render-bootstrap-source-"));
    try {
      const marker = join(directory, "executed"), receipt = join(directory, "receipt.json");
      // Explicit controlled executable fixture. It is never fetched or used by
      // a canonical producer, and makes no oracle or qualification assertion.
      const fixture = `import argparse,json\nfrom pathlib import Path\np=argparse.ArgumentParser()\np.add_argument('--fixture')\np.add_argument('--candidate-source-sha')\np.add_argument('--candidate-image')\np.add_argument('--owned-disposable-candidate',action='store_true')\np.add_argument('--receipt')\na=p.parse_args()\nPath(${JSON.stringify(marker)}).write_text('executed')\nPath(a.receipt).write_text(json.dumps({'status':'provisioned','candidateSourceSha':a.candidate_source_sha,'candidateImage':a.candidate_image}))\n`;
      for (const name of files) writeFileSync(join(directory, name), name === "mcp_render_bootstrap.py" ? fixture : "# controlled setup dependency\n");
      const pins = Object.fromEntries(files.map((name) => [name, createHash("sha256").update(readFileSync(join(directory, name))).digest("hex")]));
      const setup = createRenderBootstrap({ directory, sourceSha: "a".repeat(40), sha256: pins });
      const proof = setup({ candidateSourceSha, candidateImage, receipt });
      assert.equal(proof.candidateSourceSha, candidateSourceSha);
      assert.equal(readFileSync(marker, "utf8"), "executed");
      rmSync(marker);
      writeFileSync(join(directory, changed), "untrusted changed dependency\n");
      assert.throws(() => setup({ candidateSourceSha, candidateImage, receipt }), /bytes differ from reviewed pins/);
      assert.throws(() => readFileSync(marker), { code: "ENOENT" });
      assert.throws(() => createRenderBootstrap({ directory, sourceSha: "trunk", sha256: pins }));
      const incomplete = { ...pins }; delete incomplete[files[0]];
      assert.throws(() => createRenderBootstrap({ directory, sourceSha: "a".repeat(40), sha256: incomplete }));
    } finally { rmSync(directory, { recursive: true, force: true }); }
  }
});

test("candidate replacement before or during calls cannot qualify and raw runtime readback survives", async () => {
  for (const when of ["before", "during"]) {
    const { directory, context, responses, restore } = setup();
    try {
      const api = createRenderValidation(context);
      const control = join(directory, "runtime-control.json");
      const replace = () => {
        const state = JSON.parse(readFileSync(control));
        state.startedAt = "2026-10-19T00:01:00Z";
        writeFileSync(control, JSON.stringify(state));
      };
      if (when === "before") replace();
      let calls = 0, error;
      const results = join(directory, "runtime-result.json");
      await api.renderScenarios(async () => {
        const result = structuredClone(responses[calls++]);
        if (when === "during" && calls === 1) replace();
        return result;
      }, async (_name, run) => { try { await run(); } catch (failure) { error = failure; } },
      { candidateSourceSha, candidateImage, results });
      assert.match(String(error), /candidate changed/);
      assert.equal(calls, when === "before" ? 0 : 3);
      const raw = JSON.parse(readFileSync(`${results}.render-validation.raw.json`));
      assert.equal(raw.runtimeAfter.startedAt, "2026-10-19T00:01:00Z");
      assert.equal(raw.cases.length, calls);
    } finally { restore(); rmSync(directory, { recursive: true, force: true }); }
  }
});
