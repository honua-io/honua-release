// Fixed release-owned SDK entry. Inputs are private stdin, never process argv.
import { createHash } from "node:crypto";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

try {
  const chunks = []; let length = 0;
  for await (const chunk of process.stdin) { length += chunk.length; if (length > 2 * 1024 * 1024) throw new Error("input oversized"); chunks.push(chunk); }
  const { packageRoot, endpoint, method, params, authenticated, credential, tenantId } = JSON.parse(Buffer.concat(chunks));
  if (tenantId !== undefined && (!authenticated || typeof tenantId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(tenantId))) throw new Error("tenant selection refused");
  const { Client } = await import(pathToFileURL(join(packageRoot, "dist/esm/client/index.js")).href);
  const { StreamableHTTPClientTransport } = await import(pathToFileURL(join(packageRoot, "dist/esm/client/streamableHttp.js")).href);
  const { McpError } = await import(pathToFileURL(join(packageRoot, "dist/esm/types.js")).href);
  const client = new Client({ name: "geospatial-mcp-map-package-certification", version: "1.0.0" });
  const transport = new StreamableHTTPClientTransport(new URL(endpoint), { requestInit: { headers: authenticated ? { "X-API-Key": credential, ...(tenantId === undefined ? {} : { "X-Honua-Tenant": tenantId }) } : {} } });
  try {
    await client.connect(transport);
    let output;
    try {
      if (method === "tools/call") output = await client.callTool(params);
      else if (method === "resources/read") output = await client.readResource(params);
      else throw new Error("unsupported method");
    } catch (error) {
      if (!(error instanceof McpError)) throw error;
      output = { error: { code: error.code, message: error.message, data: error.data } };
    }
    if (typeof transport.sessionId !== "string" || !transport.sessionId) throw new Error("missing session");
    process.stdout.write(JSON.stringify({ processId: process.pid,
      sessionSha256: `sha256:${createHash("sha256").update(transport.sessionId).digest("hex")}`, output }));
  } finally { await client.close(); }
} catch { process.stderr.write("fixed SDK invocation failed\n"); process.exitCode = 1; }
