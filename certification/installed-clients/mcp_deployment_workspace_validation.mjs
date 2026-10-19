// Actual canonical installed-client deployment/workspace journeys; release-owned.
import { createHash, randomBytes } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { isDeepStrictEqual } from "node:util";
import { pathToFileURL } from "node:url";
import { observeCandidateRequest, observedResponse } from "./mcp_candidate_wire.mjs";

export const deploymentWorkspaceDependencies = Object.freeze([
  "mcp_deployment_workspace_validation.py", "mcp_deployment_workspace_store_oracle.py", "mcp_deployment_workspace_state.py",
  "mcp_deployment_workspace_prerequisites.py", "mcp_execute_plan_state.py", "mcp_execute_plan_validation.py",
  "mcp_plan_validation.py", "mcp_style_validation.py", "mcp_ingest_publish_validation.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "mcp_style_state.py", "mcp_ingest_publish_state.py", "fixture.v1.json",
  "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs", "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json",
]);
const operations = ["tools/call:deploy_operations", "tools/call:propose_rollback", "resources/read:Deployment", "resources/read:Workspace"];
const immutable = value => typeof value === "string" && /^[a-f0-9]{40}$/.test(value);
const image = value => typeof value === "string" && /^[^\s]+@sha256:[a-f0-9]{64}$/.test(value);
const hash = bytes => createHash("sha256").update(bytes).digest("hex");

export function createDeploymentWorkspaceValidation({ directory, sourceSha, sha256, bootstrapPath }) {
  if (!immutable(sourceSha) || !sha256 || typeof sha256 !== "object"
      || deploymentWorkspaceDependencies.toSorted().join() !== Object.keys(sha256).toSorted().join()
      || typeof bootstrapPath !== "string" || !bootstrapPath) throw new Error("lifecycle requires complete immutable source/bootstrap");
  sha256 = Object.freeze({ ...sha256 });
  const verifyBytes = () => {
    for (const name of deploymentWorkspaceDependencies) if (!/^[a-f0-9]{64}$/.test(sha256[name] ?? "")
        || hash(readFileSync(join(directory, name))) !== sha256[name]) throw new Error("lifecycle source dependency changed");
  };
  verifyBytes();
  const run = (name, args, { timeout = 65_000, credential } = {}) => {
    verifyBytes();
    const result = spawnSync("python3", ["-I", "-B", join(directory, name), ...args], {
      encoding: "utf8", timeout, maxBuffer: 16 * 1024 * 1024,
      env: credential === undefined ? process.env : { ...process.env, HONUA_MCP_API_KEY: credential },
    });
    verifyBytes();
    if (result.error || result.signal || result.status !== 0) throw new Error("actual canonical lifecycle prerequisite/oracle refused evidence");
    return JSON.parse(result.stdout);
  };
  const judge = (observed, advance = false) => {
    const work = mkdtempSync(join(tmpdir(), "mcp-lifecycle-oracle-"));
    try {
      const path = join(work, "observations.json"); writeFileSync(path, JSON.stringify(observed));
      return run("mcp_deployment_workspace_validation.py", ["--fixture", join(directory, "fixture.v1.json"),
        "--bootstrap", bootstrapPath, "--observations", path, ...(advance ? ["--advance"] : [])], { timeout: 20_000 });
    } finally { rmSync(work, { recursive: true, force: true }); }
  };
  const inspect = (source, selectedImage) => run("mcp_render_validation.py", ["--inspect-runtime",
    "--candidate-source-sha", source, "--candidate-image", selectedImage]);

  async function deploymentWorkspaceScenarios(record, { candidateSourceSha, candidateImage, canonicalClient, clientVersion,
    installationRoot, credential, foreignCredential, targetId, ownedDisposableCandidate, results = "results.json" }) {
    let proof;
    await record(operations[0], async () => {
      if (ownedDisposableCandidate !== true || !immutable(candidateSourceSha) || !image(candidateImage)
          || !["Official MCP TypeScript SDK", "MCP Inspector"].includes(canonicalClient)
          || !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(clientVersion ?? "") || typeof installationRoot !== "string" || !installationRoot
          || typeof credential !== "string" || !credential || credential.length > 8192 || /[\r\n]/.test(credential)
          || typeof foreignCredential !== "string" || !foreignCredential || foreignCredential === credential || foreignCredential.length > 8192 || /[\r\n]/.test(foreignCredential)
          || typeof targetId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(targetId)) {
        throw new Error("lifecycle requires explicitly owned candidate, actual installed client and real configured target");
      }
      const observed = { nonce: randomBytes(6).toString("hex"), canonicalClient, clientVersion, targetId,
        calls: [], workspacePrerequisite: null, storeBefore: null, storeAfter: null,
        proposalBefore: null, proposalAfter: null, runtimeBefore: null, runtimeRestarted: null, runtimeAfter: null };
      const retained = `${resolve(results)}.deployment-workspace.raw.json`;
      mkdirSync(dirname(retained), { recursive: true });
      const credentialSalt = randomBytes(16).toString("hex");
      const candidate = ["--candidate-source-sha", candidateSourceSha, "--candidate-image", candidateImage, "--owned-disposable-candidate"];
      let completed = false;
      try {
        observed.runtimeBefore = inspect(candidateSourceSha, candidateImage);
        const { launchCanonicalClient } = await import(pathToFileURL(join(directory, "mcp_fixed_client_launcher.mjs")).href);
        for (let step = 0; step < 128; step++) {
          observed.runtimeAfter = inspect(candidateSourceSha, candidateImage);
          const action = judge(observed, true);
          if (action.action === "complete") {
            completed = true;
            break;
          }
          if (action.action === "invoke") {
            const entry = { ...action.call, wire: null, client: null, result: null }; observed.calls.push(entry);
            const selectedCredential = entry.credentialRole === "foreign" ? foreignCredential : credential;
            const probe = await observeCandidateRequest({ method: entry.method, params: entry.params,
              authenticated: entry.authenticated, credential: selectedCredential, credentialSalt,
              invoke: endpoint => launchCanonicalClient({ installationRoot, selector: canonicalClient === "MCP Inspector" ? "inspector" : "sdk",
                endpoint, method: entry.method, params: entry.params, authenticated: entry.authenticated, credential: selectedCredential }),
              onClient: client => { entry.client = client; }, onObservation: wire => { entry.wire = wire; } });
            entry.client = probe.client;
            const frame = observedResponse(probe.wire.invocation);
            entry.result = Object.hasOwn(frame, "error") ? frame : frame.result;
          } else if (action.action === "create-workspace") {
            observed.workspacePrerequisite = run("mcp_deployment_workspace_prerequisites.py",
              [...candidate, "--service-id", action.serviceId, "--label", action.label], { timeout: 180_000, credential });
          } else if (action.action === "observe-stores") {
            if (!["storeBefore", "storeAfter"].includes(action.slot)) throw new Error("unknown lifecycle store slot");
            observed[action.slot] = run("mcp_deployment_workspace_state.py", [...candidate,
              "--target-id", action.targetId, "--deployment-id", action.deploymentId, "--workspace-id", action.workspaceId]);
          } else if (action.action === "observe-proposal") {
            if (!["proposalBefore", "proposalAfter"].includes(action.slot)) throw new Error("unknown lifecycle proposal slot");
            observed[action.slot] = run("mcp_deployment_workspace_state.py", [...candidate, "--proposal-id", action.proposalId]);
          } else if (action.action === "restart") {
            if (observed.runtimeRestarted !== null) throw new Error("unexpected second lifecycle restart");
            observed.runtimeRestarted = run("mcp_deployment_workspace_state.py", [...candidate, "--restart"]);
          } else throw new Error("unknown lifecycle action");
        }
        if (!completed) throw new Error("bounded lifecycle action sequence exhausted");
      } finally {
        try { observed.runtimeAfter = inspect(candidateSourceSha, candidateImage); }
        finally { writeFileSync(retained, `${JSON.stringify(observed, null, 2)}\n`); }
      }
      proof = { ...judge(observed), schema: "honua.mcp-deployment-workspace-proof/v1", helperSourceSha: sourceSha,
        helperSha256: `sha256:${sha256["mcp_deployment_workspace_validation.py"]}`, observations: observed };
      return proof;
    });
    for (const operation of operations.slice(1)) await record(operation, async () => {
      if (!proof) throw new Error("actual complete primary lifecycle proof absent");
      return proof;
    });
    return proof;
  }

  function verifyDeploymentWorkspaceProof(proof, candidateSourceSha, candidateImage, operation, canonicalClient, clientVersion) {
    try {
      verifyBytes();
      if (!operations.includes(operation) || proof?.schema !== "honua.mcp-deployment-workspace-proof/v1"
          || proof.helperSourceSha !== sourceSha || proof.helperSha256 !== `sha256:${sha256["mcp_deployment_workspace_validation.py"]}`
          || proof.candidateSourceSha !== candidateSourceSha || proof.candidateImage !== candidateImage
          || proof.canonicalClient !== canonicalClient || proof.clientVersion !== clientVersion) return false;
      const expected = judge(proof.observations);
      return isDeepStrictEqual(Object.fromEntries(Object.keys(expected).map(key => [key, proof[key]])), expected)
        && expected.deploymentActuationProven === false && expected.runtimeHealthProven === false;
    } catch { return false; }
  }
  return { deploymentWorkspaceScenarios, verifyDeploymentWorkspaceProof };
}
