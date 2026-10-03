# Installed-client SDK regression

Each night, the published Honua SDKs drive the exact release candidate through real customer workflows: Python
(`honua-sdk` with `honua-admin`), JavaScript (`@honua/sdk-js`) and .NET (`Honua.Sdk`). The suite answers
one question: *do the clients a customer installs today still complete real work against this server?*
It is the `installed-clients` nightly evidence class (R21; honua-release#381, #386). A nightly lock is
minted only when the train's `installed-clients` gate passes.

The suite extends `certification/installed-clients/`. The scenarios are additional matrix cells
next to the install and probe cells; it is not a second framework.

## How it works

| Part | File | Role |
|---|---|---|
| Scenario contract | `certification/installed-clients/scenarios/*.json` | Steps, each step's oracle kind, and for every SDK the exact client API the step goes through. |
| Fixture | `certification/installed-clients/fixture.v1.json` | Deterministic rows. Every oracle is computed from this file, never from a server response. |
| Oracles | `certification/installed-clients/oracles.py` | Pure comparisons of observations against the fixture, including a stdlib PNG decoder (pixel oracle) and a Mapbox vector tile decoder. |
| Engine | `certification/installed-clients/regression.py` | Seeds the fixture, publishes the harness layers, writes each driver's plan, runs the driver and judges every step. |
| Drivers | `certification/installed-clients/drivers/{python,js,dotnet}` | One program per SDK. Each runs against the installed published package only, calls the SDK's client classes (never raw HTTP), and prints one JSON observation per step. |
| Matrix | `certification/installed-clients/matrix.json` | The only source of expected outcomes: one cell per SDK and scenario, `active` or `blocked`. |
| Runner | `certification/installed-clients/run.py` | Installs the manifest-pinned bytes, boots the candidate, runs every cell and writes the receipt. |
| Gate | `.github/workflows/gate-installed-clients.yml` | Called by the release train (`gate_installed_clients`). Emits `gate-report.json` and `overall_status`. |

**Installs.** Each SDK is installed only from its registry, at the version and integrity pinned in
`platform-manifest.yaml` `clientArtifacts`:

- npm: the archive's sha512 is recomputed before installation, and the installed lockfile must match it.
- PyPI: both wheels are downloaded and their sha256 is checked, then they are installed in isolation.
- NuGet: the package is restored from anonymous nuget.org into an isolated package folder, and the restored `.nupkg` sha256 is checked.

A checkout build is never used.

**Boot.** `run.py --live` boots the manifest image by digest the way `e2e-local-docker` does: local
Docker, licensing disabled, no AWS. It uses `e2e/harness/compose.candidate.yml` plus
`compose.sdk-regression.yml`. The overlay adds a generic OIDC issuer whose HS256 signing key is minted
per run, so the operator-bearer scenarios have a bearer the candidate validates. It also adds a TCP
database healthcheck so the server never boots against a database that is not accepting TCP yet.

**Harness setup versus client under test.** Seeding the tables and publishing the shared read-only
layers uses SQL and the admin REST API directly. So does publishing each client's managed edit layer.
That is fixture preparation. Everything a scenario does goes through the SDK.

**Receipts.** Each suite cell's receipt row carries only allowlisted fields: client, version,
integrity, scenario, and per step `step`, `api`, `status`, `oracle` (and `blockedBy`). The oracle
summary is generated from fixture values. It never carries a response body, a credential, a DSN or a
server-written message. Driver stderr goes to the job log after scrubbing.

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

Current blockers (candidate `nightly-87966c3`, SDK pins JS 0.1.12 / Python 0.1.12 + admin 0.1.9 / .NET 1.10.1):

| Cell | Blocked steps | Blocker |
|---|---|---|
| all three `*-sdk-geoservices` | `apply-edits-update`, `edits-state` | [honua-server#5407](https://github.com/honua-io/honua-server/issues/5407): attribute-only update on a managed layer returns 500 |
| `npm-sdk-geoservices` | `ids`, `count` | [honua-sdk-js#1894](https://github.com/honua-io/honua-sdk-js/issues/1894): `outFields=OBJECTID` is rejected on layers whose object-id field is not `OBJECTID` |
| `pypi-sdk-geoservices` | `count` | [honua-sdk-python#236](https://github.com/honua-io/honua-sdk-python/issues/236): `return_count_only` returns no count |
| `pypi-sdk-ogc-tiles` | `raster-tile` | [honua-sdk-python#255](https://github.com/honua-io/honua-sdk-python/issues/255): `OgcTilesClient.tile` cannot request PNG |
| `nuget-sdk-ogc-tiles` | all | [honua-sdk-dotnet#405](https://github.com/honua-io/honua-sdk-dotnet/issues/405): `Honua.Sdk` has no OGC API Tiles client |

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
2. Write `scenarios/<name>.json` with id `sdk-<name>`, the steps (each with an oracle kind registered
   in `regression.ORACLES`), the promise, the receipt allowlist, and for **every** SDK the exact client
   API each step goes through. If a client has no API for a step, write it as such, for example
   `(no OGC API Tiles client in Honua.Sdk)`.
3. Implement the steps in each driver (`drivers/python/driver.py`, `drivers/js/driver.mjs`,
   `drivers/dotnet/Program.cs`) through the SDK's client classes. A missing client API is reported as
   `unsupported`, which is a finding, never a skip. Add the API names to the driver's `API` table; a
   self-test checks that each contract API appears in its driver.
4. Add one matrix cell per SDK (`<driver>-<name>`). If a step fails for a product reason, open an issue
   in the owning repository (labels `release/2026.1`, `priority/P1`, `bucket/must-fix-before-cut`; the
   body names the promise) and mark that step blocked with the issue URL and the observed signature.
   Never skip it.
5. Run the self-tests and a local `--live` run, and paste the per-scenario results in the PR.

The CLI (`honua`, `honua-admin`) and MCP drivers are follow-ons. They reuse these scenario files, with
a new client key in each contract and a new driver.
