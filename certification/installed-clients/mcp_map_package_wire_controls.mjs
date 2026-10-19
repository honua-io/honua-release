// Explicit offline HTTP/client fixtures for observer controls. Never fetched,
// imported or used by a canonical producer; no installed-client claim here.
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";

export const controlCredential = "controlled-private-key";
const hash = (value) => `sha256:${createHash("sha256").update(value).digest("hex")}`;

export async function startControlledCandidate(responses, reads, { responseMutation, reuseSessions = false, reuseAnonymousSession = false, toolTransport = false, readTransport = false, onRead, sessionPrefix = "controlled-session" } = {}) {
  const sessions = new Set();
  let sequence = 0, toolCall = 0, firstAuthenticatedSession;
  const seen = [];
  const server = createServer(async (incoming, outgoing) => {
    try {
      const bytes = [];
      for await (const chunk of incoming) bytes.push(chunk);
      const message = JSON.parse(Buffer.concat(bytes));
      if (message.method === "initialize") {
        const session = reuseAnonymousSession && incoming.headers["x-api-key"] === undefined ? firstAuthenticatedSession
          : `${sessionPrefix}-${reuseSessions ? "reused" : ++sequence}`;
        if (incoming.headers["x-api-key"] === controlCredential) firstAuthenticatedSession ??= session;
        sessions.add(session);
        outgoing.writeHead(200, { "content-type": "application/json", "mcp-session-id": session });
        outgoing.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {
          protocolVersion: "2025-06-18", capabilities: {}, serverInfo: { name: "controlled-candidate", version: "0.0.0" } } }));
        return;
      }
      const session = incoming.headers["mcp-session-id"];
      if (!sessions.has(session)) throw new Error("controlled unknown session");
      seen.push({ method: message.method, params: message.params, sessionSha256: hash(session), authenticated: incoming.headers["x-api-key"] === controlCredential });
      if ((toolTransport && message.method === "tools/call" && toolCall === 2) || (readTransport && message.method === "resources/read" && message.params.uri.endsWith("-uncreated"))) { incoming.destroy(); outgoing.destroy(); return; }
      if (message.method === "resources/read" && onRead) onRead();
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

// Alternate HTTP modules are controlled offline fixtures only. A canonical
// producer fetches the protected real manifest, which rejects these bytes.
export function prepareControlledInstallation(directory, manifestPath, { forwardedAnonymousAuth = false } = {}) {
  const root = join(directory, "node_modules", "@modelcontextprotocol", "sdk");
  const sources = {
    "package.json": JSON.stringify({ name: "@modelcontextprotocol/sdk", version: "1.32.1", type: "module" }),
    "dist/esm/types.js": `export class McpError extends Error { constructor(code,message,data){super(message);this.code=code;this.data=data;} }`,
    "dist/esm/client/index.js": `export class Client { constructor(info){this.info=info;} async connect(transport){this.transport=transport;await transport.initialize(this.info);} callTool(params){return this.transport.send("tools/call",params);} readResource(params){return this.transport.send("resources/read",params);} async close(){} }`,
    "dist/esm/client/streamableHttp.js": `import {McpError} from "../types.js";
      export class StreamableHTTPClientTransport {
        constructor(url,options){this.url=url;this.headers={"content-type":"application/json",...options.requestInit.headers};this.identifier=0;}
        async initialize(clientInfo){const r=await fetch(this.url,{method:"POST",headers:this.headers,body:JSON.stringify({jsonrpc:"2.0",id:this.identifier++,method:"initialize",params:{protocolVersion:"2025-06-18",capabilities:{},clientInfo}})});await r.json();this.sessionId=r.headers.get("mcp-session-id");}
        async send(method,params){const r=await fetch(this.url,{method:"POST",headers:{...this.headers,"mcp-session-id":this.sessionId},body:JSON.stringify({jsonrpc:"2.0",id:this.identifier++,method,params})});const frame=await r.json();if(frame.error)throw new McpError(frame.error.code,frame.error.message,frame.error.data);return frame.result;}
      }`,
  };
  if (forwardedAnonymousAuth) sources["dist/esm/client/streamableHttp.js"] = sources["dist/esm/client/streamableHttp.js"].replace('async send(method,params){', `async send(method,params){if(method==="tools/call"&&!this.headers["X-API-Key"])this.headers["X-API-Key"]="${controlCredential}";`);
  const manifest = JSON.parse(readFileSync(manifestPath));
  manifest.clients.sdk.integrity = "offline-control-not-published";
  manifest.clients.sdk.files = Object.fromEntries(Object.entries(sources).toSorted(([a],[b]) => a.localeCompare(b)).map(([name,source]) => {
    const path=join(root,name);mkdirSync(dirname(path),{recursive:true});writeFileSync(path,source);
    return [name,createHash("sha256").update(source).digest("hex")];
  }));
  // ASCII path order must match the oracle's canonical flat-map encoding.
  manifest.clients.sdk.files=Object.fromEntries(Object.entries(manifest.clients.sdk.files).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0));
  writeFileSync(manifestPath,JSON.stringify(manifest,null,2)+"\n");
  return join(directory,"node_modules");
}
