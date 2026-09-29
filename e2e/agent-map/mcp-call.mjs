// Minimal MCP stdio client: node mcp-call.mjs <baseUrl> <tool> '<json args>' [<tool> '<json args>' ...]
import { spawn } from "node:child_process";
const [baseUrl, ...rest] = process.argv.slice(2);
const child = spawn("npx", ["-y", "-p", "@honua/mcp-server", "honua-mcp"], {
  env: { ...process.env, HONUA_BASE_URL: baseUrl, HONUA_TRANSPORT: "rest", HONUA_DOCS_CORPUS_PATH: process.env.HONUA_DOCS_CORPUS_PATH ?? "" },
  stdio: ["pipe", "pipe", "pipe"],
});
let buf = ""; const pending = new Map();
child.stdout.on("data", (d) => { buf += d; let i; while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (!line) continue; try { const m = JSON.parse(line); if (m.id !== undefined && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } } catch {} } });
child.stderr.on("data", (d) => { const s = d.toString(); if (!/npm warn|npx/i.test(s)) process.stderr.write("[server] " + s.slice(0, 400)); });
let id = 0;
const rpc = (method, params) => new Promise((res) => { const m = ++id; pending.set(m, res); child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: m, method, params }) + "\n"); });
await rpc("initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "claude-code-agent", version: "0" } });
child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
for (let k = 0; k < rest.length; k += 2) {
  const name = rest[k]; const args = JSON.parse(rest[k + 1] ?? "{}");
  const r = await rpc("tools/call", { name, arguments: args });
  console.log(`### ${name} ${JSON.stringify(args)}`);
  if (r.error) console.log("RPC ERROR", JSON.stringify(r.error));
  else for (const c of r.result?.content ?? []) console.log(c.type === "text" ? c.text : JSON.stringify(c));
  if (r.result?.isError) console.log("(isError)");
}
child.kill();
