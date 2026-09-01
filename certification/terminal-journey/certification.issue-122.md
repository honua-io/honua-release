# Terminal setup-to-published-map certification — issue #122

Outcome: **BLOCKED**. This record consumes the immutable run receipt in
[PR #217](https://github.com/honua-io/honua-release/pull/217) at commit
`e40df88f6580ba171e5e3a347862b2ccaa7e0fa5`; it does not copy, edit, or supersede it.
The source receipt SHA-256 is
`8b709b44cc31f1a63ef70eb66833be40cd35154671490c56da278dd488b26d69`.

The local run proved exact server identity, anonymous Admin refusal, and installation of
the integrity-bound published clients. It completed no numbered product stage. The proxy
exited during initialization, the driver retained a contradictory missing-CLI blocker,
and no published layer, pixels, GP result, saved composition, proposal,
separate-principal approval, or final URL was produced. The red observations remain red
in `certification.issue-122.json`.

## Stage adjudication

1. **Blocked:** setup/profile discovery — [server #3428](https://github.com/honua-io/honua-server/issues/3428), [server #3741](https://github.com/honua-io/honua-server/issues/3741).
2. **Blocked:** credential proof cannot be certified while the source receipt retains its contradictory missing-CLI blocker — [PR #217](https://github.com/honua-io/honua-release/pull/217).
3. **Blocked:** datasource/import/service mutation — [server #3411](https://github.com/honua-io/honua-server/issues/3411).
4. **Blocked:** style/render pixels; no published-layer subject — [server #3411](https://github.com/honua-io/honua-server/issues/3411).
5. **Blocked:** GP lifecycle/result — [release #202](https://github.com/honua-io/honua-release/issues/202), [server #3583](https://github.com/honua-io/honua-server/issues/3583), [server #3741](https://github.com/honua-io/honua-server/issues/3741). Closed server #3739 was not reached and is not claimed re-verified.
6. **Blocked:** map/dashboard version, restart, reopen, and hash identity — [server #3411](https://github.com/honua-io/honua-server/issues/3411), [server #3475](https://github.com/honua-io/honua-server/issues/3475).
7. **Blocked:** durable proposal/poll — [server #3411](https://github.com/honua-io/honua-server/issues/3411), [server #3474](https://github.com/honua-io/honua-server/issues/3474), [server #3583](https://github.com/honua-io/honua-server/issues/3583).
8. **Blocked:** separate-principal terminal approval and final URL — [server #3431](https://github.com/honua-io/honua-server/issues/3431), [server #3474](https://github.com/honua-io/honua-server/issues/3474), [server #3599](https://github.com/honua-io/honua-server/issues/3599).

## Independent AND gates

No sibling receipt may turn the terminal result green. AWS [#129](https://github.com/honua-io/honua-release/issues/129), exact-candidate client matrix [#157](https://github.com/honua-io/honua-release/issues/157), browser Studio [#121](https://github.com/honua-io/honua-release/issues/121), browser Console [honua-console#351](https://github.com/honua-io/honua-console/issues/351), and authoritative 396/385/11 roster exports [server #3363](https://github.com/honua-io/honua-server/issues/3363) are independently blocked or absent for this cut. Fixture, skipped, contract-only, and forced-checkpoint evidence is not used.

## Verbatim one-terminal walkthrough

The release walkthrough is the following ordered terminal contract. It is published
verbatim for operators, but is **not** represented as an executed success:

```text
1. Install the exact pinned honua and honua-mcp-proxy packages; verify readiness, candidate identity, authentication, bounded profile/tool view, proxy connectivity, and paginated tools/list.
2. Run honua admin key list and effective-permissions for the installer-provisioned credential; do not create or rotate a key through MCP.
3. Run honua admin connection create/test, import, service/layer publish/configure, and access-policy commands; retain the typed operation, policy, actuator, verification, evidence, and terminal-state identities.
4. Run honua_get_style, honua_apply_style_preset, honua_render_map, then read and decode the PNG; verify changed pixels.
5. Discover and run bounded geometry.buffer; wait/cancel through the canonical job state machine and retain the result.
6. Use honua_studio_* tools to create distinct map and dashboard drafts that reference the layer and GP result; mutate, validate, save immutable versions, restart, reopen, and compare content identities.
7. Submit publication; require an AwaitingApproval proposal bound to item, version, and content hash, then poll it from the terminal.
8. As a separate human principal run honua admin operate approveOperationProposal --path id=<proposal-id> --profile approver --yes; prove proposer self-approval denied, poll to published, and verify the final URL.
```
