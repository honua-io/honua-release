# Issue #343: pinned-source and local Docker rejection evidence

This directory contains diagnostic evidence, **not a DR receipt**. The requested
capability-only fix cannot satisfy the acceptance criteria at the specified pin.

- Release candidate branch head: `baa16bd3ee88b64bd47a97f62d58523ea3510abf` (PR #342).
- Server source: `9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd`.
- Image: `ghcr.io/honua-io/honua-server@sha256:a5d962958ec8a6890ecd0f5f34f1da9c08a9d464da0418bdfbbc381c754d30fc`.
- Database schema floor: `116`.
- Manifest SHA-256: `93128f683d7cc09f83e4d3b64fc2273edc84170abfc7693bad1315e391257619`.

## Source findings

The pinned server was fetched and checked out on real disk at
`/home/mike/honua-io/wt-server-dr-343` before running the drill.

1. [PublishLayerRequest, lines 12–100](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Server/Features/Admin/Models/LayerPublishingModels.cs#L12)
   has **no capability request property**. The endpoint's request mapping also
   has none. Adding a guessed `capabilities` member cannot declare Create.
2. [PostgreSqlLayerPublishingService, line 48](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Db/Postgres/Features/Admin/PostgreSqlLayerPublishingService.cs#L48):
   `private static readonly string[] _defaultCapabilities = ["Query", "Extract"];`
   [BuildPublishedPublication, line 3356](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Db/Postgres/Features/Admin/PostgreSqlLayerPublishingService.MetadataV2Graph.cs#L3356)
   writes `Capabilities = _defaultCapabilities,`.
3. [MetadataV2EditCapabilities.SupportsDeclared, lines 106–111](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Core.Abstractions/Features/Metadata/Domain/V2/MetadataV2EditCapabilities.cs#L106)
   resolves the publication's declared set and returns false when it omits Create.
   [LayerValidationHelpers, lines 977–989](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Hosting/Features/Validation/LayerValidationHelpers.cs#L977)
   converts that denial to HTTP 405.
4. Declaring Create is insufficient. [FeatureStorageMapping, lines 91–93](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Core.Abstractions/Features/FeatureStore/Domain/FeatureStorageMapping.cs#L91)
   only allows managed writes for a non-source-backed mapping or the managed
   `features` table. The imported `honua_data.imported_dr_sentinel` table fails
   this guard. [LayerValidationHelpers, lines 1051–1062](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/src/Honua.Hosting/Features/Validation/LayerValidationHelpers.cs#L1051)
   returns HTTP 501 with: `This collection is published over an external source table, which this server cannot write through.`
5. [OgcFeaturesInsert_OnSourceBackedLayerDeclaringCreate_IsRefusedAsUnserviceable](https://github.com/honua-io/honua-server/blob/9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd/tests/dotnet/Honua.Server.Tests/Admin/LayerPublishingIntegrationTests.cs#L1303)
   explicitly declares Create in the test fixture, asserts HTTP 501 at lines
   1344–1346, and asserts unchanged source and snapshot rows. Server #4712 fixed
   #4707 by refusing the unreadable write, not by implementing source-table edits.

## Runtime reproduction

Run from a detached worktree of the exact PR #342 head above:

```sh
HONUA_DR_PROJECT=honua-dr-343-proof python3 e2e/dr-drill/full_platform.py \
  --output /tmp/honua-dr-343-proof --keep
```

The unmodified drill exits 1 in `seed_state` with HTTP 405. Both imported features
are served by OGC API Features. For a separate diagnostic, set only dr-sentinel's
publication capabilities in the disposable database's active Metadata v2 snapshot
to `["Query", "Extract", "Create"]`, restart the server to reload that metadata,
and repeat the identical insert. It returns HTTP 501. The diagnostic SQL injection
matches the capability setup in the server regression test; it is not a supported
admin API, a proposed seed change, or recovery evidence.

`diagnostic.json` retains both actual HTTP responses, candidate identity, the
injected declarations, unchanged feature counts/content, and zero outbox rows.
The diagnostic does not reach backup/recovery, emit a receipt, or sign a success
claim. No gate, test, source pin, or fleet branch was changed.

The next implementation needs either a supported managed-store publication/write
fixture (a different publication setup) or a newly imaged server implementing both
publication capability input and source-table writes. The requested read-back of
an inserted source-table feature cannot pass at this exact pin.

Refs #343 (released: pinned admin publication API has no capability field, and source-backed inserts still return 501 with Create declared)
