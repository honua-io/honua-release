# tools/ — release-repo gate tooling

GitHub Actions supply-chain pins are enforced by `check_action_pins.py`; the review and update
procedure is documented in [`docs/ACTION-PINNING.md`](../docs/ACTION-PINNING.md).

## `validate_platform.py` — Phase 0 source-of-truth gate (issue #1)

Validates `platform-manifest.yaml` + `compatibility-matrix.yaml` and enforces compatibility drift.
The manifest + matrix are only a credible source of truth if something checks them and that check
can **fail** (AGENTS.md: a gate that can't fail is worse than no gate).

Three layers of check:

| Layer | Asserts | Reddens when… |
|---|---|---|
| **structure** | both files parse; required keys present; every range parses; every client/component the matrix names exists in the manifest | a typo'd client, a malformed range, a component with neither a semver `version` nor a `pre-release`+sha pin |
| **coherence** | each pinned semver version **satisfies** every matrix range that names it; the sha couplings (iac/helm → server image, server → db schema) agree between the two files | a pin bumped out of its range, a range tightened past the pin, or a sha/db value that disagrees across files |
| **drift** | vs a git baseline, a matrix range may only **widen** unless the contract's `version` is bumped | a client's support window narrowed (floor raised / ceiling lowered) or a client dropped, with no contract-version bump — the manifest/matrix half of "a breaking change without a MAJOR bump fails CI" |

The proto wire-breaking detector (buf) runs in `gate-contract.yml` against geospatial-grpc's release
tags. The REST/OpenAPI + SDK public-API half runs via `contract_surface.py` (below); this
`validate_platform.py` gate covers the pinned-set + matrix layer.

## `contract_surface.py` — REST/OpenAPI + SDK public-API drift (gate b, `contract-rest-sdk`)

Build-free extraction + diff of the manifest-pinned components' REST/OpenAPI + SDK public-API
surfaces against the committed baseline in `contracts/baselines/<platform>/`. `2026.1-rc.0` is the
baseline-setting release (diff empty ⇒ pass); a later surface change without a baseline refresh
fails. Surfaces are read deterministically from committed source at each pinned sha (no dotnet/npm
toolchain), so re-extraction in CI reproduces the committed baseline.

```bash
python tools/contract_surface.py update      # establish/refresh baseline from current manifest pins
python tools/contract_surface.py check       # gate: pass|fail|blocked (exit 0|1|3)
python -m pytest tools/test_contract_surface.py   # self-test (proves pass/fail/blocked can fire)
```

Repos are expected as siblings of `honua-release` (override with `--repos-root`). The gate workflow
(`gate-contract.yml` → `rest-sdk-api`) clones the pinned components and runs `check`.

### Run

```bash
python tools/validate_platform.py                      # structure + coherence
python tools/validate_platform.py --baseline origin/main   # + drift vs that ref
python -m pytest tools/test_platform.py                # self-test (proves each rule can fail)
```

CI: `.github/workflows/manifest-validate.yml` runs this per-PR (drift vs the PR base) and is
callable by the release train as a reusable gate (`workflow_call`, input `baseline_ref`).

Only dependency: `pyyaml`. `semver.py` is a minimal stdlib SemVer + range implementation (no
third-party semver lib).

## `check_ai_delivery_arc.py` — D9.3 candidate journey gate

Consumes the manifest-pinned `honua-sdk-js` zero-to-map plan/receipt and checks
the 396-operation Admin API boundary, 119-tool semantic MCP family, focused
Console boundary, dual native + Esri-compatible AI GP execution, Studio artifact
use, proposal checkpoint, candidate identity, and final URL. It emits one
manifest-digest-bound receipt with the first failing stage/action/tool named.

```bash
python tools/check_ai_delivery_arc.py --sdk-root ../honua-sdk-js \
  --json-out e2e/out/ai-delivery-arc-receipt.json
python -m pytest tools/test_check_ai_delivery_arc.py -q
```

## `candidate_binding.py` — certified-candidate integrity boundary

Packages the frozen `platform-manifest.yaml` and `compatibility-matrix.yaml` with the platform gate
report. The report binds both files by SHA-256 and size and records the certifying source repository,
source SHA and branch, workflow path, run id and attempt, and explicit `live`/`dry-run` certification
mode. `promote.yml` fetches the selected run, repository, and branch from the GitHub API. It requires
the run to be successful, from the protected current default branch, and bound to `live` mode. It
then requires all identity fields and both artifact digests to match before it parses the manifest,
generates a BOM, signs anything, or creates a release. Missing protection and legacy reports without
an explicit boolean `dry_run` field fail closed.

Promotion also preflights the `release-promotion` environment. It requires a protected-branch deployment
policy and the configured human reviewer before doing release work. This is deliberately stricter
than merely naming an environment in workflow YAML, because GitHub can otherwise create an
unprotected environment implicitly.

The release train publishes the three files together as the immutable `certified-candidate` artifact.
Promotion checks out release tooling at the certified source SHA and passes the bundled files by
explicit path; files from the branch that happens to be current at promotion time are never inputs.

```bash
python -m pytest tools/test_candidate_binding.py tools/test_finalize_release.py tools/test_workflow_contracts.py
```

## `check_capabilities.py` — docs gate (h): advertised-vs-actual + `capability-key` evidence (honua-release#59)

Every `shipped` claim in `docs/capabilities.yaml` needs `evidence` that resolves to something real:
`canonical-check`/`gate`/`test` (unchanged), plus a new `capability-key` kind that resolves against
honua-evidence's `capability-matrix.v1.json`. A claim passes only if the matrix key has an implemented
(non-experimental) surface, `provingTestCount` at or above `docs/capabilities.yaml`'s
`defaults.minProvingTests` floor, and 100% CITE pass rate on every joined suite. The matrix is fetched
by the calling workflow (`gate-docs.yml`, raw.githubusercontent pinned to `HONUA_EVIDENCE_REF`) and
handed in via `--capability-matrix`/`HONUA_CAPABILITY_MATRIX` — a missing/unfetchable/malformed matrix
resolves every `capability-key` claim to `blocked`, never a fake pass.

```bash
python tools/check_capabilities.py --capability-matrix path/to/capability-matrix.v1.json
python -m pytest tools/test_check_capabilities.py -q
```

## `check_ga_surface.py` — docs gate (h): advertised-GA ⊆ evidenced-GA (honua-release#59)

Sibling of the above: applies the SAME GA criteria (`check_capabilities.resolve_capability_key`) to
EVERY matrix key the server implicitly advertises as GA — `noSurface` falsy and
`maturity.implemented > 0` — not just the hand-picked `docs/capabilities.yaml` claims. Reports
per-key verdicts so a red is diagnosable. Wired into `gate-docs.yml`'s `ga-surface` job; in bootstrap
(PR / dry-run train cut) a real fail is reported (`blocked`, not reddening the train) since it will
legitimately surface pre-existing honua-server gaps ahead of a companion re-grade fix; `strict` (a
real cut) enforces it fully.

```bash
python tools/check_ga_surface.py --matrix path/to/capability-matrix.v1.json
python -m pytest tools/test_check_ga_surface.py -q
```

## `check_evidence_freshness.py` — freeze-phase gate: evidence lineage + freshness (honua-release#60, #84)

Proves the honua-evidence capability matrix backing the claims above is actually ABOUT the release
candidate's pinned honua-server SHA (`platform-manifest.yaml`) and isn't stale, before a candidate is
certified. `lineage` needs the sha ancestor/descendant relationship (computed by the workflow shim,
`gate-evidence.yml`, via the GitHub compare API — never inside this module); `freshness` reads each
configured producer's age from the matrix's own `freshness` block against thresholds in
`certification/evidence-freshness.yaml`. A missing matrix, undecidable lineage, or a producer absent
from the freshness contract (honua-io/honua-evidence#8 pending) reports `blocked`; a genuinely
diverged sha or stale producer is `fail` in both dry-run and real cuts.

Two more checks landed with honua-release#84:

- **`ledger`** — the matrix's own `generatedAt` against `ledger.maxAgeHours`. Every per-producer
  verdict is computed from timestamps honua-evidence's aggregator stamps, so a *stalled* aggregator
  freezes them all at whatever they last said. On 2026-08-16 exactly that happened for 42h
  (honua-io/honua-evidence#17) and this gate stayed green the whole time, because the frozen
  `server-matrix` `fetchedAt` was still inside its 48h window. Checking `generatedAt` directly names
  the real failure instead of misattributing it to whichever producer ages out first.
- **`producer:<name>`** — every producer the ledger carries, not just the two with thresholds. One
  the ledger self-reports as `stale`/`missing` must be named in the config's `acknowledged:` block
  with an owning issue and an unexpired `reviewBy`, or the gate goes red. Same contract, and the same
  reason, as the demo canary's `e2e/canary-quarantine.yaml`: a known gap must be owned, never silent,
  never deleted. An acknowledgement never overrides a real threshold, expires hard at `reviewBy`, and
  is annotated as rot once its producer recovers.

```bash
python tools/check_evidence_freshness.py --manifest platform-manifest.yaml \
    --matrix path/to/capability-matrix.v1.json --lineage-status ancestor
python -m pytest tools/test_check_evidence_freshness.py -q
```
