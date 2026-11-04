import assert from "node:assert/strict";
import test from "node:test";

import { dropRetiredDependency, retiredPackage } from "./drop-retired-transitive.mjs";

test("drops only the retired transitive dependency", () => {
  const manifest = {
    dependencies: {
      "@honua/sdk": "^0.2.3",
      [retiredPackage]: "^0.2.3",
      typescript: "^5.9.2",
    },
  };
  assert.equal(dropRetiredDependency(manifest), true);
  assert.deepEqual(manifest.dependencies, { "@honua/sdk": "^0.2.3", typescript: "^5.9.2" });
  assert.equal(dropRetiredDependency(manifest), false);
  assert.equal(dropRetiredDependency({}), false);
});
