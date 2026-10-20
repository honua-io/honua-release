// A separate controlled HTTP process keeps the candidate socket responsive
// while the real producer synchronously launches its Python observers.
// Responses are authored offline vectors; this never qualifies a candidate.
import assert from "node:assert/strict";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";

const [statePath, fixturePath, seenPath, mode] = process.argv.slice(2);
const state = JSON.parse(readFileSync(statePath, "utf8"));
const calls = state.raw.calls;
const expectedCount = JSON.parse(readFileSync(fixturePath, "utf8")).area.features.length;
const fullKey = "controlled-full-key";
const limitedKey = "controlled-limited-read-key";
const sessions = new Map();
let ordinal = 0;
writeFileSync(seenPath, "");
const seen = value => appendFileSync(seenPath, JSON.stringify(value) + "\n");

const server = createServer(async (incoming, outgoing) => {
  try {
    const key = incoming.headers["x-api-key"];
    if (incoming.method === "GET") {
      assert.equal(key, limitedKey);
      assert.match(incoming.url, /\/FeatureServer\/\d+\/query\?/);
      seen({ method: "GET", path: incoming.url, key });
      if (mode === "missing-read") {
        outgoing.writeHead(404, { "content-type": "application/json" });
        outgoing.end(JSON.stringify({ error: "controlled missing owned data" }));
        return;
      }
      outgoing.writeHead(200, { "content-type": "application/json" });
      outgoing.end(JSON.stringify({ count: mode === "zero-read" ? 0 : expectedCount, exceededTransferLimit: false }));
      return;
    }
    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    const message = JSON.parse(Buffer.concat(chunks));
    if (message.method === "initialize") {
      const session = mode === "reused-session" ? "offline-reused-session" : "offline-session-" + String(sessions.size + 1);
      sessions.set(session, key);
      outgoing.writeHead(200, { "content-type": "application/json", "mcp-session-id": session });
      outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {
        protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "controlled-ops", version: "0.0.0" } } }));
      return;
    }
    assert.equal(incoming.url, "/mcp");
    if (mode !== "reused-session") assert.equal(sessions.get(incoming.headers["mcp-session-id"]), key);
    const expected = calls[ordinal++];
    assert.ok(expected, "extra candidate request");
    assert.equal(message.method, expected.method);
    assert.equal(message.params.name ?? message.params.uri, expected.params.name ?? expected.params.uri);
    const expectedKey = expected.credentialRole === "anonymous" ? undefined
      : expected.credentialRole === "limited" ? limitedKey : fullKey;
    assert.equal(key, expectedKey);
    seen({ method: message.method, id: expected.id, key, session: incoming.headers["mcp-session-id"] });
    const selected = mode === "wrong-caller" && expected.id === "limited-ops_findings"
      ? calls[0].result : expected.result;
    const frame = selected.error ? { error: selected.error } : { result: selected };
    outgoing.writeHead(200, { "content-type": "application/json" });
    outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, ...frame }));
  } catch {
    outgoing.writeHead(400, { "content-type": "application/json" });
    outgoing.end(JSON.stringify({ error: "offline controlled refusal" }));
  }
});
server.once("error", () => process.exit(1));
server.listen(8080, "127.0.0.1", () => process.send?.("ready"));
process.on("message", async value => {
  if (value !== "stop") return;
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  process.exit(0);
});
