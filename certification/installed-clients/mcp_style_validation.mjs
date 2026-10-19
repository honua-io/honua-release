// Release-owned canonical style scenario and fixed actual-client execution.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

const required = ["mcp_style_validation.py", "mcp_style_state.py", "mcp_ingest_publish_validation.py",
  "mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operations = ["tools/call:get_style", "tools/call:apply_style_preset"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createStyleValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("style requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("style source dependency changed");
  };
  verifyBytes();
  const python = (name, extra, observations, timeout = 10_000) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-style-"));
    try {
      const args = ["-I", "-B", join(directory, name), "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, ...extra];
      if (observations !== undefined) {
        const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations)); args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout, maxBuffer: 2 * 1024 * 1024 });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("authored style setup/state/oracle refused evidence");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("style actual candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };
  async function styleScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage) || ownedDisposableCandidate !== true
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("style requires actual installed client and explicitly owned disposable candidate");
      const authored = python("mcp_style_validation.py", ["--nonce", randomBytes(6).toString("hex"), "--canonical-client", canonicalClient]);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("style bootstrap belongs to another candidate");
      const observed = { schema: authored.schema, nonce: authored.nonce, canonicalClient, clientVersion, candidateSourceSha, candidateImage,
        setup: [], calls: [], snapshots: {}, runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.canonical-style.raw.json`, setupReceipt = `${retained}.setup.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      const state = () => python("mcp_style_state.py", ["--owned-disposable-candidate"], observed, 45_000);
      try {
        verifyBytes();
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual({ ...observed.runtimeBefore, startedAt: authored.runtimeBinding.startedAt }, authored.runtimeBinding)) throw new Error("style candidate changed since bootstrap");
        try {
          observed.setup = python("mcp_style_state.py", ["--owned-disposable-candidate", "--setup", "--setup-receipt", setupReceipt], observed, 75_000);
        } finally {
          if (existsSync(setupReceipt)) observed.setup = JSON.parse(readFileSync(setupReceipt, "utf8"));
        }
        observed.snapshots.initial = state();
        const tool = async (id, name, args, authenticated = true) => {
          const params = { name, arguments: args };
          const entry = { id, method: "tools/call", params, authenticated, result: null, wire: null, client: null }; observed.calls.push(entry);
          verifyBytes();
          const probe = await observeCandidateRequest({ method: "tools/call", params, authenticated, credential, credentialSalt,
            observeDiscovery: canonicalClient === "MCP Inspector",
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: "tools/call", params, authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation); entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          return entry.result;
        };
        const [a, b] = authored.styles, address = authored.address;
        await tool("catalog", "honua_get_style", {});
        await tool("direct", "honua_get_style", { styleId: a.id, includeStylesheet: true });
        await tool("dry-run", "honua_apply_style_preset", { ...address, styleId: b.id, dryRun: true });
        observed.snapshots["dry-run"] = state();
        await tool("apply", "honua_apply_style_preset", { ...address, styleId: a.id });
        observed.snapshots.applied = state();
        await tool("fresh", "honua_get_style", { ...address, includeStylesheet: true });
        await tool("render", "honua_render_map", authored.renderArguments);
        observed.snapshots.readback = state();
        await tool("replay", "honua_apply_style_preset", { ...address, styleId: a.id });
        observed.snapshots.replay = state();
        for (const [id, name, args, authenticated] of [
          ["unknown-style", "honua_apply_style_preset", { ...address, styleId: a.id + "-unknown" }, true],
          ["invalid-encoding", "honua_get_style", { styleId: a.id, encoding: "unknown" }, true],
          ["unknown-layer", "honua_apply_style_preset", { ...address, layerId: 2147483647, styleId: a.id }, true],
          ["anonymous-get", "honua_get_style", { styleId: a.id, includeStylesheet: true }, false],
          ["anonymous-apply", "honua_apply_style_preset", { ...address, styleId: b.id }, false]]) {
          try { await tool(id, name, args, authenticated); }
          finally { observed.snapshots[id] = state(); }
        }
        observed.runtimeRestarted = python("mcp_style_state.py", ["--owned-disposable-candidate", "--restart"], observed, 190_000);
        observed.snapshots.restarted = state();
        await tool("restarted-fresh", "honua_get_style", { ...address, includeStylesheet: true });
        await tool("restarted-render", "honua_render_map", authored.renderArguments);
        observed.snapshots["restarted-readback"] = state();
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n"); }
      }
      proof = { ...python("mcp_style_validation.py", [], observed), schema: "honua.mcp-canonical-style-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_style_validation.py"]}`, observations: observed };
      return proof;
    });
    await record(operations[1], async () => {
      if (!proof) throw new Error("style journey did not produce complete rejudged evidence");
      return proof;
    });
  }
  function verifyStyleProof(proof, candidateSourceSha, candidateImage, operation, canonicalClient, clientVersion) {
    try {
      if (!operations.includes(operation) || !immutable(candidateSourceSha) || !image(candidateImage)
          || !proof || proof.schema !== "honua.mcp-canonical-style-proof/v1" || proof.candidateSourceSha !== candidateSourceSha
          || proof.candidateImage !== candidateImage || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_style_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient) || proof.canonicalClient !== canonicalClient
          || typeof clientVersion !== "string" || proof.clientVersion !== clientVersion || !isDeepStrictEqual(proof.operations, operations)
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.persistedReadback, proof.restartReadback,
            proof.anonymousInvocationRefusal, proof.negativeNonmutation, proof.coloredRender].every(value => value === true)
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)) return false;
      const judged = python("mcp_style_validation.py", [], proof.observations);
      return ["candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion"].every(key => proof[key] === judged[key]);
    } catch { return false; }
  }
  return { styleScenarios, verifyStyleProof };
}
