// Release-owned real map -> app draft -> lifecycle/read-only-review journey.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const appPackageDependencies = Object.freeze(["mcp_app_package_validation.py", "mcp_execute_plan_validation.py",
  "mcp_plan_validation.py", "mcp_style_validation.py", "mcp_ingest_publish_validation.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs",
  "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"]);
const operations = ["tools/call:create_app_package", "resources/read:App package", "tools/call:validate_package", "tools/call:preview_package"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createAppPackageValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || appPackageDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("app requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of appPackageDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("app source dependency changed");
  };
  verifyBytes();
  const python = (values = {}) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-app-"));
    try {
      const args = ["-I", "-B", join(directory, "mcp_app_package_validation.py"), "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath];
      for (const [key, value] of Object.entries(values)) {
        if (key === "map-scenario") args.push("--map-scenario");
        else { const path = join(work, `${key}.json`); writeFileSync(path, JSON.stringify(value)); args.push(`--${key}`, path); }
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout: 20_000, maxBuffer: 16 * 1024 * 1024 });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("authored app prerequisite/composition/review oracle refused evidence");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("app actual candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };
  async function appPackageScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, executionObservations, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192
          || !executionObservations || executionObservations.canonicalClient !== canonicalClient
          || executionObservations.clientVersion !== clientVersion || executionObservations.candidateSourceSha !== candidateSourceSha
          || executionObservations.candidateImage !== candidateImage) throw new Error("app requires exact actual client/candidate and real execution observations");
      const observed = { prerequisites: { execution: executionObservations, map: null }, runtimeBefore: null, runtimeAfter: null, cases: [], reads: [], reviews: [] };
      const retained = `${resolve(results)}.app-package.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, executionObservations.runtimeAfter)) throw new Error("candidate changed since actual execution prerequisite");
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        const invoke = async (entry, method, params) => {
          verifyBytes();
          const probe = await observeCandidateRequest({ method, params, authenticated: entry.authenticated, credential, credentialSalt,
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method, params, authenticated: entry.authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
        };
        // Create a REAL canonical map from the independent published-layer
        // fixture. The Python oracle validates the result before deriving app
        // map/artifact references, never trusting the returned ID alone.
        const mapping = { ...python({ "map-scenario": true }), result: null, wire: null, client: null };
        observed.prerequisites.map = mapping;
        await invoke(mapping, "tools/call", { name: "honua_create_map_package", arguments: mapping.arguments });
        const authored = python({ prerequisites: observed.prerequisites });
        if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage
            || authored.canonicalClient !== canonicalClient || authored.clientVersion !== clientVersion) throw new Error("app prerequisite identity differs");
        Object.assign(observed, { ...authored, cases: [], reads: [], reviews: [] });
        const toolCase = async item => {
          const entry = { ...item, result: null, wire: null, client: null }; observed.cases.push(entry);
          await invoke(entry, "tools/call", { name: "honua_create_app_package", arguments: item.arguments });
        };
        for (const item of authored.cases.filter(item => item.authenticated)) await toolCase(item);
        const created = observed.cases[0].result;
        // This step independently validates complete creation contents first.
        const reviews = python({ prerequisites: observed.prerequisites, created });
        const output = created.structuredContent ?? JSON.parse(created.content.find(item => item.type === "text").text);
        const uri = output.resourceUri;
        if (typeof uri !== "string" || !/^honua:\/\/app-packages\/app_[A-Za-z0-9_-]{1,128}$/.test(uri)) throw new Error("app supplied no bounded URI");
        for (const [id, selected, authenticated] of [["fresh-created", uri, true], ["unknown-package", uri + "-uncreated", true], ["anonymous-created", uri, false]]) {
          const entry = { id, uri: selected, authenticated, result: null, wire: null, client: null }; observed.reads.push(entry);
          await invoke(entry, "resources/read", { uri: selected });
        }
        // Preserve all independently executable positive review observations
        // before public Inspector may locally refuse an anonymous tool name.
        for (const item of reviews.filter(item => item.authenticated)) {
          const entry = { ...item, result: null, wire: null, client: null }; observed.reviews.push(entry);
          await invoke(entry, "tools/call", { name: item.tool, arguments: item.arguments });
        }
        for (const item of authored.cases.filter(item => !item.authenticated)) await toolCase(item);
        for (const item of reviews.filter(item => !item.authenticated)) {
          const entry = { ...item, result: null, wire: null, client: null }; observed.reviews.push(entry);
          await invoke(entry, "tools/call", { name: item.tool, arguments: item.arguments });
        }
        // Canonical ordering is exact and independent of execution scheduling.
        observed.reviews.sort((a, b) => reviews.findIndex(item => item.tool === a.tool && item.id === a.id) - reviews.findIndex(item => item.tool === b.tool && item.id === b.id));
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`); }
      }
      proof = { ...python({ observations: observed }), schema: "honua.mcp-app-package-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_app_package_validation.py"]}`, observations: observed };
      return proof;
    });
    for (const operation of operations.slice(1)) await record(operation, async () => {
      if (!proof) throw new Error("actual app composition/resource/review journey did not produce rejudged proof");
      return proof;
    });
  }
  function verifyAppPackageProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (!operations.includes(operation) || !immutable(source) || !image(selectedImage) || proof?.candidateSourceSha !== source
          || proof.candidateImage !== selectedImage || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.schema !== "honua.mcp-app-package-proof/v1" || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_app_package_validation.py"]}` || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !isDeepStrictEqual(proof.operations, operations)
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.freshReadback, proof.scratchOnly,
            proof.anonymousInvocationRefusal, proof.readOnlyPreview, proof.realMapAndArtifact].every(value => value === true)) return false;
      const judged = python({ observations: proof.observations });
      return Object.keys(judged).every(key => isDeepStrictEqual(judged[key], proof[key]));
    } catch { return false; }
  }
  return { appPackageScenarios, verifyAppPackageProof };
}
