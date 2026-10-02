# Issue 377 build executor observation — 2026-10-02

The first executor slice is engineering implementation and test evidence. It does
not certify the complete build journey or close issue 377.

The final local Docker run used the unchanged manifest candidate:
`87966c3f7b6c840ffc4d4da0b451714ab717b18a`, image digest
`sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a`.
Published clients were `@honua/sdk-js@0.1.9-beta.0` and
`@honua/mcp-server@0.1.4-beta.0`, consumed at their manifest integrities.

The actual receipt is [issue-377-build.executor.local-docker.json](issue-377-build.executor.local-docker.json),
observed at `2026-10-02T23:03:56.690970Z`. The result is **blocked**:

- Exact candidate identity, readiness, disabled licensing and anonymous refusal pass.
- Stage 2 passes the actual published CLI create/effective-permissions/list/revoke
  preflight. It verifies exactly `admin:read`, confirms revocation, and deletes its
  0600 one-time-secret sink. Credential values are absent from the receipt.
- Stage 1 remains blocked: HTTP setup discovery passes wire validation and drains
  the complete 124-tool catalog over 11 pages, but the installed pinned proxy
  transport fails. The executor does not substitute direct HTTP or a resolved
  module path for that failed installed executable.
- Stages 3–8 remain blocked on verified initialize-bound discovery. Their import,
  buffer, render, replica, map, proposal and approval assertions therefore have
  **no live candidate qualification** in this receipt.

The cold-start fixture now checks the final Postgres TCP listener, avoiding the
image's temporary Unix-socket initialization server. The second candidate replica
starts after the first is healthy. An isolated temporary Docker configuration was
needed to pull the public fixture image because the host's `desktop.exe` credential
helper errored; no host credentials or persistent Docker configuration changed.

The earlier baseline remains in [issue-377-build.local-docker.json](issue-377-build.local-docker.json).
It records the original installation failure. Intermediate red observations and
the CLI argument-order repair remain in branch history. The final invocation was:

```sh
DOCKER_CONFIG=/tmp/377-docker-config python3 certification/terminal-journey/run.py \
  --mode live --target certification/terminal-journey/targets/local-docker.json \
  --workdir /tmp/377-executor-verb-first \
  --output certification/terminal-journey/fixtures/issue-377-build.executor.local-docker.json \
  --evidence-uri https://github.com/honua-io/honua-release/issues/377
```

Validation:

- Focused journey, canary and receipt-checker suite: **281 passed, 55 subtests passed**.
- Full required tools/ and licensing suite after integrating current trunk:
  **1,702 passed**, with two existing tarfile deprecation warnings.
- Published .NET SDK bridge, exact cached GitHub Packages `Honua.Sdk.Admin@1.7.0`:
  build and scoped format pass; five actual loopback SDK calls pass, with one
  import submission and no credential output. The authored peer proves SDK
  serialization/transport; this version is not substituted for the manifest pin
  and the exercise explicitly reports `qualification=false`.
- Manifest structure/coherence/drift, regenerated release decision, compatibility
  documentation/ledger, customer install manifest and signing namespace checks pass.
- Docker compose configuration and `git diff --check` pass.

Remaining criteria are a passing candidate build/approval/verification receipt,
update and rollback execution with two local image revisions and exactly-once/schema
proofs (plus signed-lock qualification when locks exist), and operate with approved
remediation and both per-run prompt-injection probes. No update, rollback or operate
qualification is claimed. Candidate selection remains the continuous strict trunk
train; this branch does not re-pin or freeze the manifest or override a gate.
