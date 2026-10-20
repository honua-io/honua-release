Resolve the SDK minimum-server rule end to end: classify nightly dispatch anchors, pin the publication receipt, state the known upgrade edges
Closes #233.

## The underlying problem

Every SDK floor rests on the first-release model (`docs/SDK-SERVER-BASELINE-RULE.md`): no earlier honua-server release exists, so every required capability resolves to the first release. All four SDKs already declare that in `release/sdk-capability-baseline.json` (honua-sdk-js#2016, honua-sdk-dotnet#427, honua-sdk-python#290, geospatial-mcp#99). The nightly resolver binds them, but the strict check still refused all four. There were two reasons, plus a third disagreement the lock never saw:

1. **The program's own tags withdrew the model.** Since the 2026-09-26 receipt was recorded, the nightly has created `nightly-candidate-<sha7>` / `nightly-candidate/<sha40>` tags on honua-server so gated workflows can run at a non-head trunk commit. The rule treated any ref as a prior publication. On 2026-10-18 honua-server had 14 such anchors and nothing else (no releases, no version tags), and `server_publication_history verify` refused.
2. **No manifest pinned the receipt.** `components.honua-server.publicationHistory` was never set, so `sdk_baselines.first_release_floor` reported "the lock pins no honua-server publication-history receipt" for every SDK.
3. **Runtime constants contradicted the declared baseline.** The baseline files all say `first-release`, but the floors the SDKs enforce at run time are JS `1.0.0`, .NET `0.1.0` and Python `1.0.0`. No capability introduced any of those numbers.

## Changes here

- `tools/server_publication_history.py`: the receipt records `dispatchAnchors` (each anchor with the commit the tags API resolves it to). `verify` accepts an anchor only when its name is exactly `nightly-candidate-<7 hex>` or `nightly-candidate/<40 hex>` and that hex is a prefix of the commit. Both tag namespaces must be fully accounted for by anchors. Any GitHub release (even on an anchor) and any other tag still withdraw the model. The old receipt format (no anchors) verifies unchanged. The committed receipt is not regenerated, because every SDK cites its digest.
- `platform-manifest.yaml`: pins `honua-server.publicationHistory` to the committed receipt (`sha256:3069fde1…`, the exact URI and digest every SDK baseline cites) with `maxAgeDays: 90`. Every strict check still re-enumerates honua-server live. `.gitattributes` keeps that receipt LF so Windows checkouts match the pin.
- `tools/generate_compatibility_table.py`, **UPGRADE EDGES**: rows now come from lock facts. When the lock pins the receipt and a locked server artifact carries the first-release version, the table states the only edge it can prove: none → first release is a fresh install, with no previous server, so application rollback, database rollback and backup restore don't apply. Pre-release → first release and first release → next release stay **unqualified** in every column. When those facts are missing, every edge is unqualified and the table says which fact is absent.
- `docs/SDK-SERVER-BASELINE-RULE.md`: the dispatch-anchor exception; a new **Runtime floor constants** section (a `first-release` baseline maps to `2026.1.0-0`, the least SemVer identity of the first release line, so candidates `2026.1.0-rc.N`, the release and later servers qualify, and pre-honua-server#4693 `1.0.0+<sha>` builds don't); and a dated **Current state** that replaces the stale 09-26 blocker text.
- `tools/vendor-terms/allowlist.json`: the two generated docs copy the locked compatibility-SDK npm coordinate verbatim. They get the same exact-identifier allowance the manifest already has, and everything else in them stays checked.
- `docs/platform-lock.v1.draft.yaml` / `docs/SDK-SERVER-COMPATIBILITY.md`: regenerated from the committed manifest. The old draft was from 09-29 (rc.2 at `7ba4226`). The committed manifest's SDK pins predate the baseline files, so the committed SDK rows stay explicitly unqualified until the manifest is re-pinned to a candidate. That is the expected state of a draft, not a disagreement.

## SDK repositories (one branch each, linked from #233)

Each sets its runtime constant to the mapped value. Each adds a drift test that reads `release/sdk-capability-baseline.json` and fails if the constant (and, where they exist, the server matrix or compatibility doc) disagrees:

- honua-sdk-js `fix/233-runtime-server-floor`: `HONUA_MINIMUM_SUPPORTED_SERVER_VERSION` `1.0.0` → `2026.1.0-0`; `test/server-floor-baseline.test.ts`. The 2.5D demo fixture now reports `2026.1.0`.
- honua-sdk-dotnet `fix/233-runtime-server-floor`: `HonuaAdminCompatibility.MinimumSupportedServerVersion` `0.1.0` → `2026.1.0-0` (PublicAPI `*REMOVED*` + new; `validate-api-compat.sh` finds no break); `ServerFloorBaselineTests`; `docs/compatibility.md` matrix.
- honua-sdk-python `fix/233-runtime-server-floor`: `MINIMUM_SUPPORTED_SERVER_VERSION` `1.0.0` → `2026.1.0-0`; `compatibility/server-matrix.json` baseline; public API snapshot; `tests/admin/test_server_floor_baseline.py`. The #219 matrix case (`1.0.0+32809f1`, a build from before honua-server#4693 gave the server a version identity) is now deliberately refused.
- geospatial-mcp / `@honua/mcp-server`: they enforce no numeric server floor, so the baseline file is the only declaration and needs no change.

## Verification

- Strict check on the current nightly candidate (run 39136507018: `2026.1-rc.3`, honua-server `c19f29d1`) with this pin added: `generate_compatibility_table.py --check` → `PASS: declared baselines match lock`. All four SDKs resolve to `2026.1.0-rc.3`, every pinned manifest and declaration verifies at its source revision, and the live publication history verifies with 14 anchors and 0 publications. On trunk the same lock refuses all four SDKs.
- Live receipt: `server_publication_history.py collect` → `verify` gives `PASS` (14 anchors, `publishedRefs: []`). Before this change it gave `BLOCKED: … 28 prior publication ref(s)`.
- Focused suites (platform/lock/bundle/resolver/sdk_baselines/publication history/source verification/mint/finalize/compat/vendor terms/workflow contracts/decision record/licensing): 1,244 passed. The only 3 failures reproduce identically on an untouched trunk checkout on Windows (symlink privilege, the bundle signature-boundary test, and the licensing compose-key test).
- Re-run 2026-10-19 after merging trunk `190fc9e`: the 24 focused suites give 1,166 passed, 15 skipped and 3 failed. All 3 failures are Windows-only and outside this diff: the symlink-privilege test, the bundle signature-boundary test, and `test_sdk_source_catalog_versions`, where trunk's `generate-protocol-requirements.py` writes CRLF in text mode on Windows. `test_server_publication_history.py` + `test_sdk_baselines.py`: 117 passed. `--check-output`, `validate_platform.py`, `release_decision_record.py --check`, `check_vendor_terms.py lint --root . --repo honua-release` and `check-baselines --base-ref origin/trunk` pass.
- `check_vendor_terms.py lint` and `check-baselines --base-ref origin/trunk` pass.
- `generate_compatibility_table.py docs/platform-lock.v1.draft.yaml --check-output` and `validate_platform.py` pass, and `release_decision_record.py --check` passes (UTF-8 mode).
- SDK branches: JS vitest compat/floor/2.5D/version-touching suites, `tsc --noEmit`, and `biome check` on the changed files; .NET full solution build (analyzers as errors), `Honua.Sdk.Admin.Tests` 223/223, `AdminBootstrapConsole.Tests` 7/7, `validate-api-compat.sh origin/trunk` with no breaking change; Python full `tests/` 2034 passed and `compatibility_gate.py` passed.

## Follow-ups (not in this PR)

- P2: honua-server reports `2026.1.1` (`Directory.Build.props` `VersionPrefix`), while R22 names the candidate `2026.1.0-rc.N` and GA `2026.1.0`. The runtime floor admits both, but the identity the binary reports and the identity the lock publishes should be reconciled in honua-server.
- P2: the pinned receipt (observed 2026-09-26) expires under `maxAgeDays: 90` on 2026-12-25. Refreshing it changes the digest every SDK baseline cites, which requires republishing all four SDKs. Do that before the cut if the cut slips past then.
- P3: sample fixtures that still advertise `serverVersion` `1.2.0` (JS first-map v1/v2 immutable packs, quickstart playground) never run `checkCompatibility`, so they were left untouched. Refresh them when those packs are next versioned.
