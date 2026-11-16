# Agent workflow evaluation

Evaluate an installed Honua workflow using discovered CLI, SDK and MCP operations.
A procedure or fixture is not an observation. Candidate adoption, proof and
publication follow the [candidate evidence guide](rc2-cut-runbook.md); do not
change release pins or the customer install manifest to make an evaluation pass.

The [agent-map harness](../e2e/agent-map/README.md) calls Bedrock directly and
renders hardcoded public-demo sources. It cannot install ECS, publish or persist
a map, roll back, or tear down. Its output cannot prove the installed server's
StudioAi adapter or a complete workflow. Record it separately from installed
product evidence. An absent or denied operation blocks that step; a demo URL or
invented command is not a substitute.

## Prerequisites and authorization

Use a fresh worktree, installed published tools and a genuine model session.
Record source SHA, exact package versions and integrities, model identity,
server SHA, image digest and lock digest separately. Discover command flags and
tool schemas from the installed versions. Floating package discovery is useful
for exploration but cannot prove an exact-lock candidate.

This guide grants no cloud access or spending authority. Before a cloud run,
obtain authorization for the account, region, run namespace, model and bounded
resource plan. Use separate short-lived provisioning, reaper and read-only audit
identities. Keep credentials in the private operator session. Do not change IAM,
remove safety tags, chain roles or widen access to overcome a refusal.

Verify the deployed boundaries and all reachable identities before provisioning:

- Provisioning, mirror, workload and reaper roles have their required boundaries;
  image mirroring uses its separate identity.
- Standing and demo resources, other runs and other regions are protected from
  mutation. Read-only policy simulation must return the expected explicit denies
  without missing context; actual deployed negative-test evidence is also required.
- The workload can invoke the approved model and cannot invoke unrelated models.
  Test through the installed StudioAi adapter as well as checking IAM; simulation
  alone cannot prove entitlement, inference routing or application configuration.
- Current and prior images are distinct immutable `@sha256:` references. Registry
  read-back confirms both, and running task read-back checks the pulled digest,
  including the platform child digest for a multi-architecture image.
- The deployment and teardown inventories include tags and untaggable dependents;
  shared resources have a baseline inventory and remain protected.

An issue, source policy, broad administrator role or unexecuted checklist cannot
replace these observations. Supply only synthetic data.

## Cost boundary

Retain the evaluation ceiling of USD $25 total and two hours from the first
provisioning mutation, unless the authorized operator sets a stricter limit.
Reserve $5 for teardown. Stop new work at $20 or 90 minutes, on an alert, or when
a trustworthy cost bound is unavailable. Approval of this guide does not approve
spending. Prepare safe teardown even when the run fails or exceeds its ceiling.

Before approval, calculate a timestamped upper bound from current regional price
sources, quantities and USD arithmetic. Include every model round, compute,
storage, network, NAT, load balancer, database, logs, secrets and retained-resource
charges through teardown. Refuse provisioning when any quantity or price is
unknown or the total exceeds the ceiling. Maintain cumulative model usage and
resource lifetimes; a last-round token count is insufficient.

Verify current budget reads, actual-spend alerts at $100 and $200, and confirmed
notification subscribers. Account alerts are separate from the per-run ceiling;
billing and budget data lag and do not cap spend. Read spend immediately before
teardown, record freshness and remaining liabilities, then reconcile settled
charges afterwards. Unknown or over-ceiling spend fails the cost criterion.

## Execute and observe

For every step record UTC start/end, sanitized invocation, identities, outcome
and `executed/pass`, `executed/fail` or `not-executed/<reason>`. Preserve retries
and failures. A model-free probe is not a genuine-model run. Each mutation needs
its own approved proposal or saved plan digest; approval cannot override a failed
preflight or policy gate.

1. **Install.** Propose an isolated deployment with the exact image, approved
   namespace, remote-state identity, inventory and cost bound. Apply only the
   approved saved plan. Verify HTTPS, readiness, server revision, anonymous admin
   refusal, authenticated MCP discovery and the approved/denied model cases
   through the installed server. Read back the active task definition, running
   image digest and desired/running/pending counts; do not infer success from an
   accepted deployment request.
2. **Publish data and a map.** Import two synthetic points at `[-156.47,20.89]`
   and `[-156.45,20.75]`, with names A/B and classes 1/2. Query count, coordinates
   and properties back. Create a styled 2D map with class 1 blue, class 2 orange,
   a legend and the matching extent. Save, close and reopen it; verify persisted
   layer and style identities. Obtain approval from a separate principal for the
   exact publication proposal. Record the ordinary final HTTPS map URL and verify
   it before teardown, in a fresh unauthenticated browser for public publication.
   A local browser URL is not a published map URL. Missing save, publication,
   approval or read-back blocks completion.
3. **Roll back.** Run deployment preflight and schema/migration compatibility
   checks for the prior digest. After explicit approval, execute the governed
   rollback, wait for convergence and read back the active image. Query the same
   layer and reopen the same map with unchanged data/styles. Never re-import or
   recreate them to produce a pass. A compatibility refusal is a recorded blocker,
   not a completed rollback; this observation alone does not prove signed-lock
   upgrades, in-flight geoprocessing or exactly-once execution.
4. **Tear down.** Read cumulative spend first, even after a failed step. Reconcile
   this run's tagged and untaggable inventory, verify standing/demo exclusions
   and propose an exact teardown plan digest. After separate approval, destroy
   through the governed operation using the reaper identity and retain its durable
   receipt. Do not alter policies or remove tags to evade a deny.

## Reconcile cleanup and evidence

Use read-only service describe/list APIs to reconcile every pre-apply, post-apply
and teardown inventory entry. A tag search alone omits unsupported resources and
can return historical tags. Drain pagination and repeat after eventual consistency.
Cover tasks, services, clusters, retained task definitions, load balancers,
listeners, target groups, databases, snapshots, network interfaces, VPC components,
NATs, IPs, caches, logs, objects/buckets, run-specific registry artifacts, secrets,
keys, scaling targets, DNS, jobs and alarms. Explain historical tag entries with
service read-back. Confirm shared standing/demo inventory is unchanged.

Retained resources, recovery windows, pending key deletion and delayed deletion
remain leftovers until completed; record private identifiers and sanitized public
counts. Request approved cleanup without widening IAM. Claim zero live/retained
resources only when every entry reconciles, and settled final spend separately.

Capture prompts, selected tool names and outcomes with an allowlist recorder in
a private mode-0700 directory. Do not capture credential setup, environment dumps,
raw plans/state, raw tool results or browser network logs. Review transcripts,
receipts and screenshots before publication: exclude secrets, DSNs, signed URLs,
customer data and account/principal/resource identifiers; preserve digests and
stable synthetic IDs. A final map URL must contain no credential/signature query.

Retain all attempts, exact identities, approvals, prerequisite denials, install,
model, layer, map and rollback observations, URL verification time, pre-teardown
spend, cleanup reconciliation and settled cost. Audit product/support claims
against those observations and the current published support contract. Do not
convert an unexecuted step, preview exploration or local probe into certification.

## Local rehearsal

The [terminal journey driver](../certification/terminal-journey/README.md) can
probe a manifest-pinned local Docker stack. Inspect its per-stage receipt:
a probe pass is not a workflow stage pass. The map harness can be checked with
`npm --prefix app run typecheck` and `npm --prefix app run build` after installing
its dependencies. Starting it without submitting a prompt needs no AWS credentials;
a model turn requires scoped Bedrock access and an explicitly approved model.
Rendering still needs a browser and access to the public demo and basemap.

Local checks cannot prove cloud IAM, notifications, model entitlement, installed
StudioAi, registry inputs, billing, ECS install/rollback or cloud teardown. Neither
the deterministic driver nor the browser harness implements the whole durable
save/approve/publish journey; record missing steps rather than implying completion.
