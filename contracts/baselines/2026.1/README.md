# Contract baseline — Honua 2026.1

The **rc.0 baseline** for the `contract-rest-sdk` half of the contract gate (gate b). It is the
frozen REST/OpenAPI + SDK public-API surface of the manifest-pinned components at the time
`2026.1-rc.0` was cut. `2026.1-rc.0` is the **baseline-setting release**: future releases diff their
pinned surfaces against these files, so a wire/public-API change that lands without an intentional
baseline refresh reddens the gate (AGENTS.md: a gate that can't fail is worse than no gate).

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
