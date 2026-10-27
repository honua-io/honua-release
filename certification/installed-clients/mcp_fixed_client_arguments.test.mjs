import assert from "node:assert/strict";
import test from "node:test";
import { anonymousDiscoveryView, inspectorToolArguments } from "./mcp_fixed_client_launcher.mjs";

test("empty get_style arguments leave subsequent metadata as a CLI option", () => {
  const args = [...inspectorToolArguments("honua_get_style", {}), "--metadata", 'honua.io/workflow-view="full"'];
  assert.deepEqual(args, ["--tool-name", "honua_get_style", "--metadata", 'honua.io/workflow-view="full"']);
  assert.equal(args.includes("--tool-arg"), false);
});

test("nonempty Inspector arguments retain every typed JSON pair", () => {
  const values = { styleId: "owned-style", dryRun: true, layerId: 0, values: ["--metadata", 2], nested: { enabled: false } };
  const args = inspectorToolArguments("honua_apply_style_preset", values);
  assert.deepEqual(args.slice(0, 3), ["--tool-name", "honua_apply_style_preset", "--tool-arg"]);
  const observed = Object.fromEntries(args.slice(3).map(pair => {
    const separator = pair.indexOf("="); return [pair.slice(0, separator), JSON.parse(pair.slice(separator + 1))];
  }));
  assert.deepEqual(observed, values);
});

test("anonymous Inspector calls select the one view that advertises the operation", () => {
  // honua-server 8cb074f7 anonymous tools/list per view; the default view omits all of these.
  const expected = {
    honua_ingest_dataset: "setup", honua_publish_service: "setup", honua_get_style: "setup",
    honua_apply_style_preset: "setup", honua_execute_plan: "setup", honua_publish_result: "setup",
    honua_ops_findings: "operate", honua_alert_events: "operate", honua_operate_events: "operate",
    honua_ops_health: "operate", honua_platform_release_status: "operate",
    honua_ground_candidates: "analyze", honua_clarify_intent: "analyze", honua_cancel_job: "analyze",
    honua_create_map_package: "setup", honua_create_app_package: "setup",
  };
  for (const [name, view] of Object.entries(expected)) assert.equal(anonymousDiscoveryView(name), view, name);
  // Not advertised to anonymous callers by any view: no view is invented for them.
  for (const name of ["honua_geocode_addresses", "honua_solve_route", "honua_plan_analysis", "honua_admin_layer_publish"])
    assert.equal(anonymousDiscoveryView(name), undefined, name);
});
