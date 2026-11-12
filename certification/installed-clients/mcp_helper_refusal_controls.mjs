// Synthetic failure diagnostics only; these controls grant no candidate credit.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join, delimiter } from "node:path";
import { spawnSync } from "node:child_process";

export const refusalCredential = "caller credential/with+symbols!'";
// Assembled at runtime so secret scanners do not read the synthetic shapes as leaked AWS key ids.
const syntheticAwsKeyIds = ["AKIA" + "ABCDEFGHIJKLMNOP", "ASIA" + "ABCDEFGHIJKLMNOP"];
const environment = { HONUA_MCP_API_KEY: "inherited-bootstrap-key", CONTROL_DB_PASS: "overlap-secret-with-tail",
  CONTROL_PWD: "overlap-secret", CONTROL_SESSION_MARKER: "seed" };
const shapes = ["hnua_seed_private_tail_to_remove", "Bearer untracked-bearer-value", "Basic dXNlcjpwYXNz",
  "authorization: untracked-auth-value", "cookie=session=untracked-cookie-value", "x-api-key: untracked-api-value",
  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature", ...syntheticAwsKeyIds,
  "password=untracked-db-pass;", "pwd=untracked-pwd;", "postgres://somebody:untracked-url-pass@localhost/db"];

export async function withRefusingPython(directory, run) {
  const python = spawnSync("python3", ["-c", "import sys;print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
  assert.ok(python.startsWith("/"));
  const values = [refusalCredential, ...Object.values(environment).filter(value => value.length >= 8)];
  writeFileSync(join(directory, "python3"), `#!${python}
import base64,sys,urllib.parse
print('controlled helper diagnostic',file=sys.stderr)
for value in ${JSON.stringify(values)}:
    print(value,file=sys.stderr); print(urllib.parse.quote(value,safe=''),file=sys.stderr); print(base64.b64encode(value.encode()).decode(),file=sys.stderr)
for value in ${JSON.stringify(shapes)}: print(value,file=sys.stderr)
sys.exit(7)
`, { mode: 0o755 });
  const before = Object.fromEntries(["PATH", ...Object.keys(environment)].map(key => [key, process.env[key]]));
  Object.assign(process.env, environment, { PATH: directory + delimiter + before.PATH });
  try { await run(); }
  finally {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
}

export function assertRefusalRedacted(error) {
  assert.match(error.message, /status=7/);
  assert.match(error.message, /controlled helper diagnostic/);
  for (const value of [refusalCredential, ...Object.values(environment).filter(value => value.length >= 8)])
    for (const form of [value, encodeURIComponent(value), Buffer.from(value).toString("base64")])
      assert.ok(!error.message.includes(form), "credential representation survived refusal redaction");
  for (const value of ["private_tail_to_remove", "untracked-bearer-value", "dXNlcjpwYXNz", "untracked-auth-value",
    "untracked-cookie-value", "untracked-api-value", "eyJhbGciOiJIUzI1NiJ9", ...syntheticAwsKeyIds,
    "untracked-db-pass", "untracked-pwd", "untracked-url-pass", "-with-tail"])
    assert.ok(!error.message.includes(value), "credential shape survived refusal redaction");
}
