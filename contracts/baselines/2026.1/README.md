# Contract baseline — Honua 2026.1

The reviewed baseline for the `contract-rest-sdk` half of the contract gate (gate b). It is the
frozen REST/OpenAPI + SDK public-API surface of the manifest-pinned components for the current
2026.1 candidate. Future release pins diff against these files, so a wire/public-API change that
lands without an intentional baseline refresh reddens the gate (AGENTS.md: a gate that can't fail
is worse than no gate).

## 2026.1-rc.1 review

The baseline was intentionally advanced from rc.0 on 2026-08-01 after extracting all four
components from their exact manifest SHAs. The review found:

- the five server OpenAPI documents removed no operation, schema, schema property, or required
  field;
- the Python SDK removed no module export (two admin-client descriptors changed);
- the .NET SDK source digest grew from 4,958 to 4,976 symbols; its two apparent removals were a
  source-only serializer-context member and the equivalent `OfflineSyncEngine` declaration changing
  from `sealed class` to `sealed partial class`;
- the JavaScript SDK beta surface reflects the intentional generated-module migration and deprecated
  entrypoint shims. Its exact package install, imports, browser bundles, and documentation checks are
  certified separately by the artifact and SDK gates.

This refresh records reviewed release movement; it does not weaken the next diff. The gate now
checks out each future component at its exact manifest SHA and fails again on any unreviewed change.

## 2026.1-rc.2 review

The baseline was advanced again on 2026-08-17 for the re-pin landed in honua-release#101 (all four
components re-extracted at the manifest pins on trunk — no `_meta.sha` was hand-edited). Review of
what moved (honua-release#104):

- `honua-server` @ `aa894e1481cd` — all five OpenAPI documents extract **byte-identically** to the
  previous pin; only `_meta.sha` advanced.
- `honua-sdk-dotnet` @ `f6c98c5cbff1` — the C# digest is **byte-identical**; only `_meta.sha`
  advanced.
- `honua-sdk-python` @ `23e9dd3d7da7` — purely additive: **0 module exports removed**, 11 added
  (`Toolbox*` translation types and the three `TOOLBOX_TRANSLATION_*` constants), and the two
  changed symbols are `HonuaAdminClient`/`AsyncHonuaAdminClient` each gaining exactly one method
  (`validate_toolbox_translation`) with no removed or re-signed method.
- `honua-sdk-js` @ `9d20f0cb4f33` — additive, and the artifact is now produced by the entry-point
  extractor (below). Re-extracting BOTH pins with the fixed extractor gives **0 removed, 942 added**
  across 56 → 65 published entry points. The five "removals" the declaring-file extractor reported
  at this re-pin were all declaration relocations that stayed exported: the three
  `HONUA_CONNECT_*` constants and `MaplibreProtocolRegistrar` are each importable from exactly as
  many subpaths as before, and `validateConnectEndpoint` is not importable from any published
  subpath at either pin (it is exported from `src/connect.ts`, which `package.json` does not
  publish), so it was never part of the consumer contract.

## 2026.1-rc.2 re-pin review (2026-10-22)

The scheduled gate had been red since the manifest moved past the 2026-09-26 capture
(run 39596554904: every component "surface changed"). All four components were re-extracted at
the current manifest pins with `contract_surface.py update`; no `_meta.sha` was hand-edited. Review
of what moved:

- `honua-server` @ `0e03bdd06b3c` (from `7ba422672e0c`): **0 operations, schemas, schema properties
  or parameters removed; no parameter or property became required.** `openapi.json` adds the
  `PortalTokenQuery` (`?token=`) security scheme as an extra alternative on the five item-write
  operations (existing `ApiKeyAuth`/`BearerAuth` alternatives unchanged), adds `style`/`explode`
  to `filter`, and widens `filter-lang` with the `cql-text` alias and an explicit default.
  `ogc-coverages-openapi.json` adds optional `subset` and `scaleSize` query parameters.
  `ogc-maps-openapi.json` adds `GET /ogc/maps/collections` and `/ogc/maps/collections/{collectionId}`,
  the `MapsCollection` schema, and optional `crs`/`extent`/`storageCrs` on `LandingPage`. Tiles and
  processes documents are byte-identical.
- `honua-sdk-python` @ `5a79352066b8` (from `40ecf7318573`): **0 module exports removed**, 8 added
  (`FailureKind`, `FieldFailure`, `ProtocolMetadata`, `TerminalFailureReceipt`, `ImportResult`,
  `BackgroundImportResponse`, `OperationProposalSummary`, `OperationProposalDetail`). The admin
  clients gain six methods (proposal list/get/approve/reject, `update_access_policy`,
  `upload_file`) with none removed or re-signed; the HTTP/gRPC error constructors gain keyword-only
  optional arguments (`receipt`, `initial_metadata`, `trailing_metadata`), which existing callers
  do not pass.
- `honua-sdk-dotnet` @ `5f09e4fca08a` (from `6ba49ec32ea8`): 4,986 to 5,341 digest lines, 357 added,
  2 apparent removals that are digest artifacts, not API removals: the
  `HonuaAdminApiException(HttpStatusCode, string, string? responseBody = null)` constructor still
  exists with the same parameters, now split across lines (the syntactic digest records only the
  first line), and `ReplicaSyncClient.SynchronizeReplicaAsync(serviceId, replicaId, syncDirection,
  cancellationToken)` keeps its signature but is no longer `async` (it delegates to a core method);
  `async` is not part of the public signature.
- `honua-sdk-js` @ `32ab864720d3` (from `d7cec2d510e0`): **0 exports removed**, 610 added, one new
  published entry point (`./client-compat`).

### Extractor change: the TypeScript surface is now keyed by entry point

`_ts_export_surface` keyed every symbol by its declaring file and did not resolve
`export { X } from "./y.js"`, so moving a declaration and re-exporting it from where it used to live
read as a public-API **removal**. That is over-reporting breakage, which teaches people to ignore a
red gate. The TS surface is now resolved from `package.json` `exports`: each published subpath is
expanded through named re-exports, `export *`, `export * as ns`, and type-only re-exports until it
names what `import { X } from "@honua/sdk-js/<subpath>"` actually yields. Kind is recorded as the
refactor-stable pair `value`/`type` (a type-only export cannot be imported as a value, so that
distinction is kept; `interface X` re-exported as `export type { X }` is not a change). An
un-resolvable `export *` from an external package is recorded verbatim rather than dropped, and if
no entry point resolves at all the extractor falls back to the declaring-file digest rather than
certifying that a package exports nothing. `tools/test_contract_surface.py` pins the regression: a
moved-and-re-exported symbol is not reported as removed, while a name that genuinely leaves an entry
point still is.

Captured surfaces (all extracted **build-free** from committed source at each component's pinned sha
— see `../../../tools/contract_surface.py`):

| component | artifact(s) | what it is |
|---|---|---|
| `honua-server` | `openapi.json` + `ogc-{tiles,maps,processes,coverages}-openapi.json` | the server's published OpenAPI documents (canonicalised) |
| `honua-sdk-python` | `public-api.json` | the SDK's own committed `compatibility/public-api.json` snapshot |
| `honua-sdk-dotnet` | `public-api.json` | syntactic C# public-surface digest (public/protected declarations under `src/`, tests excluded) — a deterministic, diffable descriptor, not a full apicompat run |
| `honua-sdk-js` | `public-api.json` | syntactic TypeScript export-surface digest resolved from the package's published entry points: every name importable from a `package.json` `exports` subpath, expanded through `export … from` / `export *`. Keyed by subpath, **not** by declaring file (honua-release#104) |

Each component dir also carries a `_meta.json` recording the pinned sha the surface was captured at.

## Refreshing the baseline (intentional surface change)

When a component is re-pinned and its surface legitimately changed, refresh in the SAME PR so the
diff and the review travel together:

```bash
# repos checked out as siblings of honua-release (…/honua-io/<component>)
python tools/contract_surface.py update            # re-capture at the current manifest pins
python tools/contract_surface.py check             # confirm pass
```

The gate (`gate-contract.yml` → `rest-sdk-api`) clones the pinned components in CI and runs
`check`, so the committed baseline must match what re-extraction produces at the pinned shas.

## 2026.1-rc.2 re-pin review (2026-10-25)

The scheduled gate went red again once the manifest moved past the 2026-10-22 capture
(runs 40089279133 and the 2026-10-25 dispatch 40169572181: `honua-sdk-dotnet` and `honua-sdk-js`
"surface changed in ['public-api.json']"). All four components were re-extracted at the current
manifest pins with `contract_surface.py update` against exact checkouts; no `_meta.sha` was
hand-edited. Review of what moved:

- `honua-server` @ `8cb074f77d01` (from `0e03bdd06b3c`): all five OpenAPI documents extract
  **byte-identically**; only `_meta.sha` advanced.
- `honua-sdk-python` @ `361978e86e81` (from `5a79352066b8`): `public-api.json` is
  **byte-identical**; only `_meta.sha` advanced.
- `honua-sdk-dotnet` @ `aada6d8987db` (1.12.0, from `5f09e4fca08a`): purely additive, **0 symbols
  removed, 8 added** (5,341 → 5,349): `Honua.Sdk.Admin.Models` gains `Capabilities` and
  `StorageMode`; `Honua.Sdk.GeoServices.FeatureServer.Models` gains `ReturnTrueCurves`;
  `Honua.Sdk.Studio.Packages` gains `StudioPublishedArtifact` (`PublicationId`, `PublishedAt`,
  `Route`) and `GetPublishedArtifactAsync`. No existing declaration changed.
- `honua-sdk-js` @ `45e2fb2c7c35` (0.2.6, from `32ab864720d3`): purely additive, **0 exports
  removed, 42 added** (7,880 → 7,922) across the same published entry points: the
  `./control-plane` dataset-import task/submission/upload clients and their receipt types, the
  STAC conformance helpers on `./honua` and `./stac`, the `./local-install` workspace agent-access
  helpers, `HonuaStyleRefLayerBinding` on `./runtime`, and the `./studio-agent` MCP read-resource
  types and workflow-knowledge constants.

This refresh records reviewed, additive release movement; the gate keeps failing on any unreviewed
change at the next pin.
