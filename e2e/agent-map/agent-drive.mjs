/**
 * Live driver, browser-agnostic edition.
 *
 * Boots the Vite app and a loopback control channel. The page polls
 * GET /next for jobs, runs them through the Honua AI map kit in the browser
 * that has it open (your Windows Chrome, a headless one, anything), and posts
 * the outcome back to POST /result. The agent side talks to:
 *
 *   GET  /tools      kit.mcpTools
 *   GET  /prompt     kit.systemPrompt()
 *   GET  /context    kit.context()
 *   GET  /viewport   current camera
 *   POST /call       { name, args } -> kit.execute
 *   GET  /status     is a page connected, queue depth
 *   POST /stop
 *
 * If CDP_URL is set (a Chrome started with --remote-debugging-port), the
 * driver also attaches over CDP and saves a screenshot after every /call.
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import http from "node:http";
import { createRequire } from "node:module";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { createAgent } from "./agent-loop.mjs";

const here = path.dirname(new URL(import.meta.url).pathname);
const appDir = path.join(here, "app");
const shotsDir = path.join(here, "run", "shots");
mkdirSync(shotsDir, { recursive: true });

const PORT = Number(process.env.CONTROL_PORT ?? 47811);
const VITE_PORT = Number(process.env.VITE_PORT ?? 5199);
const JOB_TIMEOUT_MS = 90_000;
const LONG_POLL_MS = 15_000;

function startVite() {
  return new Promise((resolve, reject) => {
    const child = spawn("npm", ["run", "dev", "--", "--host", "127.0.0.1", "--port", String(VITE_PORT), "--strictPort"], {
      cwd: appDir,
      env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    const onData = (chunk) => {
      output += chunk.toString();
      const match = output.match(/Local:\s+(http:\/\/[^\s/]+)\/?/);
      if (match) resolve({ url: match[1], child });
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.once("exit", (code) => reject(new Error(`vite exited ${code}: ${output}`)));
    setTimeout(() => reject(new Error(`vite start timeout: ${output}`)), 30_000);
  });
}

const vite = await startVite();
console.log(`[drive] vite at ${vite.url}`);

// ---------------------------------------------------------------------------
// Optional CDP attach for screenshots
// ---------------------------------------------------------------------------

let cdpPage;
async function attachCdp() {
  if (!process.env.CDP_URL) return;
  try {
    // Playwright is optional: install it next to this harness (`npm install playwright`) to enable CDP screenshots.
    const require = createRequire(import.meta.url);
    const { chromium } = require("playwright");
    const browser = await chromium.connectOverCDP(process.env.CDP_URL);
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        if (page.url().includes(`:${VITE_PORT}`)) cdpPage = page;
      }
    }
    console.log(`[drive] cdp attached: ${cdpPage ? cdpPage.url() : "no matching page yet"}`);
  } catch (error) {
    console.log(`[drive] cdp attach failed: ${error.message}`);
  }
}
await attachCdp();

let shotSeq = 0;
async function screenshot(label) {
  if (!cdpPage) {
    try {
      const snap = await enqueue("snapshot");
      shotSeq += 1;
      const file = path.join(shotsDir, `${String(shotSeq).padStart(2, "0")}-${label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.png`);
      writeFileSync(file, Buffer.from(String(snap.png).split(",")[1], "base64"));
      return file;
    } catch (error) {
      return `snapshot failed: ${error.message}`;
    }
  }
  try {
    await cdpPage.waitForTimeout(900);
    shotSeq += 1;
    const file = path.join(shotsDir, `${String(shotSeq).padStart(2, "0")}-${label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.png`);
    await cdpPage.screenshot({ path: file });
    return file;
  } catch (error) {
    return `screenshot failed: ${error.message}`;
  }
}

// ---------------------------------------------------------------------------
// Job queue between the agent and the page
// ---------------------------------------------------------------------------

const queue = [];
const pending = new Map();
let parked;
let lastSeen = 0;

function enqueue(kind, payload) {
  const id = randomUUID();
  const job = { id, kind, payload };
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`job ${kind} timed out after ${JOB_TIMEOUT_MS} ms (is the page open and polling?)`));
    }, JOB_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
    if (parked) {
      const response = parked;
      parked = undefined;
      response.writeHead(200, cors({ "content-type": "application/json" }));
      response.end(JSON.stringify(job));
    } else {
      queue.push(job);
    }
  });
}

// Only the Vite page this driver started may use the control channel. A wildcard origin would let any
// website open in the same browser POST /chat or /call and spend the operator's model credentials.
const ALLOWED_ORIGINS = new Set([`http://127.0.0.1:${VITE_PORT}`, `http://localhost:${VITE_PORT}`]);
let requestOrigin;

function cors(headers = {}) {
  const allow = requestOrigin && ALLOWED_ORIGINS.has(requestOrigin) ? { "access-control-allow-origin": requestOrigin, vary: "origin" } : {};
  return { ...allow, "access-control-allow-headers": "content-type", "access-control-allow-methods": "GET,POST,OPTIONS", ...headers };
}

function readBody(request) {
  return new Promise((resolve) => {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => resolve(body));
  });
}

const server = http.createServer(async (request, response) => {
  const send = (status, payload) => {
    response.writeHead(status, cors({ "content-type": "application/json" }));
    response.end(JSON.stringify(payload, null, 2));
  };
  try {
    const url = new URL(request.url ?? "/", "http://localhost");
    requestOrigin = request.headers.origin;
    // Browser requests carry an Origin header; refuse any that is not the harness page. Local CLI tools
    // (curl, the agent loop) send none and are allowed, since the server listens on loopback only.
    if (requestOrigin && !ALLOWED_ORIGINS.has(requestOrigin)) return send(403, { error: "origin not allowed" });
    if (request.method === "OPTIONS") {
      response.writeHead(204, cors());
      return response.end();
    }
    // ---- page side ----
    if (request.method === "GET" && url.pathname === "/next") {
      lastSeen = Date.now();
      const job = queue.shift();
      if (job) return send(200, job);
      parked = response;
      setTimeout(() => {
        if (parked === response) {
          parked = undefined;
          response.writeHead(204, cors());
          response.end();
        }
      }, LONG_POLL_MS);
      return;
    }
    if (request.method === "POST" && url.pathname === "/result") {
      const { id, output, error } = JSON.parse(await readBody(request));
      const waiter = pending.get(id);
      if (!waiter) return send(410, { error: "unknown or expired job" });
      clearTimeout(waiter.timer);
      pending.delete(id);
      if (error) waiter.reject(new Error(error));
      else waiter.resolve(output);
      return send(200, { ok: true });
    }
    // ---- agent side ----
    if (request.method === "GET" && url.pathname === "/status") {
      return send(200, { pageConnected: Date.now() - lastSeen < LONG_POLL_MS + 5000, lastSeenMsAgo: lastSeen ? Date.now() - lastSeen : null, queued: queue.length, inFlight: pending.size, cdp: Boolean(cdpPage) });
    }
    if (request.method === "GET" && ["/tools", "/prompt", "/context", "/viewport", "/widgets"].includes(url.pathname)) {
      return send(200, await enqueue(url.pathname.slice(1)));
    }
    if (request.method === "POST" && url.pathname === "/call") {
      const call = JSON.parse(await readBody(request));
      const started = Date.now();
      const result = await enqueue("call", call);
      const shot = await screenshot(call.name);
      return send(200, { ms: Date.now() - started, result, ...(shot ? { screenshot: shot } : {}) });
    }
    if (request.method === "POST" && url.pathname === "/chat") {
      const { text } = JSON.parse(await readBody(request));
      send(202, { accepted: true });
      const started = Date.now();
      try {
        const outcome = await agent.turn(text, console.log);
        console.log(`[agent] turn done in ${Date.now() - started} ms: ${JSON.stringify(outcome)}`);
        await screenshot("turn");
      } catch (error) {
        console.log(`[agent] turn failed: ${error.message}`);
        await enqueue("say", { role: "error", text: `Model turn failed: ${error.message}` }).catch(() => {});
      }
      return;
    }
    if (request.method === "POST" && url.pathname === "/attach") {
      process.env.CDP_URL = (await readBody(request)).trim() || process.env.CDP_URL;
      await attachCdp();
      return send(200, { cdp: Boolean(cdpPage), url: cdpPage?.url() });
    }
    if (request.method === "POST" && url.pathname === "/shot") {
      return send(200, { screenshot: await screenshot("manual") });
    }
    if (request.method === "POST" && url.pathname === "/stop") {
      send(200, { ok: true });
      vite.child.kill("SIGTERM");
      server.close();
      setTimeout(() => process.exit(0), 200);
      return;
    }
    send(404, { error: "unknown route" });
  } catch (error) {
    // Log the detail on the operator's console; return only a correlation id to the caller.
    const incident = randomUUID();
    console.error(`[drive] request failed (${incident}):`, error);
    send(500, { error: "request failed", incident });
  }
});

const agent = createAgent({
  tools: () => enqueue("tools"),
  prompt: () => enqueue("prompt"),
  call: (call) => enqueue("call", call),
  say: (role, text) => enqueue("say", { role: role === "assistant-progress" ? "system" : role, text }),
});
console.log(`[drive] agent model: ${agent.model}`);

server.listen(PORT, "127.0.0.1", () => console.log(`[drive] control API on http://127.0.0.1:${PORT}  — open ${vite.url} in any browser`));
