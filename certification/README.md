# certification/

Cross-repo certification gates run by the release train against a candidate manifest. The conformance
gate (issue #3) lives here as `.github/workflows/certification.yml` and is wired into the train's
`gate_conformance`.

## Conformance gate (real assertions, no false-pass)

Retires the audit's "false conformance" findings at the gate level (both now fixed upstream):
- **geospatial-mcp#25** — the conformance checker reported `FULL` while ignoring resource-family coverage.
- **honua-esri-compat#25** — image/tile cert false-passed any >100-byte body.

The gate is the release-repo half that **runs** the suites against the pinned candidate and turns the
result into a verdict that can FAIL — and refuses to manufacture a green (AGENTS.md):

| job | kind | what it does |
|---|---|---|
| `conformance-mcp` | **real** | clones `geospatial-mcp` **trunk** (the maintained suite that carries the `geospatial-mcp#25` resource-coverage fix — the Phase 0 manifest still pins a pre-fix sha, recorded for traceability), runs its `conformance/check_manifest.py` (`--strict`, with a coverage-only fallback), and parses the verdict with `parse_conformance.py`. **Fails** on any manifest `FAIL`, on a reference impl that is not `FULL` (the exact `geospatial-mcp#25` overstatement), or on **no verdict at all** (vacuous / no evidence). The parser is self-tested in-job before it is trusted. |
| `conformance-esri-geoservices` | blocked | the `honua-esri-compat` lanes need a live candidate server **and** a licensed Esri toolchain (arcpy / ArcGIS Maps SDK), which CI lacks. Reports `blocked` until `HONUA_SERVER_URL` + a licensed runner are provided. |
| `conformance-ogc-stac` | blocked | OGC API (CITE TEAM Engine) + STAC conformance run against a deployed candidate (Phase B). |

**enforcement** (same contract as `gate-artifact-consume`): `bootstrap` (default) tolerates a
no-candidate-server `blocked` so the train is runnable before staging infra exists, but a real
conformance FAILURE always fails the gate; `strict` fails on any non-pass (incl. `blocked`). A real
cut (`dry_run=false`) runs `strict`.

The report job emits `certification-gate-report.json` via the shared `gate-fragment` action (fragments
are namespaced with a `cert-` prefix so the train can call this and `gate-artifact-consume` in the same
run without cross-reading fragments). The train consumes the report; it never scrapes logs.

### Run / test
```bash
python -m pytest certification/test_conformance.py     # self-test the verdict parser (proves it fails)
# the workflow: Actions ▸ certification ▸ Run workflow   (or per-PR on certification/** changes)
```

## Per-repo build/test gate (consume continuous-green)

`gate-build-test.yml` + `check_build_test.py` implement release gate (a) **without re-running every
repo's suite at cut time** (docs/TEST-STRATEGY.md). For every component in `platform-manifest.yaml`,
it confirms that component's **GitHub CI is green on its exact pinned SHA** (the `check-runs` of that
commit). Per-component verdict: `pass` (a green run exists, all conclusions green) · `fail` (any red
conclusion) · `blocked` (sha/repo unresolvable, no runs, or CI still in progress — never silently
passed). `bootstrap` tolerates a not-yet-built (blocked) pin; `strict` (a real cut) fails on it; a red
CI always fails. `check_build_test.py` is unit-tested (`test_build_test.py`, proving green-only passes).

The live check runs in the train (`gate_build_test`), nightly, and on dispatch — **not** on unrelated
honua-release PRs (a stale manifest pin isn't an individual PR's fault); PRs run only the self-tests.

## Other certification gates (separate workflows)
- **D9.3 AI delivery arc** — `ai-delivery-arc.yaml` +
  `tools/check_ai_delivery_arc.py` consume the exact manifest-pinned SDK journey,
  bind all receipts to one candidate, and keep contract evidence distinct from a
  live release recording. Local Docker and AWS ECS are both required execution
  targets; an ECS provisioning receipt cannot substitute for its full delivery-arc
  receipt. The SDK contract requires distinct map/app/dashboard immutable
  save/read/reopen actions through the server Studio MCP lifecycle. AWS and
  real-model Studio producers use target/check-bearing
  `release-evidence-receipt.schema.json`. (#121–#123.)
- **Contract / breaking-change** — proto/REST/SDK diff; `version-contract-drift`. (#2 — the proto gate is real in geospatial-grpc; train fan-out is Phase 2.)
- **Artifact-consumption** — `gate-artifact-consume.yml` (install/consume every published artifact). (#4.)
  A strict cut requires the manifest-pinned artifact from its customer-facing registry and rejects
  every local source fallback; see `docs/PUBLIC-REGISTRY-READINESS.md` for the first-publish ledger.
- **Manifest/matrix integrity** — `manifest-validate.yml` (the pinned set satisfies the matrix). (#1.)
- **Cross-cloud parity** — `e2e-cloud-aws.yml` (canonical set on each deploy target). (Phase B.)
