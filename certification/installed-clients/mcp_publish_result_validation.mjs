// Release-owned genuine worker result publication and fixed actual-client journey.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

const required = ["fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_execute_plan_state.py", "mcp_execute_plan_validation.mjs", "mcp_execute_plan_validation.py",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_client_manifest.v1.json", "mcp_fixed_sdk_runner.mjs", "mcp_ingest_publish_state.py",
  "mcp_ingest_publish_validation.mjs", "mcp_ingest_publish_validation.py", "mcp_map_package_validation.mjs", "mcp_map_package_validation.py",
  "mcp_plan_validation.py", "mcp_render_bootstrap.py", "mcp_render_validation.mjs", "mcp_render_validation.py", "mcp_style_state.py",
  "mcp_style_validation.mjs", "mcp_style_validation.py", "oracles.py", "regression.py", "render_png.py",
  "mcp_publish_result_validation.mjs", "mcp_publish_result_validation.py", "mcp_publish_result_state.py", "mcp_publish_result_setup.py"];
const operation = "tools/call:publish_result";
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createPublishResultValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("publication requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("publication source dependency changed");
  };
  verifyBytes();
  const python = (name, extra, observations, timeout = 10_000, privateEnv = {}) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-publish-result-"));
    try {
      const args = ["-I", "-B", join(directory, name), "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, ...extra];
      if (observations !== undefined) {
        const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations)); args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...privateEnv } });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("genuine publication setup/state/oracle refused evidence");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("publication actual candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };
  async function publishResultScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate, results = "results.json" }) {
    await record(operation, async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage) || ownedDisposableCandidate !== true
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("publication requires actual installed client and explicitly owned disposable candidate");
      const authored = python("mcp_publish_result_validation.py", ["--nonce", randomBytes(6).toString("hex"), "--canonical-client", canonicalClient]);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("publication bootstrap belongs to another candidate");
      const observed = { schema: authored.schema, nonce: authored.nonce, canonicalClient, clientVersion, candidateSourceSha, candidateImage,
        setup: null, calls: [], snapshots: {}, runtimeBefore: null, runtimePrePublish: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.canonical-publish-result.raw.json`;
      const setupReceipt = `${resolve(results)}.canonical-publish-result.setup.json`;
      mkdirSync(dirname(retained), { recursive: true });
      writeFileSync(setupReceipt, "{}\n");
      const credentialSalt = randomBytes(16).toString("hex");
      const state = () => python("mcp_publish_result_state.py", ["--owned-disposable-candidate"], observed, 65_000);
      try {
        verifyBytes();
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual({ ...observed.runtimeBefore, startedAt: authored.runtimeBinding.startedAt }, authored.runtimeBinding)) throw new Error("publication candidate changed since original bootstrap");
        let identity;
        try {
          identity = python("mcp_publish_result_setup.py", ["--owned-disposable-candidate", "--receipt", setupReceipt], observed, 250_000,
            { HONUA_MCP_API_KEY: credential, HONUA_MCP_CREDENTIAL_SALT: credentialSalt });
        } finally {
          if (existsSync(setupReceipt)) observed.setup = JSON.parse(readFileSync(setupReceipt, "utf8"));
        }
        // StartedAt drift is allowed only against the ORIGINAL shared bootstrap.
        // Setup and this client's current runtime must agree exactly.
        if (!isDeepStrictEqual(observed.runtimeBefore, observed.setup?.runtimeBefore)
            || !isDeepStrictEqual(observed.runtimeBefore, observed.setup?.runtimeAfter)) throw new Error("candidate restarted during/before this client's setup");
        observed.snapshots.initial = state();
        const call = async (id, method, params, authenticated = true) => {
          const entry = { id, method, params, authenticated, result: null, wire: null, client: null }; observed.calls.push(entry);
          verifyBytes();
          const probe = await observeCandidateRequest({ method, params, authenticated, credential, credentialSalt,
            observeDiscovery: canonicalClient === "MCP Inspector" && method === "tools/call",
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method, params, authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation); entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          return entry.result;
        };
        const tool = (id, args, auth = true, name = "honua_publish_result") => call(id, "tools/call", { name, arguments: args }, auth);
        const uri = `honua://jobs/${identity.jobId}`;
        await call("worker-job", "resources/read", { uri });
        await call("worker-results", "resources/read", { uri: uri + "/results" });
        observed.snapshots["worker-read"] = state();
        const args = python("mcp_publish_result_validation.py", ["--publication-arguments"], observed);
        for (const [id, selected, auth] of [
          ["unknown-artifact", { ...args, artifactId: `${identity.jobId}:missing-artifact-${authored.nonce}` }, true],
          ["unknown-job", { ...args, sourceId: `gp-${authored.nonce.repeat(2)}` }, true], ["anonymous", args, false]]) {
          try { await tool(id, selected, auth); }
          finally { observed.snapshots[id] = state(); }
        }
        observed.runtimePrePublish = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, observed.runtimePrePublish)) throw new Error("candidate changed before actual publication mutation");
        const submitted = await tool("publish", args);
        const published = submitted?.structuredContent;
        if (submitted?.isError === true || published?.status !== "Completed" || typeof published?.serviceUri !== "string") throw new Error("actual result publication is not completed");
        observed.snapshots.published = state();
        await call("fresh-resource", "resources/read", { uri: published.serviceUri });
        const query = python("mcp_publish_result_validation.py", ["--query-arguments"], observed);
        await tool("fresh-query", query, true, "honua_query_features");
        observed.snapshots.readback = state();
        try { await call("anonymous-resource", "resources/read", { uri: published.serviceUri }, false); }
        finally { observed.snapshots["anonymous-resource"] = state(); }
        observed.runtimeRestarted = python("mcp_publish_result_state.py", ["--owned-disposable-candidate", "--restart"], observed, 190_000);
        observed.snapshots.restarted = state();
        await call("restarted-resource", "resources/read", { uri: published.serviceUri });
        await tool("restarted-query", query, true, "honua_query_features");
        await call("restarted-worker-results", "resources/read", { uri: uri + "/results" });
        observed.snapshots["restarted-readback"] = state();
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n"); }
      }
      return { ...python("mcp_publish_result_validation.py", [], observed), schema: "honua.mcp-canonical-publish-result-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_publish_result_validation.py"]}`, observations: observed };
    });
  }
  function verifyPublishResultProof(proof, candidateSourceSha, candidateImage, selectedOperation, canonicalClient, clientVersion) {
    try {
      if (selectedOperation !== operation || !immutable(candidateSourceSha) || !image(candidateImage)
          || !proof || proof.schema !== "honua.mcp-canonical-publish-result-proof/v1" || proof.candidateSourceSha !== candidateSourceSha
          || proof.candidateImage !== candidateImage || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_publish_result_validation.py"]}` || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient) || proof.canonicalClient !== canonicalClient
          || typeof clientVersion !== "string" || proof.clientVersion !== clientVersion || proof.operation !== operation
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.persistedReadback, proof.restartReadback,
            proof.anonymousInvocationRefusal, proof.negativeNonmutation, proof.genuineWorkerResult, proof.geometricResult].every(v => v === true)
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)) return false;
      const judged = python("mcp_publish_result_validation.py", [], proof.observations);
      return ["candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion"].every(k => proof[k] === judged[k]);
    } catch { return false; }
  }
  return { publishResultScenarios, verifyPublishResultProof };
}
