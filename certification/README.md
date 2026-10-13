# certification/

Cross-repo certification gates run by the release train against a candidate manifest. The conformance
gate (issue #3) lives here as `.github/workflows/certification.yml` and is wired into the train's
`gate_conformance`.

Certification evidence producers must follow the producer-strict
[receipt v2 contract](RECEIPT-V2.md). Consumers reject v1 and context-less receipts fail-closed; the
linked migration ledger owns the resulting producer reds.

## Conformance gate (real assertions, no false-pass)

Retires the audit's "false conformance" findings at the gate level (both now fixed upstream):
- **geospatial-mcp#25** — the conformance checker reported `FULL` while ignoring resource-family coverage.
- **honua-esri-compat#25** — image/tile cert false-passed any >100-byte body.

The gate is the release-repo half that **runs** the suites against the pinned candidate and turns the
result into a verdict that can FAIL — and refuses to manufacture a green (AGENTS.md):

| job | kind | what it does |
|---|---|---|
| `conformance-mcp` | **real** | clones `geospatial-mcp` **trunk** (the maintained suite that carries the `geospatial-mcp#25` resource-coverage fix — the Phase 0 manifest still pins a pre-fix sha, recorded for traceability), runs its `conformance/check_manifest.py` (`--strict`, with a coverage-only fallback), and parses the verdict with `parse_conformance.py`. **Fails** on any manifest `FAIL`, on a reference impl that is not `FULL` (the exact `geospatial-mcp#25` overstatement), or on **no verdict at all** (vacuous / no evidence). The parser is self-tested in-job before it is trusted. |
| `conformance-esri-geoservices` | blocked | evidence identifier `honua-esri-compat:conformance-esri-geoservices` has no candidate-bound result. |
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
- **Contract / breaking-change** — proto/REST/SDK diff; `version-contract-drift`. (#2 — the proto gate is real in geospatial-grpc; train fan-out is Phase 2.)
- **Artifact-consumption** — `gate-artifact-consume.yml` (install/consume every published artifact). (#4.)
- **Manifest/matrix integrity** — `manifest-validate.yml` (the pinned set satisfies the matrix). (#1.)
- **Cross-cloud parity** — `e2e-cloud-aws.yml` (canonical set on each deploy target). (Phase B.)

## Protocol execution metadata

The protocol ledger accepts a closed, optional group of seven execution fields:
`client_id`, `runner_lane`, `protocol_version`, `protocol_profile`, `performed_by`,
`request_url`, and `exercised_capabilities`. If supplied, the complete group is
required. Actor and lane must match the canonical requirement, and exercised
facets must stay within its governed facets; a pass must cover all of them. Failed
execution can report a subset without hiding the required failure outcome. Text and URL sizes are bounded.
Historical cells outside the canonical source-host harness tuple may omit this
group and retain all existing strict receipt checks. The canonical harness using
`server-protocol-harness` against `source-test-host` always requires the complete
group: removing every field cannot bypass its bound execution/outcome checks.
Unknown fields remain errors.

Network execution requires an absolute HTTP/S URL without embedded credentials.
An unexecuted skip may carry a null URL and empty exercised facets only when it
claims no receipt, digest, evidence URI or facet results. Its existing tier gate
still refuses it whenever the row is required. For executed observations, the
one null-URL exception is the canonical server protocol harness using
`source-test-host-http-adapter` against `source-test-host`, with no execution
image claim. Its v2 receipt must bind the exact execution object, contract revision,
nonempty governed test IDs and actual pass/fail outcomes, plus the five-field
server harness run identity and the exact candidate cut timestamp. The checker recomputes the receipt digest and retains
candidate, source, requirement, freshness and outcome gates. An in-memory adapter
must not invent a network URL or claim TLS coverage. The outer ledger still names
the candidate image; this exception does not certify that image's HTTP deployment.

`tools/fixtures/protocol-execution-v2` retains 9 CITE, 42 harness and 67 SDK cells produced
by the real generator and evidence intake/aggregate code with **synthetic**
run/TRX/ETS inputs. Their hashes and producer metadata are recorded alongside
unchanged receipt bytes. The SDK fixture contains 65 executed passes and two
explicit unexecuted licensed release-tier skips; nightly admission does not
satisfy those release obligations. These are consumer compatibility controls, never live
qualification evidence. The original closed-schema rejection is retained in the
implementation issue; the full chain is replayed against fixture clocks.
