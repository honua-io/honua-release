# tools/ — release-repo gate tooling

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

## Platform lock v1 (issue #231, part 1)

`schemas/platform-lock.v1.schema.json` is the release-candidate identity contract. It carries the
platform identity/status/support tier, immutable source inputs, per-component source revision and
lifecycle, contract/schema versions, exact artifact coordinates and integrity, MCP/catalog/OKF
digests, fixture revisions, SBOM/provenance references, and release notes.

Generate an honest partial lock and its release worklist:

```bash
python3 tools/generate_platform_lock.py --output /tmp/platform-lock.v1.draft.yaml
```

The generator writes the partial draft but exits 1 while any value cannot be resolved from
`platform-manifest.yaml` and `compatibility-matrix.yaml`. Missing values are omitted—not replaced
with placeholders—so a non-zero result is expected until release manufacture/signing (part 2)
supplies registry and evidence identities.

Validate a manufactured lock:

```bash
python3 tools/validate_platform_lock.py platform-lock.v1.yaml
python3 -m pytest tools/test_platform_lock.py
```

Validation refuses placeholders, floating tags, carried-forward/source-built identities, missing
type-specific integrity, and any mismatch between a component source revision and the revision
attested by its released artifact.
