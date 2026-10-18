# Real Honua ECS version recovery qualification — proposed, unrun

This is a separate qualification from the standing nginx ECS/ALB weighted-cutover
fixture. That fixture is unchanged and cannot satisfy this gate. Registry
inspection confirmed two distinct anonymous-pull Honua OCI indexes, their runnable
amd64 manifests, configuration hashes and source revision labels in
`published-artifact-discovery.json`. Those observations prove publication and
identity only. They do not prove signed release provenance, compatibility,
installation, serving, upgrade, or recovery.

`tools/ecs_honua_recovery.py` acts only on an existing, explicitly reviewed
`honua-qual-*` substrate. It has no Terraform apply, ECS update/register, ALB
modify, approval, arbitrary process, or resource deletion command. Its only
mutation requests are the existing canonical `honua_propose_deploy_operation`
and `honua_propose_rollback` MCP tools. A separate human approves each proposal
through the existing server inbox. The server remains the deployment executor.
Approval receipts contain only references and identity, never hidden execution
payloads or credentials. The harness requires a stored proposer key identity,
rechecks its active metadata, and rejects immediate execution or approval bypass.

## Concrete scope for root review before any cloud write

Provision/reuse a dedicated Honua qualification cell, not the nginx mechanics
cell and not a production target. The proposed topology is two smallest adequate
Fargate services, stable and canary, an isolated ALB non-default weighted rule,
a shared isolated PostGIS database and Redis, and an existing control server
with durable proposals/workflows. Both services retain the same approved database
secret reference. Seed a nonempty, bounded dataset (at most 256 rows), record the
ordered FeatureServer query checksum and a restorable database snapshot before
upgrading. Register baseline and candidate task definitions with the verified
immutable Honua images; credentials remain secret references, never inline in
this scope or receipts. No resource creation is authorized by this harness.

The cell needs an actual stored AwsEcs target using backend `honua-aws-ecs-alb`,
qualification environment, exact cluster/canary/stable/listener/group parameters,
explicit AWS region and staged canary percentage, server-owned telemetry
connection, exact stable/canary metrics jobs and
credential-free public synthetic golden query/response checksum bound to this
serving origin, **and its real RequiresApproval=true target
policy preserved**. The existing server intentionally has two independent actions:
MCP always creates a proposal for the human inbox; after that approval, the
backend-required deployment remains AwaitingApproval until a human explicitly
submits that workflow. This harness verifies both barriers and never changes
that governance flag to make a run pass.

Both artifacts must be bound to canonical signed release inputs: either published
platform manifest bytes with their Sigstore bundles, or an attested/signed platform
lock. Verification pins the exact release workflow, source SHA and ref, validates
the certificate issuer/repository, and compares component source, registry index
and runnable amd64 identity. The manifest route supports older releases whose
manifest binds the OCI index but does not yet carry platformDigests; the immutable
index is resolved using the existing registry proof. It does not fabricate a lock.
The published `honua-2026.1` release has signed manifest assets; their declared
server is f897700/sha256:78e3088d…, not either of the two images discovered here.
A matching signed provenance pair has not yet been established for this proposed
pair. No image will execute merely because it can be pulled from the registry.

Before approval, root must fill and review the exact AWS account, region, resource
names/ARNs, public HTTPS serving/control origins, database/snapshot references,
signed artifact provenance, seeded query/count/checksum, schema compatibility review and cost
budget. Proposed run scope creates zero new resources; owner provisioning and
cleanup require their own reviewed plan. Bound desired/concurrent task counts,
maximum run duration and cleanup ownership in that plan. The template bounds
one task per service at 256 CPU units/512 MiB; actual adequate sizing must be reviewed
before use. Both immutable task definitions are checked for amd64, CPU/memory,
exact image and retained database secret reference before any proposal. The example scope is
intentionally incomplete and cannot authorize execution.

## Run sequence after scope approval

Set only `HONUA_ECS_QUAL_PROPOSER_KEY` and `HONUA_ECS_QUAL_DATA_KEY` in the protected
host environment. Use different scoped keys for control and data origins. AWS
read-only discovery uses the existing SDK/CLI credential chain; no AWS credential
is read into model context or written to evidence. No approver key is accepted.

Run `prepare` with a completed scope and evidence directory. It writes an **unrun**
receipt plus a canonical scope digest and credential-presence flags. Every other
command requires that exact digest as `--reviewed-scope-digest`.

1. `baseline`: verify both signed release manifests/locks, registry bytes, live Honua
   readiness/revision, retained query checksum/count, actual running ECS image
   digests, stable baseline, ALB traffic and health, and canonical target/telemetry
   binding, including a genuine anonymous query denial. Evidence contains
   hashes/counts rather than row values or task secrets.
2. `propose-upgrade`: recheck provenance and the approval-required candidate plan,
   recheck live baseline/data/cloud state, persist an exclusive local intent,
   then create the canonical proposal. Lost
   responses require inspection/reconciliation; the harness never retries a proposal.
3. A separate human reviews and approves through the normal inbox.
   `workflow-upgrade-approval` then verifies that this approval produced the exact
   target workflow in **AwaitingApproval**, with actual cloud/data state still
   at baseline and no cloud submission. A human
   explicitly submits that operation through the normal deploy UI/REST. The
   harness neither approves nor submits it.
4. `observe-upgrade`: require the resolved proposal's distinct approver and actual
   successful deploy operation for the exact target/backend/task revision; verify
   candidate runtime digest, serving revision, healthy traffic and unchanged data.
5. `propose-recovery`: seal a new reviewed forward deployment to the exact retained
   baseline via `honua_propose_rollback`. It again stops at the human inbox.
6. After separate inbox approval, `workflow-recovery-approval` verifies the
   exact retained-baseline workflow is AwaitingApproval. A human submits it through
   the existing deploy UI/REST. Then `observe-recovery` verifies the restored baseline
   version/image/task revision, terminal workflow, health and retained data.
   Runs exceeding the approved budget record `recovered-over-budget` and exit
   nonzero; they are not passing qualification. Cleanup remains owner-controlled,
   never an automatic deletion of the reviewed existing target.

The concrete acceptance scenario is **successful upgrade followed by human
requested version revert**. It must not be relabeled automatic rejected-candidate
rollback. Failed readiness, telemetry-triggered automatic compensation,
restart/reconcile mid-rollout, migration incompatibility/manual snapshot restore,
and rollback failure escalation remain separate unrun qualification cells.
A green syntax/unit result cannot certify any of them. `upgrade_recovery.py`'s
existing failed-upgrade classification continues to govern those broader receipts;
this harness does not synthesize its required migration/write evidence.

No new live Honua target, dataset, credential pair, telemetry connection, signed
provenance pair, or canonical approval journey has been established. No cloud resources
were changed and this harness has not been run against AWS.

The current deploy DTO exposes the original requester, but does not expose a
distinct manual workflow submitter identity. The harness proves the distinct
inbox approver and the real AwaitingApproval-to-terminal transition; it does not
fabricate a second approver audit receipt. Qualification requiring that additional
identity must retain independent server audit evidence or add a narrowly reviewed
server audit seam before claiming it.

The retained query is private and must deny anonymous access. Server telemetry
uses a separate public synthetic golden query: the current deploy probe contract
has no credential-reference field, so no API key is embedded in a query URL.
Both health/golden URLs and the stable/canary metric job identities must match
the reviewed target parameters exactly.
