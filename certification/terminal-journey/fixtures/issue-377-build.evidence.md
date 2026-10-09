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

- Focused journey, canary and receipt-checker suite: **285 passed, 57 subtests passed**.
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

## J2 id-contract re-run — 2026-10-08

Fix unit J2 of the 2026.1 rc.3 plan. The candidate is unchanged
(`87966c3f7b6c840ffc4d4da0b451714ab717b18a`,
`sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a`); the
clients are now `@honua/sdk-js@0.1.14` and `@honua/mcp-server@0.1.14`, and the
.NET SDK pin is `Honua.Sdk@1.10.4` on nuget.org. The result is still **not a
pass**; no stage 3-8 qualification is claimed.

[issue-377-j2-baseline.local-docker.json](issue-377-j2-baseline.local-docker.json)
is the unmodified trunk harness on a fresh stack (`2026-10-08T23:12:15Z`):

- Stages 1 and 2 **pass**. The installed 0.1.14 proxy completes initialize-bound
  setup discovery and the published CLI completes the `admin:read`
  create/effective-permissions/list/revoke preflight. The release#7 blocker no
  longer applies to stages 1-2.
- Stage 3 is blocked because `sdk.py` only accepted a GitHub Packages .NET pin,
  while the manifest now pins `Honua.Sdk` on nuget.org.
- Stages 3-8 were additionally blocked by the harness id contract: every stage
  required `policyDecisionId`, `actuatorId`, `verificationId` (and stage 8
  `approvalId`). The candidate emits none of them anywhere.

What changed in the harness, verified against the live candidate:

- Stage evidence is keyed on the identities the candidate emits. A live Studio
  `create_draft`/`save_version`/`propose_publication` call returns an `operation`
  envelope carrying `operationInstanceId`, `operationId`, `correlationId`,
  `auditId` (and `proposalId` for publication); none carries a policy-decision,
  actuator or verification id. Those fields are now optional and nullable.
- The .NET bridge accepts the nuget.org pin through the manifest gate's own
  registry/catalog/package-hash/repository-commit verifier; `dotnet build` of the
  bridge against `Honua.Sdk.Admin@1.10.4` succeeds and stage 3 `CreateConnection`
  plus `TestConnection` pass live (`3.datasource`).
- Stage 6 reads the map family's `currentSchemaVersion` (`"1.0"`) from
  `GET /api/v1/studio/package-families`; the old hard-coded `"1"` would fail
  envelope validation (`Schema version must be '1.0'`).
- Stage 7 principal: an `admin:write` key resolves to the `admin` role. On this
  pin the admin still receives a proposal, but server trunk now publishes an admin
  caller immediately (`PublishImmediately = isAdmin`), so the fixture's Studio
  author is a non-admin `write:journey` key. A direct probe on the candidate
  showed: `create_draft`, `validate_draft` and `save_version` succeed for that
  key once its stamped `layer-write-key` role holds StudioDraft
  Create/Read/Update/Publish operator grants; `propose_publication` returns
  `status: AwaitingApproval` with `proposalId`, `operationInstanceId`,
  `correlationId` and `auditId`; the admin proposal detail reads
  `AwaitingApproval`; the author's own `POST /approve` returns 403
  (`admin:approve` grant required). Publication routes must start with `/`, so
  the fixture route is now `/journey-map`.

Remaining blocker (server gap, not harness):

- **Stage 3 import.** `POST /api/v1/admin/import/geoservices/start` (and
  `/discover`) with the authored source `http://source:8080/arcgis/rest/services/journey/FeatureServer`
  returns HTTP 400 `ServiceUrl must be a valid HTTPS URL`.
  `GeoservicesServiceUrlValidation` requires HTTPS and rejects any host that
  resolves to a private or loopback address (`NetworkAddressValidator`), with no
  operator opt-in (unlike `OutboundHttpUrlValidator`'s `allowPrivateNetworks`).
  An isolated local-docker stack therefore cannot import any authored
  GeoServices source, and stages 4, 6, 7 and 8 have no published layer to act on.
  No substitute ingestion path is used.

The full re-run with the new harness was not retained as a receipt: its stage 2
CLI preflight timed out under host load inside the DNS workaround namespace, and
its stage 5 replayed an idempotency key the baseline run had already used on the
same stack with a different principal. The WSL host's root filesystem then
remounted read-only (`emergency_ro`), which stopped further runs. A clean
stages 1-8 receipt needs a fresh stack and the stage 3 import gap resolved.
