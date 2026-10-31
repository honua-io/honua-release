# Coding-agent CLI arms for the AI arc

The required arms are Claude Code with `claude-opus-5-5` and Codex CLI with
`gpt-6.1-sol`. `claude-sonnet-5-5`, `claude-sonnet-4-6`, and `gpt-6.1-terra`
are comparison arms. CLI receipts record agent type, executable version, model,
CLI-default temperature, subscription cost `null`, prompt and tool-catalog
hashes, CLI flags/config hashes, and hashes of every proxy response. Provider
and CLI prepends are outside the runner-controlled input hash claim. These are
exploratory CLI receipts, not signed Studio AI proxy provenance.

## Arc runner integration

`tools/agent_cli_arc.py::run_stage` is the stage adapter. The arc runner owns
slots, credentials, approvals, fault injection, independent observations and
teardown. It starts the backend appropriate to the stage and passes exactly that
backend under the indicated name:

| Stage | Target | Backend name | Controller-side backend |
| --- | --- | --- | --- |
| 1 | AWS ECS | `honua_devops` | `StdioMcpBackend` running published `Honua.DevOps.Agent --mcp` |
| 1 | Remote VM Docker | `quickstart_shell` | `RemoteQuickstartShell` over runner-owned SSH |
| 2–3 | Either | `honua` | `HttpMcpBackend` on installed `/mcp`, `workflow_view='full'` |
| 4 | Either | `studio` | `HttpMcpBackend` on g41's origin-locked browser MCP |

The generic CLI session can proxy multiple named servers concurrently; the arc
adapter restricts each stage to its own product surface. For each backend the
runner supplies `tool_view()` and `execute(kind, action)`. The CLI connects only
to private loopback recording proxies. The proxy logs each actual action before
it is dispatched; a shared ledger orders actions across multiple servers. The
CLI transcript can detect forbidden attempts and supply the final answer/usage,
but cannot supply actions, resource hints, or independent observations.

The developing arc runner's generic `run_command` / `read_file` / `write_file`
surface includes commands outside this lane's explicit allowlist. The CLI
session refuses that unmanaged surface before launching a model. The arc runner
must wire `run_stage` and `RemoteQuickstartShell` for its setup stage; it cannot
use the generic surface as a substitute. This integration belongs to the
separate arc-runner lane.

The controller starts `StdioMcpBackend(binary, slot_env=..., workdir=...).start()`
using the published binary documented in honua-devops `docs/QUICKSTART-MCP.md`.
Supply a fresh scratch workdir and an explicit slot environment. AWS access keys,
if supplied, require a session token; no inherited AWS profile, Docker settings,
CLI provider credentials, or user environment crosses into this process. The
operator process gets the slot credentials; the model and CLI do not. The
backend screens returned data for those credentials. The release runner owns
plan review, signing the exact approval digest as a separate principal, and
forwarding approved product calls. This library neither provisions cloud
resources nor approves a plan. After the operator closes, the runner retains
its private stderr audit file and `audit_sha256`; output containing a configured
credential or exceeding the audit bound fails closed and is not exported.
The published `v2026.1.0` Linux binary was checksum-verified on this machine;
its live stdio catalog exposed 35 tools but omitted `provision_infrastructure`,
`install_handoff`, and `verify_install_handoff`. AWS setup refuses before any
model call unless all six required setup/handoff tools are advertised. The g43
lane must supply a binary containing those product tools.

For a local-Docker target, the release runner supplies a fresh remote VM and
constructs `RemoteQuickstartShell` with its host, user, SSH identity, pinned
known-hosts file, isolated `/tmp/honua-arc-...` directory and unique Compose
project. Its runner-side health check must pass before stage 1. The model can
invoke only `docker compose` and `curl` to localhost, through the recorder;
command chaining, arbitrary shell, remote HTTP, Compose exec/run/down, and file
writes via curl are refused. Compose output is reduced to safe status fields or
an output digest so generated database credentials do not enter the model.
For `docker compose -f -`, stdin must hash to the controller-approved published
Compose bytes; the runner may separately make digest-approved public quickstart
text visible through a localhost endpoint. The release runner supplies the
prerequisite files and .env in the VM slot and retains their hashes. This is a
fixture boundary: the current published source quickstart also invokes Python
and OpenSSL, which are outside the explicitly authorized shell allowlist.

After setup, `SshTunnel(remote_shell, remote_port=8080).start()` owns a pinned
SSH forward from a fresh loopback port to that VM. The controller connects an
`HttpMcpBackend(tunnel.base_url + '/mcp', header='X-API-Key', token=...,
workflow_view='full').start()` to the installed candidate. It must verify the
server-authored full tool view; a silently narrowed catalog fails closed. The
server credential and tunnel identity stay in controller memory, never CLI
configuration. Stage 4 connects to g41's browser server with its controller-
held `McpEndpoint` URL/header/token. g41 owns its origin lock and independent
Studio observation; it has not yet landed on this branch.

Independent remote slots can run in parallel. The older corpus/canary command
runner admits one case or trial per cell slot, with a per-target lock; allocate
a fresh cell for the next case. There is no host-wide campaign lock.
Host-local Docker is refused for these
CLI arms. The pre-existing local Sonnet 5.5 P10 smoke was made before the remote
redirect: it reached the candidate, then failed during MCP discovery with zero
recorded model actions. The stack was torn down and the attempt retained; no
second real CLI session has been run. An empty `prompts/list` discovery response
was added and tested with fake executables.

The legacy corpus adapter uses W2's runner-owned gateway, controller reads and
oracle. Its remote-cell mode requires the retained target configuration and
verified deployment receipt. W2 scenario support, scripted clarifications,
remote restart/fault operations, and the arc runner's orchestration remain
separate dependencies. OPS is fail-closed while PR #1153 has no real wired
scenarios. No simulated operation is scored. The release session owns execution,
AWS provisioning, approval, and cross-family methodology review.

## Tests

Fake Claude and Codex executables connect to real loopback recording proxies,
exercise all three named MCP backends, and prove ordered action logging and
credential isolation. Additional negative controls cover shell scope, command
injection, non-loopback curl, Compose exec/down, unapproved inline Compose,
credential disclosure from the operator, absence of a short-lived AWS session,
unhealthy VM refusal, tunnel confinement, per-stage surface isolation, and
parallel independent CLI sessions. No real model calls are part of these tests.
