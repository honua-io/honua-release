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
