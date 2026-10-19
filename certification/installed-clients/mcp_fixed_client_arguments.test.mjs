import assert from "node:assert/strict";
import test from "node:test";
import { inspectorToolArguments } from "./mcp_fixed_client_launcher.mjs";

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
