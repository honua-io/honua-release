// Release-owned real Ops health tool/resource reads and independent live probes.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const opsHealthDependencies = Object.freeze(["mcp_ops_health_validation.py", "mcp_ops_health_state.py",
  "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py", "mcp_plan_validation.py", "mcp_style_validation.py",
  "mcp_ingest_publish_validation.py", "mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py",
  "mcp_style_state.py", "mcp_ingest_publish_state.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"]);
const operations = ["tools/call:ops_health", "resources/read:Ops health"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createOpsHealthValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || opsHealthDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("health requires complete protected source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of opsHealthDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("health source dependency changed");
  };
  verifyBytes();
  const launch = (name, args, timeout = 25_000) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, name), ...args],
      { encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("authored live health observer/oracle refused evidence");
    return JSON.parse(result.stdout);
  };
  const python = (values = {}, extra = []) => {
    const work = mkdtempSync(join(tmpdir(), "mcp-health-"));
    try {
      const args = ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, ...extra];
      for (const [key, value] of Object.entries(values)) {
        const path = join(work, `${key}.json`); writeFileSync(path, JSON.stringify(value)); args.push(`--${key}`, path);
      }
      return launch("mcp_ops_health_validation.py", args);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => launch("mcp_render_validation.py", ["--inspect-runtime",
    "--candidate-source-sha", source, "--candidate-image", selectedImage], 65_000);
  async function opsHealthScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate = false, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (ownedDisposableCandidate !== true) throw new Error("health restart requires an explicitly owned disposable candidate");
      if (!immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("health requires exact actual client/candidate and private credential");
      const nonce = randomBytes(12).toString("hex");
      const authored = python({}, ["--nonce", nonce, "--canonical-client", canonicalClient]);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("health bootstrap selected another candidate");
      const observed = { ...authored, clientVersion, calls: [], runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.ops-health.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      const probe = restart => launch("mcp_ops_health_state.py", ["--candidate-source-sha", candidateSourceSha,
        "--candidate-image", candidateImage, "--nonce", nonce, "--owned-disposable-candidate", ...(restart ? ["--restart"] : [])], restart ? 190_000 : 90_000);
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        for (const expected of authored.calls) {
          if (expected.id === "restarted-tool") observed.runtimeRestarted = probe(true);
          const entry = { ...expected }; observed.calls.push(entry);
          const positive = ["tool", "resource", "restarted-tool", "restarted-resource"].includes(entry.id);
          if (positive) entry.before = probe(false);
          verifyBytes();
          const transport = await observeCandidateRequest({ method: entry.method, params: entry.params,
            authenticated: entry.authenticated, credential, credentialSalt,
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: entry.method, params: entry.params, authenticated: entry.authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = transport.client;
          const frame = observedResponse(transport.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          if (positive) entry.after = probe(false);
        }
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`); }
      }
      proof = { ...python({ observations: observed }), schema: "honua.mcp-ops-health-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_ops_health_validation.py"]}`, observations: observed };
      return proof;
    });
    await record(operations[1], async () => {
      if (!proof) throw new Error("actual health tool/resource journey did not produce rejudged proof");
      return proof;
    });
  }
  function verifyOpsHealthProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (!operations.includes(operation) || !immutable(source) || !image(selectedImage) || proof?.candidateSourceSha !== source
          || proof.candidateImage !== selectedImage || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.schema !== "honua.mcp-ops-health-proof/v1" || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_ops_health_validation.py"]}` || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !isDeepStrictEqual(proof.operations, operations)
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.independentStores, proof.restartRecomputed,
            proof.anonymousInvocationRefusal, proof.liveNotPersisted].every(value => value === true)) return false;
      const judged = python({ observations: proof.observations });
      return Object.keys(judged).every(key => isDeepStrictEqual(judged[key], proof[key]));
    } catch { return false; }
  }
  return { opsHealthScenarios, verifyOpsHealthProof };
}
