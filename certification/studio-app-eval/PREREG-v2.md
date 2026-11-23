# Studio app from a prompt: preregistration v2

Scenario: a running server holds two published services. An agent builds a Studio app
(map, table, chart, filter, popup) through the server's Studio MCP tools and submits it for
publication. A separate human approver approves it. The published app then opens
anonymously with the expected layers and widgets.

Version 1 was frozen on 10 November and recorded on the release issue on 11 November. Its
driver and frozen files did not survive a lane-host replacement. Version 2 re-freezes the
same arms, attempt policy and bars from that record, and adds a committed driver
(`tools/studio_app_eval.py`). No v1 pilot result counts toward v2.

## Arms

| Arm | CLI | Pinned version | Mode |
| --- | --- | --- | --- |
| `opus` | `claude-cli`, model `claude-opus-5-5` | `2.1.316 (Claude Code)` | print, built-in tools off |
| `sol` | `codex-cli`, model `gpt-6.1-sol` | `codex-cli 0.166.0` | read-only exec |

No other model. A different installed CLI version refuses before the first model turn.
Both arms run on subscription logins; API-key auth is refused.

## Surface

The agent sees only the server's Studio MCP view, through the release recording proxy, with
a proposer API key it never sees. The prompt names the customer goal and the two services by
title, not tool names or document shapes. The harness holds a separate approver identity.

## Attempt policy and stops

- One attempt per run. One retry only when the CLI exits without a final answer.
- Stop the arm on the first quota, rate-limit or CLI infrastructure error, and record it.
- Every attempt, including stopped and failed ones, is kept in the run directory.

## Per-run assertions (all observed by the harness, never taken from the transcript)

| Id | Assertion |
| --- | --- |
| `proposal_created` | The session created exactly one publication proposal for a Studio item it authored. |
| `self_approval_refused` | The proposer key cannot approve its own proposal. |
| `approval_before_anonymous` | Before approval the published route is not readable anonymously; after approval it is. |
| `app_complete` | The approved document contains both services as layers, and a table, a chart, a filter and a popup. |
| `live_readback` | `app_complete` holds on an anonymous, cookie-free read of the published document, and the anonymous map renders it in a browser. |

## Bars

| Phase | Bar |
| --- | --- |
| Pilot (3 runs per arm) | `app_complete` on 2 of 3; `approval_before_anonymous` on 3 of 3 |
| Full run (10 runs per arm) | `live_readback` on 8 of 10 |

Measured and reported with every result: task success, MCP steps, wall time, and the
correctness of the published app document.

## Candidate binding

A qualifying run names the candidate server commit and image digest, and its stack runs that
digest. Runs on any other build are development evidence only.

## Out of scope

Scenario 2 (front door to app) is deferred to rc.3.
