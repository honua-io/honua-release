import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { createIngestPublishValidation } from "./mcp_ingest_publish_validation.mjs";
import { observeCandidateRequest } from "./mcp_candidate_wire.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const names = ["mcp_ingest_publish_validation.py", "mcp_ingest_publish_state.py", "mcp_map_package_validation.py",
  "mcp_render_validation.py", "render_png.py", "fixture.v1.json", "mcp_candidate_wire.mjs", "mcp_fixed_client_launcher.mjs",
  "mcp_fixed_sdk_runner.mjs", "mcp_fixed_client_manifest.v1.json"];
const operations = ["tools/call:ingest_dataset", "tools/call:publish_service", "resources/read:Published service"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "release-ingest-judge-controls-"));
  const generated = spawnSync("python3", ["-B", "-c", "import json; from mcp_ingest_publish_validation_controls import controls, bootstrap; print(json.dumps({'observations':controls(),'bootstrap':bootstrap()}))"],
    { cwd: original, encoding: "utf8", timeout: 15_000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout);
  for (const name of names) copyFileSync(join(original, name), join(directory, name));
  const bootstrapPath = join(directory, "bootstrap.json"); writeFileSync(bootstrapPath, JSON.stringify(fixture.bootstrap));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(names.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observed = join(directory, "controlled.json"); writeFileSync(observed, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_ingest_publish_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observed], { encoding: "utf8", timeout: 15_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-ingest-publish-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_ingest_publish_validation.py"]}`, observations: fixture.observations };
  const api = createIngestPublishValidation(context);
  const verify = (value = proof, source = proof.candidateSourceSha, image = proof.candidateImage, operation = operations[0]) =>
    api.verifyIngestPublishProof(value, source, image, operation, proof.canonicalClient, proof.clientVersion);
  return { directory, context, proof, api, verify, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("all three scoped proofs require actual authored persisted/restart observations", () => {
  const control = setup();
  try {
    for (const operation of operations) assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, operation), true);
    const changed = structuredClone(control.proof); changed.observations.snapshots.after.rows[0].geometry.coordinates.reverse();
    assert.equal(control.verify(changed), false);
    assert.equal(control.verify(control.proof, "f".repeat(40)), false);
    assert.equal(control.verify(control.proof, control.proof.candidateSourceSha, control.proof.candidateImage, "tools/call:publish_result"), false);
    for (const value of [null, [], {}, { ...control.proof, observations: null }, { ...control.proof, restartReadback: false }, { ...control.proof, queuedPublishExercised: true }]) assert.equal(control.verify(value), false);
  } finally { control.close(); }
});

for (const name of ["mcp_ingest_publish_validation.py", "mcp_ingest_publish_state.py", "fixture.v1.json", "mcp_fixed_client_launcher.mjs"]) {
  test(`valid captured proof cannot rejudge after dependency mutation: ${name}`, () => {
    const control = setup();
    try {
      assert.equal(control.verify(), true);
      writeFileSync(join(control.directory, name), "untrusted changed bytes");
      assert.equal(control.verify(), false);
      assert.throws(() => createIngestPublishValidation(control.context), /dependency changed/);
    } finally { control.close(); }
  });
}

test("execution refuses absent disposable ownership before any installed client launch", async () => {
  const control = setup();
  try {
    const entries = [];
    const record = async (operation, run) => { try { await run(); entries.push([operation, "pass"]); } catch (error) { entries.push([operation, error.message]); } };
    await control.api.ingestPublishScenarios(record, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion, installationRoot: "/nonexistent",
      credential: "controlled-private-credential", ownedDisposableCandidate: false });
    assert.equal(entries.length, 3);
    assert.match(entries[0][1], /explicitly owned disposable candidate/);
    assert.ok(entries.every(([, status]) => status !== "pass"));
  } finally { control.close(); }
});

// Explicit offline forwarding controls. This callback is not a canonical-client
// producer and cannot create any client, tool or candidate qualification receipt.
for (const count of [32, 33]) test(`catalog forwarding keeps a finite ${count}-page boundary`, async () => {
  let forwardedPages = 0, targetCalls = 0, wire;
  const peer = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const message = JSON.parse(Buffer.concat(chunks));
    let result;
    if (message.method === "initialize") {
      response.setHeader("mcp-session-id", "explicit-offline-catalog-session");
      result = { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "offline", version: "0" } };
    } else if (message.method === "tools/list") {
      const index = forwardedPages++;
      assert.equal(message.params.cursor, index ? `page-${index}` : undefined);
      result = { tools: [{ name: index === count - 1 ? "honua_ingest_dataset" : `other-${index}`, inputSchema: { type: "object" } }],
        ...(index < count - 1 ? { nextCursor: `page-${index + 1}` } : {}) };
    } else { targetCalls++; result = { content: [{ type: "text", text: "explicit offline forwarding only" }] }; }
    response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }));
  });
  await new Promise((resolve, reject) => { peer.once("error", reject); peer.listen(8080, "127.0.0.1", resolve); });
  try {
    const params = { name: "honua_ingest_dataset", arguments: { explicitOfflineControl: true } };
    const execute = () => observeCandidateRequest({ method: "tools/call", params, authenticated: true,
      credential: "explicit-offline-key", credentialSalt: "a".repeat(32), observeDiscovery: true,
      onObservation: value => { wire = value; }, invoke: async endpoint => {
        let nextId = 0, session;
        const send = async (method, params) => {
          const response = await fetch(endpoint, { method: "POST", headers: { "content-type": "application/json", "x-api-key": "explicit-offline-key",
            ...(session ? { "mcp-session-id": session } : {}) }, body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }) });
          session ??= response.headers.get("mcp-session-id");
          if (response.status !== 200) return null;
          return (await response.json()).result;
        };
        await send("initialize", {});
        let cursor;
        for (let index = 0; index < count; index++) {
          const page = await send("tools/list", { _meta: { "honua.io/workflow-view": "full" }, ...(cursor ? { cursor } : {}) });
          if (!page) return { processId: 123, kind: "explicit-offline-control", exitCode: 1 };
          cursor = page.nextCursor;
        }
        await send("tools/call", params);
        return { processId: 123, kind: "explicit-offline-control", exitCode: 0 };
      } });
    if (count === 32) { const result = await execute(); assert.equal(result.wire.completed, true); }
    else await assert.rejects(execute, /wire observation refused/);
    assert.equal(forwardedPages, 32);
    assert.equal(targetCalls, count === 32 ? 1 : 0);
    assert.equal(wire.discovery.length, 32);
    assert.equal(wire.invalid, count === 33);
  } finally { peer.closeAllConnections(); await new Promise(resolve => peer.close(resolve)); }
});
