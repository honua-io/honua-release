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

The driver then proves which terminal commands the pinned bytes actually ship, rather
than trusting the manifest's `targets` labels. Against the 2026.1-rc.2 candidate:

| Command | Shipped by | Status |
| --- | --- | --- |
| `honua` | `@honua/sdk-js@0.1.7-beta.0` | present |
| `honua-mcp-proxy` | `@honua/mcp-server@0.1.4-beta.0` | present |
| `honua admin` | — | **absent** |

`@honua/mcp-server` is labelled `targets: [node, honua-cli, honua-admin, honua-mcp-proxy]`
in the manifest, but its published `bin` map is `honua-mcp` and `honua-mcp-proxy` only,
and the `honua` CLI it is credited with actually ships from `@honua/sdk-js`. No pinned
artifact ships an `honua admin` command surface, so stages 2, 3 and 8 have no runnable
client verb regardless of server readiness. The Admin REST surface those stages need
does exist on the candidate; the gap is the client, and it belongs to #7.

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

Self-tests need no stack, no Docker and no network:

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
stage is nonetheless `blocked`, because no stage's full contract is satisfied yet. The
receipt names which dependency stops each one.

AWS wrapping and genuine-model evidence are linked as #129 and #161, never embedded or
treated as substitutes.
