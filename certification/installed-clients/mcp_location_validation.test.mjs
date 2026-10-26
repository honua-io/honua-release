import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createLocationValidation, locationDependencies, locationOperations } from "./mcp_location_validation.mjs";
import { startControlledCandidate, controlCredential, prepareControlledInstallation } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const candidateSourceSha = "b".repeat(40), candidateImage = `ghcr.io/honua-io/honua-server@sha256:${"c".repeat(64)}`;
const identity = { candidateSourceSha, candidateImage, canonicalClient: "Official MCP TypeScript SDK", clientVersion: "1.32.1", credential: controlCredential };
const verify = (api, proof, operation) => api.verifyLocationProof(proof, candidateSourceSha, candidateImage, operation, identity.canonicalClient, identity.clientVersion);

async function setup(options = {}) {
  const directory = mkdtempSync(join(tmpdir(), "release-location-controls-"));
  try {
    const built = spawnSync("python3", [join(original, "mcp_location_validation_controls.py"), "--write-controls", directory],
      { encoding: "utf8", timeout: 15_000 });
    assert.equal(built.status, 0, built.stderr);
    for (const name of locationDependencies) copyFileSync(join(original, name), join(directory, name));
    const installationRoot = prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json"), options);
    const context = { directory, sourceSha: "a".repeat(40), sha256: Object.fromEntries(locationDependencies.map(name => [name,
      createHash("sha256").update(readFileSync(join(directory, name))).digest("hex")])), bootstrapPath: join(directory, "bootstrap.json") };
    const byOperation = JSON.parse(readFileSync(join(directory, "location-responses.json")));
    const responses = locationOperations.flatMap(operation => byOperation[operation]);
    const previousPath = process.env.PATH;
    process.env.PATH = directory + delimiter + previousPath;
    let candidate;
    try { candidate = await startControlledCandidate(responses, [], options); }
    catch (error) { process.env.PATH = previousPath; throw error; }
    return { directory, installationRoot, context, candidate, restore: async () => {
      await candidate.close(); process.env.PATH = previousPath; rmSync(directory, { recursive: true, force: true });
    } };
  } catch (error) { rmSync(directory, { recursive: true, force: true }); throw error; }
}

async function execute(api, directory, installationRoot) {
  const checks = {};
  await api.locationScenarios(async (operation, run) => {
    try { checks[operation] = { result: "pass", value: await run() }; }
    catch (error) { checks[operation] = { result: "fail", error: String(error) }; }
  }, { ...identity, installationRoot, results: join(directory, "results.json") });
  return checks;
}

test("actual offline HTTP/controlled installed process executes and independently rejudges each location path", async () => {
  const { directory, context, installationRoot, candidate, restore } = await setup();
  try {
    const api = createLocationValidation(context), checks = await execute(api, directory, installationRoot);
    for (const operation of locationOperations) {
      const check = checks[operation]; assert.equal(check.result, "pass", check.error);
      const proof = check.value;
      assert.equal(verify(api, proof, operation), true);
      assert.equal(verify(api, { ...proof, observations: null }, operation), false);
      for (const flag of ["positive", "negative", "mediaSchema", "coordinateSemantics", "limitSemantics", "anonymousInvocationRefusal"])
        assert.equal(verify(api, { ...proof, [flag]: false }, operation), false);
      assert.equal(verify(api, { ...proof, locationFixtureSha256: "sha256:" + "f".repeat(64) }, operation), false);
      assert.equal(api.verifyLocationProof(proof, "f".repeat(40), candidateImage, operation, identity.canonicalClient, identity.clientVersion), false);
      assert.equal(api.verifyLocationProof(proof, candidateSourceSha, candidateImage.replace(/c/g, "d"), operation, identity.canonicalClient, identity.clientVersion), false);
      assert.equal(api.verifyLocationProof(proof, candidateSourceSha, candidateImage, operation, "MCP Inspector", "2.10.1"), false);
      assert.equal(verify(api, proof, "tools/list"), false);
      const raw = JSON.parse(readFileSync(`${join(directory, "results.json")}.location-${operation.split(":")[1]}.raw.json`));
      assert.equal(raw.operation, operation);
      assert.equal(raw.cases.at(-1).authenticated, false);
      assert.ok(raw.cases.every(item => item.client.processId !== process.pid));
    }
    assert.equal(candidate.seen.length, Object.values(checks).reduce((sum, c) => sum + c.value.observations.cases.length, 0));
    // Every dependency is checked after valid proof and before further child
    // requests. Controlled temporary copies are never protected source files.
    for (const name of locationDependencies) {
      const path = join(directory, name), originalBytes = readFileSync(path), before = candidate.seen.length;
      writeFileSync(path, "controlled changed dependency\n");
      assert.equal(verify(api, checks[locationOperations[0]].value, locationOperations[0]), false, name);
      const refused = await execute(api, directory, installationRoot);
      assert.ok(Object.values(refused).every(check => check.result === "fail" && /dependency bytes differ/.test(check.error)));
      assert.equal(candidate.seen.length, before);
      writeFileSync(path, originalBytes);
    }
  } finally { await restore(); }
});

for (const mode of ["reused-session", "forwarded-anonymous-auth", "transport-failure"]) {
  test(`actual observer/process location refusal retains evidence: ${mode}`, async () => {
    const { directory, context, installationRoot, restore } = await setup({ reuseSessions: mode === "reused-session",
      forwardedAnonymousAuth: mode === "forwarded-anonymous-auth", toolTransport: mode === "transport-failure" });
    try {
      const checks = await execute(createLocationValidation(context), directory, installationRoot);
      assert.ok(Object.values(checks).every(check => check.result === "fail"), JSON.stringify(checks));
      for (const operation of locationOperations) {
        const raw = JSON.parse(readFileSync(`${join(directory, "results.json")}.location-${operation.split(":")[1]}.raw.json`));
        assert.equal(raw.operation, operation); assert.ok(raw.cases.length > 0);
      }
    } finally { await restore(); }
  });
}

test("protected real installed-client manifest rejects alternate controlled package before any request", async () => {
  const { directory, context, installationRoot, candidate, restore } = await setup();
  try {
    const name = "mcp_fixed_client_manifest.v1.json";
    copyFileSync(join(original, name), join(directory, name));
    context.sha256[name] = createHash("sha256").update(readFileSync(join(directory, name))).digest("hex");
    const checks = await execute(createLocationValidation(context), directory, installationRoot);
    assert.ok(Object.values(checks).every(check => check.result === "fail" && /package bytes differ/.test(check.error)));
    assert.equal(candidate.seen.length, 0);
  } finally { await restore(); }
});

// D3 observer controls: discovery-only anonymous probe evidence. An explicit
// offline peer and in-process client only; never installed-client credit.
async function discoveryOnlyProbe({ tool = "honua_solve_route", listed = [], pageStatus = 200, client = {}, batchCall = false,
  callAnyway = false, neverCall = false, options = {} } = {}) {
  const { observeCandidateRequest } = await import("./mcp_candidate_wire.mjs");
  const { createServer } = await import("node:http");
  const names = Array.from({ length: 12 }, (_, index) => `honua_default_${index}`).concat(listed);
  const pages = [names.slice(0, 6), names.slice(6)];
  const seen = [];
  const peer = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const message = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null;
    seen.push(Array.isArray(message) ? "batch" : message?.method ?? request.method);
    if (Array.isArray(message) || !message?.id && message?.id !== 0) { response.writeHead(202).end(); return; }
    let result;
    if (message.method === "initialize") {
      response.setHeader("mcp-session-id", "explicit-offline-unlisted-session");
      result = { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "offline", version: "0" } };
    } else if (message.method === "tools/list") {
      const index = message.params?.cursor ? Number(message.params.cursor.slice(5)) : 0;
      if (index === 1 && pageStatus !== 200) { response.writeHead(pageStatus, { "content-type": "application/json" }).end("{}"); return; }
      result = { tools: pages[index].map(name => ({ name, inputSchema: { type: "object" } })), ...(index === 0 ? { nextCursor: "page-1" } : {}) };
    } else result = { isError: true, structuredContent: { status: "error" }, content: [{ type: "text", text: "{\"status\":\"error\"}" }] };
    response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }));
  });
  await new Promise((resolve, reject) => { peer.once("error", reject); peer.listen(8080, "127.0.0.1", resolve); });
  let wire;
  const params = { name: tool, arguments: { explicitOfflineControl: true } };
  try {
    const outcome = await observeCandidateRequest({ method: "tools/call", params, authenticated: false, credential: "explicit-offline-key",
      credentialSalt: "a".repeat(32), observeDiscovery: true, anonymousUnlistedRefusal: true, ...options,
      onObservation: value => { wire = value; }, invoke: async endpoint => {
        let nextId = 0, session;
        const send = async (method, selected, notification = false) => {
          const body = notification ? { jsonrpc: "2.0", method } : { jsonrpc: "2.0", id: nextId++, method, ...(selected ? { params: selected } : {}) };
          const response = await fetch(endpoint, { method: "POST", headers: { "content-type": "application/json",
            ...(session ? { "mcp-session-id": session } : {}) }, body: JSON.stringify(batchCall && method === "tools/call" ? [body] : body) });
          session ??= response.headers.get("mcp-session-id");
          return response.status === 200 ? (await response.json()).result : null;
        };
        await send("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "explicit-offline", version: "0" } });
        await send("notifications/initialized", undefined, true);
        let page = await send("tools/list");
        const tools = [...(page?.tools ?? [])];
        if (page?.nextCursor) { page = await send("tools/list", { cursor: page.nextCursor }); tools.push(...(page?.tools ?? [])); }
        if (!neverCall && (callAnyway || batchCall || tools.some(item => item.name === tool))) await send("tools/call", params);
        return { processId: 4321, kind: "explicit-offline-control", exitCode: 5, exitSignal: null, timedOut: false, stdout: "",
          stderr: `${JSON.stringify({ error: { code: "tool_not_found", message: `Tool '${tool}' not found on server.` } })}\n`, ...client };
      } });
    return { outcome, wire, seen };
  } catch (error) { return { error, wire, seen }; }
  finally { peer.closeAllConnections(); await new Promise(resolve => peer.close(resolve)); }
}

test("D3 observer: anonymous discovery absence with exact Inspector tool_not_found completes without any tools/call", async () => {
  for (const tool of ["honua_geocode_addresses", "honua_solve_route"]) {
    const { outcome, error, seen } = await discoveryOnlyProbe({ tool });
    assert.equal(error, undefined, String(error));
    assert.equal(outcome.wire.completed, true); assert.equal(outcome.wire.invalid, false);
    assert.equal(outcome.wire.invocation, null);
    assert.equal(outcome.wire.refusalEvidence, "inspector-anonymous-unlisted");
    assert.equal(outcome.wire.discovery.length, 2);
    assert.equal(seen.includes("tools/call"), false);
  }
});

test("D3 observer: discovery absence refuses listed tool, failed page, wrong diagnostics, batch or sent call", async () => {
  const cases = {
    // A client that claims tool_not_found although the observed catalog
    // listed the tool (on the first or last page) is refused.
    "listed-not-called": { listed: ["honua_solve_route"], neverCall: true },
    "page-500": { pageStatus: 500 },
    "exit-1": { client: { exitCode: 1 } },
    "exit-0": { client: { exitCode: 0 } },
    "signal": { client: { exitSignal: "SIGTERM" } },
    "stdout": { client: { stdout: "{}" } },
    "other-code": { client: { stderr: JSON.stringify({ error: { code: "error", message: "Tool 'honua_solve_route' not found on server." } }) } },
    "other-tool": { client: { stderr: JSON.stringify({ error: { code: "tool_not_found", message: "Tool 'honua_geocode_addresses' not found on server." } }) } },
    "batched-call": { batchCall: true },
  };
  for (const [name, options] of Object.entries(cases)) {
    const { error } = await discoveryOnlyProbe(options);
    assert.match(String(error), /wire observation refused/, name);
  }
  // A listed tool is invoked: only the ordinary server-refusal path remains.
  const listed = await discoveryOnlyProbe({ listed: ["honua_solve_route"] });
  assert.equal(listed.error, undefined, String(listed.error));
  assert.notEqual(listed.outcome.wire.invocation, null);
  // An unlisted tool invoked anyway leaves the ordinary invocation path,
  // whose Python judge then requires the tool to be listed exactly once.
  const sent = await discoveryOnlyProbe({ callAnyway: true });
  assert.equal(sent.error, undefined, String(sent.error));
  assert.notEqual(sent.outcome.wire.invocation, null);
});

test("D3 observer: discovery-only evidence mode is refused for every other configuration", async () => {
  const { observeCandidateRequest } = await import("./mcp_candidate_wire.mjs");
  const base = { method: "tools/call", params: { name: "honua_solve_route", arguments: {} }, authenticated: false, credential: "explicit-offline-key",
    credentialSalt: "a".repeat(32), observeDiscovery: true, anonymousUnlistedRefusal: true, invoke: async () => { throw new Error("must not launch"); } };
  for (const [name, change] of Object.entries({
    "no-discovery": { observeDiscovery: false }, authenticated: { authenticated: true }, "resources-read": { method: "resources/read", params: { uri: "honua://x" } },
    "geocode_address": { params: { name: "honua_geocode_address", arguments: {} } },
    "create_map_package": { params: { name: "honua_create_map_package", arguments: {} } },
    "create_app_package": { params: { name: "honua_create_app_package", arguments: {} } },
    "non-boolean": { anonymousUnlistedRefusal: "yes" } })) {
    await assert.rejects(observeCandidateRequest({ ...base, ...change }), /limited to observed anonymous discovery|fixed owned request/, name);
  }
  // Without the declared mode the same discovery-only exchange is refused.
  const { error } = await discoveryOnlyProbe({ options: { anonymousUnlistedRefusal: false } });
  assert.match(String(error), /wire observation refused/);
});
