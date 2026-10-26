// Observe actual installed-client requests. Never manufacture an MCP request,
// response, server error or session. Only this probe's connections are owned.
import { createServer, request } from "node:http";
import { createHash, scryptSync } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { gunzipSync, inflateSync } from "node:zlib";

const LIMIT = 2 * 1024 * 1024;
// The real full catalog already exceeds eight 12-tool pages. Keep its
// observation bounded independently of the unchanged per/total byte limits.
const DISCOVERY_PAGES = 32;
const upstream = new URL("http://127.0.0.1:8080/mcp");
const hash = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
const id = (value) => (typeof value === "string" && value.length > 0 && value.length <= 256)
  || (typeof value === "number" && Number.isSafeInteger(value));
const credentialHash = (value, salt) => `scrypt:${salt}:${scryptSync(value, Buffer.from(salt, "hex"), 32).toString("hex")}`;
const auth = (headers, salt) => ({
  credentialPresent: typeof headers["x-api-key"] === "string" && headers["x-api-key"].length > 0,
  credentialFingerprint: typeof headers["x-api-key"] === "string" ? credentialHash(headers["x-api-key"], salt) : null,
  authorizationPresent: headers.authorization !== undefined,
  cookiePresent: headers.cookie !== undefined,
});
const session = (value) => typeof value === "string" && value ? hash(value) : null;
// Founder decision D3 (2026.1): on the MCP Inspector lane only, these two
// tools' anonymous refusal may be evidenced by their absence from the observed
// anonymous tools/list when the pinned Inspector then refuses locally
// (exit 5, tool_not_found) without sending tools/call. No other tool.
export const ANONYMOUS_UNLISTED_REFUSAL_TOOLS = Object.freeze(["honua_geocode_addresses", "honua_solve_route"]);
export const ANONYMOUS_UNLISTED_REFUSAL = "inspector-anonymous-unlisted";

export async function observeCandidateRequest({ method, params, authenticated, credential, credentialSalt, invoke, onObservation, onClient, observeDiscovery = false, tenantId, anonymousUnlistedRefusal = false }) {
  if (!["tools/call", "resources/read"].includes(method) || !params || typeof params !== "object"
      || typeof authenticated !== "boolean" || typeof credential !== "string" || !credential || typeof invoke !== "function"
      || typeof observeDiscovery !== "boolean" || typeof credentialSalt !== "string" || !/^[a-f0-9]{32}$/.test(credentialSalt)) {
    throw new Error("candidate observation requires a fixed owned request and expected credential");
  }
  if (typeof anonymousUnlistedRefusal !== "boolean" || (anonymousUnlistedRefusal && (!observeDiscovery || method !== "tools/call"
      || authenticated || tenantId !== undefined || !ANONYMOUS_UNLISTED_REFUSAL_TOOLS.includes(params.name)))) {
    throw new Error("anonymous unlisted refusal evidence is limited to observed anonymous discovery of its fixed tools");
  }
  if (tenantId !== undefined && (!authenticated || typeof tenantId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(tenantId))) throw new Error("candidate tenant selection refused");
  const observed = { schema: "honua.mcp-candidate-wire/v1", upstream: upstream.href,
    method, expectedParams: structuredClone(params), authenticated, expectedCredentialFingerprint: credentialHash(credential, credentialSalt),
    listener: null, initialize: null, invocation: null, invalid: false, completed: false };
  if (tenantId !== undefined) observed.expectedTenantId = tenantId;
  if (observeDiscovery) observed.discovery = [];
  if (anonymousUnlistedRefusal) observed.refusalEvidence = ANONYMOUS_UNLISTED_REFUSAL;
  const active = new Set();
  const sockets = new Set();
  let total = 0;
  let closing = false;
  const refusal = () => { observed.invalid = true; throw new Error("actual candidate wire observation refused"); };
  const retained = (body, message, incoming) => ({ request: {
    jsonrpc: message.jsonrpc, id: message.id, method: message.method,
    params: message.params, bodyBase64: body.toString("base64"), bodySha256: hash(body),
    sessionSha256: session(incoming.headers["mcp-session-id"]), ...auth(incoming.headers, credentialSalt),
    ...(tenantId === undefined ? {} : { tenantId: incoming.headers["x-honua-tenant"] }),
  }, response: null });
  const proxy = createServer(async (incoming, outgoing) => {
    let forwarded;
    try {
      if (incoming.url !== "/mcp" || !["GET", "POST", "DELETE"].includes(incoming.method)) refusal();
      const credentials = auth(incoming.headers, credentialSalt);
      // Bind the one supported tenant header on every request, including
      // initialization/discovery/cleanup; never accept unrequested authority.
      if (incoming.headers["x-honua-tenant"] !== tenantId
          || Object.keys(incoming.headers).some(name => (name.startsWith("x-honua-") && name !== "x-honua-tenant")
            || ["x-tenant-id", "tenant-id", "x-tenant"].includes(name))) refusal();
      if (credentials.authorizationPresent || credentials.cookiePresent
          || (authenticated ? incoming.headers["x-api-key"] !== credential : incoming.headers["x-api-key"] !== undefined)) refusal();
      const chunks = [];
      let length = 0;
      for await (const chunk of incoming) {
        if ((length += chunk.length) > LIMIT) refusal();
        chunks.push(chunk);
      }
      const body = Buffer.concat(chunks);
      const message = incoming.method === "POST" ? JSON.parse(body) : undefined;
      // Absence of tools/call is the evidence: a batch or non-object frame
      // could carry an unrecorded invocation, so refuse it in this mode.
      if (anonymousUnlistedRefusal && incoming.method === "POST"
          && (!message || typeof message !== "object" || Array.isArray(message) || typeof message.method !== "string")) refusal();
      let record;
      if (message?.method === "initialize") {
        if (observed.initialize || !id(message.id) || message.jsonrpc !== "2.0"
            || incoming.headers["mcp-session-id"] !== undefined) refusal();
        record = observed.initialize = retained(body, message, incoming);
      } else if (observeDiscovery && message?.method === "tools/list") {
        if (!observed.initialize || observed.invocation || !id(message.id) || message.jsonrpc !== "2.0" || observed.discovery.length >= DISCOVERY_PAGES) refusal();
        record = retained(body, message, incoming); observed.discovery.push(record);
      } else if (message?.method === method) {
        if (observed.invocation || !id(message.id) || message.jsonrpc !== "2.0" || !observed.initialize
            || (method === "resources/read" ? message.params?.uri !== params.uri
              : message.params?.name !== params.name || !isDeepStrictEqual(message.params?.arguments, params.arguments))) refusal();
        record = observed.invocation = retained(body, message, incoming);
      }
      const headers = { ...incoming.headers, host: upstream.host };
      for (const name of ["connection", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer", "proxy-authorization"]) delete headers[name];
      forwarded = request(upstream, { method: incoming.method, headers, timeout: 35_000 }, (response) => {
        if (response.statusCode >= 300 && response.statusCode < 400) {
          observed.invalid = true; response.destroy(); outgoing.writeHead(502).end(); return;
        }
        const capture = record ? { status: response.statusCode, contentType: response.headers["content-type"] ?? "",
          contentEncoding: response.headers["content-encoding"] ?? "", sessionSha256: session(response.headers["mcp-session-id"]),
          complete: false, delivered: false, bodyBase64: "", bodySha256: null } : undefined;
        if (record) record.response = capture;
        outgoing.writeHead(response.statusCode, response.headers);
        const bytes = []; let size = 0;
        response.on("data", (chunk) => {
          if (capture) {
            if ((size += chunk.length) > LIMIT || (total += chunk.length) > 4 * LIMIT) {
              observed.invalid = true; response.destroy(); outgoing.destroy(); return;
            }
            bytes.push(chunk);
          }
        });
        response.on("end", () => {
          if (capture) {
            const value = Buffer.concat(bytes); capture.bodyBase64 = value.toString("base64");
            capture.bodySha256 = hash(value); capture.complete = true;
          }
        });
        response.on("error", () => { if (!closing) observed.invalid = true; outgoing.destroy(); });
        outgoing.on("finish", () => { if (capture) capture.delivered = true; });
        response.pipe(outgoing);
      });
      active.add(forwarded);
      forwarded.once("close", () => active.delete(forwarded));
      forwarded.once("timeout", () => forwarded.destroy());
      forwarded.once("error", () => { if (!closing) observed.invalid = true; outgoing.destroy(); });
      outgoing.once("close", () => { if (!closing && !outgoing.writableFinished && record) observed.invalid = true; forwarded.destroy(); });
      forwarded.end(body);
    } catch {
      observed.invalid = true;
      forwarded?.destroy();
      if (!outgoing.headersSent) outgoing.writeHead(502).end(); else outgoing.destroy();
    }
  });
  proxy.on("connection", (socket) => { sockets.add(socket); socket.once("close", () => sockets.delete(socket)); });
  let timer;
  try {
    await new Promise((resolve, reject) => { proxy.once("error", reject); proxy.listen(0, "127.0.0.1", resolve); });
    const endpoint = `http://127.0.0.1:${proxy.address().port}/mcp`;
    observed.listener = endpoint;
    const client = await Promise.race([invoke(endpoint), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("candidate wire observation deadline exceeded")), 40_000);
    })]);
    // Keep real public CLI diagnostics even when it refuses locally and sends
    // no target request. Such diagnostics never replace missing wire proof.
    if (onClient) onClient(structuredClone(client));
    // Delivery is recorded by the forwarding response, not by caller strings.
    await new Promise((resolve) => setImmediate(resolve));
    const initialize = observed.initialize, invocation = observed.invocation;
    if (anonymousUnlistedRefusal && !invocation) {
      if (observed.invalid || !initialize?.response?.complete || !initialize.response.delivered
          || !initialize.response.sessionSha256 || !unlistedAbsence(observed, initialize, params.name, client)) refusal();
      observedResponse(initialize);
      observed.clientProcessId = client?.processId;
      observed.completed = true;
      return { wire: observed, client };
    }
    if (observed.invalid || !initialize?.response?.complete || !initialize.response.delivered
        || !invocation?.response?.complete || !invocation.response.delivered
        || !initialize.response.sessionSha256 || invocation.request.sessionSha256 !== initialize.response.sessionSha256
        || `${typeof initialize.request.id}:${initialize.request.id}` === `${typeof invocation.request.id}:${invocation.request.id}`) refusal();
    observedResponse(initialize);
    observedResponse(invocation);
    observed.clientProcessId = client?.processId;
    observed.completed = true;
    return { wire: observed, client };
  } finally {
    // The actual selected response must already be complete. Closing this
    // probe's optional long-lived SSE channel is cleanup, not a server error.
    closing = true;
    clearTimeout(timer);
    for (const forwarded of active) forwarded.destroy();
    for (const socket of sockets) socket.destroy();
    proxy.closeAllConnections();
    if (proxy.listening) await new Promise((resolve) => proxy.close(resolve));
    if (onObservation) onObservation(structuredClone(observed));
  }
}

// Every observed anonymous catalog page is a complete delivered 200 JSON-RPC
// result on the fresh session, the walk is complete, the tool is absent from
// the union, and the pinned Inspector reported exactly its local refusal.
function unlistedAbsence(observed, initialize, name, client) {
  try {
    const pages = observed.discovery;
    if (!Array.isArray(pages) || pages.length < 1) return false;
    const identifiers = new Set([`${typeof initialize.request.id}:${initialize.request.id}`]);
    const names = [];
    const cursors = new Set();
    let cursor;
    for (const [position, page] of pages.entries()) {
      const selected = page.request.params;
      const key = `${typeof page.request.id}:${page.request.id}`;
      if (identifiers.has(key) || page.request.sessionSha256 !== initialize.response.sessionSha256
          || page.request.credentialPresent || page.request.authorizationPresent || page.request.cookiePresent
          || (selected !== undefined && (!selected || typeof selected !== "object" || Array.isArray(selected)
            || Object.keys(selected).some(item => !["cursor", "_meta"].includes(item))
            || (selected._meta !== undefined && (!selected._meta || typeof selected._meta !== "object"
              || Object.keys(selected._meta).some(item => item.split("/", 1)[0] === "honua.io")))))
          || selected?.cursor !== cursor || (position === 0 && selected !== undefined && Object.hasOwn(selected, "cursor"))) return false;
      identifiers.add(key);
      const frame = observedResponse(page);
      const result = frame.result;
      if (!result || typeof result !== "object" || !Array.isArray(result.tools)
          || result.tools.some(tool => !tool || typeof tool !== "object" || typeof tool.name !== "string")) return false;
      names.push(...result.tools.map(tool => tool.name));
      cursor = result.nextCursor;
      if (cursor !== undefined && (typeof cursor !== "string" || !cursor || cursors.has(cursor) || position === pages.length - 1)) return false;
      cursors.add(cursor);
      if (cursor === undefined && position !== pages.length - 1) return false;
    }
    if (names.includes(name)) return false;
    const diagnostic = JSON.parse(client?.stderr ?? "");
    return client?.exitCode === 5 && client.exitSignal === null && client.timedOut === false && client.stdout === ""
      && isDeepStrictEqual(diagnostic, { error: { code: "tool_not_found", message: `Tool '${name}' not found on server.` } });
  } catch { return false; }
}

export function observedResponse(record) {
  const response = record?.response;
  if (!response?.complete || !response.delivered || response.status !== 200) throw new Error("candidate response is incomplete");
  let bytes = Buffer.from(response.bodyBase64, "base64");
  if (!bytes.length || bytes.length > LIMIT || hash(bytes) !== response.bodySha256) throw new Error("candidate response bytes differ");
  const encoding = response.contentEncoding.trim().toLowerCase();
  if (encoding === "gzip") bytes = gunzipSync(bytes, { maxOutputLength: LIMIT });
  else if (encoding === "deflate") bytes = inflateSync(bytes, { maxOutputLength: LIMIT });
  else if (encoding && encoding !== "identity") throw new Error("candidate response encoding is unsupported");
  const type = response.contentType.split(";", 1)[0].trim().toLowerCase();
  const frames = type === "application/json" ? [JSON.parse(bytes)] : type === "text/event-stream"
    ? bytes.toString("utf8").split(/\r?\n\r?\n/).map((event) => event.split(/\r?\n/)
      .filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n")).filter(Boolean).map((data) => JSON.parse(data)) : [];
  if (frames.length !== 1 || frames[0]?.jsonrpc !== "2.0" || !id(frames[0].id)
      || typeof frames[0].id !== typeof record.request.id || frames[0].id !== record.request.id
      || (Object.hasOwn(frames[0], "result") === Object.hasOwn(frames[0], "error"))) throw new Error("candidate response does not match the actual request");
  return frames[0];
}
