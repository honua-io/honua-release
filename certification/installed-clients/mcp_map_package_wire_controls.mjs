// Explicit offline HTTP/client fixtures for observer controls. Never fetched,
// imported or used by a canonical producer; no installed-client claim here.
import { createServer } from "node:http";
import { createHash } from "node:crypto";

export const controlCredential = "controlled-private-key";
const hash = (value) => `sha256:${createHash("sha256").update(value).digest("hex")}`;

export async function startControlledCandidate(responses, reads, { responseMutation, reuseSessions = false } = {}) {
  const sessions = new Set();
  let sequence = 0, toolCall = 0;
  const seen = [];
  const server = createServer(async (incoming, outgoing) => {
    try {
      const bytes = [];
      for await (const chunk of incoming) bytes.push(chunk);
      const message = JSON.parse(Buffer.concat(bytes));
      if (message.method === "initialize") {
        const session = `controlled-session-${reuseSessions ? "reused" : ++sequence}`;
        sessions.add(session);
        outgoing.writeHead(200, { "content-type": "application/json", "mcp-session-id": session });
        outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {
          protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "controlled-candidate", version: "0.0.0" } } }));
        return;
      }
      const session = incoming.headers["mcp-session-id"];
      if (!sessions.has(session)) throw new Error("controlled unknown session");
      seen.push({ method: message.method, params: message.params, sessionSha256: hash(session), authenticated: incoming.headers["x-api-key"] === controlCredential });
      let result;
      if (message.method === "tools/call") result = responses[toolCall++];
      else if (message.method === "resources/read") result = reads[incoming.headers["x-api-key"] === undefined ? 2 : message.params.uri.endsWith("-uncreated") ? 1 : 0];
      else result = { error: { code: -32601, message: "controlled unrelated method", data: { code: "not_found" } } };
      let frame = { jsonrpc: "2.0", ...(result?.error ? result : { result }), id: message.id };
      if (responseMutation) frame = responseMutation(frame);
      outgoing.writeHead(200, { "content-type": "application/json" }); outgoing.end(JSON.stringify(frame));
    } catch {
      outgoing.writeHead(400, { "content-type": "application/json" }); outgoing.end(JSON.stringify({ error: "controlled refusal" }));
    }
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(8080, "127.0.0.1", resolve); });
  return { seen, close: async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); } };
}

export async function controlledClient(endpoint, method, params, authenticated, { wrongSession, inheritedAuthorization } = {}) {
  const headers = { "content-type": "application/json", ...(authenticated ? { "x-api-key": controlCredential } : {}),
    ...(inheritedAuthorization ? { authorization: "Bearer controlled-inherited-token" } : {}) };
  const initial = await fetch(endpoint, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 0, method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "controlled-installed-client", version: "1.32.1" } } }) });
  await initial.json();
  const session = initial.headers.get("mcp-session-id");
  const response = await fetch(endpoint, { method: "POST", headers: { ...headers, "mcp-session-id": wrongSession ? "controlled-wrong-session" : session },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  const frame = await response.json();
  return { kind: "sdk", canonicalClient: "Official MCP TypeScript SDK", version: "1.32.1", processId: process.pid,
    requestUrl: endpoint, sessionSha256: hash(session ?? "missing"), output: frame.error ? { error: frame.error } : frame.result };
}
