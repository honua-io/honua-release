# Contract baseline — Honua 2026.1

This directory holds release API baselines used by the `contract-rest-sdk` gate. The server and
Python client baselines are captured from their pinned source revisions. Their surfaces remain
subject to the regular comparison gate.

## Client API baseline status

The previous generated JavaScript client API snapshot and .NET public API snapshot have been
removed from the current release source. The API gate reports both components as **blocked** until
their neutral client packages are published and their public APIs can be captured from those real
releases. No baseline has been inferred from an unpublished change, and this cleanup does not
publish a package or other release artifact.

Do not run the baseline update command to recreate either snapshot before those releases are
available. The missing component baselines are intentionally fail-closed, and the updater skips
them: `contract_surface.py check` returns `blocked` for both until a real neutral release exists.

## Refreshing captured baselines

When a captured component is re-pinned and its surface legitimately changes, refresh its baseline
in the same review so the diff and the review travel together:

```bash
python tools/contract_surface.py update
python tools/contract_surface.py check
```

The gate (`gate-contract.yml` → `rest-sdk-api`) checks out pinned components in CI and compares
their current surfaces with the committed baselines.
