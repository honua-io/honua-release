# Contract baseline — Honua 2026.1

This directory holds release API baselines used by the `contract-rest-sdk` gate. The server,
Python client, and .NET client baselines are captured from their pinned source revisions. Their
surfaces remain subject to the regular comparison gate.

## JavaScript client status

The previous generated JavaScript client API snapshot has been removed from the current release
source. The API gate reports this component as **blocked** until a neutral client package has been
published and its public API can be captured from that real release. No baseline has been inferred
from an unpublished change, and this cleanup does not publish a package or other release artifact.

Do not run the baseline update command to recreate this component's snapshot before that release is
available. The missing component baseline is intentionally fail-closed: `contract_surface.py check`
returns `blocked` when a pinned component has no captured baseline.

## Refreshing captured baselines

When a captured component is re-pinned and its surface legitimately changes, refresh its baseline
in the same review so the diff and the review travel together:

```bash
python tools/contract_surface.py update
python tools/contract_surface.py check
```

The gate (`gate-contract.yml` → `rest-sdk-api`) checks out pinned components in CI and compares
their current surfaces with the committed baselines.
