/*
 * S9-demos-geoprocessing — the topology decision and the Redis-off verdict, kept pure so they are
 * unit-tested without a browser (gp-topology.test.mjs, run by e2e/test_cloud.py).
 *
 * A Redis-off cell is a different topology, not a broken one (2026.1 release fix plan, decision 3;
 * docs/2026.1-release-decision-overrides.json): it serves, configures and renders, but geoprocessing
 * jobs need the Redis-backed durable job store, so the server refuses an async execution up front
 * with the typed capability-unavailable receipt (honua-release#202). S5 (drivers/gp/run.sh) asserts
 * exactly that refusal on the wire; this asserts that the honua-site demo page surfaces the SAME
 * refusal honestly instead of waiting for a job that can never run.
 *
 * The topology is the one harness/lib/common.sh resolve_topology settles (cell declares E2E_REDIS,
 * the server must confirm it), passed in by run.sh as topology_evidence JSON. Only
 * `declared off + detected redis-off` licenses the refusal expectation; everything else that is not
 * a mismatch keeps the full live-execution expectation.
 */

export const CAPABILITY_UNAVAILABLE = "https://honua.io/problems/capability-unavailable";

/** -> { mode: "live" | "refusal" | "mismatch", why? } */
export function geoprocessingExpectation(topology) {
  if (!topology || typeof topology !== "object") return { mode: "live" };
  if (topology.mismatch) return { mode: "mismatch", why: `topology: ${topology.mismatch}` };
  if (topology.declared === "off" && topology.topology === "redis-off") return { mode: "refusal" };
  if (topology.declared === "off") {
    // resolve_topology always reports a mismatch here; stay fail-closed if it ever does not.
    return { mode: "mismatch", why: `topology: cell declares Redis off but the server reported ${topology.topology}` };
  }
  return { mode: "live" };
}

function parseBody(text) {
  if (text && typeof text === "object") return text;
  try { return JSON.parse(text); } catch { return null; }
}

/**
 * Judge the Redis-off geoprocessing demo.
 *   topology    — topology_evidence object (common.sh)
 *   jobsRunner  — the manifest's `jobs.runner` capability entry (or null when not reported)
 *   execution   — { status, body } of the page's own POST .../execution as seen on the network, or null
 *   page        — { pill, summary, out } as rendered after the page settled, or { settled:false, ... }
 * -> { status: "pass" | "fail" | "blocked", why, evidence }
 */
export function judgeRedisOffGeoprocessing({ topology, jobsRunner, execution, page }) {
  const body = parseBody(execution?.body);
  const refusal = {
    httpStatus: execution ? String(execution.status) : null,
    type: body?.type ?? null,
    missingDependency: body?.missingDependency ?? null,
    capability: body?.capability ?? null,
  };
  const evidence = {
    topology: { ...(topology || {}), expectation: "redis-off" },
    ...refusal,
    jobsRunner: jobsRunner ?? null,
    page: page ? { pill: page.pill ?? "", summary: (page.summary ?? "").slice(0, 400), out: (page.out ?? "").slice(0, 600) } : null,
  };
  const verdict = (status, why) => ({ status, why: `redis-off: ${why}`, evidence });

  if (jobsRunner && jobsRunner.available === true) {
    return verdict("fail", "the capability manifest advertises jobs.runner available on a Redis-off cell");
  }
  if (!execution) {
    return verdict("fail", "the demo page never submitted generalization.simplify-layer for execution");
  }
  const typed = refusal.httpStatus === "503" && refusal.type === CAPABILITY_UNAVAILABLE &&
    refusal.missingDependency === "redis";
  if (!typed) {
    const accepted = ["200", "201", "202"].includes(refusal.httpStatus);
    return verdict("fail", accepted
      ? `the server accepted the execution (HTTP ${refusal.httpStatus}) although no durable job store exists`
      : `expected 503 capability-unavailable (missingDependency=redis), got HTTP ${refusal.httpStatus}`);
  }

  // The server refused correctly. Now the page: it must SHOW that refusal, not spin.
  if (!page || page.settled === false) {
    return verdict("blocked",
      "the server returned the typed capability-unavailable refusal but the pinned honua-site " +
      "demo-geoprocessing page never left its submitting state (site behaviour, not a server defect)");
  }
  if (/network\/CORS/.test(page.out || "") || /request failed/.test(page.pill || "")) {
    return verdict("fail",
      "the server's typed refusal reached the network but the page could not read it " +
      "(the 503 response carried no CORS allowance for the demo origin)");
  }
  const showsRefusal = /\b503\b/.test(page.pill || "") && /job store|durable/i.test(page.pill + " " + page.summary);
  // The verbatim problem document the page prints must carry the same typed fields (exact match).
  const shownField = (name) => new RegExp(`"${name}":\\s*"([^"]*)"`).exec(page.out || "")?.[1];
  const verbatim = shownField("type") === CAPABILITY_UNAVAILABLE && shownField("missingDependency") === "redis";
  const claimsRun = /successful\. Results:/.test(page.out || "") || /·\s*done/.test(page.pill || "");
  if (claimsRun) {
    return verdict("fail", "the demo claims a completed job although the server refused the execution");
  }
  // honua-site#294 (owner decision 3): nothing was accepted, so the pill and the headline (the
  // summary's first sentence; the server's own detail is quoted later and is not judged) must not say so.
  // A negated mention ("not accepted", "never accepted") is the honest wording, not a claim.
  const headline = (page.summary || "").split(/(?<=\.)\s/)[0];
  if (/(?<!\b(?:not|never)\s+)\baccepted\b/i.test(`${page.pill || ""} ${headline}`)) {
    return verdict("blocked",
      `the pinned honua-site demo-geoprocessing page claimed the refused execution was accepted ` +
      `(pill="${page.pill}") (site behaviour, not a server defect)`);
  }
  if (!showsRefusal || !verbatim) {
    return verdict("blocked",
      `the pinned honua-site demo-geoprocessing page did not render the typed refusal as its job-store-unavailable state ` +
      `(pill="${page.pill}", verbatim refusal shown=${verbatim}) (site behaviour, not a server defect)`);
  }
  const signal = topology?.signal ? ` (${topology.signal}${topology.reasonCode ? `: ${topology.reasonCode}` : ""})` : "";
  return { status: "pass", evidence, why:
    `redis-off${signal}: generalization.simplify-layer execution refused with the typed ` +
    `capability-unavailable receipt (missingDependency=redis) and the demo rendered its honest ` +
    `job-store-unavailable state ("${page.pill}") with the verbatim server refusal` };
}

/**
 * The page's own verdict when it could not load the selected process's description.
 *
 * The pinned honua-site demo (assets/demos/geoprocessing/app.js selectProcess) keeps the Execute
 * button hidden until GET /ogc/processes/processes/{id} answers, and on a failed fetch writes
 * "Could not load schema: <reason>" into #gp-detail-desc. Run 38133126673 (aws-serverless/redis-off)
 * waited 45 s for a button the page had already decided never to show; this names that decision.
 *   detailDesc — #gp-detail-desc text after the process was selected
 * -> the reason string, or null while the page has not reported a load failure
 */
export function processDetailFailure(detailDesc) {
  const text = String(detailDesc ?? "").trim();
  const match = /^Could not load schema:\s*(.*)$/s.exec(text);
  return match ? `the page could not load the process description: "${text.slice(0, 300)}"` : null;
}
