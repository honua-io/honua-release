// Canonical planning primary calls with original managed callers and store reads.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, lstatSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const planningDependencies = Object.freeze([
  "mcp_planning_disclosure.py", "mcp_planning_state.py", "mcp_managed_identity_fixture.py",
  "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py", "mcp_plan_validation.py",
  "mcp_style_validation.py", "mcp_style_state.py", "mcp_ingest_publish_validation.py",
  "mcp_ingest_publish_state.py", "mcp_map_package_validation.py", "mcp_render_validation.py",
  "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs",
  "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json",
]);
export const planningOperation = "tools/call:plan_analysis";
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);

export function createPlanningDisclosure({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || planningDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || ![directory, bootstrapPath].every(value => typeof value === "string" && value))
    throw new Error("planning requires complete immutable dependency and original bootstrap bindings");
  sha256 = Object.freeze({ ...sha256 });
  const bootstrapSha256 = digest(readFileSync(bootstrapPath));
  const verifyBytes = () => {
    for (const name of planningDependencies)
      if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "") || digest(readFileSync(join(directory, name))) !== sha256[name])
        throw new Error("planning protected source dependency changed");
    if (digest(readFileSync(bootstrapPath)) !== bootstrapSha256)
      throw new Error("planning original bootstrap changed");
  };
  verifyBytes();
  const launch = (name, args, { env = {}, timeout = 90_000 } = {}) => {
    verifyBytes();
    const child = spawnSync("python3", ["-I", "-B", join(directory, name), ...args], {
      encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...env },
    });
    verifyBytes();
    if (child.error || child.signal || child.status !== 0)
      throw new Error("independent planning fixture, caller, store or oracle refused");
    return JSON.parse(child.stdout);
  };
  const inspect = (source, selectedImage) => launch("mcp_render_validation.py", [
    "--inspect-runtime", "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const fixtureArgs = ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath];
  const authored = (nonce, canonical) => launch("mcp_planning_disclosure.py", [
    ...fixtureArgs, "--nonce", nonce, "--canonical-client", canonical]);
  const store = (nonce, canonical) => launch("mcp_planning_state.py", [
    ...fixtureArgs, "--nonce", nonce, "--canonical-client", canonical, "--owned-disposable-candidate"]);
  const judge = observations => {
    const work = mkdtempSync(join(tmpdir(), "mcp-planning-judge-"));
    try {
      const path = join(work, "original-observations.json");
      writeFileSync(path, JSON.stringify(observations));
      return launch("mcp_planning_disclosure.py", [...fixtureArgs, "--observations", path]);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function planningScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate = false, results = "results.json" }) {
    await record(planningOperation, async () => {
      if (ownedDisposableCandidate !== true || !immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot
          || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192 || /[\r\n]/.test(credential))
        throw new Error("planning requires an owned exact candidate and installed canonical client");
      const nonce = randomBytes(12).toString("hex"), setup = authored(nonce, canonicalClient);
      if (setup.candidateSourceSha !== candidateSourceSha || setup.candidateImage !== candidateImage)
        throw new Error("planning original bootstrap differs from selected candidate");
      const observed = { ...setup, clientVersion, calls: [], identityFixtures: {}, snapshots: [],
        runtimeBefore: null, runtimeAfter: null };
      const retained = `${resolve(results)}.planning.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const privateWork = mkdtempSync(join(tmpdir(), "mcp-planning-private-"));
      const credentialSalt = randomBytes(16).toString("hex"), fixtures = {}, keys = {};
      const managed = (role, action) => {
        const profile = role === "full" ? "admin" : "limited-read";
        const paths = fixtures[role] ??= { privatePath: join(privateWork, role + ".private.json"),
          receiptPath: join(privateWork, role + ".receipt.json") };
        return launch("mcp_managed_identity_fixture.py", [action, "--profile", profile,
          "--private-path", paths.privatePath, "--receipt-path", paths.receiptPath,
          "--credential-salt", credentialSalt, "--candidate-source-sha", candidateSourceSha,
          "--candidate-image", candidateImage, "--owned-disposable-candidate"],
          { env: { HONUA_MCP_ADMIN_API_KEY: credential } });
      };
      let cleanupFailed = false;
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        if (!isDeepStrictEqual(observed.runtimeBefore, setup.runtimeBinding))
          throw new Error("planning runtime changed since original publication bootstrap");
        for (const role of ["full", "limited"]) {
          observed.identityFixtures[role] = managed(role, "create");
          const state = lstatSync(fixtures[role].privatePath);
          if (!state.isFile() || (state.mode & 0o777) !== 0o600 || state.nlink !== 1 || state.size > 65536)
            throw new Error("planning owned private key boundary differs");
          keys[role] = JSON.parse(readFileSync(fixtures[role].privatePath, "utf8")).key;
          if (typeof keys[role] !== "string" || !keys[role])
            throw new Error("planning normal managed identity is incomplete");
        }
        observed.snapshots.push(store(nonce, canonicalClient));
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        for (const expected of setup.calls) {
          const entry = { ...structuredClone(expected), wire: null, client: null, result: null,
            startedAt: new Date().toISOString(), completedAt: null };
          observed.calls.push(entry);
          const selectedKey = keys[entry.credentialRole === "limited" ? "limited" : "full"];
          try {
            verifyBytes();
            const probe = await observeCandidateRequest({ method: entry.method, params: entry.params,
              authenticated: entry.authenticated, credential: selectedKey, credentialSalt,
              invoke: endpoint => launchCanonicalClient({ installationRoot,
                selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk", endpoint,
                method: entry.method, params: entry.params, authenticated: entry.authenticated, credential: selectedKey }),
              onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
            verifyBytes();
            entry.client = probe.client;
            const frame = observedResponse(probe.wire.invocation);
            entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          } finally { entry.completedAt = new Date().toISOString(); }
        }
        observed.snapshots.push(store(nonce, canonicalClient));
      } finally {
        // Attempt both normal revocations even after a partial create or call.
        for (const role of ["limited", "full"]) if (fixtures[role]) {
          try { observed.identityFixtures[role] = managed(role, "revoke"); }
          catch {
            cleanupFailed = true;
            try { observed.identityFixtures[role] = JSON.parse(readFileSync(fixtures[role].receiptPath, "utf8")); }
            catch { /* Original absence remains nonpassing. */ }
          }
        }
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally {
          writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n");
          // An ambiguous normal revocation keeps only this owned private journal.
          if (!cleanupFailed) rmSync(privateWork, { recursive: true, force: true });
        }
      }
      if (cleanupFailed) throw new Error("planning managed-key revocation remains ambiguous");
      return { ...judge(observed), schema: "honua.mcp-planning-proof/v1", observations: observed,
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_planning_disclosure.py"]}` };
    });
  }

  function verifyPlanningProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (operation !== planningOperation || !immutable(source) || !image(selectedImage)
          || proof?.schema !== "honua.mcp-planning-proof/v1" || proof.candidateSourceSha !== source
          || proof.candidateImage !== selectedImage || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.helperSourceSha !== sourceSha || proof.helperSha256 !== `sha256:${sha256["mcp_planning_disclosure.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}` || proof.operation !== operation)
        return false;
      const judged = judge(proof.observations);
      return Object.keys(judged).every(key => isDeepStrictEqual(proof[key], judged[key]));
    } catch { return false; }
  }
  return { planningScenarios, verifyPlanningProof };
}
