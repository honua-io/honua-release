# Terminal candidate journey — issue #123

- Run: `2026-08-31T20:00:40Z`
- Driver: `0e3e995` (PR #205), from release trunk `32b68de`
- Server source: `honua-server@6b5f34fe725ec58b785d12b143942eb5f0a66aff` (current `origin/trunk` at the cut)
- Local image: `honua-server:release-123-6b5f34f@sha256:5ae2ed71fca3cb0c0d73f962c6cef90cd58b497e124c57c64caec226785c446b`
- Receipt: `terminal-journey-issue-123.json`
- Receipt SHA-256: `8b709b44cc31f1a63ef70eb66833be40cd35154671490c56da278dd488b26d69`
- Outcome: **blocked**

The server image was built locally from a clean detached worktree with
`HONUA_GIT_SHA=6b5f34fe725ec58b785d12b143942eb5f0a66aff`. Its OCI revision label and live
capability-manifest `deploymentRevision` both matched that SHA. A run-scoped manifest
bound this identity in the receipt; `platform-manifest.yaml` was not repinned.

The published clients were integrity-verified and installed from their exact registry
tarballs. The verified command surface contains `honua`, `honua admin`, and
`honua-mcp-proxy`. The proxy nevertheless exited during initialize when launched through
the installed npm shim, so tool discovery and all downstream MCP stages remained blocked.

The receipt is intentionally unedited. In particular, the landed stage metadata still
claims that `honua admin` is absent in stages 2, 3, and 8 even though the same receipt's
verified `clientWorkspace.commandSurface` reports it present. It also marks compose
ownership blocked because the SHA-bound stack was externally managed with `--base-url`.
Neither contradiction was relabelled green.

Stage outcomes:

1. **Blocked** — readiness, exact current-trunk identity, and anonymous auth refusal pass;
   proxy initialize and bounded setup/profile discovery block completion.
2. **Blocked** — Admin API-key endpoint presence passes; landed stage metadata incorrectly
   retains the obsolete missing-`honua admin` blocker.
3. **Blocked** — no certified typed operation envelope; landed metadata also retains the
   obsolete missing-CLI blocker.
4. **Blocked** — proxy discovery failed and stage 3 produced no published-layer subject.
5. **Blocked** — proxy discovery failed; the local target has no Redis-backed job runner.
   The prior GP lifecycle defect is tracked by honua-server#3739 and is closed on this trunk;
   this run did not reach the lifecycle and therefore does not claim it verified.
6. **Blocked** — proxy discovery failed and no immutable map/dashboard version could be
   created, restarted, reopened, or hash-compared.
7. **Blocked** — no durable proposal path or Redis-backed control plane.
8. **Blocked** — no durable proposal existed for separate-principal approval.

The missing `esri-gp` MCP profile remains an open server blocker at
honua-server#3741. The receipt additionally preserves the driver's linked blockers for
the setup view, canonical operation runtime, roster exports, proposal authorization,
source-evidence posture, Redis durability, and approval store.
