/*
 * S9 — drives the five honua-site demos headlessly against the seeded candidate server.
 *
 * Each demo is loaded from a LOCAL static copy of honua-site (a different origin from the server,
 * exactly as honua.io -> demo.honua.io is in production) with the honua-site backend-override shim
 * engaged via ?apiBase=<E2E_BASE>. Nothing in the demos is modified or stubbed: no route
 * interception, no injected fixtures, no request rewriting. Whatever the page does against the
 * candidate server is what is asserted.
 *
 * Every check is a REAL functional assertion — rendered features, matching cross-protocol counts, a
 * completed geoprocessing job with a result document, a click-query popup carrying live attributes,
 * an edit applied and visible in the DOM. Nothing passes on an HTTP 200.
 *
 * Writes $E2E_OUT/demos-results.json: [{ id, status, why, evidence }]; run.sh turns that into
 * emit_scenario rows. A thrown/timed-out check is a FAIL, never a silent pass.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

// Playwright lives in a cache dir outside the repo (run.sh installs it there and passes the entry
// point in E2E_PW_MODULE); a normally-installed copy is used when one is on the resolution path.
const playwright = await (async () => {
  try {
    return await import("playwright");
  } catch (error) {
    if (!process.env.E2E_PW_MODULE) throw error;
    return await import(pathToFileURL(process.env.E2E_PW_MODULE).href);
  }
})();
// playwright is CJS: an out-of-tree dynamic import lands the exports on `default`.
const chromium = playwright.chromium ?? playwright.default?.chromium;
if (!chromium) throw new Error("playwright loaded but exposes no chromium launcher");

const SITE = process.env.E2E_SITE_URL;
const BASE = process.env.E2E_BASE;
const KEY = process.env.E2E_API_KEY || "honua-console-dev-key";
const OUT = process.env.E2E_OUT;
const HEADLESS = process.env.E2E_DEMOS_HEADED !== "1";
const STEP = Number(process.env.E2E_DEMOS_TIMEOUT_MS || 45000);

const manifest = JSON.parse(readFileSync(join(OUT, "seed-manifest.json"), "utf8"));
const demoLayers = manifest.demo || {};

const results = [];
function record(id, status, why, evidence) {
  results.push({ id, status, why, evidence: evidence ?? null });
  writeFileSync(join(OUT, "demos-results.json"), JSON.stringify(results, null, 2));
  console.error(`  [${status.toUpperCase().padEnd(7)}] ${id}: ${why}`);
}

/* ── page plumbing ─────────────────────────────────────────────────────────── */

async function newPage(context) {
  const page = await context.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("requestfailed", (r) => {
    failedRequests.push(`${r.method()} ${r.url().slice(0, 160)} — ${r.failure()?.errorText}`);
  });
  page.__diag = { consoleErrors, failedRequests };
  return page;
}

function diag(page, extra = {}) {
  return {
    ...extra,
    consoleErrors: page.__diag.consoleErrors.slice(0, 8),
    failedRequests: page.__diag.failedRequests.slice(0, 8),
  };
}

const demoUrl = (file, params = {}) => {
  const url = new URL(file, SITE);
  url.searchParams.set("apiBase", BASE);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  return url.toString();
};

/* MapLibre renders to a WebGL canvas, so a feature has no DOM node to click. These demos never move
 * the camera after construction, so the screen position of a lng/lat is exact Web-Mercator math
 * against the container box and the configured center/zoom. */
async function mapPoint(page, containerSelector, center, zoom, lngLat) {
  const box = await page.locator(containerSelector).boundingBox();
  if (!box) throw new Error(`map container ${containerSelector} has no box`);
  const merc = ([lng, lat]) => {
    const x = (lng + 180) / 360;
    const s = Math.sin((lat * Math.PI) / 180);
    const y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return [x, y];
  };
  const world = 512 * Math.pow(2, zoom);
  const [cx, cy] = merc(center);
  const [tx, ty] = merc(lngLat);
  return {
    x: box.x + box.width / 2 + (tx - cx) * world,
    y: box.y + box.height / 2 + (ty - cy) * world,
  };
}

async function waitForAttr(page, selector, attr, value, timeout = STEP) {
  await page.waitForFunction(
    ([sel, a, v]) => document.querySelector(sel)?.getAttribute(a) === v,
    [selector, attr, value],
    { timeout }
  );
}

/* ── 0. shim security ──────────────────────────────────────────────────────────────────────────
 *
 * honua.io is served by GitHub Pages, which cannot set response headers, so each demo page's own
 * <meta> CSP is the ONLY policy it has. Three properties have to hold on EVERY demo page, and all
 * three are load-bearing for a public site:
 *
 *   a) the policy binds even when the external assets/demos/backend-override.js cannot be fetched
 *      (adblocker / network blip / partial deploy). This is the one case that is deliberately
 *      simulated with request interception — everywhere else in this driver the demos run
 *      untouched. A page that loses its CSP because a fetch failed is a regression against the
 *      parser-read <meta> it replaced.
 *   b) a non-allow-listed ?apiBase= is refused: no override, no widened policy, and the connect it
 *      would have enabled is still blocked.
 *   c) an allow-listed ?apiBase= IS honoured and lands in connect-src.
 *
 * The probe used for (a) and (b) is E2E_BASE — the candidate server, which really is reachable and
 * really does answer, so "blocked" can only mean the policy blocked it rather than a dead socket.
 * (c) proves that discrimination directly: the same fetch from the same page succeeds once the
 * policy admits the origin.
 */

const DEMO_PAGES = [
  "demo-two-protocols.html",
  "demo-geoprocessing.html",
  "demo-editing.html",
  "demo-esri-leaflet.html",
  "demo-analyst-workbench.html",
];

async function probePage(context, url, { starveShim = false } = {}) {
  const page = await newPage(context);
  try {
    if (starveShim) {
      await page.route("**/backend-override.js", (route) => route.abort("failed"));
    }
    await page.goto(url, { waitUntil: "commit", timeout: STEP });
    await page.waitForFunction(() => document.readyState !== "loading", null, { timeout: STEP })
      .catch(() => {});
    return await page.evaluate(async (probe) => {
      const metas = [...document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]')]
        .map((m) => m.getAttribute("content"));
      let connect = "allowed";
      try {
        const res = await fetch(probe + "/healthz/ready", { mode: "cors" });
        await res.text();
      } catch (_error) {
        connect = "blocked";
      }
      // What the SHIPPED shim's rebase() actually does to a set of look-alike hosts. Asserted in
      // case (c) below; null when the external shim is absent (the starved case).
      let rebased = null;
      if (window.HonuaDemoBackend && typeof window.HonuaDemoBackend.rebase === "function") {
        const rebase = (value) => window.HonuaDemoBackend.rebase(value);
        rebased = {
          exact: rebase("https://demo.honua.io"),
          path: rebase("https://demo.honua.io/rest/services"),
          lookalikeSuffix: rebase("https://demo.honua.io.evil.com/x"),
          embedded: rebase("https://evil.com/https://demo.honua.io"),
          userinfo: rebase("https://demo.honua.io@evil.com/x"),
          nested: rebase({ server: { baseUrl: "https://demo.honua.io" }, hostile: "https://demo.honua.io.evil.com/x" }),
        };
      }
      return {
        metaCount: metas.length,
        policy: metas[0] ?? null,
        bootstrapRan: typeof window.HONUA_DEMO_CSP === "string",
        externalShim: typeof window.HonuaDemoBackend !== "undefined",
        origin: window.HONUA_DEMO_BACKEND_ORIGIN ?? null,
        connect,
        rebased,
      };
    }, BASE);
  } finally {
    await page.close();
  }
}

async function checkShimSecurity(context) {
  const origin = new URL(BASE).origin;
  const problems = [];
  const evidence = {};
  try {
    for (const demo of DEMO_PAGES) {
      const base = new URL(demo, SITE).toString();

      // (a) the external shim cannot be fetched — the policy must still bind.
      const starved = await probePage(context, base, { starveShim: true });
      if (!(starved.bootstrapRan && starved.metaCount === 1 && starved.externalShim === false && starved.connect === "blocked")) {
        problems.push(`${demo}: with backend-override.js unavailable the policy did not bind ` +
          `(meta=${starved.metaCount} bootstrap=${starved.bootstrapRan} externalShim=${starved.externalShim} connect=${starved.connect})`);
      }

      // (b) a hostile override is refused outright.
      const hostile = new URL(demo, SITE);
      hostile.searchParams.set("apiBase", "https://evil.example.com");
      const refused = await probePage(context, hostile.toString());
      if (refused.origin !== null) problems.push(`${demo}: hostile ?apiBase was honoured (origin=${refused.origin})`);
      // Compare the whole emitted policy against the no-override baseline from (a) rather than
      // searching it for the hostile host: an exact match proves NOTHING changed, which is the
      // actual requirement, and a substring search over a URL-bearing string is the same weak
      // pattern this scenario exists to catch (CodeQL js/incomplete-url-substring-sanitization).
      if (refused.policy !== starved.policy) {
        problems.push(`${demo}: the emitted policy changed for a rejected origin (${refused.policy})`);
      }
      if (refused.connect !== "blocked") problems.push(`${demo}: the emitted CSP did not block a connect to ${origin} with no valid override`);

      // (c) the allow-listed override is honoured, and the same connect then succeeds.
      const accepted = await probePage(context, demoUrl(demo));
      const connectSrc = ((accepted.policy || "").split(";").find((d) => d.trim().startsWith("connect-src")) || "").trim();
      if (accepted.origin !== origin) problems.push(`${demo}: allow-listed ?apiBase=${BASE} was not honoured (origin=${accepted.origin})`);
      if (!connectSrc.includes(origin)) problems.push(`${demo}: connect-src was not widened (${connectSrc})`);
      if (accepted.connect !== "allowed") problems.push(`${demo}: the widened policy still blocked the connect`);

      // (d) rebase() must match on an ORIGIN BOUNDARY, not a prefix. A host that merely starts with
      // the default origin ("https://demo.honua.io.evil.com") is a DIFFERENT host and must be left
      // alone; rewriting it would invent "<override>.evil.com", a host the allow-list never
      // approved. The emitted CSP would block the request either way, which is exactly why this is
      // asserted here rather than left to the policy to hide
      // (CodeQL js/incomplete-url-substring-sanitization).
      const r = accepted.rebased;
      if (!r) {
        problems.push(`${demo}: the shim exposed no rebase() to check`);
      } else {
        const expected = {
          exact: origin,
          path: `${origin}/rest/services`,
          lookalikeSuffix: "https://demo.honua.io.evil.com/x",
          embedded: "https://evil.com/https://demo.honua.io",
          userinfo: "https://demo.honua.io@evil.com/x",
        };
        for (const [name, want] of Object.entries(expected)) {
          if (r[name] !== want) problems.push(`${demo}: rebase(${name}) = ${r[name]} (expected ${want})`);
        }
        if (r.nested?.server?.baseUrl !== origin || r.nested?.hostile !== expected.lookalikeSuffix) {
          problems.push(`${demo}: rebase() mis-rewrote a nested config (${JSON.stringify(r.nested)})`);
        }
      }

      evidence[demo] = {
        shimUnavailable: { metaCount: starved.metaCount, bootstrapRan: starved.bootstrapRan, externalShim: starved.externalShim, connect: starved.connect },
        hostileOverride: { origin: refused.origin, connect: refused.connect },
        allowListedOverride: { origin: accepted.origin, connect: accepted.connect, connectSrc },
        rebaseOriginBoundary: accepted.rebased,
      };
    }

    if (problems.length > 0) {
      record("shim-security", "fail", problems.slice(0, 4).join(" | "), evidence);
      return;
    }
    record(
      "shim-security",
      "pass",
      `all ${DEMO_PAGES.length} demo pages: the CSP binds even when backend-override.js cannot be fetched, ` +
        `a non-allow-listed ?apiBase is refused with the policy unwidened and the connect blocked, ` +
        `the allow-listed origin is honoured and admitted by connect-src, ` +
        `and rebase() rewrites only on an origin boundary (a look-alike host such as ` +
        `https://demo.honua.io.evil.com is left untouched)`,
      evidence
    );
  } catch (error) {
    record("shim-security", "fail", `shim security check threw: ${error.message}`, evidence);
  }
}

/* ── 1. two-protocols ──────────────────────────────────────────────────────── */

async function checkTwoProtocols(context) {
  const page = await newPage(context);
  try {
    await page.goto(demoUrl("demo-two-protocols.html"), { waitUntil: "domcontentloaded", timeout: STEP });
    await waitForAttr(page, "#tp-status", "data-state", "live");
    await page.waitForFunction(
      () => /features/.test(document.querySelector("#tp-match")?.textContent || ""),
      null,
      { timeout: STEP }
    );

    const observed = await page.evaluate(() => {
      const text = (sel) => document.querySelector(sel)?.textContent?.trim() || "";
      const count = (sel) => {
        const m = /(\d+)\s*features/.exec(text(sel));
        return m ? Number(m[1]) : null;
      };
      return {
        status: text("#tp-status"),
        match: text("#tp-match"),
        matchState: document.querySelector("#tp-match")?.dataset.state,
        geoservices: count("#tp-stats-geoservices"),
        ogc: count("#tp-stats-ogc"),
        odata: count("#tp-stats-odata"),
        geoservicesUrl: text("#tp-req-geoservices"),
        rawGeoservices: (document.querySelector("#tp-raw-geoservices")?.textContent || "").length,
      };
    });

    const origin = new URL(BASE).origin;
    if (observed.matchState !== "match") {
      record("two-protocols", "fail", `protocol lanes disagreed: ${observed.match}`, diag(page, observed));
      return;
    }
    if (!observed.geoservices || !observed.ogc) {
      record("two-protocols", "fail", "a protocol lane returned zero features", diag(page, observed));
      return;
    }
    if (observed.geoservices !== observed.ogc) {
      record("two-protocols", "fail", `GeoServices ${observed.geoservices} != OGC ${observed.ogc}`, diag(page, observed));
      return;
    }
    if (!observed.geoservicesUrl.includes(origin)) {
      record("two-protocols", "fail", `the page did not query the override backend: ${observed.geoservicesUrl}`, diag(page, observed));
      return;
    }
    if (observed.odata === null || observed.odata !== observed.geoservices) {
      record("two-protocols", "fail", `OData lane did not agree (${observed.odata} vs ${observed.geoservices})`, diag(page, observed));
      return;
    }
    record(
      "two-protocols",
      "pass",
      `live against the candidate: GeoServices/OGC/OData each returned ${observed.geoservices} features and the page shows "${observed.match}"`,
      observed
    );
  } catch (error) {
    record("two-protocols", "fail", `demo did not reach a live, matching result: ${error.message}`, diag(page));
  } finally {
    await page.close();
  }
}

/* ── 2. esri-leaflet ───────────────────────────────────────────────────────── */

async function checkEsriLeaflet(context) {
  const page = await newPage(context);
  try {
    await page.goto(demoUrl("demo-esri-leaflet.html"), { waitUntil: "domcontentloaded", timeout: STEP });
    await waitForAttr(page, "#el-status", "data-state", "live");

    // Scene 1 renders the zoning FeatureServer through unmodified esri-leaflet: real SVG paths.
    await page.waitForFunction(
      () => document.querySelectorAll(".leaflet-overlay-pane svg path").length > 0,
      null,
      { timeout: STEP }
    );
    const rendered = await page.locator(".leaflet-overlay-pane svg path").count();

    // Scene 3 ("Click to query") runs L.esri.query().intersects() against the parcels FeatureServer.
    await page.locator('.el-scene-chip[data-scene="query"]').click();
    await page.waitForTimeout(1800); // scene flyTo
    const box = await page.locator("#el-map").boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForSelector(".leaflet-popup-content", { timeout: STEP });
    const popup = (await page.locator(".leaflet-popup-content").innerText()).trim();
    const codeStrip = await page.locator("#el-code-block, .el-code-strip pre").first().innerText().catch(() => "");

    const statusText = (await page.locator("#el-status").innerText()).trim();
    if (!/Parcel \(TMK\)/i.test(popup) || !/tmk/i.test(popup)) {
      record("esri-leaflet", "fail", `click query returned no parcel attributes: ${popup.slice(0, 160)}`, diag(page, { statusText, rendered, popup: popup.slice(0, 300) }));
      return;
    }
    if (!/feature\(s\) in/.test(codeStrip)) {
      record("esri-leaflet", "fail", "the code strip did not record a completed L.esri.query run", diag(page, { statusText, rendered, codeStrip: codeStrip.slice(0, 300) }));
      return;
    }
    record(
      "esri-leaflet",
      "pass",
      `unmodified esri-leaflet rendered ${rendered} features from the candidate and L.esri.query().intersects() returned live parcel attributes`,
      { statusText, renderedPaths: rendered, popup: popup.slice(0, 300), codeStrip: codeStrip.slice(0, 300) }
    );
  } catch (error) {
    record("esri-leaflet", "fail", `demo did not render/query live: ${error.message}`, diag(page));
  } finally {
    await page.close();
  }
}

/* ── 3. geoprocessing ──────────────────────────────────────────────────────── */

async function checkGeoprocessing(context) {
  const page = await newPage(context);
  try {
    await page.goto(demoUrl("demo-geoprocessing.html"), { waitUntil: "domcontentloaded", timeout: STEP });
    await waitForAttr(page, "#gp-status", "data-state", "ok");
    const catalogText = (await page.locator("#gp-status").innerText()).trim();

    // The page never ships a credential — the operator pastes one. Do exactly that.
    await page.locator("#gp-key").fill(KEY);
    await page.locator(`.gp-proc[data-id="generalization.simplify-layer"]`).click();
    await page.waitForSelector("#gp-run", { state: "visible", timeout: STEP });
    await page.waitForFunction(() => !document.querySelector("#gp-run")?.disabled, null, { timeout: STEP });
    await page.locator("#gp-run").click();

    // Submit -> job -> poll -> results, driven entirely by the page.
    await page.waitForFunction(
      () => /successful\. Results:/.test(document.querySelector("#gp-exec-out")?.textContent || "") ||
            /·\s*(done|failed|dismissed)|error|HTTP \d/.test(document.querySelector("#gp-exec-pill")?.textContent || ""),
      null,
      { timeout: Math.max(STEP, 120000) }
    );

    const observed = await page.evaluate(() => ({
      pill: document.querySelector("#gp-exec-pill")?.textContent?.trim() || "",
      out: (document.querySelector("#gp-exec-out")?.textContent || "").slice(0, 1200),
      summary: document.querySelector("#gp-result-summary")?.textContent?.trim() || "",
      resultLayers: ["gp-result-fill", "gp-result-line", "gp-result-pt"].length,
    }));

    if (!/successful\. Results:/.test(observed.out)) {
      record("geoprocessing", "fail", `the OGC Processes job did not run to a result: pill="${observed.pill}"`, diag(page, observed));
      return;
    }
    const hasPayload = /"(features|value|outputs|result)"/.test(observed.out) || observed.out.length > 200;
    if (!hasPayload) {
      record("geoprocessing", "fail", "job reported successful but returned an empty result document", diag(page, observed));
      return;
    }
    record(
      "geoprocessing",
      "pass",
      `live OGC API Processes catalog (${catalogText}) and generalization.simplify-layer ran to a successful job with a result document`,
      { catalogText, pill: observed.pill, out: observed.out.slice(0, 600) }
    );
  } catch (error) {
    record("geoprocessing", "fail", `geoprocessing demo did not complete a live execution: ${error.message}`, diag(page));
  } finally {
    await page.close();
  }
}

/* ── 4. editing ────────────────────────────────────────────────────────────── */

async function checkEditing(context) {
  const page = await newPage(context);
  try {
    await page.goto(demoUrl("demo-editing.html"), { waitUntil: "domcontentloaded", timeout: STEP });
    await page.waitForFunction(
      () => (document.querySelector("#ed-data-chip")?.dataset.state || "probing") !== "probing",
      null,
      { timeout: STEP }
    );
    const dataChip = (await page.locator("#ed-data-chip").innerText()).trim();
    const writesChip = (await page.locator("#ed-writes-chip").innerText()).trim();
    const statusText = (await page.locator("#ed-status").innerText()).trim();

    // The data lane MUST be live: the page resolved maui-inspections through the candidate's OData
    // catalog and read its features. The bundled fixture lane is a fail here, not a pass.
    if (!/live/i.test(dataChip)) {
      record("editing", "fail", `data lane did not go live against the candidate: "${dataChip}"`, diag(page, { dataChip, writesChip, statusText }));
      return;
    }
    const readCode = (await page.locator("#ed-code-block").innerText()).trim();
    if (!readCode.includes(new URL(BASE).origin)) {
      record("editing", "fail", "the read shown in the code strip was not issued against the override backend", diag(page, { readCode: readCode.slice(0, 300) }));
      return;
    }

    // Open a known seeded inspection by its map position and change its status.
    const target = { name: "Kahului Harbor pier 2", lngLat: [-156.475, 20.899] };
    const point = await mapPoint(page, "#ed-map", [-156.34, 20.8], 9.6, target.lngLat);
    await page.mouse.click(point.x, point.y);
    await page.waitForSelector(".ed-popup-host .ed-card", { timeout: STEP });
    const cardTitle = (await page.locator(".ed-popup-host .ed-card-title").innerText()).trim();
    if (cardTitle !== target.name) {
      record("editing", "fail", `clicked the map but opened "${cardTitle}" instead of "${target.name}"`, diag(page, { cardTitle }));
      return;
    }
    const kicker = (await page.locator(".ed-popup-host .ed-card-kicker").innerText()).trim();
    // The kicker is CSS-uppercased, so innerText reads "HARBOR · OBJECTID 12".
    if (!/objectid\s+\d+/i.test(kicker)) {
      record("editing", "fail", `the opened feature is not a live server row (no ObjectId): "${kicker}"`, diag(page, { kicker }));
      return;
    }

    await page.locator('.ed-popup-host .ed-status-chip[data-value="urgent"]').click();
    await page.locator(".ed-popup-host .ed-note-input").fill("e2e S9 edit round-trip");
    await page.locator(".ed-popup-host .ed-btn-primary").click();
    await page.waitForFunction(
      () => (document.querySelector(".ed-popup-host .ed-card-result")?.dataset.state || "idle") !== "idle",
      null,
      { timeout: STEP }
    );
    const resultState = await page.locator(".ed-popup-host .ed-card-result").getAttribute("data-state");
    const resultText = (await page.locator(".ed-popup-host .ed-card-result").innerText()).trim();
    const patchCode = (await page.locator("#ed-code-block").innerText()).trim();

    if (!/^PATCH /m.test(patchCode) || !patchCode.includes(new URL(BASE).origin)) {
      record("editing", "fail", "Save did not emit an OData PATCH against the override backend", diag(page, { patchCode: patchCode.slice(0, 400) }));
      return;
    }
    if (resultState === "error") {
      record("editing", "fail", `the edit was rejected: ${resultText}`, diag(page, { resultText, patchCode: patchCode.slice(0, 400) }));
      return;
    }

    // The edit must be visible in the DOM: reopen the feature and read back the new status.
    await page.locator(".ed-popup-host .maplibregl-popup-close-button").click().catch(() => {});
    await page.mouse.click(point.x, point.y);
    await page.waitForSelector(".ed-popup-host .ed-card", { timeout: STEP });
    const pressed = await page.locator('.ed-popup-host .ed-status-chip[data-value="urgent"]').getAttribute("aria-pressed");
    const note = await page.locator(".ed-popup-host .ed-note-input").inputValue();
    if (pressed !== "true" || note !== "e2e S9 edit round-trip") {
      record("editing", "fail", `the edit did not survive a reopen (status pressed=${pressed}, note="${note}")`, diag(page, { pressed, note }));
      return;
    }

    // The write lane is a CLAIM the page makes after probing /api/v1/capabilities/manifest. Hold it
    // to the server's own answer: if the candidate advertises edit.features the page must go live
    // AND the row must actually change server-side; if it does not, the page must say so and keep
    // the edit local. Either way the page has to tell the truth about the server it is talking to.
    const capsRes = await fetch(`${BASE}/api/v1/capabilities/manifest`); // anonymous, exactly as the page asks
    const caps = (await capsRes.json().catch(() => ({}))).capabilities || [];
    const editCap = caps.find((c) => c.id === "edit.features") || null;
    const advertised = editCap?.available === true;
    const claimsLive = /live/i.test(writesChip);
    if (advertised !== claimsLive) {
      record(
        "editing",
        "fail",
        `the page's write lane ("${writesChip}") contradicts the candidate's capability manifest (edit.features available=${advertised})`,
        diag(page, { writesChip, editCap })
      );
      return;
    }
    let serverEcho = null;
    if (advertised) {
      const objectId = /objectid\s+(\d+)/i.exec(kicker)[1];
      const res = await fetch(`${BASE}/odata/Layers(${demoLayers["maui-inspections"].layerId})/Features(${objectId})`, {
        headers: { "X-API-Key": KEY },
      });
      const row = await res.json().catch(() => ({}));
      serverEcho = row.status ?? null;
      if (serverEcho !== "urgent") {
        record("editing", "fail", `writes were advertised live but the server row still reads status="${serverEcho}"`, diag(page, { writesChip, serverEcho }));
        return;
      }
    }

    record(
      "editing",
      "pass",
      `live OData read of maui-inspections from the candidate (${dataChip}); status edit applied, PATCH emitted against the override backend, and the change survived a reopen; the write lane ("${writesChip}") matches the candidate's own edit.features capability (available=${advertised})${advertised ? " and the server row echoed the new status" : ""}`,
      { dataChip, writesChip, statusText, cardTitle, kicker, resultState, resultText, editCapability: editCap, serverEcho, patchCode: patchCode.slice(0, 400) }
    );
  } catch (error) {
    record("editing", "fail", `editing demo did not complete a live read + edit: ${error.message}`, diag(page));
  } finally {
    await page.close();
  }
}

/* ── 5. analyst-workbench ──────────────────────────────────────────────────── */

async function runWorkbenchLane(context, lane) {
  const workbench = demoLayers["workbench-assets"];
  const page = await newPage(context);
  const backendCalls = [];
  page.on("response", (r) => {
    if (r.url().startsWith(BASE)) backendCalls.push(`${r.status()} ${decodeURIComponent(r.url()).slice(0, 500)}`);
  });
  await page.goto(
    demoUrl("demo-analyst-workbench.html", {
      // demo-analyst-workbench.html loads the published spatial-analytics-workbench SDK sample,
      // which already reads its own live-lane parameters. ?apiBase= is what widens the page CSP so
      // those requests are allowed to leave the page at all.
      mode: "live",
      baseUrl: BASE,
      protocol: "geoservices-feature-service",
      serviceId: workbench.service,
      layerId: workbench.layerId,
    }),
    { waitUntil: "domcontentloaded", timeout: STEP }
  );
  await page.waitForFunction(
    () => /live/i.test(document.querySelector("#data-mode")?.textContent || ""),
    null,
    { timeout: STEP }
  );

  // Drive the workbench's own explain -> accept -> execute contract on the requested policy.
  await page.selectOption("#execution-lane", lane);
  await page.locator("#explain-analysis").click();
  await page.waitForFunction(() => !document.querySelector("#accept-plan")?.disabled, null, { timeout: STEP });
  await page.locator("#accept-plan").click();
  await page.waitForFunction(
    () => /accepted/i.test(document.querySelector("#plan-state")?.textContent || ""),
    null,
    { timeout: STEP }
  );
  await page.locator("#run-analysis").click();

  // Either the execution lands an output artifact, or the page surfaces the execution error.
  await page.waitForFunction(
    () => document.querySelector("#execution-error")?.hidden === false ||
          /"aggregateRows"/.test(document.querySelector("#artifact-json")?.textContent || ""),
    null,
    { timeout: STEP }
  ).catch(() => {});

  const observed = await page.evaluate(() => {
    const t = (sel) => (document.querySelector(sel)?.textContent || "").trim();
    let aggregateRows = null;
    try {
      aggregateRows = JSON.parse(t("#artifact-json")).aggregateRows ?? null;
    } catch (_error) {
      aggregateRows = null;
    }
    return {
      dataMode: t("#data-mode"),
      planState: t("#plan-state"),
      truth: t("#execution-truth"),
      aggregateRows,
      executionError: document.querySelector("#execution-error")?.hidden === false
        ? t("#execution-error-message")
        : null,
    };
  });
  const diagnostics = diag(page, { lane, ...observed, backendCalls: backendCalls.slice(-3) });
  await page.close();
  return { lane, observed, backendCalls, diagnostics, service: workbench };
}

/* The workbench's DEFAULT execution policy is "GeoServices remote pushdown" — that is the demo's
 * headline claim and the lane that is asserted. When it fails, the same demo's "bounded local" policy
 * is run as a DIAGNOSTIC (not as a substitute verdict): it proves whether the shim + the seeded layer
 * are correct and the fault is server-side. */
async function checkAnalystWorkbench(context) {
  try {
    const primary = await runWorkbenchLane(context, "remote-pushdown");
    const rows = primary.observed.aggregateRows;
    if (!primary.observed.executionError && Array.isArray(rows) && rows.length > 0) {
      record(
        "analyst-workbench",
        "pass",
        `the GeoServices remote-pushdown lane executed live against ${primary.service.service}/${primary.service.layerId} on the candidate and returned ${rows.length} aggregate rows`,
        { ...primary.observed, backendCalls: primary.backendCalls.slice(-3) }
      );
      return;
    }

    let fallback = null;
    try {
      fallback = await runWorkbenchLane(context, "bounded-local");
    } catch (error) {
      fallback = { observed: { executionError: `bounded-local diagnostic threw: ${error.message}` } };
    }
    const fallbackRows = fallback?.observed?.aggregateRows;
    const fallbackWorked = !fallback?.observed?.executionError && Array.isArray(fallbackRows) && fallbackRows.length > 0;
    const offending = primary.backendCalls.at(-1) || "none";
    record(
      "analyst-workbench",
      "fail",
      `the demo's default GeoServices remote-pushdown lane did not execute against the candidate: ` +
        `${primary.observed.executionError || "no output artifact was produced"} — request: ${offending.slice(0, 300)}. ` +
        (fallbackWorked
          ? `The same demo's bounded-local lane DID execute live on the same seeded layer (${fallbackRows.length} aggregate rows), so the shim and the seed are correct and the fault is server-side.`
          : `The demo's bounded-local lane did not execute either (${fallback?.observed?.executionError || "no output artifact"}).`),
      {
        remotePushdown: { ...primary.observed, backendCalls: primary.backendCalls.slice(-3) },
        boundedLocalDiagnostic: fallback?.observed ?? null,
      }
    );
  } catch (error) {
    record("analyst-workbench", "fail", `workbench did not complete a live analysis: ${error.message}`);
  }
}

/* ── main ──────────────────────────────────────────────────────────────────── */

const browser = await chromium.launch({ headless: HEADLESS });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
try {
  await checkShimSecurity(context);
  await checkTwoProtocols(context);
  await checkEsriLeaflet(context);
  await checkGeoprocessing(context);
  await checkEditing(context);
  await checkAnalystWorkbench(context);
} finally {
  await context.close();
  await browser.close();
}
writeFileSync(join(OUT, "demos-results.json"), JSON.stringify(results, null, 2));
