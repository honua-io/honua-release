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
`toolCount` (37 from the 3ecd214 server pin, whose roster `views.setup` grew from 25 tools at revision
`setup.v2` to 37 at `setup.v3`). Published `@honua/mcp-server` 0.1.12 dropped the selector and returned the 12-tool
`default` view ([honua-sdk-js#1875](https://github.com/honua-io/honua-sdk-js/issues/1875)). The pinned
0.1.14 proxy retains the `setup` view, so the cell is active. A proxy that drops the selector again
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

## Existing AWS endpoint

The existing AWS cell workflow also invokes this complete suite on ECS/Redis-on and
Lambda+Batch/Redis-on. Provisioning applies the same committed fixture SQL and publishes the
fixture layers. Its application credentials, short-lived bearer and separate principals travel
only in the encrypted handoff to the journey job. That job has no cloud role or OIDC exchange
token; installers and drivers receive an allowlisted environment. Datasource clients honor the
provisioned database's TLS settings.

`run.py --endpoint-context <private-context.json> --subset all --evidence-uri <run-uri>` is the
equivalent endpoint entrypoint. The context must be owner-readable only and prepared by the
trusted provisioner; it is never an uploaded artifact. It binds the workflow run/attempt, endpoint,
cell, manifest digest, observed running image and source revision, and committed fixture bytes.
The endpoint independently advertises its exact source before and after execution. A supplied
context alone is not cloud attestation; qualification comes from the trusted workflow's observed
deployment and retained receipt. The receipt contains the nonsecret deployment binding only.

Redis-off retains the existing serving and typed-refusal scenarios. It does not run or claim the
full installed-client workflow suite. Both Redis-on runs retain all 41 matrix cells, including
their blockers; missing import-fidelity proof retains all 142 supported requirements and ten
not-applicable requirements. A closed upstream issue cannot reactivate a row without a real
published-pin replay and reviewed matrix change. Missing fixture prerequisites report blocked,
identity/install/oracle failures report fail, and neither outcome qualifies a cell.

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

### GP release hardening

The MCP workflow requires five preflight/retry observations in addition to execution:
complete paginated process discovery, valid-plan validation, actionable invalid-WKB
refusal, a FeatureLayer dry run, and replay of the same idempotency key returning the
original job. Polling and result packages must identify that submitted job; failed,
truncated, unidentified, or error-bearing result packages fail certification.

All SDK and MCP buffer fixtures use EPSG:3857 and a 500-metre distance around
`[10, 20]`. The independent oracle checks closed finite coordinates, vertex radius,
centering, polygon area and boundary chords. Correct-radius diamonds, holes and
non-finite coordinates cannot pass as circles.

The S5 OGC seam driver also rejects missing job IDs, unknown/failed/dismissed states,
poll exhaustion and malformed results. An aborted driver emits a failure receipt.
For bounded diagnostic runs, `E2E_GP_POLL_ATTEMPTS` (default 30, maximum 300) and
`E2E_GP_POLL_INTERVAL` (default 2 seconds) control polling; exhaustion always fails.

Run the gate's self-tests with Python 3.12 or newer:

```sh
python -m pytest certification/installed-clients/test_regression.py \
  certification/installed-clients/test_run.py tools/test_gp_driver.py -q
```

These self-tests inject faulty responses to verify that the gate rejects them.
A passing self-test run does not replace the live installed-client release receipt.
