// Unit tests for the S9-demos-geoprocessing topology decision (node --test; run by e2e/test_cloud.py).
// The fake capability manifest / topology mirror what harness/lib/common.sh reports for each cell.
import { test } from "node:test";
import assert from "node:assert/strict";
import { CAPABILITY_UNAVAILABLE, geoprocessingExpectation, judgeRedisOffGeoprocessing } from "./gp-topology.mjs";

const manifest = (off) => ({
  capabilities: [
    { id: "operations.proposals", available: !off, reasonCode: off ? "disabled-by-configuration" : null },
    { id: "jobs.runner", available: !off, reasonCode: off ? "dependency-unavailable" : null },
  ],
});
const jobsRunner = (off) => manifest(off).capabilities.find((c) => c.id === "jobs.runner");
const topology = (declared, detected, mismatch = null) => ({
  topology: detected, declared, signal: "manifest:operations.proposals",
  deploymentEnvironment: "Production", reasonCode: detected === "redis-off" ? "disabled-by-configuration" : null, mismatch,
});
const REFUSAL = {
  type: CAPABILITY_UNAVAILABLE, status: 503, code: "dependency-unavailable",
  capability: "jobs.runner", missingDependency: "redis",
};
// What the pinned honua-site (0db19d7e) demo-geoprocessing page renders for a 503 naming the job store.
const honestPage = (body = REFUSAL) => ({
  settled: true,
  pill: "plan accepted · 503 job store",
  summary: "Plan validated & accepted live. ... The public demo does not provision durable async job storage ...",
  out: "POST .../generalization.simplify-layer/execution\n\n-> HTTP 503\n\nJob persistence is unavailable on the public demo " +
    "(no Redis-backed durable store),\nso the accepted plan cannot be enqueued. Verbatim server response:\n\n" +
    JSON.stringify(body, null, 2),
});
const off = topology("off", "redis-off");

test("redis-on keeps the full live-execution expectation", () => {
  assert.equal(geoprocessingExpectation(topology("on", "redis-on")).mode, "live");
  assert.equal(geoprocessingExpectation(topology("on", "unknown")).mode, "live");
  assert.equal(geoprocessingExpectation(null).mode, "live");
});

test("only a declared AND server-confirmed redis-off cell expects the refusal", () => {
  assert.equal(geoprocessingExpectation(off).mode, "refusal");
  const contradicted = geoprocessingExpectation(topology("off", "redis-on", "cell declares Redis off but the server reports a durable control plane"));
  assert.equal(contradicted.mode, "mismatch");
  assert.match(contradicted.why, /^topology: cell declares Redis off/);
  assert.equal(geoprocessingExpectation(topology("on", "redis-off", "cell declares Redis on but ...")).mode, "mismatch");
  assert.equal(geoprocessingExpectation({ declared: "off", topology: "unknown" }).mode, "mismatch");
});

test("redis-off passes on the typed refusal rendered honestly, in S5's evidence shape", () => {
  const v = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: JSON.stringify(REFUSAL) }, page: honestPage(),
  });
  assert.equal(v.status, "pass", v.why);
  assert.match(v.why, /^redis-off \(manifest:operations\.proposals: disabled-by-configuration\): /);
  assert.equal(v.evidence.httpStatus, "503");
  assert.equal(v.evidence.type, CAPABILITY_UNAVAILABLE);
  assert.equal(v.evidence.missingDependency, "redis");
  assert.equal(v.evidence.capability, "jobs.runner");
  assert.equal(v.evidence.topology.topology, "redis-off");
  assert.equal(v.evidence.jobsRunner.available, false);
  assert.equal(v.evidence.page.pill, "plan accepted · 503 job store");
});

test("redis-off fails when the server accepts a job or refuses untyped", () => {
  const accepted = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 201, body: '{"jobID":"j1","status":"accepted"}' },
    page: { settled: true, pill: "job j1 · accepted", summary: "", out: "" },
  });
  assert.equal(accepted.status, "fail");
  assert.match(accepted.why, /accepted the execution \(HTTP 201\)/);
  const untyped = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: '{"title":"Service Unavailable"}' },
    page: honestPage({ title: "Service Unavailable" }),
  });
  assert.equal(untyped.status, "fail");
  assert.match(untyped.why, /expected 503 capability-unavailable \(missingDependency=redis\), got HTTP 503/);
  const wrongDependency = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: { ...REFUSAL, missingDependency: "postgres" } },
    page: honestPage(),
  });
  assert.equal(wrongDependency.status, "fail");
  const never = judgeRedisOffGeoprocessing({ topology: off, jobsRunner: jobsRunner(true), execution: null, page: honestPage() });
  assert.equal(never.status, "fail");
});

test("redis-off fails when the manifest contradicts the topology", () => {
  const v = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(false), execution: { status: 503, body: REFUSAL }, page: honestPage(),
  });
  assert.equal(v.status, "fail");
  assert.match(v.why, /jobs\.runner available/);
});

test("redis-off fails when the refusal is unreadable cross-origin (server CORS), never blames the site", () => {
  const v = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: REFUSAL },
    page: { settled: true, pill: "request failed", summary: "", out: "Execution request failed (network/CORS):\nFailed to fetch" },
  });
  assert.equal(v.status, "fail");
  assert.match(v.why, /CORS/);
});

test("redis-off is BLOCKED on site behaviour when the page spins or hides the refusal", () => {
  const spinning = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: REFUSAL },
    page: { settled: false, pill: "submitting…", summary: "", out: "" },
  });
  assert.equal(spinning.status, "blocked");
  assert.match(spinning.why, /never left its submitting state/);
  const hidden = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: REFUSAL },
    page: { settled: true, pill: "HTTP 503", summary: "", out: "-> HTTP 503" },
  });
  assert.equal(hidden.status, "blocked");
  assert.match(hidden.why, /site behaviour/);
});

test("redis-off fails when the page claims a completed job the server refused", () => {
  const v = judgeRedisOffGeoprocessing({
    topology: off, jobsRunner: jobsRunner(true), execution: { status: 503, body: REFUSAL },
    page: { ...honestPage(), pill: "job j1 · done", out: "Job j1 successful. Results:\n{}" },
  });
  assert.equal(v.status, "fail");
});
