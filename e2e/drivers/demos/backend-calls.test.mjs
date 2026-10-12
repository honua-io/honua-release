// Unit tests for the S9 demo call log (node --test; run by e2e/test_cloud.py).
// The events mirror what Chromium/Playwright reported on cloud run 38192851328 (aws-serverless):
// gateway 500/503 answers without CORS headers that the page saw only as net::ERR_FAILED.
import { test } from "node:test";
import assert from "node:assert/strict";
import { CallLog, answeredBy, describeCall, editingFallbackReason, workbenchReach } from "./backend-calls.mjs";

const BASE = "https://38192851328-aws-serverless-redis-off.cert.demo.honua.io";
const QUERY = `${BASE}/rest/services/honua-workbench/FeatureServer/8/query?where=1%3D1&f=json`;
const GATEWAY = { "content-type": "application/json", "apigw-requestid": "FG8NsgZsIAMEJow=" };
const SERVER = { "content-type": "application/json", "honua-instance": "2026/10/12/[1]236900e833c6" };

test("a CORS-blocked call the gateway answered still proves the sample requested the candidate", () => {
  const log = new CallLog(BASE);
  log.request("OPTIONS", QUERY); // preflights are not page calls
  log.request("GET", QUERY);
  log.status("GET", QUERY, 500, GATEWAY); // CDP responseReceivedExtraInfo: raw status despite CORS
  log.failed("GET", QUERY, "net::ERR_FAILED"); // Playwright: requestfailed, no `response` event
  assert.equal(log.calls.length, 1);
  assert.equal(log.requested("/rest/services/honua-workbench/"), true);
  const reach = workbenchReach(log, "honua-workbench", BASE);
  assert.equal(reach.reached, true);
  assert.match(reach.why, /^the workbench requested the candidate \(https:\/\/38192851328-aws-serverless-redis-off/);
  assert.match(reach.why, /GET \/rest\/services\/honua-workbench\/FeatureServer\/8\/query\?where=1=1&f=json → 500 \(API Gateway, no server body\); the page could not read it \(net::ERR_FAILED\)/);
  assert.doesNotMatch(reach.why, /apiBase/);
});

test("a gateway 503 is named as the gateway's; a server 503 as the server's", () => {
  assert.equal(answeredBy(503, GATEWAY), "gateway");
  assert.equal(answeredBy(503, SERVER), "server");
  assert.equal(answeredBy(503, { "Honua-Instance": "i-1" }), "server");
  assert.equal(answeredBy(404, {}), "unknown");
  assert.equal(answeredBy(null, {}), "unknown");
  const log = new CallLog(BASE);
  log.request("GET", `${BASE}/odata/Layers`);
  log.status("GET", `${BASE}/odata/Layers`, 503, GATEWAY);
  assert.equal(describeCall(log.calls[0]), "GET /odata/Layers → 503 (API Gateway, no server body)");
});

test("a workbench that never requested the candidate is told apart from one that got an error", () => {
  const log = new CallLog(BASE);
  log.request("GET", "https://demo.honua.io/rest/services/honua-workbench/FeatureServer/8/query"); // not ours
  log.request("GET", `${BASE}/api/v1/capabilities/manifest`);
  const reach = workbenchReach(log, "honua-workbench", BASE);
  assert.equal(reach.reached, false);
  assert.match(reach.why, /^the workbench never requested the candidate — \?apiBase= did not reach the sample's own baseUrl/);
});

test("a workbench that got one good server answer passes the reach precondition", () => {
  const log = new CallLog(BASE);
  log.request("GET", QUERY);
  log.status("GET", QUERY, 200, SERVER);
  assert.equal(workbenchReach(log, "honua-workbench", BASE), null);
});

test("an unanswered request says no response, with the failure text", () => {
  const log = new CallLog(BASE);
  log.request("GET", QUERY);
  log.failed("GET", QUERY, "net::ERR_ABORTED");
  assert.match(workbenchReach(log, "honua-workbench", BASE).why, /→ no response \(net::ERR_ABORTED\)$/);
});

test("the editing verdict names the probe and features reads the gateway answered", () => {
  // redis-off: /odata/Layers 200 (server), then Layers(7)/Features 503 from API Gateway.
  const log = new CallLog(BASE);
  const layers = `${BASE}/odata/Layers`;
  const features = `${BASE}/odata/Layers(7)/Features?$top=500`;
  log.request("GET", layers);
  log.status("GET", layers, 200, SERVER);
  log.request("GET", features);
  log.status("GET", features, 503, GATEWAY);
  log.failed("GET", features, "net::ERR_FAILED");
  assert.equal(editingFallbackReason("data: sample fixture · 27 inspections", log),
    'data lane did not go live against the candidate: "data: sample fixture · 27 inspections"; features GET ' +
    "/odata/Layers(7)/Features?$top=500 → 503 (API Gateway, no server body); the page could not read it (net::ERR_FAILED)");

  // redis-on: the Lambda runtime aborted, every probe read is a gateway 500.
  const on = new CallLog(BASE);
  for (const url of [`${BASE}/odata/Layers`, `${BASE}/api/v1/capabilities/manifest`]) {
    on.request("GET", url);
    on.status("GET", url, 500, GATEWAY);
    on.failed("GET", url, "net::ERR_ABORTED");
  }
  const why = editingFallbackReason("data: sample fixture · 27 inspections", on);
  assert.match(why, /probe GET \/odata\/Layers → 500 \(API Gateway, no server body\)/);
  assert.match(why, /probe GET \/api\/v1\/capabilities\/manifest → 500 \(API Gateway, no server body\)/);
});

test("the editing verdict says so when the page never asked or nothing failed", () => {
  const none = new CallLog(BASE);
  assert.match(editingFallbackReason("data: sample fixture · 27 inspections", none), /never requested \/odata\/Layers/);
  const ok = new CallLog(BASE);
  ok.request("GET", `${BASE}/odata/Layers`);
  ok.status("GET", `${BASE}/odata/Layers`, 200, SERVER);
  assert.match(editingFallbackReason("data: sample fixture · 27 inspections", ok), /the page itself chose the fixture/);
});
