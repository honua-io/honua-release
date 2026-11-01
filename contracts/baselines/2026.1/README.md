# Contract baseline — Honua 2026.1

This directory holds release API baselines used by the `contract-rest-sdk` gate. The Python client
baseline is captured from its pinned source revision and remains subject to the regular comparison
gate.

## API baseline status

The generated server OpenAPI snapshot and the JavaScript and .NET client API snapshots have been
removed from the current release source. The API gate reports all three components as **blocked**
until neutral server and client artifacts are published and their public APIs can be captured from
those real releases. No baseline has been inferred from an unpublished change, and this cleanup
does not publish a package or other release artifact.

Do not run the baseline update command to recreate these snapshots before those releases are
available. The missing component baselines are intentionally fail-closed, and the updater skips
them: `contract_surface.py check` returns `blocked` for all three until real neutral releases exist.

## Refreshing captured baselines

When a captured component is re-pinned and its surface legitimately changes, refresh its baseline
in the same review so the diff and the review travel together:

```bash
python tools/contract_surface.py update
python tools/contract_surface.py check
```

The gate (`gate-contract.yml` → `rest-sdk-api`) checks out pinned components in CI and compares
their current surfaces with the committed baselines.
