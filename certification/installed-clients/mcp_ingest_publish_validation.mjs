// Release-owned executable inline import → publication → persisted fresh reads.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

const required = ["mcp_ingest_publish_validation.py", "mcp_ingest_publish_state.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operations = ["tools/call:ingest_dataset", "tools/call:publish_service", "resources/read:Published service"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createIngestPublishValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("ingest requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("ingest source dependency changed");
  };
  verifyBytes();
  const python = (name, extra, observations, timeout = 10_000) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-ingest-publish-"));
    try {
      const args = ["-I", "-B", join(directory, name), "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, ...extra];
      if (observations !== undefined) {
        const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations));
        args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout, maxBuffer: 2 * 1024 * 1024 });
      verifyBytes();
      if (result.error || result.signal || result.status !== 0) throw new Error("authored ingest/persistence oracle refused evidence");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", source, "--candidate-image", selectedImage], { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("ingest actual candidate runtime inspection refused");
    return JSON.parse(result.stdout);
  };
  async function ingestPublishScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage) || ownedDisposableCandidate !== true
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("ingest requires actual installed client and explicitly owned disposable candidate");
      const authored = python("mcp_ingest_publish_validation.py", ["--nonce", randomBytes(6).toString("hex"), "--canonical-client", canonicalClient]);
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("ingest bootstrap belongs to another candidate");
      const observed = { schema: authored.schema, nonce: authored.nonce, canonicalClient, clientVersion, candidateSourceSha, candidateImage,
        cases: [], reads: [], queries: [], snapshots: {}, runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.ingest-publish.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      try {
        verifyBytes();
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        const expected = { ...observed.runtimeBefore, startedAt: authored.runtimeBinding.startedAt };
        if (!isDeepStrictEqual(expected, authored.runtimeBinding)) throw new Error("ingest candidate changed since bootstrap");
        const invoke = async (collection, id, method, params, authenticated = true) => {
          const entry = { id, method, params, authenticated, result: null, wire: null, client: null }; collection.push(entry);
          verifyBytes();
          const probe = await observeCandidateRequest({ method, params, authenticated, credential, credentialSalt,
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method, params, authenticated, credential }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation); entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          return entry.result;
        };
        const tool = (collection, id, name, args, auth = true) => invoke(collection, id, "tools/call", { name, arguments: args }, auth);
        const output = result => result?.structuredContent ?? JSON.parse(result?.content?.find(item => item.type === "text")?.text ?? "null");
        const ingested = output(await tool(observed.cases, "ingest", "honua_ingest_dataset", authored.ingestArguments));
        // These fields select the next real operation only. The independent
        // judge and persisted geometry/metadata snapshots decide correctness.
        if (!ingested || ingested.success !== true || typeof ingested.connectionId !== "string" || !ingested.connectionId
            || ["schema", "table", "geometryColumn", "primaryKey"].some(key => typeof ingested[key] !== "string" || !/^[a-z_][a-z0-9_]{0,62}$/.test(ingested[key]))) throw new Error("ingest returned no complete publishable tuple");
        await tool(observed.cases, "invalid-ingest", "honua_ingest_dataset", authored.invalidIngestArguments);
        const args = { ...Object.fromEntries(["connectionId", "schema", "table", "geometryColumn", "primaryKey"].map(key => [key, ingested[key]])),
          serviceName: authored.serviceName, layerName: "authored-ingest", srid: 4326, geometryType: "Point" };
        const published = output(await tool(observed.cases, "publish", "honua_publish_service", args));
        if (!published || published.status !== "Completed" || typeof published.serviceUri !== "string"
            || typeof published.serviceName !== "string" || !/^(0|[1-9][0-9]{0,9})$/.test(published.layerId ?? "")) throw new Error("publication is not a completed readable operation; retain queued/approval/refusal without credit");
        await tool(observed.cases, "invalid-publish", "honua_publish_service", { ...args, table: "__missing_" + authored.datasetName });
        observed.snapshots.before = python("mcp_ingest_publish_state.py", ["--owned-disposable-candidate"], observed, 45_000);
        const fresh = await invoke(observed.reads, "fresh-created", "resources/read", { uri: published.serviceUri });
        const view = JSON.parse(fresh?.contents?.[0]?.text ?? "null");
        if (!view || !Array.isArray(view.servingLayerIds) || view.servingLayerIds.length !== 1
            || !Number.isSafeInteger(view.servingLayerIds[0]) || view.servingLayerIds[0] < 0) throw new Error("fresh resource supplied no public service layer index");
        const query = { serviceId: published.serviceName, layerId: view.servingLayerIds[0], where: "1=1", outFields: ["*"], outSrid: 4326, returnGeometry: true, limit: 20 };
        await tool(observed.queries, "before", "honua_query_features", query);
        observed.runtimeRestarted = python("mcp_ingest_publish_state.py", ["--owned-disposable-candidate", "--restart"], observed, 190_000);
        observed.snapshots.after = python("mcp_ingest_publish_state.py", ["--owned-disposable-candidate"], observed, 45_000);
        await invoke(observed.reads, "restarted-created", "resources/read", { uri: published.serviceUri });
        await tool(observed.queries, "after", "honua_query_features", query);
        await invoke(observed.reads, "unknown", "resources/read", { uri: published.serviceUri + "-uncreated" });
        await invoke(observed.reads, "anonymous", "resources/read", { uri: published.serviceUri }, false);
        // Preserve positive and persisted/restart observations before any
        // public-CLI discovery refusal on anonymous tool invocation.
        await tool(observed.cases, "anonymous-ingest", "honua_ingest_dataset", authored.ingestArguments, false);
        await tool(observed.cases, "anonymous-publish", "honua_publish_service", args, false);
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n"); }
      }
      proof = { ...python("mcp_ingest_publish_validation.py", [], observed), schema: "honua.mcp-ingest-publish-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_ingest_publish_validation.py"]}`, observations: observed };
      return proof;
    });
    for (const operation of operations.slice(1)) await record(operation, async () => {
      if (!proof) throw new Error("ingest/publication journey did not produce a complete rejudged proof");
      return proof;
    });
  }
  function verifyIngestPublishProof(proof, candidateSourceSha, candidateImage, operation, canonicalClient, clientVersion) {
    try {
      if (!operations.includes(operation) || !immutable(candidateSourceSha) || !image(candidateImage)
          || !proof || proof.schema !== "honua.mcp-ingest-publish-proof/v1" || proof.candidateSourceSha !== candidateSourceSha
          || proof.candidateImage !== candidateImage || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_ingest_publish_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient) || proof.canonicalClient !== canonicalClient
          || typeof clientVersion !== "string" || proof.clientVersion !== clientVersion
          || !isDeepStrictEqual(proof.operations, operations) || proof.queuedPublishExercised !== false
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.persistedReadback, proof.restartReadback, proof.anonymousInvocationRefusal].every(value => value === true)
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)) return false;
      const judged = python("mcp_ingest_publish_validation.py", [], proof.observations);
      return ["candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion"].every(key => proof[key] === judged[key]);
    } catch { return false; }
  }
  return { ingestPublishScenarios, verifyIngestPublishProof };
}
