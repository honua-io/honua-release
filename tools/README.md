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

## `candidate_binding.py` — certified-candidate integrity boundary

Packages the frozen `platform-manifest.yaml` and `compatibility-matrix.yaml` with the platform gate
report. The report binds both files by SHA-256 and size and records the certifying source repository,
source SHA and branch, workflow path, run id and attempt, and explicit `live`/`dry-run` certification
mode. `promote.yml` fetches the selected run, repository, and branch from the GitHub API. It requires
the run to be successful, from the protected current default branch, and bound to `live` mode. It
then requires all identity fields and both artifact digests to match before it parses the manifest,
generates a BOM, signs anything, or creates a release. Missing protection and legacy reports without
an explicit boolean `dry_run` field fail closed.

Promotion also preflights the `production` environment. It requires a protected-branch deployment
policy and the configured human reviewer before doing release work. This is deliberately stricter
than merely naming an environment in workflow YAML, because GitHub can otherwise create an
unprotected environment implicitly.

The release train publishes the three files together as the immutable `certified-candidate` artifact.
Promotion checks out release tooling at the certified source SHA and passes the bundled files by
explicit path; files from the branch that happens to be current at promotion time are never inputs.

```bash
python -m pytest tools/test_candidate_binding.py tools/test_finalize_release.py tools/test_workflow_contracts.py
```
