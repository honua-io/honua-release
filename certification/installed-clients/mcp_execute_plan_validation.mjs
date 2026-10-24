// Release-owned canonical execute/replay/result scenario and fixed actual-client execution.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

const required = ["mcp_execute_plan_validation.py", "mcp_execute_plan_state.py", "mcp_plan_validation.py", "mcp_style_validation.py", "mcp_style_state.py", "mcp_ingest_publish_validation.py",
  "mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operation = "tools/call:execute_plan";
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createExecutePlanValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("execution requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("execution source dependency changed");
  };
  verifyBytes();
  const python = (name, extra, observations, timeout = 10_000) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-execute-"));
    try {
      const args = ["-I", "-B", join(directory, name), "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, ...extra];
      if (observations !== undefined) {
        const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations)); args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout, maxBuffer: 2 * 1024 * 1024 });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("authored execution setup/state/oracle refused evidence");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("execution actual candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };
  async function executePlanScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate, results = "results.json" }) {
    await record(operation, async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage) || ownedDisposableCandidate !== true
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("execution requires actual installed client and explicitly owned disposable candidate");
      const authored = python("mcp_execute_plan_validation.py", ["--nonce", randomBytes(6).toString("hex"), "--canonical-client", canonicalClient]);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("execution bootstrap belongs to another candidate");
      const observed = { schema: authored.schema, nonce: authored.nonce, canonicalClient, clientVersion, candidateSourceSha, candidateImage,
        calls: [], snapshots: {}, runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.canonical-execute-plan.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      const state = () => python("mcp_execute_plan_state.py", ["--owned-disposable-candidate"], observed, 45_000);
      try {
        verifyBytes();
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual({ ...observed.runtimeBefore, startedAt: authored.runtimeBinding.startedAt }, authored.runtimeBinding)) throw new Error("execution candidate changed since bootstrap");
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
        const tool = (id, args, auth = true) => call(id, "tools/call", { name: "honua_execute_plan", arguments: args }, auth);
        try { await tool("invalid", authored.invalidArguments); }
        finally { observed.snapshots.invalid = state(); }
        try { await tool("anonymous", authored.anonymousArguments, false); }
        finally { observed.snapshots.anonymous = state(); }
        const submitted = await tool("execute", authored.arguments);
        const identity = submitted?.structuredContent;
        if (submitted?.isError === true || typeof identity?.jobId !== "string" || !/^gp-[a-f0-9]{24,32}$/.test(identity.jobId)
            || identity.resourceUri !== `honua://jobs/${identity.jobId}`) throw new Error("actual execution did not return a canonical accepted job");
        const uri = identity.resourceUri;
        let terminal = false;
        for (let index = 0; index < 121; index++) {
          const result = await call(`poll-${index}`, "resources/read", { uri });
          const contents = result?.contents;
          if (!Array.isArray(contents) || contents.length !== 1 || contents[0]?.uri !== uri
              || contents[0]?.mimeType !== "application/json" || typeof contents[0]?.text !== "string") throw new Error("actual job poll did not return its JSON resource");
          const status = JSON.parse(contents[0].text);
          if (["Succeeded", "Failed", "Cancelled"].includes(status.status)) { terminal = status.status === "Succeeded"; break; }
          await new Promise(resolve => setTimeout(resolve, 500));
        }
        if (!terminal) throw new Error("actual authored job failed, cancelled or timed out");
        await call("results", "resources/read", { uri: uri + "/results" });
        observed.snapshots.completed = state();
        await tool("replay", authored.arguments);
        observed.snapshots.replay = state();
        for (const [id, selected] of [["anonymous-job", uri], ["anonymous-results", uri + "/results"]]) {
          try { await call(id, "resources/read", { uri: selected }, false); }
          finally { observed.snapshots[id] = state(); }
        }
        observed.runtimeRestarted = python("mcp_execute_plan_state.py", ["--owned-disposable-candidate", "--restart"], observed, 190_000);
        observed.snapshots.restarted = state();
        await call("restarted-job", "resources/read", { uri });
        await call("restarted-results", "resources/read", { uri: uri + "/results" });
        observed.snapshots["restarted-readback"] = state();
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n"); }
      }
      const proof = { ...python("mcp_execute_plan_validation.py", [], observed), schema: "honua.mcp-canonical-execute-plan-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_execute_plan_validation.py"]}`, observations: observed };
      return proof;
    });
  }
  function verifyExecutePlanProof(proof, candidateSourceSha, candidateImage, operation, canonicalClient, clientVersion) {
    try {
      if (operation !== "tools/call:execute_plan" || !immutable(candidateSourceSha) || !image(candidateImage)
          || !proof || proof.schema !== "honua.mcp-canonical-execute-plan-proof/v1" || proof.candidateSourceSha !== candidateSourceSha
          || proof.candidateImage !== candidateImage || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_execute_plan_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient) || proof.canonicalClient !== canonicalClient
          || typeof clientVersion !== "string" || proof.clientVersion !== clientVersion || proof.operation !== operation
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.persistedReadback, proof.restartReadback,
            proof.anonymousInvocationRefusal, proof.negativeNonmutation, proof.idempotentReplay, proof.geometricResult].every(value => value === true)
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)) return false;
      const judged = python("mcp_execute_plan_validation.py", [], proof.observations);
      return ["candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion"].every(key => proof[key] === judged[key]);
    } catch { return false; }
  }
  return { executePlanScenarios, verifyExecutePlanProof };
}
