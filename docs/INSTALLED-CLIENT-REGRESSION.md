# Installed-client regression

Each night, the published Honua clients drive the exact release candidate through real customer workflows:

- the SDKs: Python (`honua-sdk` with `honua-admin`), JavaScript (`@honua/sdk-js`) and .NET (`Honua.Sdk`);
- the command-line clients: the `honua` CLI of `@honua/sdk-js` and the `honua` console script the PyPI wheels install;
- the MCP proxy: `honua-mcp-proxy` from `@honua/mcp-server`.

The suite answers one question: *do the clients a customer installs today still complete real work
against this server, and do they work together?*
The cross-client interop scenarios (`interop-*`) answer the second half. One client produces state and
a different client consumes it, all against the same booted candidate (see
[Cross-client interop](#cross-client-interop)).

It is the `installed-clients` nightly evidence class (R21; honua-release#381, #386). A nightly lock is
minted only when the train's `installed-clients` gate passes.

The suite extends `certification/installed-clients/`. The scenarios are additional matrix cells
next to the install and probe cells; it is not a second framework.

## How it works

| Part | File | Role |
|---|---|---|
| Scenario contract | `certification/installed-clients/scenarios/*.json` | Steps, each step's oracle kind, and for every client the exact SDK API, command or MCP request the step goes through. The id's prefix is the family: `sdk-` (the three SDKs), `cli-` (npm and PyPI command lines), `mcp-` (the MCP proxy), `interop-` (one hand-off across clients: each step names its own client and the earlier steps it consumes). |
| Fixture | `certification/installed-clients/fixture.v1.json`, `interop/fixture.v1.json` | Deterministic rows. Every oracle is computed from these files, never from a server response. |
| Oracles | `certification/installed-clients/oracles.py` | Pure comparisons of observations against the fixture, including a stdlib PNG decoder (pixel oracle) and a Mapbox vector tile decoder. |
| Engine | `certification/installed-clients/regression.py` | Seeds the fixture, publishes the harness layers, writes each driver's plan, runs the driver and judges every step. |
| Drivers | `certification/installed-clients/drivers/{python,js,dotnet,cli,mcp}` | One program per client. Each runs against the installed published package only and prints one JSON observation per step. The SDK drivers call the SDK's client classes. The `cli` driver runs the installed `honua` executable. The `mcp` driver speaks JSON-RPC over the installed proxy's stdio. None makes a raw HTTP call. |
| Interop | `certification/installed-clients/interop/` | `orchestrator.py` hands the work between clients: long-lived SDK runners (`drivers/{python,js,dotnet}`, one JSON request and reply per line, client instances kept between requests), the installed `honua` command and the installed proxy's stdio. `judge.py` holds the interop oracles and names the seam of every failing hand-off; `engine.py` seeds the handoff table and builds the plan. |
| Matrix | `certification/installed-clients/matrix.json` | The only source of expected outcomes: one cell per client and scenario, `active` or `blocked`. |
| Runner | `certification/installed-clients/run.py` | Installs the manifest-pinned bytes, boots the candidate, runs every cell and writes the receipt. |
| Gate | `.github/workflows/gate-installed-clients.yml` | Called by the release train (`gate_installed_clients`). Emits `gate-report.json` and `overall_status`. |

**Installs.** Each SDK is installed only from its registry, at the version and integrity pinned in
`platform-manifest.yaml` `clientArtifacts`:

- npm: the archive's sha512 is recomputed before installation, and the installed lockfile must match it.
- PyPI: both wheels are downloaded and their sha256 is checked, then they are installed in isolation.
- NuGet: the package is restored from anonymous nuget.org into an isolated package folder, and the restored `.nupkg` sha256 is checked.

The command-line and MCP cells install the same pinned bytes:

- `npm-cli`: `@honua/sdk-js` with the byte-verified `@honua/mcp-server`. Commands run through the npm `node_modules/.bin` shims.
- `pypi-cli`: both wheels, then the `honua` console script they install (with the `datasource`, `layer` and
  `proposal` commands `honua-admin` mounts on it; `HONUA_PYTHON_CLI=1` keeps it in Python). The proposal
  step uses the byte-verified `@honua/mcp-server` proxy, as `npm-cli` does.
- `npm-mcp-workflow`: `@honua/mcp-server` with its byte-verified peer `@honua/sdk-js`.
- `interop`: all of the above from the same pinned bytes: `@honua/sdk-js` with `@honua/mcp-server`, both
  PyPI wheels, and `Honua.Sdk` restored from nuget.org into the .NET runner.

A checkout build is never used.

**Boot.** `run.py --live` boots the manifest image by digest the way `e2e-local-docker` does: local
Docker, licensing disabled, no AWS. It uses `e2e/harness/compose.candidate.yml` plus
`compose.sdk-regression.yml`. The overlay adds a generic OIDC issuer whose HS256 signing key is minted
per run, so the operator-bearer scenarios have a bearer the candidate validates. It also adds a TCP
database healthcheck so the server never boots against a database that is not accepting TCP yet.
Finally it turns on the candidate's operation policy with one rule: `service.publish` requires
approval. Every other operation, including the admin REST publish the SDK scenarios use, is
unchanged. That rule is what makes the governed proposal path in `cli-workflow` observable.

**Harness setup versus client under test.** Seeding the tables and publishing the shared read-only
layers uses SQL and the admin REST API directly. So does publishing each client's managed edit layer
and minting the short-lived proposer (`admin:write`) and approver (`admin:approve`) API keys for the
command-line workflow. That is fixture preparation. Only the principals' key ids enter the driver's
plan; the keys travel in its environment. Everything a scenario does goes through the client under test.

**Child environments.** No client process inherits the suite's credentials. Each `honua` command starts
from `PATH`, `HOME`, the temp and locale variables and `PYTHONPATH`, plus its base URL, profile
directory and the one credential its step needs. Each `honua-mcp-proxy` starts from the same startup
variables and the remote URL (`probes.proxy_environment`). An anonymous
session carries no `SDKREG_*`, `E2E_*` or `HONUA_*` credential. An authenticated session carries its one
`HONUA_API_KEY`. Each SDK runner starts from the same startup variables plus `SDKREG_PLAN` and only the
keys that runner reads: Python and .NET get `SDKREG_API_KEY`; JavaScript gets `SDKREG_API_KEY` and
`SDKREG_PROPOSER_KEY` (the Studio principal). The .NET runner also receives the installer runtime
locations (`DOTNET_ROOT`, `DOTNET_CLI_HOME`, `DOTNET_CLI_TELEMETRY_OPTOUT`, `DOTNET_NOLOGO`,
`DOTNET_MULTILEVEL_LOOKUP`, `DOTNET_ROLL_FORWARD`, `NUGET_PACKAGES`, `NUGET_HTTP_CACHE_PATH`) when they
are set. No runner receives the operator bearer, the database password, the approver key, or any other
`SDKREG_*`, `E2E_*` or `HONUA_*` value.

**Receipts.** Each suite cell's receipt row carries only allowlisted fields: client, version,
integrity, scenario, and per step `step`, `api`, `status`, `oracle` (and `blockedBy`). The oracle
summary is generated from fixture values. It never carries a response body, a credential, a DSN or a
server-written message. Driver stderr goes to the job log after scrubbing.

## Cross-client interop

The interop scenarios prove the clients work together, not only one at a time. Each step names the
client artifact that performs it and the exact SDK API, command or MCP request, and lists the earlier
steps it consumes and what it takes from them (a layer, object ids, a job id, a proposal, an API key).
All steps run against the one booted candidate, in contract order. The scenarios are:

- **`interop-publish-query-edit`.** `Honua.Sdk` publishes a table. `honua-sdk` queries the layer (features
  with exact ordinates, ids, count) and hands each row's object id on. `@honua/sdk-js` edits one feature
  (attributes and geometry) through that id. `Honua.Sdk` reads the edited state back.
- **`interop-import-render-buffer`.** The `honua` CLI imports the fixture's GeoJSON polygon
  (`honua admin import uploadImportFile`), discovers the imported table
  (`honua admin connect getConnectionTables`) and publishes it. `honua-mcp-proxy` reads the polygon
  (the fixture ring, in order, with its `gid` and `name`),
  renders it (pixel oracle) and buffers the polygon it read (`honua_execute_plan`, `geometry.buffer`).
  `honua-sdk` reads that job by its id (`HonuaGeoprocessing.job`) and its result
  (`HonuaGeoprocessing.results`); the ring must be that polygon's buffer at the fixture distance,
  vertices and edges.
- **`interop-proposal-approval`.** `@honua/sdk-js` creates a Studio map draft as the proposer, saves a
  version and requests its publication.
  - `honua admin operate approveOperationProposal --profile proposer` must fail with the server's 403.
  - The separate approver approves; `getOperationProposal` must show it requested by the proposer and
    resolved by the approver.
  - The JS SDK polls the request to `Active` and its final URL.
  - `Honua.Sdk` verifies the published pointer, the published version's content hash and map body, and
    the final publication. Agreeing digests are not enough: the judge recomputes the content hash as
    SHA-256 of the canonical hash input the reader returns (base64 `contentHashInput`) and requires
    that input to carry the fixture map body. No published client returns that input yet
    ([honua-server#5449](https://github.com/honua-io/honua-server/issues/5449)), so `published-content`
    cannot pass until it does.
- **`interop-api-key-revocation`.** `honua admin secure createAdminApiKey --secret-output` mints one key.
  The secret goes to a private file, never to output or a log. Each of `honua-sdk`, `@honua/sdk-js`,
  `Honua.Sdk` and `honua-mcp-proxy` reads the sites layer with it on a client instance (one proxy
  session) it keeps. `honua admin secure revokeAdminApiKey` revokes it, and all four clients then probe
  concurrently on those same instances.
  - The documented window is zero: `revokeAdminApiKey` "revokes a server-managed API key so it can no
    longer authenticate", and validation reads the shared registry on every request.
  - So the first call after the revoke command returns must be refused (401, 403 or 499, or an MCP
    `permission_denied`) within the 30 s observation bound. A refusal that arrives later is a hang.
  - Each confirmation call must itself come back as an authentication refusal. A timeout, a connection
    error or any other status is not evidence that revocation held.
  - A client that keeps succeeding, or that hangs for the 30 s observation bound, fails.

**A failure names the seam.** When a step that consumed another client's state fails, its oracle summary
starts with both sides of the hand-off:

```
seam layer from Honua.Sdk 1.10.1 `IHonuaAdminClient.PublishLayerAsync` (publish) and object ids from
honua-sdk 0.1.12 `honua_sdk.GeoServicesFeatureServerClient.query` (query) -> @honua/sdk-js 0.1.12
`HonuaFeatureLayer.applyEdits(updates)`: raised HonuaHttpError (status 400)
```

Each interop receipt row also names the client that performed the step (`client`, for example
`honua-sdk 0.1.12`), next to `step`, `api`, `status`, `oracle` and `blockedBy`. A seam failure gets an
issue in the repository that owns the failing side, and the row is `blocked` by it with the observed
signature.

**Harness setup.** Seeding the handoff table, creating the datasource the publishing clients publish
through, and minting the short-lived proposer and approver keys is fixture preparation (SQL and the admin
REST API). Names carry a per-run tag. The plan carries only ids; the keys travel in the orchestrator's
environment and reach each runner, `honua` command and proxy session only as the credential that step needs.

## What each cell proves

| Scenario | Steps (oracle) | Proves |
|---|---|---|
| `sdk-auth` | API-key query (row count = fixture rows); bearer admin `listServices` (fixture service listed); bearer query (row count); anonymous query (SDK raises an authentication refusal: 401, 403 or 499) | The SDK authenticates with an API key and with an operator bearer, and surfaces a refused anonymous call as an error, not an empty success. |
| `sdk-admin-lifecycle` | create datasource (id); test datasource (healthy); publish service and layer (names, enabled, id); list layers (published layer enabled); served (row count); unpublish (`enabled=false`); query unpublished (not found, 404) | An operator can publish and retire a layer entirely through the SDK's admin client. |
| `sdk-geoservices` | query with the fixture filter (attributes and exact ordinates); ids; count; resolve edit object ids; `applyEdits` add, attribute-only update and delete; post-edit state; add attachment; query attachments (name, type, size) | FeatureServer read, edit and attachment workflows return exactly the fixture's features, ids and ordinates. |
| `sdk-ogc-features` | items in the fixture bbox (ids and ordinates); one item (properties and ordinates) | OGC API Features reads. |
| `sdk-ogc-tiles` | vector tile (one polygon at the projected fixture bounds); PNG tile (sample pixels painted inside the fixture polygon, transparent outside); empty tile | OGC API Tiles in both encodings. |
| `sdk-ogc-processes` | asynchronous `geometry.buffer` submit (job id); poll (successful); result (every ring vertex at the fixture distance, centroid at the fixture point) | Asynchronous OGC API Processes jobs. |
| `sdk-stac` | search by collection and bbox (ids and ordinates) | STAC item search. |
| `cli-workflow` | `honua services` (fixture service listed); `honua admin connect createConnection` (id); `honua connection test` (healthy); `honua admin publish publishLayer` (names, enabled, id); `getPublishedLayers` (layer enabled); `honua query --where --format geojson` (filtered features, exact ordinates); `honua query --count` (row count); as the proposer, find `honua_publish_service` in the `setup` view of `honua-mcp-proxy` and call it (a proposal awaiting approval, not a publication; `honua layers` on the proposed service is 404 or empty until approval); `honua admin operate approveOperationProposal --profile proposer` (exit 1 with 403, proposal still awaiting approval); the same command `--profile approver` (succeeded); `getOperationProposal` read until terminal within a bounded deadline (succeeded, requested by the proposer's key, resolved by the approver's key); `honua query` the approved layer (the proposal fixture rows); `setLayerEnabled` (`enabled=false`); `honua query` the unpublished layer (exit 1 with 404) | An operator runs the whole publication workflow from the terminal. A governed proposal from the agent surface is only published after a different principal approves it with the typed approval command, the same split as terminal-journey stages 7 and 8. The PyPI cell runs the same steps with `honua services --format json`, `honua datasource create --password-env` / `test`, `honua layer publish` / `list` / `unpublish`, `honua query <service> <layer> [--count]` and `honua proposal approve` / `read --wait`, each principal's key in `HONUA_ADMIN_KEY`. |
| `mcp-workflow` | `initialize` with the `setup` workflow view (protocol and identity); selector-free `tools/list` (complete 25-tool setup view); a new session's `tools/list` (complete 12-tool default view, exactly the pinned `default.v1` roster); the first anonymous `tools/list {view: full}` page (`permission_denied`; any page returned fails); authenticated full view drained page by page (exactly the pinned roster in `e2e/drivers/mcp/expected-tools.json` by name, no duplicates, including the default view; a later selector-free list restores the default view); `honua_query_features` (filtered features, exact ordinates); `honua_render_map` (sample pixels painted inside the fixture polygon, transparent outside); `honua_execute_plan` with a `kind: Geoprocess` `geometry.buffer` step and `outputs: [FeatureLayer]` (job id), `resources/read honua://jobs/{id}` (succeeded) and `.../results` (ring and centroid from the fixture) | An agent configured with the installed proxy discovers, reads, renders and runs a buffer job entirely over the proxy's stdio, and gets the full catalog only when authenticated. |

| `interop-publish-query-edit` | .NET `PublishLayerAsync` (names, enabled, id); Python `query` (fixture rows, exact ordinates, object ids handed on); Python ids (fixture gids); Python count (fixture row count); JS `applyEdits(updates)` through the Python-read id (one successful result); .NET `QueryAsync` (fixture rows with the edit applied) | A layer one SDK publishes is the layer every other SDK reads and edits. |
| `interop-import-render-buffer` | CLI `uploadImportFile` (the fixture's feature count imported); CLI table discovery and `publishLayer` (names, enabled, polygon); proxy `honua_query_features` (fixture envelope ring in order, with gid and name); proxy `honua_render_map` (sample pixels); proxy `honua_execute_plan` (job accepted); Python `job` (successful); Python `results` (buffer boundary at the fixture distance, bounds expanded by it) | Terminal, agent and SDK hand the same data and the same job to each other. |
| `interop-proposal-approval` | JS draft (valid map draft); JS save version (version id, sha-256 hash, same item); JS publication request (a proposal); CLI self-approval (exit 1 with 403, still awaiting approval); CLI approval (succeeded); CLI proposal read (requested by the proposer, resolved by the approver); JS poll (`Active` at the fixture route); .NET pointer (the saved version is published); .NET version (hash and map body equal the saved version and fixture); .NET final publication | A publication one client proposes is only published after a different principal approves, and another client sees exactly the approved content. |
| `interop-api-key-revocation` | CLI mint (fixture permissions, secret only in a private file); Python, JS, .NET and proxy reads with that key (fixture row count); CLI revoke (revoked); Python, JS, .NET and proxy on the same instance or session (first call refused within 30 s, each confirmation an authentication refusal) | One credential works across every client and its revocation reaches every client within the documented window. |

A failure names the client, the SDK API and the candidate. For example: `@honua/sdk-js 0.1.12
HonuaFeatureLayer.queryObjectIds ids: raised HonuaHttpError (status 400)`.

## Expected outcomes and blockers

`matrix.json` decides every outcome:

- An `active` cell must pass every step.
- A `blocked` cell names each blocked step's issue and the signature (a regex on the oracle summary)
  that must be observed. Every other step must still pass.
- A blocked step that starts passing turns the cell red until the matrix flips it to `active`. A fix
  is observed, never assumed.
- A blocked step that fails for a different reason is red.
- A missing step, an observation that names a different API than the contract, an install failure
  or a harness failure is red.
- Nothing is retried.

The gate reports `pass` only when every cell passes. With matrix-declared blockers it reports
`blocked`. That is not a pass: the nightly receipt for `installed-clients` is then not `pass`, and no
lock is minted.

Current blockers (candidate `nightly-87966c3`, pins JS 0.1.13 / MCP 0.1.13 / Python 0.1.13 + admin 0.1.10 / .NET 1.10.2):

| Cell | Blocked steps | Blocker |
|---|---|---|
| all three `*-sdk-geoservices` | `apply-edits-update`, `edits-state` | [honua-server#5407](https://github.com/honua-io/honua-server/issues/5407): attribute-only update on a managed layer returns 500 |
| `npm-sdk-geoservices` | `ids`, `count` | [honua-sdk-js#1894](https://github.com/honua-io/honua-sdk-js/issues/1894): `outFields=OBJECTID` is rejected on layers whose object-id field is not `OBJECTID` |
| `pypi-sdk-geoservices` | `count` | [honua-sdk-python#236](https://github.com/honua-io/honua-sdk-python/issues/236): `return_count_only` returns no count |
| `pypi-sdk-ogc-tiles` | `raster-tile` | [honua-sdk-python#255](https://github.com/honua-io/honua-sdk-python/issues/255): `OgcTilesClient.tile` cannot request PNG |
| `nuget-sdk-ogc-tiles` | all | [honua-sdk-dotnet#405](https://github.com/honua-io/honua-sdk-dotnet/issues/405): `Honua.Sdk` has no OGC API Tiles client |
| `interop-publish-query-edit` | `count` | [honua-sdk-python#236](https://github.com/honua-io/honua-sdk-python/issues/236): `return_count_only` returns no count |
| `interop-publish-query-edit` | `edit`, `read-back` | [honua-sdk-dotnet#410](https://github.com/honua-io/honua-sdk-dotnet/issues/410): `PublishLayerRequest` cannot declare a storage mode or edit capabilities, so the JS SDK's edit of the layer the .NET SDK published is refused (400) and the .NET read-back sees the unedited row |
| `interop-proposal-approval` | `save-version` and every later step except `published-url` | [honua-server#5433](https://github.com/honua-io/honua-server/issues/5433): the candidate refuses the JS SDK's bodiless `content-versions` POST (400) although its OpenAPI makes the body optional; nothing can be proposed. The next seam on this path, the governed publish request returning no pollable request id, is [honua-server#5434](https://github.com/honua-io/honua-server/issues/5434). After that, `published-content` fails until the reader returns the canonical content-hash input: [honua-server#5449](https://github.com/honua-io/honua-server/issues/5449) |
| `interop-proposal-approval` | `published-url` | [honua-sdk-dotnet#411](https://github.com/honua-io/honua-sdk-dotnet/issues/411): `Honua.Sdk` has no reader for a published Studio route |
| `interop-api-key-revocation` | `mcp-revoked` | [honua-server#5435](https://github.com/honua-io/honua-server/issues/5435): the candidate answers the revoked key's MCP session with a JSON-RPC error whose `id` is `null` (HTTP 200), so the proxy's call never returns. The three SDKs are refused on their first call |

The SDK re-pin or server re-pin that carries a fix must flip the matching `blockedSteps` entry in the
same PR, or the gate turns red.

## Running it locally

You need Docker, Node 20, Python 3.12 with pip, and the .NET 10 SDK. The run downloads the pinned
packages from npm, PyPI and nuget.org, and pulls the pinned server image from GHCR.

```sh
# Full gate: boot the candidate, run every cell, write the receipt, tear down.
COMPOSE_PROJECT_NAME=sdkreg E2E_SERVER_PORT=18191 \
  python certification/installed-clients/run.py --live --subset all \
  --evidence-uri local --output artifacts/installed-client-certification.json

# Check a receipt against the matrix (what the gate asserts).
python certification/installed-clients/run.py --subset all \
  --verify-receipt artifacts/installed-client-certification.json

# The fast PR subset (installs and probe cells only).
python certification/installed-clients/run.py --live --subset fast --evidence-uri local

# Self-tests (no Docker needed).
python -m pytest certification/installed-clients/ -q
```

`COMPOSE_PROJECT_NAME` and `E2E_SERVER_PORT` keep the stack apart from other local stacks. Driver
output and scrubbed SDK error messages go to stderr; the receipt is the JSON file.

## Adding a scenario

1. Add the rows the oracle needs to `fixture.v1.json`. Compute expectations from them in `oracles.py`
   or `regression.py`, never from a server response. Bump the fixture `revision` if existing rows change.
2. Write `scenarios/<name>.json` with id `sdk-<name>`, `cli-<name>` or `mcp-<name>`, the steps (each
   with an oracle kind registered in `regression.ORACLES`), the promise, the receipt allowlist, and for
   **every** client of the family (`regression.FAMILIES`) the exact SDK API, command or MCP request
   each step goes through. If a client has no API for a step, write it as such, for example
   `(no OGC API Tiles client in Honua.Sdk)`.
3. Implement the steps in each driver of the family (`drivers/python/driver.py`, `drivers/js/driver.mjs`,
   `drivers/dotnet/Program.cs`; `drivers/cli/driver.py`; `drivers/mcp/driver.py`) through the client
   under test. A missing client API is reported as
   `unsupported`, which is a finding, never a skip. Add the API names to the driver's `API` table; a
   self-test checks that each contract API appears in its driver.
4. Add one matrix cell per client of the family (`<driver>-<name>`). If a step fails for a product reason, open an issue
   in the owning repository (labels `release/2026.1`, `priority/P1`, `bucket/must-fix-before-cut`; the
   body names the promise) and mark that step blocked with the issue URL and the observed signature.
   Never skip it.
5. Run the self-tests and a local `--live` run, and paste the per-scenario results in the PR.

An interop scenario is `scenarios/interop-<name>.json`. Each step names its `client` artifact, its `api`
and the earlier steps it `consumes` (each with the `handoff` it takes). It must cross at least two clients
and hand at least one piece of state between them (`interop/judge.py` validates this). Its expectations come
from `interop/fixture.v1.json` and its oracles live in `interop/judge.py`. The orchestrator's `API` table
must equal the contract, and every SDK step must go through the named call in that SDK's runner (both are
self-tested). Add one matrix cell with `driver: interop`, whose `artifact` is the client of the first step.

A new client of an existing family is a new key in that family's contracts, a new entry in
`regression.DRIVER_CLIENTS` and `run.SUITE_DRIVERS`, an install branch in `run.install_suite_client`,
and one cell per scenario of the family. Validation fails if the matrix drops any regression driver.
