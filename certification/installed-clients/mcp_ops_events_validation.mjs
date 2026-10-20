// Installed canonical clients, actual wire, and independent findings/events reads.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, lstatSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const opsEventsDependencies = Object.freeze([
  "mcp_ops_events_validation.py", "mcp_ops_events_data_read.py", "mcp_ops_events_state.py", "mcp_ops_events_config.py", "mcp_ops_events_setup.py",
  "mcp_managed_identity_fixture.py", "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py", "mcp_plan_validation.py",
  "mcp_style_validation.py", "mcp_ingest_publish_validation.py", "mcp_map_package_validation.py", "mcp_render_validation.py",
  "render_png.py", "mcp_style_state.py", "mcp_ingest_publish_state.py", "fixture.v1.json", "mcp_candidate_wire.mjs",
  "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json",
]);
const operations = ["tools/call:ops_findings", "tools/call:alert_events", "tools/call:operate_events", "resources/read:Ops findings"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");

export function createOpsEventsValidation({ directory, sourceSha, sha256, bootstrapPath, setupPath,
  platformManifestPath, platformManifestSha256, manifestSourceSha }) {
  if (!immutable(sourceSha) || !immutable(manifestSourceSha) || !sha256 || typeof sha256 !== "object"
      || opsEventsDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || ![bootstrapPath, setupPath, platformManifestPath].every(value => typeof value === "string" && value)
      || !/^[a-f0-9]{64}$/.test(platformManifestSha256 ?? "")) throw new Error("findings/events require complete protected sources and original fixture/manifest");
  sha256 = Object.freeze({ ...sha256 });
  const setupSha256 = digest(readFileSync(setupPath)), bootstrapSha256 = digest(readFileSync(bootstrapPath));
  const verifyBytes = () => {
    for (const name of opsEventsDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || digest(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("findings/events dependency changed");
    if (digest(readFileSync(setupPath)) !== setupSha256 || digest(readFileSync(bootstrapPath)) !== bootstrapSha256
        || digest(readFileSync(platformManifestPath)) !== platformManifestSha256) throw new Error("original findings/events setup/bootstrap/manifest changed");
  };
  verifyBytes();
  const launch = (name, args, { timeout = 90_000, env = {} } = {}) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, name), ...args],
      { encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...env } });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("authored findings/events observer, fixture, or oracle refused");
    return JSON.parse(result.stdout);
  };
  const inspect = (source, selectedImage) => launch("mcp_render_validation.py", ["--inspect-runtime",
    "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const config = (source, selectedImage) => launch("mcp_ops_events_config.py", ["--manifest", platformManifestPath,
    "--release-source-sha", manifestSourceSha, "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const python = (declaration, { nonce, canonicalClient, observations } = {}) => {
    const work = mkdtempSync(join(tmpdir(), "mcp-ops-judge-"));
    try {
      const declarationPath = join(work, "declaration.json"); writeFileSync(declarationPath, JSON.stringify(declaration));
      const args = ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, "--setup", setupPath,
        "--manifest", platformManifestPath, "--declaration", declarationPath];
      if (observations) { const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observations)); args.push("--observations", path); }
      else args.push("--nonce", nonce, "--canonical-client", canonicalClient);
      return launch("mcp_ops_events_validation.py", args);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  async function opsEventsScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate = false, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (ownedDisposableCandidate !== true || !immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192) throw new Error("findings/events require owned runtime and exact installed client/candidate");
      const declaration = config(candidateSourceSha, candidateImage);
      const authored = python(declaration, { nonce: randomBytes(12).toString("hex"), canonicalClient });
      if (authored.candidateSourceSha !== candidateSourceSha || authored.candidateImage !== candidateImage) throw new Error("findings/events bootstrap selected another candidate");
      const observed = { ...authored, clientVersion, calls: [], runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null, identityFixtures: {}, limitedRead: null };
      const retained = `${resolve(results)}.ops-events.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const privateWork = mkdtempSync(join(tmpdir(), "mcp-ops-private-")), credentialSalt = randomBytes(16).toString("hex");
      const seedPath = join(privateWork, "original-seed.json"), declarationPath = join(privateWork, "declaration.json");
      writeFileSync(seedPath, JSON.stringify(JSON.parse(readFileSync(setupPath, "utf8")).seed));
      writeFileSync(declarationPath, JSON.stringify(declaration));
      const fixtures = {}, keys = {};
      const managed = (role, action) => {
        const profile = role === "full" ? "admin" : "limited-read";
        const paths = fixtures[role] ??= { privatePath: join(privateWork, role + ".private.json"), receiptPath: join(privateWork, role + ".receipt.json") };
        return launch("mcp_managed_identity_fixture.py", [action, "--profile", profile, "--private-path", paths.privatePath,
          "--receipt-path", paths.receiptPath, "--credential-salt", credentialSalt,
          "--candidate-source-sha", candidateSourceSha, "--candidate-image", candidateImage, "--owned-disposable-candidate"],
          { env: { HONUA_MCP_ADMIN_API_KEY: credential } });
      };
      const probe = restart => {
        const path = join(privateWork, "context-" + randomBytes(8).toString("hex") + ".json");
        // The observer's CLI writes its original bounded observation to a file.
        // Its stdout is intentionally empty, so invoke through the file mode below.
        verifyBytes();
        const result = spawnSync("python3", ["-I", "-B", join(directory, "mcp_ops_events_state.py"),
          "--candidate-source-sha", candidateSourceSha, "--candidate-image", candidateImage,
          "--seed-report", seedPath, "--configuration", declarationPath, "--credential-salt", credentialSalt,
          "--output", path, "--owned-disposable-candidate", ...(restart ? ["--restart"] : [])],
          { encoding: "utf8", timeout: restart ? 240_000 : 120_000, maxBuffer: 1024 * 1024,
            env: { ...process.env, HONUA_CERT_ADMIN_API_KEY: keys.full } });
        verifyBytes();
        if (result.error || result.signal || result.status !== 0) throw new Error("actual independent operate context refused");
        return JSON.parse(readFileSync(path, "utf8"));
      };
      let cleanupFailed = false;
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        for (const role of ["full", "limited"]) {
          observed.identityFixtures[role] = managed(role, "create");
          const state = lstatSync(fixtures[role].privatePath);
          if (!state.isFile() || (state.mode & 0o777) !== 0o600 || state.nlink !== 1 || state.size > 65536) throw new Error("private managed key fixture boundary changed");
          keys[role] = JSON.parse(readFileSync(fixtures[role].privatePath, "utf8")).key;
          if (typeof keys[role] !== "string" || !keys[role]) throw new Error("normal managed key fixture is incomplete");
        }
        observed.limitedRead = launch("mcp_ops_events_data_read.py", ["--fixture", join(directory, "fixture.v1.json"),
          "--bootstrap", bootstrapPath, "--credential-salt", credentialSalt, "--candidate-source-sha", candidateSourceSha,
          "--candidate-image", candidateImage, "--owned-disposable-candidate"],
          { env: { HONUA_MCP_READ_API_KEY: keys.limited } });
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        let cursor;
        for (const expected of authored.calls) {
          if (expected.id === "restarted-findings") observed.runtimeRestarted = probe(true).runtimeBinding;
          const entry = structuredClone(expected); observed.calls.push(entry);
          if (entry.id === "alerts-second") {
            if (typeof cursor !== "string" || !cursor || cursor.length > 1024) throw new Error("actual first page did not supply its continuation");
            entry.params.arguments.cursor = cursor;
          }
          const positive = ["finding", "empty-finding", "alerts", "operate"].includes(entry.expectation);
          if (positive) entry.before = probe(false);
          const selectedKey = keys[entry.credentialRole === "limited" ? "limited" : "full"];
          verifyBytes();
          const transport = await observeCandidateRequest({ method: entry.method, params: entry.params,
            authenticated: entry.authenticated, credential: selectedKey, credentialSalt,
            invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method: entry.method, params: entry.params, authenticated: entry.authenticated, credential: selectedKey }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = transport.client;
          const frame = observedResponse(transport.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          if (positive) entry.after = probe(false);
          if (entry.id === "alerts-first") cursor = entry.result?.structuredContent?.nextCursor;
        }
      } finally {
        for (const role of ["limited", "full"]) if (fixtures[role]) {
          try { observed.identityFixtures[role] = managed(role, "revoke"); }
          catch {
            cleanupFailed = true;
            try { observed.identityFixtures[role] = JSON.parse(readFileSync(fixtures[role].receiptPath, "utf8")); } catch { /* original missing receipt remains NONPASS */ }
          }
        }
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally {
          writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`);
          // Successful normal revocation removes each private journal itself.
          // On ambiguous cleanup retain the private directory for owned recovery.
          if (!cleanupFailed) rmSync(privateWork, { recursive: true, force: true });
        }
      }
      if (cleanupFailed) throw new Error("normal managed-key cleanup did not prove revocation");
      proof = { ...python(declaration, { observations: observed }), schema: "honua.mcp-ops-events-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_ops_events_validation.py"]}`, observations: observed };
      return proof;
    });
    for (const operation of operations.slice(1)) await record(operation, async () => {
      if (!proof) throw new Error("actual findings/events journey did not produce independently rejudged proof");
      return proof;
    });
  }
  function verifyOpsEventsProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (!operations.includes(operation) || !immutable(source) || !image(selectedImage)
          || proof?.candidateSourceSha !== source || proof.candidateImage !== selectedImage
          || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.schema !== "honua.mcp-ops-events-proof/v1" || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_ops_events_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}` || !isDeepStrictEqual(proof.operations, operations)) return false;
      const judged = python(config(source, selectedImage), { observations: proof.observations });
      return Object.keys(judged).every(key => isDeepStrictEqual(judged[key], proof[key]));
    } catch { return false; }
  }
  return { opsEventsScenarios, verifyOpsEventsProof };
}
