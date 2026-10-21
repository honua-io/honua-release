import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createOpsHealthValidation, opsHealthDependencies } from "./mcp_ops_health_validation.mjs";
import { startControlledCandidate, prepareControlledInstallation, controlCredential } from "./mcp_map_package_wire_controls.mjs";

const original = fileURLToPath(new URL("./", import.meta.url));
const support = ["mcp_ops_health_validation_controls.py", "mcp_map_package_validation_controls.py", "mcp_render_validation_controls.py",
  "mcp_render_bootstrap.py", "regression.py", "oracles.py"];
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function setup(live = false) {
  const directory = mkdtempSync(join(tmpdir(), "release-ops-health-controls-"));
  for (const name of [...opsHealthDependencies, ...support]) copyFileSync(join(original, name), join(directory, name));
  const installationRoot = live ? prepareControlledInstallation(directory, join(directory, "mcp_fixed_client_manifest.v1.json")) : null;
  const generated = spawnSync("python3", ["-B", "-c",
    "import json,base64; from mcp_ops_health_validation_controls import controls,bootstrap; print(json.dumps({'observations':controls(),'bootstrapBytesBase64':base64.b64encode(json.dumps(bootstrap()).encode()).decode()}))"],
    { cwd: directory, encoding: "utf8", timeout: 20_000, maxBuffer: 16 * 1024 * 1024 });
  assert.equal(generated.status, 0, generated.stderr);
  const fixture = JSON.parse(generated.stdout), bootstrapPath = join(directory, "bootstrap.json");
  writeFileSync(bootstrapPath, Buffer.from(fixture.bootstrapBytesBase64, "base64"));
  const context = { directory, sourceSha: "a".repeat(40), bootstrapPath,
    sha256: Object.fromEntries(opsHealthDependencies.map(name => [name, hash(readFileSync(join(directory, name)))])) };
  const observations = join(directory, "observations.json"); writeFileSync(observations, JSON.stringify(fixture.observations));
  const judged = spawnSync("python3", ["-I", "-B", join(directory, "mcp_ops_health_validation.py"), "--fixture", join(directory, "fixture.v1.json"),
    "--bootstrap", bootstrapPath, "--observations", observations], { encoding: "utf8", timeout: 20_000 });
  assert.equal(judged.status, 0, judged.stderr);
  const proof = { ...JSON.parse(judged.stdout), schema: "honua.mcp-ops-health-proof/v1", helperSourceSha: context.sourceSha,
    helperSha256: `sha256:${context.sha256["mcp_ops_health_validation.py"]}`, observations: fixture.observations };
  const api = createOpsHealthValidation(context);
  const verify = (value = proof) => api.verifyOpsHealthProof(value, proof.candidateSourceSha, proof.candidateImage,
    "tools/call:ops_health", proof.canonicalClient, proof.clientVersion);
  return { directory, context, proof, api, verify, fixture, installationRoot, close: () => rmSync(directory, { recursive: true, force: true }) };
}

test("complete offline health proof rejudges both operations and refuses missing independent evidence", () => {
  const control = setup();
  try {
    for (const operation of control.proof.operations) assert.equal(control.api.verifyOpsHealthProof(control.proof, control.proof.candidateSourceSha,
      control.proof.candidateImage, operation, control.proof.canonicalClient, control.proof.clientVersion), true);
    for (const key of ["observations", "independentStores", "restartRecomputed", "liveNotPersisted", "anonymousInvocationRefusal"]) {
      const changed = structuredClone(control.proof); delete changed[key]; assert.equal(control.verify(changed), false);
    }
    const missing = structuredClone(control.proof); delete missing.observations.calls[0].before;
    assert.equal(control.verify(missing), false);
    const fabricated = structuredClone(control.proof); fabricated.observations.calls[0].before.redis.jobs = [];
    assert.equal(control.verify(fabricated), false);
    assert.equal(control.api.verifyOpsHealthProof(control.proof, "f".repeat(40), control.proof.candidateImage,
      "tools/call:ops_health", control.proof.canonicalClient, control.proof.clientVersion), false);
  } finally { control.close(); }
});

for (const name of opsHealthDependencies) test(`offline health positive refuses changed protected dependency ${name}`, () => {
  const control = setup();
  try {
    assert.equal(control.verify(), true);
    writeFileSync(join(control.directory, name), "unreviewed changed bytes");
    assert.equal(control.verify(), false);
    assert.throws(() => createOpsHealthValidation(control.context), /dependency changed/);
  } finally { control.close(); }
});

test("health refuses absent disposable ownership before state probe or actual client launch", async () => {
  const control = setup();
  try {
    const checks = [];
    await control.api.opsHealthScenarios(async (operation, run) => {
      try { checks.push([operation, await run()]); } catch (error) { checks.push([operation, error.message]); }
    }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
      canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion,
      credential: "controlled-private-key", installationRoot: "/absent", ownedDisposableCandidate: false });
    assert.equal(checks.length, 2);
    assert.match(checks[0][1], /explicitly owned disposable candidate/);
    assert.match(checks[1][1], /did not produce rejudged proof/);
  } finally { control.close(); }
});

// This controlled HTTP/process test exercises the production producer and real
// protected semantic oracle. Independent-state observations below are explicit
// OFFLINE vectors; actual observer commands have separate Redis/PostGIS controls.
// No stand-in package, Docker program or state vector grants candidate credit.
for (const mode of ["valid", "fabricated-zero-queue", "cached-after-restart", "reused-session"]) {
  test(`offline HTTP/fixed process/live-health producer and complete oracle: ${mode}`, async () => {
    const control = setup(true), priorPath = process.env.PATH;
    const raw = control.fixture.observations;
    const runtimePath = join(control.directory, "offline-runtime.json"), independentPath = join(control.directory, "offline-independent-vector.json");
    const commandsPath = join(control.directory, "offline-state-commands.json");
    writeFileSync(runtimePath, JSON.stringify(raw.runtimeBefore)); writeFileSync(independentPath, JSON.stringify(raw.calls[0].before));
    writeFileSync(commandsPath, "[]");
    const realPython = spawnSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).stdout.trim();
    assert.ok(realPython.startsWith("/"));
    writeFileSync(join(control.directory, "python3"), `#!${realPython}
import json,os,sys
from pathlib import Path
from datetime import datetime,timezone
if any(arg.endswith('/mcp_ops_health_state.py') for arg in sys.argv[1:]):
    commands=Path(${JSON.stringify(commandsPath)}); seen=json.loads(commands.read_text()); seen.append(sys.argv[1:]); commands.write_text(json.dumps(seen))
    runtime=Path(${JSON.stringify(runtimePath)})
    if '--restart' in sys.argv:
        value=json.loads(runtime.read_text()); value['startedAt']=datetime.now(timezone.utc).isoformat(); runtime.write_text(json.dumps(value)); print(json.dumps(value))
    else:
        value=json.loads(Path(${JSON.stringify(independentPath)}).read_text()); value['startedAt']=datetime.now(timezone.utc).isoformat(); value['completedAt']=datetime.now(timezone.utc).isoformat(); print(json.dumps(value))
else:
    os.execv(${JSON.stringify(realPython)},[${JSON.stringify(realPython)},*sys.argv[1:]])
`, { mode: 0o755 });
    writeFileSync(join(control.directory, "docker"), `#!${realPython}
import json,sys
from pathlib import Path
value=json.loads(Path(${JSON.stringify(runtimePath)}).read_text())
print(json.dumps(value['imageReadback'] if sys.argv[1]=='image' else {key:value[key] for key in ['running','imageId','imageReference','startedAt','containerId']}))
`, { mode: 0o755 });
    const responses = [0, 2, 4, 6].map(index => structuredClone(raw.calls[index].result));
    const reads = [1, 3, 5].map(index => structuredClone(raw.calls[index].result));
    let candidate, rendered = 0, originalPayload;
    const update = body => {
      if (mode === "cached-after-restart" && rendered++ >= 2) return structuredClone(originalPayload);
      const now = Date.now(), stamp = time => new Date(time).toISOString();
      body.generatedAt = stamp(now);
      body.alertDispatch.lastPollAt = body.alertDispatch.backlogObservedAt = stamp(now - 1000);
      for (const source of body.evidencePosture.sources) {
        const observed = source.sourceId === "honua_ops_health" || source.sourceId.endsWith(".alert_dispatch") ? now - 1000 : now;
        source.observedAt = source.lastSuccessfulAt = stamp(observed);
        source.validUntil = stamp(observed + source.maximumAgeSeconds * 1000);
      }
      if (mode === "fabricated-zero-queue") body.geoprocessing = { available: true, totalActive: 0, buckets: [] };
      originalPayload ??= structuredClone(body);
      return body;
    };
    candidate = await startControlledCandidate(responses, reads, {
      sessionPrefix: "offline-live-health-session", reuseSessions: mode === "reused-session",
      onRead: () => { reads[0] = structuredClone(candidate.seen.at(-1).params.uri === "honua://ops/health" ? raw.calls[1].result : raw.calls[3].result); },
      responseMutation: frame => {
        if (frame.result?.structuredContent?.overallStatus) {
          frame.result.structuredContent = update(frame.result.structuredContent);
          frame.result.content[0].text = "Ops health snapshot returned in structuredContent.";
        } else if (frame.result?.contents?.[0]?.text) {
          frame.result.contents[0].text = JSON.stringify(update(JSON.parse(frame.result.contents[0].text)));
        }
        return frame;
      },
    });
    process.env.PATH = control.directory + delimiter + priorPath;
    try {
      const checks = {}, results = join(control.directory, "offline-results.json");
      await control.api.opsHealthScenarios(async (operation, run) => {
        try { checks[operation] = { result: "pass", value: await run() }; }
        catch (error) { checks[operation] = { result: "fail", error: String(error) }; }
      }, { candidateSourceSha: control.proof.candidateSourceSha, candidateImage: control.proof.candidateImage,
        canonicalClient: control.proof.canonicalClient, clientVersion: control.proof.clientVersion,
        credential: controlCredential, installationRoot: control.installationRoot, ownedDisposableCandidate: true, results });
      assert.deepEqual(Object.keys(checks), control.proof.operations);
      const observed = JSON.parse(readFileSync(`${results}.ops-health.raw.json`));
      assert.equal(observed.calls.length, 8);
      assert.equal(candidate.seen.length, 8);
      const stateCalls = JSON.parse(readFileSync(commandsPath));
      assert.equal(stateCalls.length, 9);
      assert.equal(stateCalls.filter(args => args.includes("--restart")).length, 1);
      if (mode === "valid") {
        for (const operation of control.proof.operations) assert.equal(checks[operation].result, "pass", JSON.stringify(checks));
        assert.equal(control.verify(checks[control.proof.operations[0]].value), true);
        assert.equal(checks[control.proof.operations[0]].value.liveNotPersisted, true);
      } else {
        for (const operation of control.proof.operations) assert.equal(checks[operation].result, "fail", mode);
      }
    } finally { process.env.PATH = priorPath; await candidate.close(); control.close(); }
  });
}
