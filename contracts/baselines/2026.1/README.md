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

Captured surfaces (all extracted **build-free** from committed source at each component's pinned sha
— see `../../../tools/contract_surface.py`):

| component | artifact(s) | what it is |
|---|---|---|
| `honua-server` | `openapi.json` + `ogc-{tiles,maps,processes,coverages}-openapi.json` | the server's published OpenAPI documents (canonicalised) |
| `honua-sdk-python` | `public-api.json` | the SDK's own committed `compatibility/public-api.json` snapshot |
| `honua-sdk-dotnet` | `public-api.json` | syntactic C# public-surface digest (public/protected declarations under `src/`, tests excluded) — a deterministic, diffable descriptor, not a full apicompat run |
| `honua-sdk-js` | `public-api.json` | syntactic TypeScript export-surface digest (top-level `export` + re-exports under `src/`, tests excluded) |

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
