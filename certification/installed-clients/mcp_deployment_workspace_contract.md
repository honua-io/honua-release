# Deployment and workspace primary execution contract

This producer is being completed for the original `deploy_operations`, `propose_rollback`,
Deployment resource and Workspace resource on both canonical installed MCP
clients. It preserves all eight rows and all four required facets. Source
controls are regression evidence; actual candidate qualification remains separate.
The current PR remains incomplete until the full two-client producer/oracle,
positive and negative controls, and protected consumer have been validated and
delivered. The earlier fingerprint-only semantic component cannot qualify a row.

## Complete proof intake

The complete entry point is `createDeploymentWorkspaceCohort` in
`mcp_deployment_workspace_cohort.mjs`. Its `honua.mcp-deployment-workspace-proof/v2`
proof must pass the complete Python cohort oracle for BOTH original client
journeys before any of the eight original operation records is emitted.
`mcp_deployment_workspace_validation` is an independently checked semantic
component only: its proof has `semanticOnly: true` and the distinct
`honua.mcp-deployment-workspace-semantics/v1` schema. That component is never the
complete proof intake for the consumer or authority facet.

One normal owned `scoped-api-key` role lifecycle spans both SDK and Inspector.
Normal deletion preserves that globally unique name as a tombstone, so the
fixture cannot be recreated per client or rerun by resetting/reseeding the same
candidate. Both original raw journeys, configuration hash and candidate tuple
are bound before final role cleanup. Four normal managed keys use a common
salt and distinct key IDs, canonical actors, Studio owners and workspace owners.
Their public original receipts are independently rejudged through provisioning,
effective permissions, Redis records/indexes and normal revocation. Private
mode600 journals stay outside artifacts; failed cleanup preserves recovery
journals and a nonsecret recovery-path receipt.

The explicitly declared owned multi-tenant/end-user fixture posture is checked
against safe daemon-side selected configuration values. This is a tested
configuration, not a claim about unchanged default posture. Normal admins keep
their supported tenant override and cross-tenant bypass. The foreign caller uses
the normal non-bypass scoped role with only Deployment/Read and Workspace/Read
grants, plus its exact `read:*` and owned-service geometry execution grants.

The foreign caller first reads its own real published deployment and its own
real GP workspace, and positively queries the original nonempty FeatureServer
fixture with `read:*`. Valid scoped platform-operation and rollback denials then
preserve the product's actual authorization semantics. The original cross-tenant
Deployment/Workspace denials remain mandatory. A separate real primary-owned
workspace in the foreign caller's default tenant additionally exercises the
personal-owner denial branch; a shared-scope denial cannot substitute for it.
Both clients use fresh sessions and the exact supported tenant header only on
the explicitly selected admin path. Original GP job bytes independently bind
`audit.requestedBy`/`principalId`/`ownerActorId` to the canonical managed actor,
and `workspaceOwnerId` to the bare managed key GUID.

## Required production prerequisites

The canonical Studio deployment writer is server PR 6393. Read its immutable
source contract for implementation, but require its normal protected delivery
and an image containing that source before executing a positive journey. A
branch, unit fixture, domain Draft or directly inserted Active/Succeeded record
cannot replace that prerequisite. `publish_result` with deployment target is a
separate unsupported runtime path and does not become supported here.

Actual platform workflow history must already contain two real succeeded Deploy
operations for the configured owned target. The producer reads the canonical
Redis terminal materialization window and sorts target matches by creation time
and ordinal operation ID, matching the production rollback selector. The
per-target converge index is not the rollback selector. Missing/truncated or
ambiguous history refuses the implicit rollback positive. No history is created
by modifying Redis or by approving/submitting a platform operation.

## Actual product journeys

1. Create a Studio map draft through `honua_studio_create_draft`, update a valid
   authored map body, validate, and save its exact generation. Capture the actual
   item/version/content hash. Publish that immutable version with the real
   authorized tenant admin through `honua_studio_propose_publication`.
2. Read the exact returned Deployment URI through each installed canonical client.
   Compare source identity, saved content and publication pointers with an
   independent owned Postgres transaction. Require the real publication-created
   transitions and ETag. Runtime health remains Unknown until an actual runtime
   probe proves otherwise; published pointer state is not observed runtime health.
3. Create a real named GP workspace through the supported GPServer
   `geometry.area` execution with `env:workspace`. Require the actual result
   scalar and independently compare the durable workspace/artifact owner, scope,
   metadata and bytes. This is a prerequisite to the primary Workspace read,
   not an extra MCP primary-row claim.
4. Read actual platform operation IDs through `honua_deploy_operations`; compare
   IDs, revisions, statuses, ordering and truthful evidence posture against the
   independent Redis observations. Request `honua_propose_rollback` without an
   explicit revision so selection must use actual prior history. Compare the
   new forward proposal's durable gateway payload and audit/idempotency identity.
   Approval remains required. Do not approve, submit, apply or claim actuation.
5. Restart the same explicitly owned disposable server container, preserving its
   source/image identity. Fresh installed clients must read the same persisted
   Deployment and Workspace, immutable version/hash and ETag. A fresh MCP session
   alone is not restart persistence.

## Refusals and independent controls

Retain exact raw request/response bytes and hashes, installed client launch and
version, private credential fingerprints, distinct initialized sessions and
actual typed anonymous refusals for every primary operation. A second distinct
normal policy-bound API-key caller must receive typed not-found responses for
both owned resources; a missing second caller fixture refuses execution before
any setup mutation. Unknown IDs,
invalid arguments, cross-tenant/owner resource denials, changed saved
hash/generation, wrong prior revision, altered sealed proposal payload,
missing real history, degraded workspace, altered store state, and false runtime
health must refuse a positive proof. Offline vectors must explicitly identify
both their synthetic transport and control data; they earn no candidate credit.

The independent state observer is read-only. It targets only the fixed owned
loopback database and Redis endpoints, never changes history/indexes, and never
retains private credentials or arbitrary backend parameters.

Refs honua-io/honua-release#729 (producer implementation and actual execution).
Refs honua-io/geospatial-mcp#130 (protected thin consumer and qualification).
