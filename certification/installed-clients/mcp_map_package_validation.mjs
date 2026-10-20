// Release-owned executable journey and fixed installed-client launch boundary.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";
import { pathToFileURL } from "node:url";

const required = ["mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operations = ["tools/call:create_map_package", "resources/read:Map package"];
const immutable = (value) => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = (value) => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function createMapPackageValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || required.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("map package requires immutable complete source and fixture bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of required) {
      if (typeof sha256[name] !== "string" || !/^[a-f0-9]{64}$/.test(sha256[name])
          || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("map package dependency bytes differ from reviewed pins");
    }
  };
  verifyBytes();
  const inspectRuntime = (candidateSourceSha, candidateImage) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_render_validation.py"), "--inspect-runtime",
      "--candidate-source-sha", candidateSourceSha, "--candidate-image", candidateImage],
    { encoding: "utf8", timeout: 65_000, maxBuffer: 1024 * 1024 });
    if (result.error || result.signal || result.status !== 0) throw new Error("actual map candidate identity inspection refused");
    return JSON.parse(result.stdout);
  };
  const run = (observations) => {
    verifyBytes();
    const work = mkdtempSync(join(tmpdir(), "mcp-map-package-"));
    try {
      const args = ["-I", "-B", join(directory, "mcp_map_package_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
        "--bootstrap", bootstrapPath];
      if (observations !== undefined) {
        const path = join(work, "observations.json");
        writeFileSync(path, JSON.stringify(observations));
        args.push("--observations", path);
      }
      const result = spawnSync("python3", args, { encoding: "utf8", timeout: 10_000, maxBuffer: 2 * 1024 * 1024 });
      if (result.error || result.signal || result.status !== 0) throw new Error("authored map package oracle refused retained observations/bootstrap");
      return JSON.parse(result.stdout);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function mapPackageScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, installationRoot,
    clientVersion, credential, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (!immutable(candidateSourceSha) || !image(candidateImage)) throw new Error("map composition requires exact candidate source/image");
      if (!["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || typeof clientVersion !== "string" || !/^\d+\.\d+\.\d+$/.test(clientVersion)
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("map composition requires actual installed client and private expected credential");
      verifyBytes();
      const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
      const authored = run();
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("map fixture belongs to another candidate");
      const credentialSalt = randomBytes(16).toString("hex");
      const observed = { ...authored, canonicalClient, clientVersion, cases: [], reads: [], runtimeBefore: null, runtimeAfter: null };
      const retained = `${resolve(results)}.map-package.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      try {
        observed.runtimeBefore = inspectRuntime(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, authored.runtimeBinding)) throw new Error("map candidate changed since fixture bootstrap");
        const toolCase = async (item) => {
          const entry = { id: item.id, arguments: item.arguments, authenticated: item.authenticated, result: null, wire: null, client: null };
          observed.cases.push(entry);
          verifyBytes();
          const probe = await observeCandidateRequest({ method: "tools/call", params: { name: authored.tool, arguments: item.arguments },
            authenticated: item.authenticated, credential, credentialSalt,
            invoke: (endpoint) => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: "tools/call", params: { name: authored.tool, arguments: item.arguments }, authenticated: item.authenticated, credential }),
            onClient: (client) => { entry.client = client; },
            onObservation: (wire) => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
        };
        for (const item of authored.cases.filter((item) => item.authenticated)) await toolCase(item);
        // Extracting the returned URI selects what to read; only the independent
        // Python oracle may credit its identity, selectors and actual readback.
        const first = observed.cases[0].result;
        const output = first?.structuredContent ?? JSON.parse(first?.content?.find((item) => item.type === "text")?.text ?? "null");
        const uri = output?.resourceUri;
        if (typeof uri !== "string" || !/^honua:\/\/map-packages\/map_[A-Za-z0-9_-]{1,128}$/.test(uri)) throw new Error("map creation supplied no bounded draft URI to read");
        for (const [id, selected, authenticated] of [["fresh-created", uri, true], ["unknown-package", uri + "-uncreated", true], ["anonymous-created", uri, false]]) {
          const entry = { id, uri: selected, authenticated, result: null, wire: null, client: null };
          observed.reads.push(entry);
          verifyBytes();
          const probe = await observeCandidateRequest({ method: "resources/read", params: { uri: selected }, authenticated, credential, credentialSalt,
            invoke: (endpoint) => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: "resources/read", params: { uri: selected }, authenticated, credential }),
            onClient: (client) => { entry.client = client; },
            onObservation: (wire) => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
        }
        // The public Inspector can reject a long-tail name locally. Keep the
        // independently executable resource observations before that refusal;
        // missing actual anonymous tools/call wire still cannot produce proof.
        for (const item of authored.cases.filter((item) => !item.authenticated)) await toolCase(item);
      } finally {
        try {
          observed.runtimeAfter = inspectRuntime(candidateSourceSha, candidateImage);
          if (!isDeepStrictEqual(observed.runtimeAfter, authored.runtimeBinding)) throw new Error("map candidate changed during actual client calls");
        } finally { writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`); }
      }
      proof = { ...run(observed), schema: "honua.mcp-map-package-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_map_package_validation.py"]}`, observations: observed };
      return proof;
    });
    await record(operations[1], async () => {
      if (!proof) throw new Error("actual map creation/resource journey did not produce a rejudged proof");
      return proof;
    });
  }

  function verifyMapPackageProof(proof, candidateSourceSha, candidateImage, operation, canonicalClient, clientVersion) {
    try {
      if (!operations.includes(operation) || !immutable(candidateSourceSha) || !immutable(proof?.candidateSourceSha)
          || !image(candidateImage) || proof.candidateSourceSha !== candidateSourceSha || proof.candidateImage !== candidateImage
          || proof.schema !== "honua.mcp-map-package-proof/v1" || proof.helperSourceSha !== sourceSha
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient) || proof.canonicalClient !== canonicalClient
          || typeof clientVersion !== "string" || proof.clientVersion !== clientVersion
          || proof.helperSha256 !== `sha256:${sha256["mcp_map_package_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !isDeepStrictEqual(proof.operations, operations)
          || ![proof.positive, proof.negative, proof.mediaSchema, proof.freshReadback, proof.scratchOnly, proof.anonymousToolRefusal].every((value) => value === true)
          || proof.observations === null || typeof proof.observations !== "object" || Array.isArray(proof.observations)) return false;
      const judged = run(proof.observations);
      return ["candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion"]
        .every((key) => judged[key] === proof[key]);
    } catch { return false; }
  }
  return { mapPackageScenarios, verifyMapPackageProof };
}
