# Clean-room installation and failure rehearsal — 2026-09-04

**pre-cut, current packages**

**BLOCKED:** the current public server package cannot complete Production startup
with the published customer Compose recipe. This is an installation failure
receipt, not an end-to-end pass or exact-candidate certification. Repeat the whole
walk against the exact candidate after the cut.

## Scope and isolation

Native Windows PowerShell, Docker Desktop client/server `29.6.2`, Compose
`v5.3.1`; public SDK installed with Windows Node/npm. No WSL, Bash, local server
build, developer helper, source-installed client, license grant, or development
mode substitution was used for the customer walk. Linux containers ran through
Docker Desktop as authorized.

Fresh customer directory: `C:\Users\mike\honua-io\clean-room-20260904`.
`git -C <directory> rev-parse --show-toplevel` returned “not a git repository.”
An empty local Docker config established anonymous package access independently
of the host's existing registry credentials. npm used an empty user config and
local cache. The documented production `.env` held random disposable secrets
outside every checkout, with an owner-only Windows ACL. No credential values,
raw environment/config dumps, tokens, or license material are recorded here.
Runtime logs were captured in memory and scrubbed before display; retained
excerpts below contain no credentials.

Record base: `7870615`; site base:
`06d3ab8d8555b0888841072eafba334dabbe8566`. No matching rehearsal branches existed
on origin; both branches were pushed immediately after worktree creation, about
11:01 HST. Site source was edited only for documentation repairs, and its validator
was run only to verify those repairs. Release README/docs inventory and the release
cut checklist were inspected for record context; they supplied no customer setup
workaround. No server implementation or developer script was read to get unstuck.

## Inputs and pins

- Server: `ghcr.io/honua-io/honua-server:trunk`, resolved anonymously to Linux/amd64
  `ghcr.io/honua-io/honua-server@sha256:e971442db410dc0e095a9073d70195d0841d196a303050bdb7efa189195109fc`.
  OCI revision label: `a104af3c823d29f7d684545ab50af6cb0c525911`. This is a current
  development package, not a release candidate selected from engineering sources.
- Database: documented `pgrouting/pgrouting:17-3.5-3.7.3`, already cached at
  `sha256:5e6767abbd1fd9ead84c9988aa31bac71ca292ef8a1fc2e4a5ee2b613ac6a7bb`.
  The database volume was new; no preexisting database state was reused.
- Redis: documented `redis:7.4-alpine`, downloaded for this run; Docker deletion
  reported image ID `sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf`.
- SDK: `@honua/sdk-js@0.1.7-beta.0`, downloaded from
  <https://registry.npmjs.org/@honua/sdk-js/-/sdk-js-0.1.7-beta.0.tgz>.
  Integrity: `sha512-e2NwOqg5rv4jDzyXFc8dTzOBYvkfd/gugzPNOpBZTbgJgA5o+dtP/uc6kSLqPu8IfcfhtGDrp/ptICxYnB2udg==`.
- Supplied dataset: two synthetic points, dedicated to the public domain (CC0),
  names Rehearsal A/B, coordinates `[-157.8583,21.3069]` / `[-157.85,21.30]`.
  `rehearsal.geojson` SHA-256:
  `8579cfd11e6f7725e6b82e54d36955aeb2444fd097210e3b95649face7477c39`.

Exact dataset bytes (single line, no final newline):

```json
{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"Rehearsal A"},"geometry":{"type":"Point","coordinates":[-157.8583,21.3069]}},{"type":"Feature","properties":{"name":"Rehearsal B"},"geometry":{"type":"Point","coordinates":[-157.85,21.30]}}]}
```

The inline Compose block was copied verbatim from the published Docker Compose
page, without retrieving repository Compose files. File SHA-256:
`e41145376cb02164654d60f379c058b31d282ea75886b2c9ffc27747dfcdf1f2`.

Public documentation content hashes rechecked about 11:16 HST (UTF-8 content
returned by HTTPS; the GitBook pages advertise their `.md` representation):

| URL | SHA-256 |
| --- | --- |
| <https://honua.io/docs.html> | `5e4759ccdcf25dd9e4c0ce29d4563e112cb80c56b16e89e8cbf313f57a93bb5b` |
| <https://honua.gitbook.io/honuaio/get-started/quickstart.md> | `1ea7809cd457f0c8b743f7014dd838cfaa5c1676e38d9ea3e408a71ea804af8c` |
| <https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose.md> | `cdbeba50d979b8759f7c6672f833bee1708c435d1f5c278e143cf5b2677338a8` |
| <https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting.md> | `0aea611d969458e678a78d15d0aa16eccc86e0b1722f39318fe4efadcd60cdec` |

## Steps, timings, and results

Times are HST (UTC−10), on 2026-09-04. Minute ranges are approximate observations,
not benchmark measurements. Total customer investigation through runtime teardown
was about 18 minutes (10:57–11:15); subsequent time is reporting/PR validation.

| Time | Step and documented reference | Command/action and result |
| --- | --- | --- |
| 10:57–11:02 | [Docs fast lane](https://honua.io/docs.html#quickstart); [Quickstart, Steps](https://honua.gitbook.io/honuaio/get-started/quickstart#steps) | Read published instructions. Source/Bash-only quickstart not executed; see F01–F03. Host sandbox initially denied network/Docker; native tool permission resolved it. This is harness friction, not a product defect. |
| 11:02 | [First dataset, Create a small dataset](https://honua.gitbook.io/honuaio/get-started/first-dataset#id-1.-create-a-small-dataset) | Created and parsed two-feature GeoJSON in the fresh non-Git directory. PowerShell literal write replaces shell heredoc. |
| 11:03–11:05 | [Site image pin](https://honua.io/docs.html#pin) | `docker --config ./docker-config manifest inspect ghcr.io/honua-io/honua-server:trunk`, then `docker --config ./docker-config pull <resolved-image@digest>` succeeded anonymously. Manifest lookup approximately 6 seconds. No GitHub Packages login needed for this image. |
| 11:05:40 | [Docker Compose, Steps 1–2](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps) | `docker --config ./docker-config compose --project-name honua-rehearsal-20260904 config --quiet` exited 0. Fresh secrets, exact published Compose block, unique storage name; documented Linux file-creation/permission commands translated to native Windows. |
| 11:07–11:09 | [Docker Compose, Step 4](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps) | `docker --config ./docker-config compose --project-name honua-rehearsal-20260904 up -d` exited 0, but `compose ... ps` showed Honua `Restarting (139)` while Postgres/Redis were healthy. Creating containers is not readiness. |
| 11:08–11:12 | [SDK availability](https://honua.io/client-compatibility.html), Current SDK availability | `npm.cmd install @honua/sdk-js@0.1.7-beta.0 --registry=https://registry.npmjs.org --ignore-scripts --no-audit --no-fund` succeeded: 66 packages, npm reported 3 minutes. Extra flags isolate registry choice and suppress unrelated install activity; `npm.cmd` is the native Windows entrypoint. |
| 11:09 | [Docker Compose, Troubleshoot](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#troubleshoot) | `docker ... compose ... logs --no-color --tail 70 honua` captured the missing-PostGIS startup failure. Secrets scrubbed in memory before output. |
| 11:09–11:10:08 | [Troubleshooting, Database connections](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#database-connections) and [Emergency procedures](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#emergency-procedures) | `docker ... compose ... exec -T postgres psql -U honua -d honua -c 'CREATE EXTENSION IF NOT EXISTS postgis;'` returned `CREATE EXTENSION`; `docker ... compose ... restart honua` completed. Manual initialization cleared the first preflight failure. |
| 11:11:26 | [Troubleshooting, Startup](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#startup) | A second log collection showed Production startup aborting for missing safe approval mappers; no documented remedy found. Recovery failed. No development-mode bypass attempted. |
| 11:12–11:13 | [First dataset, Query the published layer](https://honua.gitbook.io/honuaio/get-started/first-dataset#id-5.-query-the-published-layer) | Set `HONUA_BASE_URL` and the disposable `HONUA_API_KEY` in process memory; invoked local `.\node_modules\.bin\honua.cmd services` (same installed CLI as documented `npx ... services`). Exit 1, `error: fetch failed`. Removed key from process environment afterward. This is a discovery attempt, not a successful feature query. |
| 11:13 | Stop restart loop | `docker ... compose ... stop honua`. No useful import/publication endpoint was available. |
| 11:14:48 | [One-terminal setup, teardown after GP](https://honua.gitbook.io/honuaio/get-started/one-terminal-setup#id-3.-run-bounded-gp-execution-verified) | Applied documented scoped `docker compose --project-name <own-project> down --volumes --remove-orphans`. All three containers, three volumes, and project network removed. Only newly pulled server/Redis images removed; cached database image retained. |

`docker ... compose ...` above abbreviates the same explicit
`--config ./docker-config compose --project-name honua-rehearsal-20260904`
prefix; no unscoped stop/down command was run. The public production guide's
DNS/TLS proxy step was not executed for this loopback-only evaluation; therefore
this receipt does not validate production TLS. No proxy, DNS, cloud, or remote
service was created.

### Redacted failure evidence

First failure, 21:08:54 UTC:

```text
PostGIS preflight check failed: PostGIS extension is not installed.
Honua requires PostGIS for spatial operations. Startup aborted.
```

After the documented PostGIS remedy and restart, 21:11:26 UTC:

```text
System.InvalidOperationException: Production operation runtime requires exactly one safe approval mapper for: control-plane.coordinated-release.rollback, control-plane.deploy.rollback.
at Honua.Server.Features.Operations.OperationRuntimeStartupValidator.<StartAsync>
```

The published diagnostics procedure (`compose logs`, health/status guidance and
emergency restart) was exercised against the actual installation failure. No
healthy server existed for admin diagnostic endpoints. No diagnostic-bundle
export command was found in the consulted troubleshooting/monitoring pages;
no bundle export or post-recovery health pass is claimed.

## Findings and ownership

| ID | Exact doc URL/section | Finding / manual intervention | Disposition |
| --- | --- | --- | --- |
| F01 | [Site fast lane](https://honua.io/docs.html#quickstart), “From clone to a responding server” and “Run Honua and publish your first layer” | Short recipe omits explicit build, GitHub CLI/package-read access and Python. Describes GeoJSON import although canonical quickstart seeds SQL. Ten-minute promise hides source-build prerequisites. | [Site PR #267](https://github.com/honua-io/honua-site/pull/267) replaces incomplete recipe with accurate scope/prerequisites and links. |
| F02 | [Quickstart, Steps 1–2, 6](https://honua.gitbook.io/honuaio/get-started/quickstart#steps); [One-terminal setup, section 1](https://honua.gitbook.io/honuaio/get-started/one-terminal-setup#id-1.-install-publish-and-verify-execution-verified) | Bash build/developer-validation scripts and Git-installed Python SDK/admin packages do not satisfy a package-only native Windows customer path. Documented GitHub Packages flow is build-oriented; not exercised or replaced with inherited credentials. | [Server #4300](https://github.com/honua-io/honua-server/issues/4300). |
| F03 | [Docker Compose, Steps 1–3 and Verify](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps) | `/opt`, heredocs, chmod and systemd have no Windows recipe. Release-digest placeholder lacks a direct manifest link. Used Windows file writes/owner ACL, current public digest, unique storage name and loopback origin. Skipped DNS/TLS for local evaluation. Python admin install remains source-based. | [Server #4300](https://github.com/honua-io/honua-server/issues/4300); site PR states current gap without inventing a passing recipe. |
| F04 | [Docker Compose, Steps 2/4](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps); [Troubleshooting, Database connections](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#database-connections) | Fresh database passes health check but lacks PostGIS; install restart-loops. Manually applied documented extension SQL, absent from install sequence. | [Server #4301](https://github.com/honua-io/honua-server/issues/4301). |
| F05 | [Docker Compose, Steps](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps); [Troubleshooting, Startup / Emergency procedures](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#startup) | Current package aborts Production startup after PostGIS remedy because two safe approval mappers are missing. Documented restart fails to recover; supported CLI cannot discover services. | **Blocking** [Server #4302](https://github.com/honua-io/honua-server/issues/4302). |
| F06 | [Operations](https://honua.io/operations.html), “Open the operations docs” | CTA leads back to generic docs hub rather than deployment/operations runbooks. | [Site PR #267](https://github.com/honua-io/honua-site/pull/267) links directly to the published operations guide. |
| F07 | [First dataset, Import the file / Publish the table / Query](https://honua.gitbook.io/honuaio/get-started/first-dataset#id-2.-import-the-file) | Supplied dataset was created, but upload, publication, actual feature query and post-restart data persistence could not run because startup never completed. Upload guide itself acknowledges no high-level SDK wrapper and requires the local API explorer. | Not tested; blocked by [#4302](https://github.com/honua-io/honua-server/issues/4302), not treated as separate unobserved runtime defects. |
| F08 | [Troubleshooting, Emergency procedures](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/troubleshooting#emergency-procedures) | Captured logs and attempted graceful recovery, but no matching remedy for approval-mapper failure. No authenticated health/bundle export possible while server is down. | Recovery not achieved; [#4302](https://github.com/honua-io/honua-server/issues/4302). |

| F09 | [Geoprocessing, Set it up → Admin API](https://honua.io/docs/geoprocessing/#set-it-up) | Existing gap link points to closed server #3275, so the live issue gate fails. Open site #235 explicitly owns this missing setup documentation. Replaced only the link and regenerated Markdown/HTML; capability state unchanged. | [Site PR #267](https://github.com/honua-io/honua-site/pull/267); existing [site #235](https://github.com/honua-io/honua-site/issues/235). |
| F10 | [Site AGENTS.md, Commands / Capability-slice docs](https://github.com/honua-io/honua-site/blob/06d3ab8d8555b0888841072eafba334dabbe8566/AGENTS.md) | Direct native Windows generator/validator commands silently exit 0 without executing because their entry guards compare a POSIX file URL against Windows argv. Detected by unchanged generated files. Read the site tooling entry guards for this documentation validation failure (not for customer installation), then used a native Node path adapter. Initial silent exits are invalid evidence. | [Site #268](https://github.com/honua-io/honua-site/issues/268); no tooling code changed in the docs PR. |
Auxiliary Windows evidence-tool corrections: Docker's quoted Go-template label
lookup failed under PowerShell argument handling; a full **image metadata** JSON
read (not container environment) supplied the OCI revision. Windows PowerShell's
`ConvertFrom-Json` rejected npm lockfile's empty root key; Node read package metadata
instead. Neither command is a customer product workaround. Browser fetch initially
failed in the research fetcher; native HTTPS returned 200 and GitBook's advertised
Markdown representation was used. No GitHub 403 or device-auth flow occurred.

## Teardown and validation

Runtime teardown succeeded at 11:14:48 HST. Filtered container, volume and network
inventories for the rehearsal project were empty. Removed the newly pulled server
and Redis images without force; preserved preexisting database and other images.
The unique local storage volume was explicitly reported removed. No remote
resources, uploads, published layers, or customer connection records were created.

At 11:20:01 HST the exact fresh directory was deleted after verifying its resolved
absolute path stayed inside the authorized workspace. `Test-Path` returned false.
This removed disposable secrets, the dataset, SDK installation, isolated npm/Docker
caches and temporary PR/issue bodies. No known rehearsal resources remain.
The two isolated review worktrees and pushed branches are retained as requested
deliverables, not running customer resources.

Site PR: <https://github.com/honua-io/honua-site/pull/267> (non-draft, base `trunk`).
Native focused validation: `node scripts/validate-internal-links.mjs` passed for
140 pages, then checkpoint `baa78a41644269ef041039fb26b117669647cc45` was pushed.
The initial site gate rejected the new phrase “source evaluation” under its
existing vocabulary check. Changed it to “a source build” and corrected HTML
container nesting; the gate was not weakened. Focused link validation passed
after each correction and checkpoints were pushed immediately. Latest site head:
`5b3b08e` (full CI pending at this record update). Report branch checkpoint
`abbb8b7` was pushed after Compose validation and `47b1f89` preserved the runtime
findings. Author and committer on every checkpoint are Mike McDougall
<mike@honua.io>, without attribution trailers.

Record PR: <https://github.com/honua-io/honua-release/pull/273> (non-draft, base
`trunk`). Final head-specific gate results must be checked before handoff.

Release-record CI at `9377657` passed: validate (3m35s), commit hygiene and all
CodeQL analyses. Site CI at `ae217c9` passed the corrected vocabulary check but
failed on the closed issue reference (F09):
<https://github.com/honua-io/honua-site/actions/runs/33920666959>.
The reference repair and generated pages are now at site `5b3b08e`; this record
update still requires its own final-head CI verification.

Actual native validation after detecting F10: 10 generated files match; 17
Markdown files / 44 relative links and fragments resolve; 1 slice, 109 capability
keys, 23 sample IDs and 7 live gap issues validated. The adapter imports the
unchanged tool after setting `process.argv[1]` to
`pathToFileURL(resolve(script)).pathname`, making the existing entry guard run.
HTML element nesting for both edited pages also verified. These are site editing
checks, not installation pass evidence. Temporary issue-body files used after
customer-directory cleanup were removed immediately.

## Exact-candidate repeat

After the cut, obtain the customer-accessible immutable candidate artifact and
published client versions through the documented channel. Start from another empty
Windows directory and new project volumes. Record candidate image/SDK digests,
execute import → publish → supported-client feature query, verify the same two
features after service restart, induce a bounded documented failure, collect
redacted diagnostics, recover to readiness and repeat the feature query. Complete
scoped teardown and verify no leaked resources. This pre-cut blocked attempt cannot
substitute for that receipt.