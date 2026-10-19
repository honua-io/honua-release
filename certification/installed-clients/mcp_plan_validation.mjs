// Release-owned authored plan scenario. Callbacks use the actual installed client.
import { createHash } from "node:crypto";
import { readFileSync, mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";

const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function createPlanValidation({ directory, sourceSha, helperSha256, fixtureSha256 }) {
  if (typeof sourceSha !== "string" || !/^[a-f0-9]{40}$/.test(sourceSha)) throw new Error("plan scenario requires immutable helper source");
  const pins = { sourceSha, sha256: { helper: helperSha256, fixture: fixtureSha256 } };
  const verifyBytes = () => {
    for (const [name, expected] of [["mcp_plan_validation.py", helperSha256], ["fixture.v1.json", fixtureSha256]]) {
      if (!/^[a-f0-9]{64}$/.test(expected ?? "") || digest(readFileSync(join(directory, name))) !== expected) {
        throw new Error("shared plan scenario bytes differ from reviewed pins");
      }
    }
  };
  verifyBytes();
  const run = (observations) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-plan-oracle-"));
    try {
      const args = ["-I", "-B", join(directory, "mcp_plan_validation.py"), "--fixture", join(directory, "fixture.v1.json")];
      if (observations) {
        const path = join(work, "observations.json");
        writeFileSync(path, JSON.stringify(observations));
        args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout: 10_000, maxBuffer: 4 * 1024 * 1024 });
      if (result.error || result.signal || result.status !== 0) throw new Error("shared authored plan oracle refused the retained observations");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function planScenarios(call, record, { candidateSourceSha, results = "results.json" }) {
    await record("tools/call:validate_plan", async () => {
      if (!/^[a-f0-9]{40}$/.test(candidateSourceSha ?? "")) throw new Error("plan scenario requires the exact candidate source");
      const authored = run();
      const observations = { ...authored, cases: [] };
      const retained = `${resolve(results)}.plan-validation.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      try {
        for (const item of authored.cases) {
          const arguments_ = structuredClone(item.arguments);
          const result = await call(authored.tool, arguments_);
          observations.cases.push({ id: item.id, arguments: item.arguments, result });
        }
      } finally { writeFileSync(retained, `${JSON.stringify(observations, null, 2)}\n`); }
      const judged = run(observations);
      return { ...judged, schema: "honua.mcp-plan-validation-proof/v1", candidateSourceSha,
        helperSourceSha: pins.sourceSha, helperSha256: `sha256:${pins.sha256.helper}`, observations };
    });
  }

  function verifyPlanProof(proof, candidateSourceSha) {
    try {
      if (typeof candidateSourceSha !== "string" || !/^[a-f0-9]{40}$/.test(candidateSourceSha)
          || typeof proof?.candidateSourceSha !== "string" || !/^[a-f0-9]{40}$/.test(proof.candidateSourceSha)
          || proof?.schema !== "honua.mcp-plan-validation-proof/v1" || proof.candidateSourceSha !== candidateSourceSha
          || proof.helperSourceSha !== pins.sourceSha || proof.helperSha256 !== `sha256:${pins.sha256.helper}`
          || proof.fixtureSha256 !== `sha256:${pins.sha256.fixture}`
          || ![proof.positive, proof.negative, proof.mediaSchema].every((value) => value === true)) return false;
      const judged = run(proof.observations);
      return judged.fixtureSha256 === proof.fixtureSha256;
    } catch { return false; }
  }

  return { planScenarios, verifyPlanProof };
}
