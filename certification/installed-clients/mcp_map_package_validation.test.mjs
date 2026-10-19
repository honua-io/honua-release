import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createMapPackageValidation } from "./mcp_map_package_validation.mjs";
import { observeCandidateRequest } from "./mcp_candidate_wire.mjs";
import { startControlledCandidate, controlledClient, controlCredential } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["mcp_map_package_validation.py", "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs"];
const sha256 = Object.fromEntries(names.map((name) => [name, createHash("sha256").update(readFileSync(join(original, name))).digest("hex")]));
const candidateSourceSha = "b".repeat(40), candidateImage = `ghcr.io/honua-io/honua-server@sha256:${"c".repeat(64)}`;
const operations = ["tools/call:create_map_package", "resources/read:Map package"];
const identity = { candidateSourceSha, candidateImage, canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1", credential: controlCredential };
const verify = (api, proof, source = candidateSourceSha, selectedImage = candidateImage, operation = operations[0]) =>
  api.verifyMapPackageProof(proof, source, selectedImage, operation, identity.canonicalClient, identity.clientVersion);

async function setup(options = {}) {
  const directory = mkdtempSync(join(tmpdir(), "release-map-package-controls-"));
  const built = spawnSync("python3", [join(original, "mcp_map_package_validation_controls.py"), "--write-controls", directory],
    { encoding: "utf8", timeout: 10_000 });
  assert.equal(built.status, 0, built.stderr);
  const context = { directory: original, sourceSha: "a".repeat(40), sha256, bootstrapPath: join(directory, "bootstrap.json") };
  const responses = JSON.parse(readFileSync(join(directory, "responses.json"))), reads = JSON.parse(readFileSync(join(directory, "reads.json")));
  const previousPath = process.env.PATH;
  process.env.PATH = directory + delimiter + previousPath;
  const candidate = await startControlledCandidate(responses, reads, options);
  return { directory, context, responses, reads, candidate, restore: async () => {
    await candidate.close(); process.env.PATH = previousPath; rmSync(directory, { recursive: true, force: true });
  } };
}

for (const mode of ["valid", "ignored-view", "wrong-readback", "negative-accepted", "anonymous-accepted", "tool-transport", "read-transport", "runtime-replaced", "reused-session", "anonymous-tool-forwarded-auth", "anonymous-tool-reused-session"]) {
  test(`release-owned map composition and fresh-resource callbacks: ${mode}`, async () => {
    const { directory, context, responses, reads, restore } = await setup({ reuseSessions: mode === "reused-session", reuseAnonymousSession: mode === "anonymous-tool-reused-session" });
    try {
      const api = createMapPackageValidation(context), tools = [], resourceCalls = [], checks = {};
      const results = join(directory, "result.json");
      if (mode === "ignored-view") {
        responses[0].structuredContent.package.initialView.bbox = [0, 0, 1, 1];
        responses[0].content[0].text = JSON.stringify(responses[0].structuredContent);
      }
      if (mode === "negative-accepted") responses[1].isError = false;
      if (mode === "wrong-readback") {
        const value = JSON.parse(reads[0].contents[0].text); value.packageId = "map_other"; reads[0].contents[0].text = JSON.stringify(value);
      }
      if (mode === "anonymous-accepted") reads[2].error.data.requiresReauthentication = false;
      await api.mapPackageScenarios(async (tool, arguments_, { endpoint, authenticated }) => {
        const index = tools.length; tools.push({ tool, arguments_ });
        if (mode === "tool-transport" && index === 2) throw new Error("controlled actual-client transport failure");
        return controlledClient(endpoint, "tools/call", { name: tool, arguments: arguments_ }, mode === "anonymous-tool-forwarded-auth" ? true : authenticated);
      }, async (uri, { authenticated, endpoint }) => {
        const index = resourceCalls.length; resourceCalls.push({ uri, authenticated });
        if (mode === "read-transport" && index === 1) throw new Error("controlled fresh client read failure");
        const result = await controlledClient(endpoint, "resources/read", { uri }, authenticated);
        if (index === 0 && mode === "runtime-replaced") {
          const path = join(directory, "runtime-control.json"), state = JSON.parse(readFileSync(path));
          state.imageReadback.sourceSha = "f".repeat(40); writeFileSync(path, JSON.stringify(state));
        }
        return result;
      }, async (name, run) => {
        try { checks[name] = { result: "pass", value: await run() }; }
        catch (error) { checks[name] = { result: "fail", error: String(error) }; }
      }, { ...identity, results });
      assert.deepEqual(Object.keys(checks), operations);
      assert.equal(tools.length, ["tool-transport", "read-transport"].includes(mode) ? 3 : 4);
      assert.ok(tools.every(({ tool }) => tool === "honua_create_map_package"));
      assert.deepEqual(tools[0].arguments_.initialView, { bbox: [-90, 0, -45, 60], crs: 4326 });
      assert.equal(tools[0].arguments_.sourceBindings[0].layerId, "3");
      const raw = JSON.parse(readFileSync(`${results}.map-package.raw.json`));
      assert.equal(raw.cases.length, ["tool-transport", "read-transport"].includes(mode) ? 3 : 4);
      assert.equal(raw.cases.filter((item) => item.result !== null).length, mode === "tool-transport" ? 2 : ["read-transport", "anonymous-tool-forwarded-auth"].includes(mode) ? 3 : 4);
      assert.equal(raw.reads.length, mode === "tool-transport" ? 0 : mode === "read-transport" ? 2 : 3);
      assert.equal(raw.reads.filter((item) => item.result !== null).length, mode === "tool-transport" ? 0 : mode === "read-transport" ? 1 : 3);
      if (mode === "anonymous-tool-reused-session") {
        const sessions = raw.cases.map((item) => item.wire.initialize.response.sessionSha256);
        assert.equal(new Set(sessions.slice(0, 3)).size, 3);
        assert.equal(sessions[3], sessions[0], "anonymous initialization deliberately reuses only the creator session");
        assert.match(checks[operations[0]].error, /oracle refused/);
      }
      if (mode === "anonymous-tool-forwarded-auth") assert.equal(raw.cases[3].wire.invalid, true);
      if (mode === "valid") {
        assert.deepEqual(resourceCalls, [
          { uri: "honua://map-packages/map_controlled", authenticated: true },
          { uri: "honua://map-packages/map_controlled-uncreated", authenticated: true },
          { uri: "honua://map-packages/map_controlled", authenticated: false }]);
        for (const operation of operations) {
          const proof = checks[operation].value;
          assert.equal(checks[operation].result, "pass");
          assert.equal(proof.anonymousToolRefusal, true);
          assert.equal(verify(api, { ...proof, anonymousToolRefusal: false }, candidateSourceSha, candidateImage, operation), false);
          assert.equal(verify(api, proof, candidateSourceSha, candidateImage, operation), true);
          for (const observation of [undefined, null, false, 0, "", [], {}]) {
            assert.equal(verify(api, { ...proof, observations: observation }, candidateSourceSha, candidateImage, operation), false);
          }
          assert.equal(verify(api, proof, "f".repeat(40), candidateImage, operation), false);
          assert.equal(verify(api, proof, candidateSourceSha, candidateImage.replace(/c/g, "d"), operation), false);
          assert.equal(verify(api, proof, candidateSourceSha, candidateImage, "tools/list"), false);
          assert.equal(api.verifyMapPackageProof({ ...proof, canonicalClient: "MCP Inspector" }, candidateSourceSha,
            candidateImage, operation, "MCP Inspector", identity.clientVersion), false);
          assert.equal(api.verifyMapPackageProof({ ...proof, clientVersion: "1.32.2" }, candidateSourceSha,
            candidateImage, operation, identity.canonicalClient, "1.32.2"), false);
        }
      } else {
        assert.ok(operations.every((operation) => checks[operation].result === "fail"));
        if (mode === "runtime-replaced") {
          assert.equal(raw.runtimeAfter.imageReadback.sourceSha, "f".repeat(40));
          assert.equal(raw.runtimeAfter.inspectionRefused, true);
        }
      }
    } finally { await restore(); }
  });
}

test("valid proof before dependency mutation refuses rejudge and prevents any callbacks", async () => {
  for (const changed of names) {
    const { directory, context, restore } = await setup();
    try {
      for (const name of names) copyFileSync(join(original, name), join(directory, name));
      const api = createMapPackageValidation({ ...context, directory });
      let call = 0, read = 0, proof;
      await api.mapPackageScenarios(async (tool, arguments_, { endpoint, authenticated }) => {
        call += 1; return controlledClient(endpoint, "tools/call", { name: tool, arguments: arguments_ }, authenticated);
      }, async (uri, { endpoint, authenticated }) => {
        read += 1; return controlledClient(endpoint, "resources/read", { uri }, authenticated);
      }, async (_name, run) => { proof = await run(); }, { ...identity, results: join(directory, "before.json") });
      assert.equal(verify(api, proof), true);
      writeFileSync(join(directory, changed), "untrusted changed helper/fixture bytes\n");
      assert.equal(verify(api, proof), false, changed);
      call = 0; read = 0;
      const errors = [];
      await api.mapPackageScenarios(async () => { call += 1; }, async () => { read += 1; },
        async (_name, run) => { try { await run(); } catch (error) { errors.push(String(error)); } },
        { ...identity, results: join(directory, "after.json") });
      assert.equal(call, 0); assert.equal(read, 0);
      assert.match(errors[0], /dependency bytes differ from reviewed pins/);
    } finally { await restore(); }
  }
});

for (const mode of ["valid", "missing-observation", "wrong-uri", "wrong-method", "wrong-session", "wrong-response-id", "nested-response-id", "anonymous-inherited-auth"]) {
  test(`transparent candidate observer binds actual request/session/response: ${mode}`, async () => {
    const { candidate, restore } = await setup({ responseMutation: mode === "wrong-response-id" ? (frame) => ({ ...frame, id: 2 })
      : mode === "nested-response-id" ? (frame) => ({ ...frame, id: { secret: "row" } }) : undefined });
    let wire, client;
    const uri = "honua://map-packages/map_controlled", authenticated = mode !== "anonymous-inherited-auth";
    try {
      let result, error;
      try {
        result = await observeCandidateRequest({ method: "resources/read", params: { uri }, authenticated, credential: controlCredential, credentialSalt: "a".repeat(32),
          invoke: (endpoint) => mode === "missing-observation" ? { noRequestWasSent: true }
            : controlledClient(endpoint, mode === "wrong-method" ? "resources/list" : "resources/read",
              { uri: mode === "wrong-uri" ? uri + "-other" : uri }, authenticated,
              { wrongSession: mode === "wrong-session", inheritedAuthorization: mode === "anonymous-inherited-auth" }),
          onObservation: (observed) => { wire = observed; }, onClient: (outcome) => { client = outcome; } });
      } catch (failure) { error = failure; }
      assert.equal(Boolean(error), mode !== "valid");
      assert.ok(wire, "partial observation must survive refusal");
      if (mode === "missing-observation") assert.deepEqual(client, { noRequestWasSent: true });
      const serialized = JSON.stringify(wire);
      assert.equal(serialized.includes(controlCredential), false);
      assert.equal(serialized.includes("controlled-inherited-token"), false);
      assert.equal(serialized.includes("controlled-session-"), false);
      if (mode === "valid") {
        assert.equal(result.wire.completed, true);
        assert.equal(result.wire.invocation.request.params.uri, uri);
        assert.equal(result.wire.invocation.request.sessionSha256, result.wire.initialize.response.sessionSha256);
        assert.equal(candidate.seen.length, 1);
      } else if (["missing-observation", "wrong-uri", "anonymous-inherited-auth"].includes(mode)) {
        assert.equal(candidate.seen.length, 0, "no unrelated read can be credited");
      }
    } finally { await restore(); }
  });
}
