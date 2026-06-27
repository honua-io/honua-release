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

## Other certification gates (separate workflows)
- **Contract / breaking-change** — proto/REST/SDK diff; `version-contract-drift`. (#2, component repos.)
- **Artifact-consumption** — `gate-artifact-consume.yml` (install/consume every published artifact). (#4.)
- **Manifest/matrix integrity** — `manifest-validate.yml` (the pinned set satisfies the matrix). (#1.)
