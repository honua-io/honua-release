# Installed-client certification

This gate creates clean consumer environments and installs the exact public package bytes in
`platform-manifest.yaml`. It never builds a checkout, accepts a floating version, substitutes a
local server image, or omits a matrix cell. The receipt records every pass and non-pass with the
release, package integrity, source SHA, immutable server image, fixture/config/auth revisions,
operation, target, and durable CI evidence URI.

`matrix.json` is the only source of expected outcomes. A cell with `status: active` must pass. A
cell with `status: blocked` must name the issue that blocks it in `blockedBy`. It still runs on
every execution and is reported `blocked` only when its specific named blocker is observed.
Installation, integrity, infrastructure, handshake, and supplied-evidence validation failures
remain fatal. The setup-view blocker requires a valid complete 12-tool `default.v1` catalog;
the import-fidelity blocker requires an absent receipt because no producer exists yet. If a blocked cell starts
passing, it is reported as a failure until the matrix row is set back to `active`, so a fix is
observed rather than assumed. `run.py` exits 0 only when every active cell passes and no blocked
cell passes silently or fails for an unexpected reason. `run.py --verify-receipt <receipt>` checks a receipt against the matrix, and
the workflow asserts nothing else.

Every cell is executable. Both npm lanes download the registry tarball and recompute its SHA-512
before installation; lockfile metadata alone is not accepted as byte proof. The MCP lanes co-install
the independently byte-verified manifest-pinned JS SDK (its declared peer). `npm-mcp-tools-list`
gives each declared package binary an explicit execution contract in the matrix: `help` (the CLI
must answer `--help`), `mcp-stdio` (the stdio server, configured with `HONUA_BASE_URL`, must answer
`initialize` plus `tools/list`) or `mcp-proxy` (the proxy, pointed at the candidate's `/mcp`, must
answer `initialize` plus `tools/list`). Each binary is launched through npm's `node_modules/.bin`
shim exactly as a customer launches it. A binary without a contract fails the cell.

`npm-mcp-setup-view-tools-list` is the terminal journey's discovery contract. The installed proxy
sends `initialize` with `_meta["honua.io/workflow-view"] = "setup"`, then a selector-free
`tools/list`, and must return the complete server-authored `setup` view with the matrix's
`toolCount` (25). Published `@honua/mcp-server` 0.1.12 dropped the selector and returned the 12-tool
`default` view ([honua-sdk-js#1875](https://github.com/honua-io/honua-sdk-js/issues/1875)). The pinned
0.1.13 proxy keeps the `setup` view, so the cell is active. A proxy that drops the selector again
reports the 12-tool `default` view and fails the cell.

The NuGet cell restores the pinned `Honua.Sdk` from anonymous nuget.org into a clean consumer with
an isolated package folder and a cleared source list. It checks that the restored `.nupkg` matches
the manifest's sha256 and came from nuget.org, then builds and runs the shared GeoServices error
probe against the candidate. A release-mode run therefore cannot pass with an ecosystem missing.

The service/layer import cell (`nuget-service-layer-import-fidelity`,
[honua-release#317](https://github.com/honua-io/honua-release/issues/317)) is part of this same
matrix, not a second certification framework. Its requirement IDs are the frozen import-fidelity
scorecard denominator plus the published-SDK journey (discover, select, apply, wait,
cancellation/recovery, reconciliation, fixtures, and Esri cross-checks). `time_query_parity` stays
not-applicable and is outside the pass denominator. The cell consumes a receipt from a clean
consumer of the manifest-pinned public NuGet package (`--import-fidelity-receipt`). It does not
pack a checkout, restore a local feed, or synthesize a receipt. Omitted, skipped, stale,
source-built, waived, released, wrong-pin, mocked-seam, or shrunk-denominator evidence fails.
No producer emits that receipt yet, so the cell is blocked by
[honua-release#418](https://github.com/honua-io/honua-release/issues/418).

The Python admin cell installs the pinned wheel with its declared dependencies and imports both
admin clients. Every manifest client artifact must have a matrix cell; omissions fail validation.

The live driver reuses the repository's one-server/one-PostgreSQL candidate harness and immutable
`e2e/harness/seed` fixture. Static input validation and exact package-byte installation can be run
without a server:

```sh
python certification/installed-clients/run.py --validate-only \
  --evidence-uri "https://github.com/honua-io/honua-release/actions/runs/$RUN_ID"
```

Final end-to-end authorization-profile coverage remains dependent on the server proof tracked by
[honua-server#3475](https://github.com/honua-io/honua-server/issues/3475); this repository does not
modify or simulate that server behavior.

Use `--live` for the certification run. It boots the manifest image by digest once with the
candidate PostgreSQL service, applies the shared seed once, then runs every installed client probe
against that same target before teardown. Omitting `--live` is an install-integrity preflight and
cannot be used as release evidence.

## SDK regression scenarios

The matrix also holds one cell per published SDK (Python, JS, .NET) and scenario in `scenarios/*.json`:
auth, admin lifecycle, GeoServices, OGC API Features/Tiles/Processes and STAC. Each scenario runs
through the SDK's own client classes against the booted candidate, with oracles computed from
`fixture.v1.json`. `run.py --subset fast` runs only the install and probe cells (the PR check);
`--subset all` (the default) adds the scenarios, which `gate-installed-clients.yml` runs for the
`installed-clients` nightly evidence class. See
[docs/INSTALLED-CLIENT-REGRESSION.md](../../docs/INSTALLED-CLIENT-REGRESSION.md).

The same suite runs the command-line clients (`cli-workflow`, through the `honua` CLI of
`@honua/sdk-js` and the `honua` console script of the PyPI wheels) and the MCP proxy
(`mcp-workflow`, over `honua-mcp-proxy` stdio only), with the same fixture, oracles and receipt
allowlist.

The cross-client interop scenarios (`scenarios/interop-*.json`, `interop/`) run in the same suite and the
same nightly class. Each one hands one piece of work between clients on the same booted candidate, and
every step names the client and API that performed it. The four hand-offs are:
- .NET publishes, Python queries, JS edits, .NET reads the edit back;
- the CLI imports and publishes, the MCP proxy renders and buffers, Python reads the job;
- JS proposes, the admin CLI approves as a separate principal, .NET verifies;
- one admin-CLI key is used and then revoked across all three SDKs and the proxy.

A failing hand-off names its seam (the producing client and API, and the consuming client and API) in the
receipt.
