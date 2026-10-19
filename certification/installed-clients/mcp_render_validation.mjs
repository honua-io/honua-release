// Release-owned executable canonical render scenario; callbacks use actual clients.
import { createHash } from "node:crypto";
import { readFileSync, mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";

const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const immutableSha = (value) => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const imageDigest = (value) => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const required = ["mcp_render_validation.py", "render_png.py", "fixture.v1.json"];

export function createRenderValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutableSha(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("render scenario requires immutable source and bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) {
      if (typeof sha256[name] !== "string" || !/^[a-f0-9]{64}$/.test(sha256[name])
          || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("shared render bytes differ from reviewed pins");
    }
  };
  verifyBytes();
  const run = (observations) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-render-oracle-"));
    try {
      const args = ["-I", "-B", join(directory, "mcp_render_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
        "--bootstrap", bootstrapPath];
      if (observations !== undefined) {
        const path = join(work, "observations.json");
        writeFileSync(path, JSON.stringify(observations));
        args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout: 10_000, maxBuffer: 8 * 1024 * 1024 });
      if (result.error || result.signal || result.status !== 0) throw new Error("shared render oracle refused retained observations/bootstrap");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function renderScenarios(call, record, { candidateSourceSha, candidateImage, results = "results.json" }) {
    await record("tools/call:render_map", async () => {
      if (!immutableSha(candidateSourceSha) || !imageDigest(candidateImage)) throw new Error("render requires exact candidate source and image");
      const authored = run();
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("render bootstrap belongs to another candidate");
      const observations = { ...authored, cases: [] };
      const retained = `${resolve(results)}.render-validation.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      try {
        for (const item of authored.cases) {
          const result = await call(authored.tool, structuredClone(item.arguments));
          observations.cases.push({ id: item.id, arguments: item.arguments, result });
        }
      } finally { writeFileSync(retained, `${JSON.stringify(observations, null, 2)}\n`); }
      return { ...run(observations), schema: "honua.mcp-render-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_render_validation.py"]}`,
        pngOracleSha256: `sha256:${sha256["render_png.py"]}`, observations };
    });
  }

  function verifyRenderProof(proof, candidateSourceSha, candidateImage) {
    try {
      if (!immutableSha(candidateSourceSha) || !immutableSha(proof?.candidateSourceSha)
          || !imageDigest(candidateImage) || proof?.candidateImage !== candidateImage
          || proof?.candidateSourceSha !== candidateSourceSha || proof?.schema !== "honua.mcp-render-proof/v1"
          || proof.helperSourceSha !== sourceSha || proof.helperSha256 !== `sha256:${sha256["mcp_render_validation.py"]}`
          || proof.pngOracleSha256 !== `sha256:${sha256["render_png.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)
          || ![proof.positive, proof.negative, proof.mediaSchema].every((value) => value === true)) return false;
      const judged = run(proof.observations);
      return ["candidateSourceSha", "candidateImage", "bootstrapSha256", "fixtureSha256"].every((key) => judged[key] === proof[key]);
    } catch { return false; }
  }
  return { renderScenarios, verifyRenderProof };
}
