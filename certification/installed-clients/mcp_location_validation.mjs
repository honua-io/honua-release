// Release-owned installed-client execution. Raw wire and independent Python
// semantics must agree; source controls never qualify a candidate.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse, ANONYMOUS_UNLISTED_REFUSAL_TOOLS } from "./mcp_candidate_wire.mjs";

export const locationDependencies = Object.freeze(["mcp_location_validation.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"]);
export const locationOperations = Object.freeze(["tools/call:geocode_address", "tools/call:geocode_addresses", "tools/call:solve_route"]);
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createLocationValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || locationDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("location requires immutable complete source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of locationDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("location dependency bytes differ from reviewed pins");
  };
  verifyBytes();
  const python = (operation, observations) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-location-"));
    try {
      const args = ["-I", "-B", join(directory, "mcp_location_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
        "--bootstrap", bootstrapPath, "--operation", operation];
      if (observations !== undefined) {
        const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations));
        args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout: 20_000, maxBuffer: 16 * 1024 * 1024 });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("independent location oracle refused evidence/bootstrap");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("actual location candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };

  async function locationScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, results = "results.json" }) {
    // Each primary path gets its own complete retained observation/proof. A
    // failed geocoder cannot prevent an independently executable route attempt.
    for (const operation of locationOperations) await record(operation, async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("location requires actual installed client/private credential and exact candidate");
      verifyBytes();
      const authored = python(operation);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("location fixture belongs to another candidate");
      const observed = { ...authored, canonicalClient, clientVersion, cases: [], runtimeBefore: null, runtimeAfter: null };
      const retained = `${resolve(results)}.location-${operation.split(":")[1]}.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, authored.runtimeBinding)) throw new Error("location candidate changed since fixture bootstrap");
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        for (const item of authored.cases) {
          const entry = { ...item, result: null, wire: null, client: null }; observed.cases.push(entry);
          verifyBytes();
          const params = { name: authored.tool, arguments: item.arguments };
          // D3: only the Inspector's anonymous probe of these two tools may be
          // evidenced by observed anonymous discovery absence. The oracle
          // still requires the server refusal whenever tools/call was sent.
          const unlisted = canonicalClient === "MCP Inspector" && item.authenticated === false
            && ANONYMOUS_UNLISTED_REFUSAL_TOOLS.includes(authored.tool);
          const probe = await observeCandidateRequest({ method: "tools/call", params, authenticated: item.authenticated, credential, credentialSalt,
            ...(unlisted ? { observeDiscovery: true, anonymousUnlistedRefusal: true } : {}),
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: "tools/call", params, authenticated: item.authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          verifyBytes(); entry.client = probe.client;
          if (unlisted && probe.wire.invocation === null) { entry.result = null; continue; }
          const frame = observedResponse(probe.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
        }
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`); }
      }
      return { ...python(operation, observed), schema: "honua.mcp-location-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_location_validation.py"]}`, observations: observed };
    });
  }

  function verifyLocationProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (!locationOperations.includes(operation) || !immutable(source) || !image(selectedImage)
          || proof?.candidateSourceSha !== source || proof.candidateImage !== selectedImage || proof.operation !== operation
          || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.schema !== "honua.mcp-location-proof/v1" || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_location_validation.py"]}` || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.anonymousInvocationRefusal, proof.coordinateSemantics, proof.limitSemantics]
            .every(value => value === true)) return false;
      const judged = python(operation, proof.observations);
      return Object.keys(judged).every(key => isDeepStrictEqual(judged[key], proof[key]));
    } catch { return false; }
  }
  return { locationScenarios, verifyLocationProof };
}
