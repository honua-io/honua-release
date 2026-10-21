// Offline controlled HTTP process. This is not a Honua runtime or candidate.
import assert from "node:assert/strict";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";

const [statePath, seenPath, mode] = process.argv.slice(2);
const expected = JSON.parse(readFileSync(statePath, "utf8")).calls;
const sessions = new Map();
let ordinal = 0;
writeFileSync(seenPath, "");
const seen = value => appendFileSync(seenPath, JSON.stringify(value) + "\n");
const server = createServer(async (incoming, outgoing) => {
  try {
    assert.equal(incoming.url, "/mcp");
    const chunks = [];
    for await (const chunk of incoming) chunks.push(chunk);
    const message = JSON.parse(Buffer.concat(chunks));
    const key = incoming.headers["x-api-key"];
    if (message.method === "initialize") {
      const session = mode === "reused-session" ? "controlled-reused-session" : `controlled-session-${sessions.size + 1}`;
      sessions.set(session, key);
      outgoing.writeHead(200, { "content-type": "application/json", "mcp-session-id": session });
      outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {
        protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "controlled", version: "0.0.0" } } }));
      return;
    }
    const item = expected[ordinal++];
    assert.ok(item, "extra MCP request");
    assert.equal(message.method, item.method);
    assert.equal(message.params.name ?? message.params.uri, item.params.name ?? item.params.uri);
    assert.deepEqual(message.params.arguments ?? null, item.params.arguments ?? null);
    assert.equal(key, item.authenticated
      ? item.expectation === "permission_denied" ? "controlled-limited-read-key" : "controlled-full-key"
      : undefined);
    if (mode !== "reused-session") assert.equal(sessions.get(incoming.headers["mcp-session-id"]), key);
    seen({ id: item.id, method: item.method, authenticated: item.authenticated,
      session: incoming.headers["mcp-session-id"] });
    const chosen = mode === "wrong-status" && item.id === "status" ? expected[1].result : item.result;
    outgoing.writeHead(200, { "content-type": "application/json" });
    outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id,
      ...(chosen.error ? { error: chosen.error } : { result: chosen }) }));
  } catch {
    outgoing.writeHead(400, { "content-type": "application/json" });
    outgoing.end(JSON.stringify({ error: "controlled grounding refusal" }));
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
