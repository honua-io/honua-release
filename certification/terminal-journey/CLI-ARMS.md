# Native coding-agent CLI arms

These exploratory arms use Claude Code (`claude-opus-5-5`, `claude-sonnet-5-5`)
and Codex (`gpt-6.1-sol`, `gpt-6.1-terra`) on the controller machine. They do not
use Bedrock. Subscription cost is `null`, temperature is the CLI default, and
receipts identify the agent, CLI version, requested model and controlled input
hashes. Provider/CLI prepends are outside the input hash claim. A CLI receipt is
not signed Studio AI proxy provenance and cannot be substituted for that arm.

Run from the release worktree with the repository's Python dependencies installed:

```bash
PYTHON=/path/to/venv/bin/python tools/agent_cli_arms.sh \
  --surface corpus --agent claude-cli --model claude-sonnet-5-5 \
  --cases P10:A,P10:B --trials 1 --target local \
  --pack /path/to/pack.v2.AB-only.json --output /path/to/new-run-directory
```

`--surface canary` runs the complete terminal journey. It sends each reviewed
unguided stage goal to the native CLI, preserving driver observations, the render
fault and recovery checks, the separate approver and final URL/pixel verification.
The controller creates/tests the authored datasource and uploads the fixture;
these are recorded as harness fixture work. The model selects publication,
style/render, GP, durable composition and proposal actions. This fixture boundary
differs from the shell-capable Bedrock arm and is explicit in the receipt.

Both surfaces accept `--target https://<cell>.cert.demo.honua.io` together with
`--target-config <retained-cell-target.json>` and
`--deployment-receipt <deterministic-journey-receipt.json>`. The existing driver
checks the receipt's image, source, target hash and deployment readback before
attaching to that ECS cell. Provision the cell using the existing release-owned
IaC/workflows separately. These tools never create AWS resources or dispatch a
workflow. Controller datasource references remain in the controller. The CLI sees
only the recording proxy URL; upstream Honua credentials stay behind the proxy.
Remote corpus key cleanup revokes case-minted keys, and the provisioner retains
ownership of the cell. Scenarios needing a cell restart fail closed until a real
controller operation exists. Corpus fault/approver support remains bounded by W2.

The corpus registers `ModelSession` implementations with the PR #1152 runner.
The recording proxy dispatches through its runner-owned gateway; the runner seals
the action log before controller observation/replay. Session-reported actions and
model prose cannot supply oracle evidence. P22 receives a fresh CLI process and
scratch HOME for each session, with only the previous final answer as follow-up.
The same plugin is selectable directly with
`tools/agent_e2e_local_runner.py case --agent claude-cli --model ...`.

OPS exposes `certification/agent-ops/cli_agent.py::run_real_session` for W1's real
scenario adapters. The current W1 revision has no registered executable real
scenarios, so `--surface ops` and the OPS runner's `--agent` hook refuse before
launching a CLI. The simulated controller is never used for these arms. The hook
requires a registered real adapter, verified seed, real probes/readback context,
MCP execution and independent server observation. W1 still owns campaign wiring.

Each campaign takes a host lock and runs sequentially. Local runs check Docker
health before and after each case and stop on infrastructure/cleanup failure.
Receipts are never replaced; each trial has its own directory and index row.
Do not run against the full corpus pack or held-out variants. The A/B loader
refuses the full pack by filename before reading it.

The CLI environment is allowlisted, with a scratch HOME outside all repositories.
Only subscription authentication is copied, excluding settings, MCP servers,
plugins, hooks and repository instructions. Claude disables builtins and restricts
allowed tools to this Honua MCP server. Codex uses read-only sandboxing, no user
config/rules, disabled shell/web/file/agent features and a bundled model catalog
with builtin shell/patch capabilities removed. All controlled catalog bytes are
hashed. Unsupported CLI behavior fails closed. Transcript events are used to
detect forbidden attempts and extract the final answer/usage; actions and resource
hints come from the recording proxy, and state evidence comes from the controller.

## Development validation

Fake executable tests exercise both CLIs over an actual loopback HTTP recording
proxy, including environment/config isolation, non-MCP attempts, time/action bounds,
forged transcript arguments and canonical action digests. The corpus integration
uses W2's real recorder and independent oracle against its fake HTTP candidate.
Negative controls fail the canary for missing fault recovery, missing final proof,
self-approval or incomplete cleanup. OPS is tested to refuse before any CLI call.

One authorized local Sonnet 5.5 P10:A smoke was attempted on 2026-10-31. It reached
the fresh candidate and Claude Code 2.1.316, then stopped during MCP discovery with
zero recorded model actions and clean Compose teardown. The initial proxy treated
an unsupported discovery request as an action failure. Empty `prompts/list`
discovery is now supported and covered with fake binaries; the real session was
not repeated. This is not successful model-evaluation evidence. Its original
receipt is retained at the controller's `rc2-cli-arms-sonnet-smoke-1` directory.
