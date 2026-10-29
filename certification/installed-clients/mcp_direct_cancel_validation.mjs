// Release-owned original direct cancellation through both fixed installed clients.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync, lstatSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const directCancelDependencies = Object.freeze([
  "mcp_direct_cancel_validation.py", "mcp_direct_cancel_state.py", "mcp_direct_cancel_config.py",
  "mcp_deployment_identity_configuration.py", "mcp_managed_identity_fixture.py",
  "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py", "mcp_plan_validation.py",
  "mcp_style_validation.py", "mcp_style_state.py", "mcp_ingest_publish_validation.py",
  "mcp_ingest_publish_state.py", "mcp_map_package_validation.py", "mcp_render_validation.py",
  "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs",
  "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json",
]);
export const directCancelOperation = "tools/call:cancel_job";
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export function createDirectCancelValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || directCancelDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || ![directory, bootstrapPath].every(value => typeof value === "string" && value))
    throw new Error("direct cancellation needs complete protected source and original bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const bootstrapSha256 = digest(readFileSync(bootstrapPath));
  const verifyBytes = () => {
    for (const name of directCancelDependencies)
      if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "") || digest(readFileSync(join(directory, name))) !== sha256[name])
        throw new Error("direct cancellation protected dependency changed");
    if (digest(readFileSync(bootstrapPath)) !== bootstrapSha256)
      throw new Error("direct cancellation original bootstrap changed");
  };
  verifyBytes();
  const launch = (name, args, { env = {}, timeout = 90_000 } = {}) => {
    verifyBytes();
    const child = spawnSync("python3", ["-I", "-B", join(directory, name), ...args], {
      encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024, env: { ...process.env, ...env },
    });
    verifyBytes();
    if (child.error || child.signal || child.status !== 0)
      throw new Error("independent direct cancellation fixture, state, caller or oracle refused");
    return JSON.parse(child.stdout);
  };
  const inspect = (source, selectedImage) => launch("mcp_render_validation.py", [
    "--inspect-runtime", "--candidate-source-sha", source, "--candidate-image", selectedImage]);
  const fixtureArgs = ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath];
  const authored = (nonce, canonical) => launch("mcp_direct_cancel_validation.py", [
    ...fixtureArgs, "--nonce", nonce, "--canonical-client", canonical]);
  const state = (setup, jobId) => launch("mcp_direct_cancel_state.py", [
    "--plan-id", setup.plan.planId, "--candidate-source-sha", setup.candidateSourceSha,
    "--candidate-image", setup.candidateImage, "--owned-disposable-candidate",
    ...(jobId ? ["--job-id", jobId] : [])], { timeout: 60_000 });
  const judge = observations => {
    const work = mkdtempSync(join(tmpdir(), "mcp-direct-cancel-judge-"));
    try {
      const path = join(work, "original-observations.json");
      writeFileSync(path, JSON.stringify(observations));
      return launch("mcp_direct_cancel_validation.py", [...fixtureArgs, "--observations", path]);
    } finally { rmSync(work, { recursive: true, force: true }); }
  };

  async function directCancelScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, ownedDisposableCandidate = false, results = "results.json" }) {
    await record(directCancelOperation, async () => {
      if (ownedDisposableCandidate !== true || !immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || typeof installationRoot !== "string" || !installationRoot
          || !/^\d+\.\d+\.\d+$/.test(clientVersion ?? "")
          || typeof credential !== "string" || !credential || credential.length > 8192 || /[\r\n]/.test(credential))
        throw new Error("direct cancellation requires owned exact candidate and installed canonical client");
      const nonce = randomBytes(12).toString("hex"), setup = authored(nonce, canonicalClient);
      if (setup.candidateSourceSha !== candidateSourceSha || setup.candidateImage !== candidateImage)
        throw new Error("direct cancellation original bootstrap differs from candidate");
      const observed = Object.fromEntries(Object.entries(setup).filter(([key]) => !["plan", "arguments", "fakeJobId"].includes(key)));
      Object.assign(observed, { clientVersion, calls: [], identityFixtures: {}, snapshots: {},
        configuration: null, runtimeBefore: null, runtimeAfter: null, lateWaitMilliseconds: 3000 });
      const retained = `${resolve(results)}.direct-cancel.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const privateWork = mkdtempSync(join(tmpdir(), "mcp-direct-cancel-private-"));
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
          throw new Error("direct cancellation runtime differs from original bootstrap");
        observed.configuration = launch("mcp_direct_cancel_config.py", [
          "--nonce", nonce, "--candidate-source-sha", candidateSourceSha,
          "--candidate-image", candidateImage, "--owned-disposable-candidate"]);
        if (observed.configuration.tenantId !== setup.tenantId)
          throw new Error("direct cancellation tenant setup differs from authored plan");
        for (const role of ["full", "limited"]) {
          observed.identityFixtures[role] = managed(role, "create");
          const file = lstatSync(fixtures[role].privatePath);
          if (!file.isFile() || (file.mode & 0o777) !== 0o600 || file.nlink !== 1 || file.size > 65536)
            throw new Error("direct cancellation private managed key boundary differs");
          keys[role] = JSON.parse(readFileSync(fixtures[role].privatePath, "utf8")).key;
          if (typeof keys[role] !== "string" || !keys[role])
            throw new Error("direct cancellation managed key missing");
        }
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        const call = async (id, method, params, role = "full", selectedTenant = undefined) => {
          const tenantId = role === "anonymous" ? undefined
            : role === "limited" ? selectedTenant
            : selectedTenant ?? setup.tenantId;
          const entry = { id, method, params, authenticated: role !== "anonymous", credentialRole: role,
            tenantId: tenantId ?? null, wire: null, client: null, result: null,
            startedAt: new Date().toISOString(), completedAt: null };
          observed.calls.push(entry);
          const selectedKey = keys[role === "limited" ? "limited" : "full"];
          try {
            verifyBytes();
            const probe = await observeCandidateRequest({ method, params, authenticated: role !== "anonymous",
              credential: selectedKey, credentialSalt, ...(tenantId ? { tenantId } : {}),
              invoke: endpoint => launchCanonicalClient({ installationRoot,
                selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk", endpoint,
                method, params, authenticated: role !== "anonymous", credential: selectedKey,
                ...(tenantId ? { tenantId } : {}) }),
              onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
            verifyBytes();
            entry.client = probe.client;
            const frame = observedResponse(probe.wire.invocation);
            entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
            return entry.result;
          } finally { entry.completedAt = new Date().toISOString(); }
        };
        const tool = (id, name, args, role = "full") => call(id, "tools/call", { name, arguments: args }, role);
        const jobRead = (id, uri) => call(id, "resources/read", { uri });
        observed.snapshots.initial = state(setup);
        await jobRead("catalog", "honua://catalog/processes");
        await tool("validate", "honua_validate_plan", { plan: setup.plan });
        await tool("invalid", "honua_cancel_job", { jobId: "" });
        await tool("anonymous-pre", "honua_cancel_job", { jobId: setup.fakeJobId }, "anonymous");
        await tool("limited-pre", "honua_cancel_job", { jobId: setup.fakeJobId }, "limited");
        observed.snapshots.preflight = state(setup);
        const submission = await tool("execute", "honua_execute_plan", setup.arguments);
        const identity = submission?.structuredContent;
        if (submission?.isError === true || typeof identity?.jobId !== "string"
            || !/^gp-[a-f0-9]{24,32}$/.test(identity.jobId)
            || identity.resourceUri !== `honua://jobs/${identity.jobId}`)
          throw new Error("direct cancellation did not submit an original durable job");
        const jobId = identity.jobId, uri = identity.resourceUri;
        observed.snapshots.submitted = state(setup, jobId);
        let running = false;
        for (let index = 0; index < 40; index++) {
          const result = await jobRead(`poll-running-${index}`, uri);
          const content = result?.contents?.[0];
          if (content?.mimeType !== "application/json" || content?.uri !== uri || typeof content.text !== "string")
            throw new Error("direct cancellation did not read the original job");
          const status = JSON.parse(content.text).status;
          if (["Succeeded", "Failed", "Cancelled"].includes(status))
            throw new Error("direct cancellation job became terminal before an actual Running observation");
          if (status === "Running") { running = true; break; }
          await pause(100);
        }
        if (!running) throw new Error("direct cancellation never observed actual Running state");
        observed.snapshots.running = state(setup, jobId);
        await call("limited-target", "tools/call", { name: "honua_cancel_job", arguments: { jobId } }, "limited", setup.tenantId);
        observed.snapshots.limited = state(setup, jobId);
        await tool("anonymous-target", "honua_cancel_job", { jobId }, "anonymous");
        observed.snapshots.anonymous = state(setup, jobId);
        await call("other-tenant", "tools/call", { name: "honua_cancel_job", arguments: { jobId } }, "full", "public");
        observed.snapshots.otherTenant = state(setup, jobId);
        await tool("owner-cancel", "honua_cancel_job", { jobId });
        observed.snapshots.acknowledged = state(setup, jobId);
        await tool("repeat-cancel", "honua_cancel_job", { jobId });
        let cancelled = false;
        for (let index = 0; index < 80; index++) {
          const result = await jobRead(`poll-terminal-${index}`, uri);
          const content = result?.contents?.[0];
          if (content?.mimeType !== "application/json" || content?.uri !== uri || typeof content.text !== "string")
            throw new Error("direct cancellation lost original job readback");
          const status = JSON.parse(content.text).status;
          if (["Succeeded", "Failed"].includes(status))
            throw new Error("direct cancellation reached wrong terminal state");
          if (status === "Cancelled") { cancelled = true; break; }
          await pause(100);
        }
        if (!cancelled) throw new Error("direct cancellation never reached terminal Cancelled");
        observed.snapshots.terminal = state(setup, jobId);
        await jobRead("results", uri + "/results");
        observed.snapshots.results = state(setup, jobId);
        await pause(observed.lateWaitMilliseconds);
        await jobRead("late-job", uri);
        await jobRead("late-results", uri + "/results");
        observed.snapshots.late = state(setup, jobId);
      } finally {
        for (const role of ["limited", "full"]) if (fixtures[role]) {
          try { observed.identityFixtures[role] = managed(role, "revoke"); }
          catch {
            cleanupFailed = true;
            try { observed.identityFixtures[role] = JSON.parse(readFileSync(fixtures[role].receiptPath, "utf8")); }
            catch { /* The original absence remains nonpassing. */ }
          }
        }
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally {
          writeFileSync(retained, JSON.stringify(observed, null, 2) + "\n");
          if (!cleanupFailed) rmSync(privateWork, { recursive: true, force: true });
        }
      }
      if (cleanupFailed) throw new Error("direct cancellation managed-key cleanup remains ambiguous");
      return { ...judge(observed), schema: "honua.mcp-direct-cancel-proof/v1", observations: observed,
        helperSourceSha: sourceSha, helperSha256: `sha256:${sha256["mcp_direct_cancel_validation.py"]}` };
    });
  }

  function verifyDirectCancelProof(proof, source, selectedImage, operation, canonical, version) {
    try {
      if (operation !== directCancelOperation || !immutable(source) || !image(selectedImage)
          || proof?.schema !== "honua.mcp-direct-cancel-proof/v1" || proof.candidateSourceSha !== source
          || proof.candidateImage !== selectedImage || proof.canonicalClient !== canonical || proof.clientVersion !== version
          || proof.helperSourceSha !== sourceSha || proof.helperSha256 !== `sha256:${sha256["mcp_direct_cancel_validation.py"]}`
          || proof.fixtureSha256 !== `sha256:${sha256["fixture.v1.json"]}` || proof.operation !== operation)
        return false;
      const judged = judge(proof.observations);
      return Object.keys(judged).every(key => isDeepStrictEqual(proof[key], judged[key]));
    } catch { return false; }
  }
  return { directCancelScenarios, verifyDirectCancelProof };
}
