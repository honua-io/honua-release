import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createPlanValidation } from "./mcp_plan_validation.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const hash = (name) => createHash("sha256").update(readFileSync(join(original, name))).digest("hex");
const context = { directory: original, sourceSha: "a".repeat(40),
  helperSha256: hash("mcp_plan_validation.py"), fixtureSha256: hash("fixture.v1.json") };
const candidateSourceSha = "b".repeat(40);
function response(arguments_, mode) {
  const invalid = arguments_.plan.steps[0].inputs.wkb === "not-base64";
  const value = { isExecutable: !invalid || mode === "invalid-accepted", requiresApproval: false, warnings: [],
    violations: invalid ? [{ code: "INVALID_PARAMETER_VALUE", fieldPath: "steps[buffer].inputs.wkb", message: "Invalid base64 WKB" }] : [] };
  if (mode === "wrong-path" && invalid) value.violations[0].fieldPath = "steps[other].inputs.wkb";
  if (mode === "missing-schema") delete value.warnings;
  return { structuredContent: value };
}
for (const mode of ["valid", "invalid-accepted", "wrong-path", "missing-schema", "transport-failure"]) {
  test(`owned scenario preserves authored requests/raw observations: ${mode}`, async () => {
    const directory = mkdtempSync(join(tmpdir(), "release-plan-execution-"));
    const results = join(directory, "result.json");
    const api = createPlanValidation(context);
    const calls = [];
    let proof, error;
    try {
      await api.planScenarios(async (tool, arguments_) => {
        calls.push({ tool, arguments_: structuredClone(arguments_) });
        if (mode === "transport-failure" && calls.length === 2) throw new Error("controlled failure");
        return response(arguments_, mode);
      }, async (name, run) => {
        assert.equal(name, "tools/call:validate_plan");
        try { proof = await run(); } catch (failure) { error = failure; }
      }, { candidateSourceSha, results });
      assert.equal(calls.length, 2);
      assert.ok(calls.every(({ tool }) => tool === "honua_validate_plan"));
      const first = calls[0].arguments_.plan, second = calls[1].arguments_.plan;
      assert.equal(second.steps[0].inputs.wkb, "not-base64");
      second.steps[0].inputs.wkb = first.steps[0].inputs.wkb;
      assert.deepEqual(first, second);
      const raw = JSON.parse(readFileSync(`${results}.plan-validation.raw.json`));
      assert.equal(raw.cases.length, mode === "transport-failure" ? 1 : 2);
      assert.equal(Boolean(error), mode !== "valid");
      if (mode === "valid") {
        assert.equal(api.verifyPlanProof(proof, candidateSourceSha), true);
        const broken = structuredClone(proof);
        broken.observations.cases[1].result.structuredContent.isExecutable = true;
        assert.equal(api.verifyPlanProof(broken, candidateSourceSha), false);
        assert.equal(api.verifyPlanProof(proof, "c".repeat(40)), false);
        for (const value of [undefined, null, false, 0, "", [], {}]) {
          const unobserved = structuredClone(proof);
          if (value === undefined) delete unobserved.observations;
          else unobserved.observations = value;
          assert.equal(api.verifyPlanProof(unobserved, candidateSourceSha), false, `raw observations ${String(value)}`);
        }
        for (const value of [undefined, null, "", "b".repeat(39), "B".repeat(40), "z".repeat(40), [candidateSourceSha], {}]) {
          const unbound = structuredClone(proof);
          if (value === undefined) delete unbound.candidateSourceSha;
          else unbound.candidateSourceSha = value;
          assert.equal(api.verifyPlanProof(unbound, value), false, `expected/proof candidate ${String(value)}`);
          assert.equal(api.verifyPlanProof(proof, value), false, `expected candidate ${String(value)}`);
          assert.equal(api.verifyPlanProof(unbound, candidateSourceSha), false, `proof candidate ${String(value)}`);
        }
      }
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}
test("source binding refuses changed helper/fixture before any callback", async () => {
  assert.throws(() => createPlanValidation({ ...context, sourceSha: [context.sourceSha] }));
  for (const key of ["helperSha256", "fixtureSha256", "sourceSha"]) {
    assert.throws(() => createPlanValidation({ ...context, [key]: "0".repeat(64) }));
  }
  for (const name of ["mcp_plan_validation.py", "fixture.v1.json"]) {
    const directory = mkdtempSync(join(tmpdir(), "release-plan-integrity-"));
    try {
      for (const file of ["mcp_plan_validation.py", "fixture.v1.json"]) copyFileSync(join(original, file), join(directory, file));
      const api = createPlanValidation({ ...context, directory });
      let proof;
      await api.planScenarios(async (_tool, arguments_) => response(arguments_, "valid"),
        async (_name, run) => { proof = await run(); }, { candidateSourceSha, results: join(directory, "before.json") });
      assert.equal(api.verifyPlanProof(proof, candidateSourceSha), true);
      const marker = join(directory, "untrusted-helper-ran");
      if (name.endsWith(".py")) {
        const helper = readFileSync(join(directory, name), "utf8");
        writeFileSync(join(directory, name), helper.replace("from __future__ import annotations",
          `from __future__ import annotations\nfrom pathlib import Path\nPath(${JSON.stringify(marker)}).write_text("executed")`));
      } else {
        const fixture = JSON.parse(readFileSync(join(directory, name), "utf8"));
        fixture.processes.point[0] += 1;
        writeFileSync(join(directory, name), JSON.stringify(fixture));
      }
      assert.equal(api.verifyPlanProof(proof, candidateSourceSha), false, name);
      let calls = 0, refusal;
      await api.planScenarios(async () => { calls += 1; }, async (_name, run) => {
        try { await run(); } catch (error) { refusal = error; }
      }, { candidateSourceSha, results: join(directory, "after.json") });
      assert.match(String(refusal), /bytes differ from reviewed pins/);
      assert.equal(calls, 0, "changed authored bytes must refuse before any real client call");
      assert.throws(() => readFileSync(marker), { code: "ENOENT" });
    } finally { rmSync(directory, { recursive: true, force: true }); }
  }
});
