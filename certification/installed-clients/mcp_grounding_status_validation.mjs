// Exact installed-client execution for grounding, clarification and declared status.
// The Python judge rechecks original source observations and wire; this module
// alone cannot qualify an immutable candidate or a public release.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, lstatSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const groundingStatusDependencies = Object.freeze([
  "mcp_grounding_status_validation.py", "mcp_grounding_status_state.py", "mcp_ops_events_config.py",
  "mcp_managed_identity_fixture.py", "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py",
  "mcp_plan_validation.py", "mcp_style_validation.py", "mcp_ingest_publish_validation.py",
  "mcp_style_state.py", "mcp_ingest_publish_state.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json",
  "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs",
  "mcp_fixed_client_manifest.v1.json",
]);
export const groundingStatusOperations = Object.freeze([
  "tools/call:ground_candidates", "tools/call:clarify_intent", "tools/call:platform_release_status",
]);
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);

export function createGroundingStatusValidation({ directory, sourceSha, sha256, bootstrapPath,
  platformManifestPath, platformManifestSha256, manifestSourceSha }) {
  if (!immutable(sourceSha) || !immutable(manifestSourceSha) || !sha256 || typeof sha256 !== "object"
      || groundingStatusDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || ![directory, bootstrapPath, platformManifestPath].every(value => typeof value === "string" && value)
      || !/^[a-f0-9]{64}$/.test(platformManifestSha256 ?? ""))
    throw new Error("grounding/status requires complete immutable source and manifest bindings");
  sha256 = Object.freeze({ ...sha256 });
  const bootstrapSha256 = digest(readFileSync(bootstrapPath));
  const verifyBytes = () => {
    for (const name of groundingStatusDependencies)
      if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "") || digest(readFileSync(join(directory, name))) !== sha256[name])
        throw new Error("grounding/status source dependency changed");
    if (digest(readFileSync(bootstrapPath)) !== bootstrapSha256
        || digest(readFileSync(platformManifestPath)) !== platformManifestSha256)
      throw new Error("original grounding bootstrap or governed manifest changed");
  };
  verifyBytes();
  const launch = (name, args, { env = {}, timeout = 90_000 } = {}) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, name), ...args], {
      encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...env },
    });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0)
      throw new Error("independent grounding/status source observer or judge refused");
    return JSON.parse(result.stdout);
  };
  const inspect = (source, selectedImage) => launch("mcp_render_validation.py", [
    "--inspect-runtime", "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const authored = () => launch("mcp_grounding_status_validation.py", [
    "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath]);
  const config = (source, selectedImage) => launch("mcp_ops_events_config.py", [
    "--manifest", platformManifestPath, "--release-source-sha", manifestSourceSha,
    "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const sourceRead = (mode, credential, credentialSalt) => launch("mcp_grounding_status_state.py", [
    "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath,
    "--owned-disposable-candidate", "--credential-salt", credentialSalt,
    ...(mode === "preflight" ? ["--admin-preflight"] : [])], {
    env: { HONUA_MCP_API_KEY: credential },
  });
  const judge = (declaration, observations) => {
    const work = mkdtempSync(join(tmpdir(), "mcp-grounding-judge-"));
    try {
      const original = join(work, "observations.json"), reviewed = join(work, "declaration.json");
      writeFileSync(original, JSON.stringify(observations));
      writeFileSync(reviewed, JSON.stringify(declaration));
      return launch("mcp_grounding_status_validation.py", [
        "--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath,
        "--manifest", platformManifestPath, "--declaration", reviewed, "--observations", original]);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function groundingStatusScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate = false, results = "results.json" }) {
    let proof;
    await record(groundingStatusOperations[0], async () => {
      if (ownedDisposableCandidate !== true || !immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192)
        throw new Error("grounding/status requires an owned exact candidate and installed client");
      const setup = authored(), declaration = config(candidateSourceSha, candidateImage);
      if (setup.candidateSourceSha !== candidateSourceSha || setup.candidateImage !== candidateImage)
        throw new Error("grounding bootstrap differs from selected candidate");
      const observed = { ...setup, canonicalClient, clientVersion, runtimeBefore: null, runtimeAfter: null,
        graphObservation: null, preflightBefore: null, preflightAfter: null, declaration, calls: [],
        identityFixture: null, limitedIdentityFixture: null,
        credentialSalt: randomBytes(16).toString("hex") };
      const retained = `${resolve(results)}.grounding-status.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const privateWork = mkdtempSync(join(tmpdir(), "mcp-grounding-private-"));
      const profiles = Object.freeze({
        admin: { privatePath: join(privateWork, "admin.private.json"), receiptPath: join(privateWork, "admin.receipt.json") },
        "limited-read": { privatePath: join(privateWork, "limited.private.json"), receiptPath: join(privateWork, "limited.receipt.json") },
      });
      const managed = (profile, action) => launch("mcp_managed_identity_fixture.py", [action,
        "--profile", profile, "--private-path", profiles[profile].privatePath,
        "--receipt-path", profiles[profile].receiptPath,
        "--credential-salt", observed.credentialSalt, "--candidate-source-sha", candidateSourceSha,
        "--candidate-image", candidateImage, "--owned-disposable-candidate"],
        { env: { HONUA_MCP_ADMIN_API_KEY: credential }, timeout: 120_000 });
      const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
      const managedKeys = {};
      let intentId, cleanupFailed = false;
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, setup.runtimeBinding))
          throw new Error("grounding runtime changed since owned bootstrap");
        for (const profile of ["admin", "limited-read"]) {
          managed(profile, "create");
          const privatePath = profiles[profile].privatePath;
          const privateStat = lstatSync(privatePath);
          if (!privateStat.isFile() || (privateStat.mode & 0o777) !== 0o600
              || privateStat.nlink !== 1 || privateStat.size > 65536)
            throw new Error("owned managed caller private journal changed");
          managedKeys[profile] = JSON.parse(readFileSync(privatePath, "utf8")).key;
          if (typeof managedKeys[profile] !== "string" || !managedKeys[profile] || managedKeys[profile].length > 8192)
            throw new Error("normal managed caller credential is missing");
        }
        observed.graphObservation = sourceRead("graph", managedKeys.admin, observed.credentialSalt);
        observed.preflightBefore = sourceRead("preflight", managedKeys.admin, observed.credentialSalt);
        for (let index = 0; index < 13; index++) {
          const script = launch("mcp_grounding_status_validation.py", ["--fixture", join(directory, "fixture.v1.json"),
            "--bootstrap", bootstrapPath, "--calls", ...(intentId ? ["--call-intent-id", intentId] : [])]);
          const expected = script.calls[index];
          if (!expected || (index >= 3 && !intentId)) throw new Error("actual material clarification intent is absent");
          const { id: name, method, params, authenticated } = expected;
          const callKey = expected.expectation === "permission_denied" ? managedKeys["limited-read"] : managedKeys.admin;
          const entry = { ...expected, wire: null, client: null, result: null };
          observed.calls.push(entry);
          const probe = await observeCandidateRequest({ method, params, authenticated,
            credential: callKey, credentialSalt: observed.credentialSalt,
            invoke: endpoint => launchCanonicalClient({ installationRoot,
              selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
              endpoint, method, params, authenticated, credential: callKey }),
            onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
          entry.client = probe.client;
          const frame = observedResponse(probe.wire.invocation);
          entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          if (name === "ambiguous") intentId = entry.result?.structuredContent?.draftIntent?.intentId;
          if (name === "status") observed.preflightAfter = sourceRead("preflight", managedKeys.admin, observed.credentialSalt);
        }
      } finally {
        for (const [profile, field] of [["admin", "identityFixture"], ["limited-read", "limitedIdentityFixture"]]) {
          const { privatePath, receiptPath } = profiles[profile];
          if (existsSync(privatePath)) {
            try { observed[field] = managed(profile, "revoke"); }
            catch {
              cleanupFailed = true;
              try { observed[field] = JSON.parse(readFileSync(receiptPath, "utf8")); }
              catch { /* missing normal receipt remains NONPASS */ }
            }
          }
        }
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally {
          writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`);
          if (!cleanupFailed && observed.identityFixture?.cleanupComplete === true
              && observed.limitedIdentityFixture?.cleanupComplete === true)
            rmSync(privateWork, { recursive: true, force: true });
        }
      }
      if (cleanupFailed) throw new Error("normal managed caller revocation was not proven");
      proof = { ...judge(declaration, observed), schema: "honua.mcp-grounding-status-proof/v1",
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_grounding_status_validation.py"]}`,
        observations: observed };
      return proof;
    });
    for (const operation of groundingStatusOperations.slice(1)) await record(operation, async () => {
      if (!proof) throw new Error("grounding/status journey did not pass independent judgment");
      return proof;
    });
  }

  function verifyGroundingStatusProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (!groundingStatusOperations.includes(operation) || !immutable(source) || !image(selectedImage)
          || proof?.schema !== "honua.mcp-grounding-status-proof/v1" || proof.candidateSourceSha !== source
          || proof.candidateImage !== selectedImage || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.helperSourceSha !== sourceSha || proof.helperSha256 !== `sha256:${sha256["mcp_grounding_status_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}`
          || !isDeepStrictEqual(proof.operations, groundingStatusOperations)) return false;
      const judged = judge(config(source, selectedImage), proof.observations);
      return Object.keys(judged).every(key => isDeepStrictEqual(judged[key], proof[key]));
    } catch { return false; }
  }
  return { groundingStatusScenarios, verifyGroundingStatusProof };
}
