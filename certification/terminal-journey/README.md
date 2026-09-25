# Terminal-first control-plane certification

This directory owns the deterministic, model-free terminal journey (honua-release#123,
evidence key `release.e2e.terminal-zero-to-map`) and the control-plane roster drift gate
(#121). The primary 2026.1 workspace is one terminal using exact installed client
artifacts; Console and browser Studio remain separate client receipts and do not define
this roster.

## Layout

| Path | Role |
| --- | --- |
| `journey.v1.json` | The eight numbered stages: id, command, the pinned client commands each needs, and its upstream contracts. Imported by #161; never duplicated there. |
| `receipt.schema.json` | `terminal-journey-receipt-v1`. Binds package integrities, source SHAs, and the server/fixture/config/auth-policy tuple to a per-stage outcome. |
| `targets/local-docker.json` | Local Docker target. Reuses `e2e/local-docker/docker-compose.yml` so the server image is injected from `platform-manifest.yaml`. |
| `pins.py` | Consumes the exact #136 `clientArtifacts` from published registry bytes and proves which terminal commands they actually ship. |
| `probes.py` | Deterministic probe primitives: HTTP, compose lifecycle, and MCP JSON-RPC through the pinned `honua-mcp-proxy`. |
| `stages.py` | The eight stage implementations and the outcome discipline. |
| `run.py` | The driver. `--mode build` (contract only) or `--mode live --target …`. |
| `live_driver.py` | The `terminal-journey-driver-v1` adapter #161 calls, at the path its protocol contract fixes. |
| `fixtures/smoke-receipt.local-docker.json` | A real receipt from a real run against the pinned candidate. Not a template. |

## Honesty rules

These are enforced by `receipt.schema.json` and asserted by `test_run.py`:

- **There is no skip state.** A stage is `pass`, `fail`, or `blocked`. A stage that
  cannot run yet is `blocked` and must name at least one missing dependency.
- **A pass requires a live observation.** `harness-build` evidence can never be
  `verified-current`/`complete`, and build mode can never report `pass`.
- **A failure names the numbered stage and the command or tool that broke it**, in
  `failure.number` / `failure.stage` / `failure.command` / `failure.check`.
- **No fabricated receipts.** If the pinned client artifacts cannot be consumed from
  published bytes at their manifest integrity, the run fails closed: every affected
  stage is blocked and says why.
- **No model calls anywhere.** Fixed commands and tool calls only, so a failure
  identifies a broken contract. The genuine-model canary is #161 and is linked, never
  embedded.

## Pin consumption

`pins.py` resolves each `clientArtifacts` entry from the registry, checks the registry
integrity against the manifest, re-hashes the downloaded bytes, and re-runs
`tools/verify_client_artifacts.py`'s own archive identity check — the repository's
existing verifier is imported rather than reimplemented, so this driver and the manifest
gate agree by construction. Only those verified tarballs are installed. There is no
checkout, workspace, regenerated or floating-`npx` fallback, and the workspace is
materialized fresh on every run so stale bytes can never stand in for the pins under
certification.

The driver proves which terminal commands the pinned bytes actually ship, using
`clientWorkspace.commandSurface` in each run. `honua` and its `admin` command come
from `@honua/sdk-js`; `honua-mcp-proxy` comes from `@honua/mcp-server`. A successful
`honua --help` probe must advertise `admin`. A failed process, missing verification,
or ambiguous command row cannot certify presence.

The historical smoke fixture used SDK 0.1.7-beta.0 without Admin. The later
[issue #123 run](../../artifacts/terminal-journey-issue-123.md) verified Admin in
SDK 0.1.9-beta.0, but preserved obsolete stage blockers. Both are immutable
observations, not the current command inventory. New receipts distinguish command
discovery from credentialed execution. When the pinned `honua` binary and a
loopback admin credential are present, stage 2 runs a reversible private-CLI
preflight: mint an `admin:read` key into a 0600 sink, read its effective
permissions, list it without key material, revoke it, and delete the sink.
The root credential and the one-time key never enter the receipt. Service
mutation, style pixels, GP, composition, publication, and separate-principal
approval remain blocked until their own observations exist.

See the [#120 acceptance audit](../../docs/2026.1-terminal-arc-acceptance.md) for
pre-cut implementation gaps and separately released exact-candidate reruns.

## Running it

Contract only, no target, no network:

```
python certification/terminal-journey/run.py \
  --output artifacts/terminal-journey.json --evidence-uri "$EVIDENCE_URI"
```

Live against the pinned candidate on local Docker:

```
python certification/terminal-journey/run.py --mode live \
  --target certification/terminal-journey/targets/local-docker.json \
  --output artifacts/terminal-journey.json --evidence-uri "$EVIDENCE_URI"
```

The live run brings the compose stack up on the manifest-pinned digest, probes it, and
tears it down. `--base-url` reuses an already-running stack; `--keep-stack` leaves it up.

Self-tests need Python with PyYAML/jsonschema and Node.js (the CLI subprocess
fixture), but no stack, Docker or network:

```
python certification/terminal-journey/test_run.py -v
```

## Control-plane roster gate

Once server#3363 publishes the authoritative Admin OpenAPI/CLI and MCP projection
exports, pass both files to `--rest-roster` and `--mcp-roster`; the gate requires an
exact 396 = 385 + 11 partition, unique IDs, and no overlap. Secret/session exclusions
stay REST/CLI-only and use a private secret sink. Anonymous MCP discovery never implies
call authorization. Until those exports exist the roster verdict is `blocked`, never
`pass`.

## What the live lane proves today

The committed smoke receipt records eleven passing probes against the pinned candidate,
including readiness, exact candidate identity (`deploymentRevision` equal to the manifest
server SHA), anonymous admin refusal, a paginated 52-tool surface read through the pinned
proxy, and the presence of the style, GP, Studio and publication tool families. Every
stage in that receipt is `blocked`, because no stage's full contract was satisfied.
The receipt names which dependency stops each one. It is not rewritten by the
stage 2 preflight. A later live run can pass stage 2 only from those observations,
and that still does not pass the journey.

AWS wrapping and genuine-model evidence are linked as #129 and #161, never embedded or
treated as substitutes.
# Measured setup discovery

Stage 1 now selects the server's `setup` view in the actual HTTP and installed
stdio initialize requests. Later selector-free discovery must retain that view.
An authenticated `full` request is drained independently of any expected tool
count, and a subsequent selector-free request must restore the initialized view.
Full catalog names remain available to later stage diagnostics; only the bounded
setup descriptors are exposed in `toolView.tools`.

`setup-discovery.json` retains the original HTTP JSON response, complete canonical
catalog descriptors, revision metadata, and measured byte/digest evidence. The
tools array and each descriptor are sliced from original UTF-8 JSON without
reserialization. The unchanged ceilings are 48 tools, 128 KiB aggregate and
16 KiB per descriptor; estimated tokens must equal aggregate bytes divided by
four, rounded down. The membership digest follows each tool's stage in original
wire order. The revision digest is server-authored and compared across transports,
not reconstructed from incomplete stage metadata. Full-catalog comparison hashes
use a separately named normalized serialization and are not server wire digests.

Both transports reject duplicate JSON keys, invalid/nonstandard JSON, oversized
responses and incomplete catalog pagination. The stdio consumer reads bounded
binary chunks before parsing. It invokes only the verified installed executable;
there is no source, resolved-module or floating-package success fallback.

The optional `Terminal journey contract` workflow dispatch accepts
`engineering_server_image` (an immutable Honua GHCR digest) and
`engineering_server_revision` (its full source SHA). The dedicated engineering
job reuses the hosted Docker fixture, generates an ephemeral bootstrap credential,
verifies image configuration/runtime identity and anonymous refusal, and consumes
the unchanged frozen client pins. It retains only `rehearsal.json` and
`setup-discovery.json`, excluding installation directories and credentials. The
receipt explicitly sets `qualification: false`; an image override does not create
a governed candidate cut or attestation. Failed pinned-proxy behavior remains a
failed rehearsal even if direct HTTP measurements pass.

The proxy negotiation prerequisite is
[SDK PR 1783](https://github.com/honua-io/honua-sdk-js/pull/1783). Its locally packed
source tests do not replace a published package or update this repository's
frozen pins. Later execution, distinct-principal approval, model/error recovery
and saved-map stages remain blocked until their actual driver implementation and
candidate receipts exist. Discovery never grants call authority.
