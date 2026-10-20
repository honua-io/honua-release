// Controlled HTTP/API process for offline factory tests. No Honua image,
// published client, Redis service, model or candidate is executed here.
import assert from "node:assert/strict";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { readFileSync, writeFileSync, renameSync } from "node:fs";
import { createServer } from "node:http";

const [statePath, mode] = process.argv.slice(2);
const state = JSON.parse(readFileSync(statePath, "utf8"));
const sessions = new Map();
const save = () => {
  writeFileSync(statePath + ".next", JSON.stringify(state));
  renameSync(statePath + ".next", statePath);
};
const metadata = entry => ({ id: entry.record.Id, name: entry.record.Name,
  permissions: entry.record.Permissions, status: entry.record.RevokedAt ? "revoked" : "active" });
const server = createServer(async (request, response) => {
  const reply = (status, value, headers = {}) => {
    response.writeHead(status, { "content-type": "application/json", ...headers });
    response.end(JSON.stringify(value));
  };
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const body = raw.length ? JSON.parse(raw) : undefined;
    const path = request.url, key = request.headers["x-api-key"];
    if (path.startsWith("/api/v1/admin/api-keys/")) {
      assert.equal(key, "controlled-fixture-admin");
      if (request.method === "POST" && path === "/api/v1/admin/api-keys/") {
        assert.deepEqual(Object.keys(body).sort(), ["expiresAt", "name", "permissions"]);
        assert.match(body.name, /^mcp-owned-read-[a-f0-9]{24}$/);
        assert.ok(JSON.stringify(body.permissions) === '["admin"]' || JSON.stringify(body.permissions) === '["read:*"]');
        const id = randomUUID(), secret = "hnua_" + randomBytes(32).toString("base64url");
        const now = new Date().toISOString();
        const record = { Id: id, Name: body.name, KeyPrefix: secret.slice(0, 12), Permissions: body.permissions,
          KeyHash: createHash("sha256").update(secret).digest("base64"), CreatedAt: now, UpdatedAt: now,
          ExpiresAt: body.expiresAt, LastUsedAt: null, RevokedAt: null, RotatedAt: null, CreatedBy: "controlled-owner" };
        const entry = { key: secret, record, rawRecord: JSON.stringify(record) };
        state.keys[id] = entry;
        state.api.push({ method: request.method, action: "create", role: body.permissions[0] === "admin" ? "full" : "limited" });
        save();
        reply(201, { success: true, data: { key: secret, apiKey: metadata(entry) } });
        return;
      }
      const match = /^\/api\/v1\/admin\/api-keys\/([a-f0-9-]{36})\/(effective-permissions|revoke)$/.exec(path);
      assert.ok(match);
      const entry = state.keys[match[1]];
      assert.ok(entry);
      const role = entry.record.Permissions[0] === "admin" ? "full" : "limited";
      state.api.push({ method: request.method, action: match[2], role });
      if (match[2] === "revoke") {
        assert.equal(request.method, "POST");
        if (mode === "cleanup-failure" && role === "limited") {
          save(); reply(500, { success: false, error: "controlled normal revoke failure" }); return;
        }
        entry.record.RevokedAt = entry.record.UpdatedAt = new Date().toISOString();
        entry.rawRecord = JSON.stringify(entry.record);
      } else assert.equal(request.method, "GET");
      save();
      const data = metadata(entry);
      if (match[2] === "effective-permissions") data.canAuthenticate = entry.record.RevokedAt === null;
      reply(200, { success: true, data });
      return;
    }
    assert.equal(path, "/mcp");
    if (body.method === "initialize") {
      const session = mode === "reused-session" ? "controlled-reused-session" : "controlled-session-" + randomUUID();
      sessions.set(session, key);
      reply(200, { jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-06-18", capabilities: {},
        serverInfo: { name: "controlled-planning", version: "0.0.0" } } }, { "mcp-session-id": session });
      return;
    }
    if (mode !== "reused-session") assert.equal(sessions.get(request.headers["mcp-session-id"]), key);
    const expected = state.raw.calls[state.seen.length];
    assert.ok(expected, "extra controlled invocation");
    assert.equal(body.method, expected.method);
    assert.equal(body.params.name ?? body.params.uri, expected.params.name ?? expected.params.uri);
    if (expected.credentialRole === "anonymous") assert.equal(key, undefined);
    else {
      const entry = Object.values(state.keys).find(item => item.key === key);
      assert.ok(entry && entry.record.RevokedAt === null);
      assert.deepEqual(entry.record.Permissions, expected.credentialRole === "limited" ? ["read:*"] : ["admin"]);
    }
    state.seen.push({ id: expected.id, method: body.method, role: expected.credentialRole });
    save();
    const selected = structuredClone(expected.result);
    if (mode === "wrong-model" && expected.id === "matched-demo") {
      selected.structuredContent.engine = "live";
      selected.content[0].text = JSON.stringify(selected.structuredContent);
    }
    reply(200, { jsonrpc: "2.0", id: body.id, ...(selected.error ? { error: selected.error } : { result: selected }) });
  } catch {
    reply(400, { error: "controlled planning request refused" });
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
