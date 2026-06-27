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

The wire-level breaking-change detectors (buf for proto, OpenAPI diff, SDK public-API diff) live in
the component repos (issues #2/#3); this gate covers the pinned-set + matrix layer.

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
