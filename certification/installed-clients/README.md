# Installed-client certification

This gate creates clean consumer environments and installs the exact public package bytes in
`platform-manifest.yaml`. It never builds a checkout, accepts a floating version, substitutes a
local server image, or omits a matrix cell. The receipt records every pass and non-pass with the
release, package integrity, source SHA, immutable server image, fixture/config/auth revisions,
operation, target, and durable CI evidence URI.

`matrix.json` is the only source of expected outcomes. A cell with `status: active` must pass. A
cell with `status: blocked` must name the issue that blocks it in `blockedBy` and declare an `owner`
(GitHub login), a `reason` and a `target` release line such as `2026.1.x`. It still runs on
every execution and is reported `blocked` only when its specific named blocker is observed.
Installation, integrity, infrastructure, handshake, and supplied-evidence validation failures
remain fatal. If a blocked cell starts passing, it is reported as a failure until the matrix row is
set back to `active`, so a fix is observed rather than assumed. The receipt is `pass` with
`declaredLimitations` when every blocked cell is declared and listed by the founder ruling
`dotnet-declared-blocked-2026-10-26` in `docs/2026.1-release-decision-overrides.json`; any other
blocked cell makes it `blocked`. `run.py` exits 0 only when every active cell passes and no blocked
cell passes silently or fails for an unexpected reason. `run.py --verify-receipt <receipt>` checks a
receipt against the matrix, and the workflow asserts nothing else.

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
`toolCount` (41 from the a4bde4b server pin, whose roster `views.setup` is 41 tools at revision
`setup.v6`; it grew from 25 at `setup.v2` to 37 at `setup.v3`, and RC re-pin #4 adds
`honua_create_map_package`, `honua_create_app_package`, `honua_get_job_results` and
`honua_ingest_job_artifact`). Published `@honua/mcp-server` 0.1.12 dropped the selector and returned the 12-tool
`default` view ([honua-sdk-js#1875](https://github.com/honua-io/honua-sdk-js/issues/1875)). The pinned
0.1.14 proxy retains the `setup` view, so the cell is active. A proxy that drops the selector again
reports the 12-tool `default` view and fails the cell.

The NuGet cell restores the pinned `Honua.Sdk` from anonymous nuget.org into a clean consumer with
an isolated package folder and a cleared source list. It checks that the restored `.nupkg` matches
the manifest's sha256 and came from nuget.org, then builds and runs the shared GeoServices error
probe against the candidate. A release-mode run therefore cannot pass with an ecosystem missing.

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
full installed-client workflow suite. Both Redis-on runs retain all 39 current matrix cells,
including their declared blockers and unsupported scenarios; this suite does not claim to qualify
retired client-import coverage. A closed upstream issue cannot reactivate a row without a real
published-pin replay and reviewed matrix change. Missing fixture prerequisites report blocked,
identity/install/oracle failures report fail, and neither outcome qualifies a cell.

The image binding uses the existing per-cell resolver: ECS uses the generic server image;
Lambda uses the dedicated Lambda artifact after its ECR mirror digest is read back and verified.
The approved HTTP exception for an owned ALB remains a harness transport rule. Published CLI/MCP
clients retain their HTTPS requirement, so such a cell reports an explicit missing HTTPS prerequisite
before installing clients and emits no synthetic client receipt. Other HTTP hosts remain refused.

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

The installed MCP render step sends `honua_render_map` through the installed proxy for the
`sdkreg_area` polygon that `regression.seed_sql` creates and `publish_harness` publishes on the
candidate. Its pure [`render_png.py`](render_png.py) oracle requires `image/png`, a complete
CRC-checked, size-bounded PNG, the requested 260×320 dimensions, and painted/transparent
alpha at geographic-coordinate samples authored in `fixture.v1.json`. Malformed media,
containers and pixels fail with typed reasons. A canonical client may import that helper
from a protected immutable release revision with the matching fixture digest, but must
retain its own raw proxy response and genuine candidate seed/publication identity. A
helper-only test does not qualify a client or change any governed row.

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

Client-import qualification is retired. This repository does not retain its former producer, fixtures, or proof records, and the current certification inventory makes no claim for that coverage. Existing package installation and API probe cells remain separate checks.

The MCP plan-validation requests and semantic oracles are shared in
`mcp_plan_validation.py`. The installed proxy imports that pure helper. Canonical
client producers can fetch the helper and `fixture.v1.json` from one authenticated
immutable protected release revision, invoke its JSON CLI to obtain the valid and
invalid-WKB request pair, execute those calls through their own public clients,
and submit retained raw tool envelopes back to the same helper for judgment.
The helper binds the request arguments and fixture digest before applying the
existing executable/no-approval and actionable invalid-WKB oracles. It performs
no transport calls or job submission and emits no certification receipt itself.
Client, candidate, source, tier and global behavior admission remain the caller's
responsibility; proxy observations cannot substitute for canonical-client proof.

The release-owned `mcp_plan_validation.mjs` module executes the authored
valid/invalid plan pair through a caller-provided installed-client callback. It
retains raw observations even when the transport or pure Python oracle refuses,
rechecks helper/fixture SHA256 before every oracle execution, and rejudges proof
against the exact candidate source. Canonical producers fetch all three files
from protected immutable release history; the specification repository only
bridges its existing client adapters and consumes this resulting evidence.
This scenario validates a plan and never submits a geoprocessing job.

### Canonical plan execution and durable results

`mcp_execute_plan_validation.mjs` owns the actual installed SDK/public Inspector
execution journey for `tools/call:execute_plan` (release #708). Its pure Python
oracle rederives a single `Geoprocess` / `geometry.buffer` plan from the authored
metric point fixture. This is authored execution, not real-model planning.

The producer retains the invalid-WKB and fresh anonymous tool refusals before
submitting work. It uses the actual returned job URI for fresh-session terminal
polling and result reads, replays the same idempotency key, then restarts only the
explicitly owned disposable candidate and reads the same job/results again.
Every request is launched by the verified installed-package launcher, with raw
wire and separate client diagnostics. Inspector uses the server's actual public
`setup` discovery view for anonymous execution; missing advertisement or absent
invocation remains a failure. It never substitutes a raw transport for the CLI.

The independent state observer takes one bounded read-only Redis Lua snapshot of
job and result records, checks Redis key types, and excludes only the verified
store-owned coordination namespaces. Malformed durable records fail closed.
Complete bytes are hashed, while only this run's public job/result binding is
retained. Invalid/anonymous calls, replay, fresh reads and restart must preserve
the applicable committed census exactly; the positive submission adds exactly
one job and result without changing prior records. Result resources must match
the durable package. The oracle independently checks the authored circle's
center, radius, area, centroid, chord coverage, convex traversal, CRS and process
provenance, without asking the server geometry engine for expected output.

The required self-test floor runs pure semantic and source-binding controls.
`execute-plan-redis-controls` also runs the actual Lua against its own isolated
Redis service, including real active/index/lease/log key types, malformed record
refusal, and changes outside the projected fields. These are offline controls;
only retained actual installed-client evidence against the selected immutable
candidate can qualify the operation. All 82 governed rows, per-operation auth,
frozen client cohort and global gates remain required. Native/cloud parity,
cancellation/worker cessation and `publish_result` remain separate obligations.

### Genuine worker result publication

`mcp_publish_result_validation.mjs` owns the fixed installed SDK/public Inspector
journey for `publish_result`. Its setup module uses the supported Console
workflow package/version/validation/publication/manual-run APIs with the same
credential as the canonical client. A dormant schedule runs exactly one authored
`sink.external-postgis` workflow; direct `execute_plan` does not admit this
WorkflowOnly process. No setup inserts a job, workflow, result artifact, or
publication record into a database.

The producer retains the actual HTTP setup responses, immutable worker spec,
committed JSON receipt, Redis workflow/job/result identities, and exact returned
artifact. The result must declare `application/json` for its JSON sink receipt.
It then invokes `publish_result` through the real installed client, performs fresh
job/result and published-resource reads, queries the returned public layer, and
restarts the same owned candidate before fresh readback. Explicit `id` and
`attributes` projection proves the authored rows and geometry; wildcard behavior
is a separate server obligation, not inferred from this projection. Physical
publication layer, public layer index, source row ID, and materialized snapshot
object ID remain independently bound.

The SQL observer uses one statement for a complete source/catalog/active-graph
snapshot; its Redis census is independently atomic. These are quiescent cuts,
not a distributed transaction. Prior rows, graph members, full geometry,
connection hashes, and durable record hashes must survive successful publication.
Unknown-job/artifact and fresh anonymous attempts must leave the complete
immediately preceding committed state and revision unchanged. A completed
publication may retain its own operation-instance receipt; accepted, queued,
approval-required, denied, and failed outcomes are never completed publication
proof. This non-idempotent tool is not replayed.

The consumer must fetch and verify the complete 27-file closure from one
protected immutable revision before import, setup, every client call, and
rejudging. The trusted release launcher verifies installed package bytes and
owns asynchronous child launch. JSON receipts are not independent process or
socket attestation; source/workflow/artifact provenance remains the outer trust
boundary. Partial `.canonical-publish-result.raw.json` and `.setup.json` files
must be uploaded even when setup or execution refuses.

The offline Python/Node, actual isolated Redis, and actual isolated PostGIS
controls supply no candidate or installed-client qualification. Actual two-client
execution, every operation's authentication facets, frozen cohort reconciliation,
and all global gates remain required. See
[mcp_publish_result_contract.md](mcp_publish_result_contract.md) and release issue
[710](https://github.com/honua-io/honua-release/issues/710).
