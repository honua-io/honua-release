// Complete original two-client cohort. Semantic-only components never admit rows.
import { createHash, randomBytes, scryptSync } from "node:crypto";
import { constants, openSync, closeSync, fstatSync, readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmdirSync, existsSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";
import { createDeploymentWorkspaceValidation as createSemanticComponent, deploymentWorkspaceDependencies } from "./mcp_deployment_workspace_validation.mjs";

export const deploymentWorkspaceCohortDependencies = Object.freeze([...new Set([...deploymentWorkspaceDependencies,
  "mcp_deployment_workspace_validation.mjs", "mcp_deployment_workspace_authority.py", "mcp_deployment_workspace_cohort.py",
  "mcp_deployment_identity_fixture.py", "mcp_deployment_identity_validation.py", "mcp_deployment_identity_role.py",
  "mcp_deployment_identity_configuration.py", "mcp_managed_identity_fixture.py", "mcp_ops_events_data_read.py", "mcp_ops_events_state.py",
])]);
const clients = Object.freeze({ sdk: "Official MCP TypeScript SDK", inspector: "MCP Inspector" });
const operations = Object.freeze(["tools/call:deploy_operations", "tools/call:propose_rollback", "resources/read:Deployment", "resources/read:Workspace"]);
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const timestamp = () => new Date().toISOString();
function pythonJson(value) {
  if (typeof value === "string") return JSON.stringify(value).replace(/[\u007f-\uffff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
  if (Array.isArray(value)) return `[${value.map(pythonJson).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${pythonJson(key)}:${pythonJson(value[key])}`).join(",")}}`;
  if (value === null || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) return JSON.stringify(value);
  throw new Error("public identity receipt cannot be canonically bound");
}
function readOwnedCredential(path, receipt, salt, profile, serviceId) {
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const before = fstatSync(descriptor);
    if (!before.isFile() || (before.mode & 0o777) !== 0o600 || before.uid !== process.getuid() || before.nlink !== 1 || before.size > 32768) throw new Error("private managed journal ownership refused");
    const value = JSON.parse(readFileSync(descriptor, "utf8"));
    const after = fstatSync(descriptor);
    if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs
      || value.keyId !== receipt.keyId || value.name !== receipt.name || value.profile !== profile
      || value.serviceId !== undefined
      || !isDeepStrictEqual(receipt.permissions, profile === "admin" ? ["admin"] : ["read:*", `execute:${serviceId}/geometry.area`])
      || value.credentialSalt !== salt || !isDeepStrictEqual(value.candidate, receipt.candidate)
      || value.receiptSha256 !== hash(pythonJson(receipt)) || !/^hnua_[A-Za-z0-9_-]{43}$/.test(value.key ?? "")) throw new Error("private key lost its original public fixture binding");
    const record = JSON.parse(Buffer.from(receipt.identity.recordBytesBase64, "base64").toString("utf8"));
    const fingerprint = `scrypt:${salt}:${scryptSync(value.key, Buffer.from(salt, "hex"), 32, { N: 16384, r: 8, p: 1 }).toString("hex")}`;
    if (record.KeyHash !== createHash("sha256").update(value.key).digest("base64") || fingerprint !== receipt.identity.credentialFingerprint) throw new Error("private key differs from the independently observed managed record");
    return value.key;
  } finally { closeSync(descriptor); }
}
export function createDeploymentWorkspaceCohort({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!/^[a-f0-9]{40}$/.test(sourceSha ?? "") || !sha256 || typeof bootstrapPath !== "string" || !bootstrapPath
      || Object.keys(sha256).toSorted().join() !== deploymentWorkspaceCohortDependencies.toSorted().join()) throw new Error("complete cohort requires exact protected dependency closure");
  sha256 = Object.freeze({ ...sha256 });
  const bootstrapSha256 = hash(readFileSync(bootstrapPath));
  const verifyBytes = () => {
    for (const name of deploymentWorkspaceCohortDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "") || hash(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("complete cohort dependency changed");
    if (hash(readFileSync(bootstrapPath)) !== bootstrapSha256) throw new Error("original complete cohort bootstrap changed");
  };
  verifyBytes();
  const semantic = createSemanticComponent({ directory, sourceSha, bootstrapPath,
    sha256: Object.fromEntries(deploymentWorkspaceDependencies.map(name => [name, sha256[name]])) });
  const run = (name, args, { credential, readCredential, admin, timeout = 65_000 } = {}) => {
    verifyBytes();
    if (!deploymentWorkspaceCohortDependencies.includes(name) || !name.endsWith(".py")) throw new Error("unknown cohort helper refused");
    const result = spawnSync("python3", ["-I", "-B", join(directory, name), ...args], { encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024,
      env: { ...process.env, ...(credential === undefined ? {} : { HONUA_MCP_API_KEY: credential }),
        ...(readCredential === undefined ? {} : { HONUA_MCP_READ_API_KEY: readCredential }), ...(admin === undefined ? {} : { HONUA_MCP_ADMIN_API_KEY: admin }) } });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("complete original cohort helper refused; retain original partial receipts");
    return JSON.parse(result.stdout);
  };
  const inspect = (source, image) => run("mcp_render_validation.py", ["--inspect-runtime", "--candidate-source-sha", source, "--candidate-image", image]);
  const judge = (value, cohort, { phase = "judge" } = {}) => {
    // Caller-owned original public paths are unique per attempt. Never rewrite
    // retained original member bytes to make a failed proof qualify.
    const work = mkdtempSync(join(tmpdir(), "mcp-deployment-judge-"));
    try {
      const observed = join(work, "observations.json"), terminal = join(work, "cohort.json");
      writeFileSync(observed, JSON.stringify(value), { mode: 0o600, flag: "wx" });
      const args = ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, "--observations", observed, "--phase", phase];
      if (phase === "judge") { writeFileSync(terminal, JSON.stringify(cohort), { mode: 0o600, flag: "wx" }); args.push("--cohort", terminal); }
      return run("mcp_deployment_workspace_cohort.py", args, { timeout: 25_000 });
    } finally {
      // Only public rejudging inputs live here; private key journals are elsewhere.
      for (const name of ["observations.json", "cohort.json"]) if (existsSync(join(work, name))) unlinkSync(join(work, name));
      rmdirSync(work);
    }
  };
  async function deploymentWorkspaceCohortScenarios(records, options) {
    const allowed = ["candidateSourceSha", "candidateImage", "adminCredential", "targetId", "ownedDisposableCandidate", "clients", "results"];
    if (!options || Object.keys(options).some(name => !allowed.includes(name)) || options.ownedDisposableCandidate !== true
        || !/^[a-f0-9]{40}$/.test(options.candidateSourceSha ?? "") || !/^[^\s]+@sha256:[a-f0-9]{64}$/.test(options.candidateImage ?? "")
        || typeof options.adminCredential !== "string" || !options.adminCredential || options.adminCredential.length > 8192 || /[\r\n]/.test(options.adminCredential)
        || (options.results !== undefined && (typeof options.results !== "string" || !options.results || options.results.includes("\0")))
        || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(options.targetId ?? "")
        || !records || Object.keys(records).toSorted().join() !== Object.keys(clients).toSorted().join()
        || !options.clients || Object.keys(options.clients).toSorted().join() !== Object.keys(clients).toSorted().join()) throw new Error("complete original cohort requires both owned actual clients and configured target");
    const manifest = JSON.parse(readFileSync(join(directory, "mcp_fixed_client_manifest.v1.json")));
    for (const selector of Object.keys(clients)) {
      const selected = options.clients[selector];
      if (typeof records[selector] !== "function" || !selected || Object.keys(selected).toSorted().join() !== ["clientVersion", "installationRoot"].sort().join()
          || selected.clientVersion !== manifest.clients[selector].version || typeof selected.installationRoot !== "string" || !selected.installationRoot) throw new Error("both exact normal installed client identities required");
    }
    const bootstrap = JSON.parse(readFileSync(bootstrapPath));
    if (bootstrap.candidateSourceSha !== options.candidateSourceSha || bootstrap.candidateImage !== options.candidateImage) throw new Error("owned bootstrap and requested candidate differ before fixture mutation");
    const nonce = randomBytes(6).toString("hex"), salt = randomBytes(16).toString("hex"), privateRoot = mkdtempSync(join(tmpdir(), "honua-mcp-deployment-private-"));
    const base = resolve(options.results ?? "results.json"); mkdirSync(dirname(base), { recursive: true });
    const statePath = `${base}.deployment-cohort.${nonce}.json`;
    const candidate = ["--candidate-source-sha", options.candidateSourceSha, "--candidate-image", options.candidateImage, "--owned-disposable-candidate"];
    const fixtureArgs = ["--private-root", privateRoot, "--state-path", statePath, "--nonce", nonce, "--service-id", bootstrap.ownedArea.serviceId, "--credential-salt", salt, ...candidate];
    const originalPaths = {}, originals = {}, failures = []; let preparation, terminal;
    const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
    try {
      preparation = run("mcp_deployment_identity_fixture.py", ["prepare", ...fixtureArgs], { admin: options.adminCredential });
      for (const selector of Object.keys(clients)) {
        const selected = options.clients[selector], canonical = clients[selector], startedAt = timestamp();
        const value = { schema: "honua.mcp-deployment-workspace-client-observations/v2", nonce, canonicalClient: canonical,
          clientVersion: selected.clientVersion, targetId: options.targetId, identityPreparation: structuredClone(preparation), calls: [],
          authority: { schema: "honua.mcp-deployment-workspace-authority/v1", callsBefore: [], callsAfter: [], foreignWorkspacePrerequisite: null,
            personalWorkspacePrerequisite: null, storeBefore: null, personalStoreBefore: null, storeAfter: null, personalStoreAfter: null,
            limitedRead: null, runtimeBefore: null, runtimeAfter: null },
          semantic: { nonce, canonicalClient: canonical, clientVersion: selected.clientVersion, targetId: options.targetId, calls: [],
            workspacePrerequisite: null, storeBefore: null, storeAfter: null, proposalBefore: null, proposalAfter: null,
            runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null } };
        const credentials = {};
        try {
          for (const [role, profile] of [["primary", "admin"], ["foreign", "workspace-reader"]]) credentials[role] = readOwnedCredential(join(privateRoot, `${selector}-${role}.private.json`), preparation.keys[selector][`${role}Receipt`], salt, profile, bootstrap.ownedArea.serviceId);
          value.authority.runtimeBefore = inspect(options.candidateSourceSha, options.candidateImage);
          const authorityPhase = async phase => {
            if (phase === "after") value.authority.runtimeAfter = inspect(options.candidateSourceSha, options.candidateImage);
            for (let step = 0; step < 64; step++) {
              const action = judge(value, null, { phase });
              if (action.action === "complete") return;
              if (action.action === "invoke") {
                const entry = { ...action.call, wire: null, client: null, result: null, startedAt: timestamp(), completedAt: null };
                value.authority[phase === "before" ? "callsBefore" : "callsAfter"].push(entry);
                const credential = credentials[entry.credentialRole];
                const probe = await observeCandidateRequest({ method: entry.method, params: entry.params, authenticated: true, credential, credentialSalt: salt, tenantId: entry.tenantId,
                  invoke: endpoint => launchCanonicalClient({ installationRoot: selected.installationRoot, selector, endpoint, method: entry.method, params: entry.params, authenticated: true, credential, tenantId: entry.tenantId }),
                  onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
                entry.client = probe.client; const frame = observedResponse(probe.wire.invocation); entry.result = Object.hasOwn(frame, "error") ? frame : frame.result; entry.completedAt = timestamp();
              } else if (action.action === "create-workspace") {
                if (!["foreignWorkspacePrerequisite", "personalWorkspacePrerequisite"].includes(action.slot)) throw new Error("unknown owned workspace slot");
                value.authority[action.slot] = run("mcp_deployment_workspace_prerequisites.py", [...candidate, "--service-id", action.serviceId, "--label", action.label, "--credential-salt", salt,
                  ...(action.tenantId === undefined ? [] : ["--tenant-id", action.tenantId])], { credential: credentials[action.credentialRole], timeout: 180_000 });
              } else if (action.action === "observe-stores") {
                if (!["storeBefore", "personalStoreBefore", "storeAfter", "personalStoreAfter"].includes(action.slot)) throw new Error("unknown authority store slot");
                value.authority[action.slot] = run("mcp_deployment_workspace_state.py", [...candidate, "--target-id", action.targetId, "--deployment-id", action.deploymentId, "--workspace-id", action.workspaceId]);
              } else if (action.action === "limited-data-read") {
                value.authority.limitedRead = run("mcp_ops_events_data_read.py", ["--fixture", join(directory, "fixture.v1.json"), "--bootstrap", bootstrapPath, "--credential-salt", salt, ...candidate], { readCredential: credentials.foreign });
              } else throw new Error("unknown owned authority action");
            }
            throw new Error("bounded authority sequence exhausted");
          };
          await authorityPhase("before");
          const component = await semantic.deploymentWorkspaceScenarios(async (_operation, execute) => execute(), {
            candidateSourceSha: options.candidateSourceSha, candidateImage: options.candidateImage, canonicalClient: canonical,
            clientVersion: selected.clientVersion, installationRoot: selected.installationRoot, credential: credentials.primary,
            foreignCredential: credentials.foreign, targetId: options.targetId, ownedDisposableCandidate: true,
            results: `${base}.${selector}.${nonce}`, cohortNonce: nonce, cohortSalt: salt, tenantId: `mcp-deployment-${nonce}` });
          if (component?.semanticOnly !== true) throw new Error("complete original semantic component absent");
          value.semantic = component.observations; await authorityPhase("after");
        } catch { failures.push(selector); }
        finally {
          credentials.primary = null; credentials.foreign = null;
          value.calls = [...value.authority.callsBefore, ...value.semantic.calls, ...value.authority.callsAfter];
          const envelope = { schema: "honua.mcp-deployment-client-journey/v1", canonicalClient: canonical, cohortId: nonce,
            candidate: preparation.candidate, configurationSha256: preparation.roleReceipt.configurationSha256,
            startedAt, completedAt: timestamp(), observations: value };
          const path = `${base}.${selector}.${nonce}.original.json`;
          writeFileSync(path, `${JSON.stringify(envelope)}\n`, { mode: 0o600, flag: "wx" }); originalPaths[selector] = path; originals[selector] = value;
        }
      }
    } catch { failures.push("prepare-or-cohort"); }
    finally {
      try {
        terminal = run("mcp_deployment_identity_fixture.py", ["close", ...fixtureArgs,
          ...(originalPaths.sdk === undefined ? [] : ["--sdk-journey", originalPaths.sdk]),
          ...(originalPaths.inspector === undefined ? [] : ["--inspector-journey", originalPaths.inspector])], { admin: options.adminCredential });
      } catch { failures.push("owned-cleanup"); }
      // Never destroy private recovery journals after failed cleanup. An empty
      // successfully cleaned directory may be removed without recursive deletion.
      if (terminal?.cleanupComplete === true) { try { rmdirSync(privateRoot); } catch { failures.push("private-cleanup"); } }
      try {
        writeFileSync(`${base}.deployment-cohort.${nonce}.recovery.json`, `${JSON.stringify({
          schema: "honua.mcp-deployment-private-recovery/v1", statePath, privateRoot,
          privateRootRetained: existsSync(privateRoot), cleanupComplete: terminal?.cleanupComplete === true,
          failures: [...failures],
        })}\n`, { mode: 0o600, flag: "wx" });
      } catch { failures.push("public-recovery-receipt"); }
    }
    const proofs = {};
    for (const selector of Object.keys(clients)) {
      let proof;
      if (failures.length === 0) {
        try { proof = { ...judge(originals[selector], terminal), schema: "honua.mcp-deployment-workspace-proof/v2", helperSourceSha: sourceSha,
          helperSha256: `sha256:${sha256["mcp_deployment_workspace_cohort.py"]}`, observations: originals[selector], identityCohort: terminal }; } catch { failures.push(`${selector}-final-oracle`); }
      }
      proofs[selector] = proof;
    }
    // All original operation records are emitted only after BOTH full original
    // journeys and the shared owned cleanup have passed the independent oracle.
    for (const selector of Object.keys(clients)) for (const operation of operations) await records[selector](operation, async () => {
      if (failures.length || !proofs.sdk || !proofs.inspector) throw new Error("complete original two-client deployment/workspace proof absent; retain partial receipts");
      return proofs[selector];
    });
    return proofs;
  }
  function verifyDeploymentWorkspaceProof(proof, source, image, operation, canonical, version) {
    try {
      verifyBytes();
      if (!operations.includes(operation) || proof?.schema !== "honua.mcp-deployment-workspace-proof/v2" || proof.helperSourceSha !== sourceSha
          || proof.helperSha256 !== `sha256:${sha256["mcp_deployment_workspace_cohort.py"]}` || proof.candidateSourceSha !== source || proof.candidateImage !== image
          || proof.canonicalClient !== canonical || proof.clientVersion !== version || proof.semanticOnly !== false) return false;
      const expected = judge(proof.observations, proof.identityCohort);
      return isDeepStrictEqual(Object.fromEntries(Object.keys(expected).map(key => [key, proof[key]])), expected)
        && expected.originalTwoClientCohortJoined === true && expected.tenantOwnerIsolation === true && expected.sameTenantPersonalOwnerRefusal === true
        && expected.deploymentActuationProven === false && expected.runtimeHealthProven === false;
    } catch { return false; }
  }
  return { deploymentWorkspaceCohortScenarios, verifyDeploymentWorkspaceProof };
}
