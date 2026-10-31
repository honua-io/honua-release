import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { runInNewContext } from "node:vm";
import { GpExecutionTrace, gpExecutionSettled, gpRenderedExecution } from "./gp-execution.mjs";

const rendered = (pill, out = "") => ({ document: { querySelector: (selector) => ({
  textContent: selector === "#gp-exec-pill" ? pill : selector === "#gp-exec-out" ? out : "12 features",
}) } });
const inPage = (fn, pill, out) => runInNewContext(`(${fn.toString()})()`, rendered(pill, out));

for (const pill of ["poll timeout", "job failed", "job dismissed", "job gp-123 · done", "error", "HTTP 503"]) {
  test(`page settles immediately on ${pill}`, () => assert.equal(inPage(gpExecutionSettled, pill), true));
}
for (const pill of ["", "submitting…", "job gp-123 · accepted", "job gp-123 · running"]) {
  test(`page keeps waiting on ${pill}`, () => assert.equal(inPage(gpExecutionSettled, pill), false));
}
test("success still requires the result output", () => {
  assert.equal(inPage(gpExecutionSettled, "job gp-123 · running", "successful. Results: {}"), true);
});
test("rendered diagnostics preserve the last output, including a long status tail", () => {
  const observed = inPage(gpRenderedExecution, "poll timeout", "x".repeat(18000) + "last status: running");
  assert.equal(observed.out.length, 16003);
  assert.match(observed.out, /last status: running$/);
});

test("timeout evidence keeps the latest poll body, job id and timings even if an older body finishes later", async () => {
  const page = new EventEmitter();
  page.evaluate = async (fn) => inPage(fn, "poll timeout", 'job gp-123 still running');
  let now = 1000;
  const trace = new GpExecutionTrace(page, "https://candidate.example", () => now);
  trace.start();
  const request = () => ({ method: () => "GET" });
  const response = (req, body, path = "/ogc/processes/jobs/gp-123") => ({
    request: () => req, url: () => "https://candidate.example" + path, status: () => 200, text: () => body,
  });
  let finishOld;
  const old = request();
  page.emit("request", old);
  page.emit("response", response(old, new Promise((resolve) => { finishOld = resolve; })));
  now = 8000;
  const latest = request();
  page.emit("request", latest);
  now = 8200;
  page.emit("response", response(latest, Promise.resolve('{"jobID":"gp-123","status":"running"}')));
  page.emit("response", response(request(), Promise.resolve("results"), "/ogc/processes/jobs/gp-123/results"));
  finishOld('{"status":"accepted"}');
  now = 91000;
  const evidence = await trace.evidence(page);
  assert.equal(evidence.jobId, "gp-123");
  assert.equal(evidence.pollCount, 2);
  assert.equal(evidence.elapsedMs, 90000);
  assert.equal(evidence.lastStatus.startedAt, 8000);
  assert.equal(evidence.lastStatus.receivedAt, 8200);
  assert.equal(JSON.parse(evidence.lastStatus.body).status, "running");
  assert.equal(evidence.pill, "poll timeout");
  assert.match(evidence.out, /still running/);
});

test("diagnostics survive a closed page and a failed status-body read", async () => {
  const page = new EventEmitter();
  page.evaluate = async () => { throw new Error("page closed"); };
  const trace = new GpExecutionTrace(page, "https://candidate.example");
  page.emit("response", {
    request: () => ({ method: () => "GET" }), url: () => "https://candidate.example/ogc/processes/jobs/gp-1",
    status: () => 200, text: async () => { throw new Error("body unavailable"); },
  });
  const evidence = await trace.evidence(page);
  assert.equal(evidence.jobId, "gp-1");
  assert.equal(evidence.lastStatus.bodyReadError, "body unavailable");
});


test("submission identifies the job even when no status poll arrives", async () => {
  const page = new EventEmitter();
  page.evaluate = async (fn) => inPage(fn, "poll timeout", "no status response");
  const trace = new GpExecutionTrace(page, "https://candidate.example");
  page.emit("response", {
    request: () => ({ method: () => "POST" }),
    url: () => "https://candidate.example/ogc/processes/processes/generalization.simplify-layer/execution",
    status: () => 201, text: async () => '{"jobID":"gp-created","status":"accepted"}',
  });
  const evidence = await trace.evidence(page);
  assert.equal(evidence.jobId, "gp-created");
  assert.equal(evidence.submission.httpStatus, 201);
  assert.equal(evidence.lastStatus, null);
});
