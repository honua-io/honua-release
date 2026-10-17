# Actual AWS update and rollback producer

`aws-update-rollback.yml` drives the supported deployment actuators in exactly four cells:
ECS and Lambda, each with Redis on and off. It runs the ordinary baseline journey first,
then performs A → B → A, observes the actual native routing/image and booted source/contracts,
checks the independent authored `e2e_src_fs` rows at all three revisions, and always tears down.
ECS Redis-on uses `honua-aws-ecs-alb` through an independent retained controller; both Lambda
cells use `honua-gitops-aws-lambda`. ECS Redis-off is explicitly `terraform-exact-apply`.
Deployment operations are submitted with separate approval credentials. The native reconciler
must pass its own telemetry/protection policy; the producer does not force promotion.

Each Redis-on transition has its own real durable job. The job must be Running immediately
before and after that switch with unchanged job/provider/worker identity and attempt count one.
It then must produce one successful worker-start/completion event pair, a stable successful
terminal record, and a stable independently checked result. The rollback transition repeats the
same obligations with another job. A job already completed before a switch fails the proof.
Redis-off instead requires actual typed `dependency-unavailable` / `jobs.runner` / `redis`
refusals at A, B and restored A, independently confirmed by the capability manifest. Those
cells explicitly record `executionOutcome: refused`, `functionalGpExecuted: false`, and have
no in-flight-job record. Refusal never counts as GP completion.

Before any cloud mutation, the admission job authenticates both immutable builds (and both
signed locks in qualification mode) and admits only identical declared database schema versions.
It has read-only repository/artifact permissions and no AWS identity.
Before staging any B image, the producer starts its authenticated generic image against the
actual cell database with `HONUA_SKIP_MIGRATIONS=true`, no Redis or deploy target catalog,
and requires its supported deploy preflight to show zero pending and zero extra migrations.
A temporary database /32 admission and the probe container are removed in all outcomes.
The actual A/B/A application preflights must also remain compatible. No undeclared schema
range, schema upgrade, forced downgrade or application migration is inferred by this proof.

## Required reviewed inputs

The workflow only runs from trusted `trunk` with repository variable
`HONUA_AWS_UPDATE_ROLLBACK_ENABLED=true`. This source change does not enable that variable,
provision anything or dispatch the workflow. Existing cloud prerequisites from `e2e/README.md`
remain required, including real DNS, application key-ring/audit/storage references and cost limits.
Select an IaC pin containing the retained ECS safety and typed release-identity contract and
honua-iac#258's Batch revision-retention contract. A stale root fails before its image update.

Two modes remain separate:

- `rehearsal`: `baseline_ref` is an exact reviewed prepared-deployment SHA and
  `candidate_run_id` retains B's `prepared-deployment` artifact. Both distinct server builds
  need real GitHub image attestations. The Lambda child is authenticated through membership in
  its attested parent index, and its ECR mirror must preserve those manifest bytes. This emits
  only diagnostic rehearsal output, without promotion class, signed-lock identity or RC credit.
- `qualification`: `baseline_ref` selects A's nightly candidate; `baseline_run_id` and
  `candidate_run_id` retain two distinct real `certified-candidate` bundles with authentic
  signed locks. Both locks must bind the retained manifests and exact server builds. Only a
  successful complete four-cell run with actual cleanup/cost evidence emits and attests
  `promotion-receipt-update-rollback`. The trusted fetcher accepts that artifact only from this
  workflow's successful default-branch dispatch. No rehearsed receipt can satisfy that class.

Both manifests must declare the real release version, `dbSchema`, complete `contractVersions`,
source SHA, generic index and platform digests, ECS architecture, Lambda child architecture,
GHCR digest and byte-preserving observed ECR digest. `pending-ecr-mirror` cannot qualify.
Use `RELEASE_GH_TOKEN` with the existing cross-repository read access plus image/attestation
read access for opt-in authentication. The ordinary reusable cloud jobs retain their existing
GITHUB_TOKEN permission floor; no caller's grants are silently raised.

Protected secret `UPDATE_ROLLBACK_CONTROLLER_ENV` contains the normal Production startup,
audit/storage/secret-reference configuration and the retained controller's own database/Redis.
It must not override derived targets, release identity or bind address. The controller Redis
endpoint/database must differ from the cell queue, and must be reserved for this run. Secret
`UPDATE_ROLLBACK_APPROVAL_KEY` must identify a separately authorized principal valid in that
controller store; equality with the submitting application key is rejected. This source does
not create either identity or claim the broader self-approval security probe.

Variable `HONUA_UPDATE_ROLLBACK_CONTROLLER_ROLE_ARN` names the operator-approved independent
role. It must match the maintained ECS safety output for the Redis-on ECS cell. The trusted
server controller and read-only candidate schema probe assume that role; provisioning credentials
remain in the credentialed controller job. Variable `HONUA_UPDATE_ROLLBACK_ECS_SAFETY` is JSON
with approved `controller_role_name`, `telemetry_connection_id`, `prometheus_canary_job` and
optional bounded timing settings accepted by the maintained root. The golden functional URL
is derived from this run's allocated authored layer; its expected baseline bytes are hashed only
after checking every row against the source SQL oracle. Variable
`HONUA_UPDATE_ROLLBACK_ECR_REPOSITORY` selects the existing approved same-region mirror
repository. No role, secret, repository or telemetry connection is adopted by this source change.

Original Terraform arguments, including protected input values, travel privately inside the
existing encrypted and digest-authenticated state archive. Fresh controllers restore those exact
arguments rather than reconstructing a different configuration from mutable workflow variables.
Only the explicit image, alias and typed release identity fields change. No private input is
included in a public receipt or artifact allowlist.

The default real workload is a symmetric polygon centroid with an independent origin oracle.
There is no synthetic hold hook: if it finishes too quickly, qualification fails honestly.
For a sufficiently long reviewed workload, commit a reviewed `certification/update-rollback-workload.json` in the trusted workflow source
(or retain `update-rollback-workload.json` in B's prepared/certified bundle) and bind its exact bytes in **both** retained manifests as
`updateRollbackWorkload: {sha256: 'sha256:<digest>'}`. The pack has exactly `schema`
(`honua.update-rollback-workload/v1`), `processId` (supported `geometry.*`), `execution`
(with only `inputs`) and an independently authored literal `expectedFeature`.
The producer verifies those bytes before submitting the real operation and compares its complete
result to the literal oracle. Preparing that pack is a governed input, not permission to replace
an expensive operation with fabricated wait/completion evidence.

The Lambda Redis-on run opts into retaining prior Batch definitions. It records all four exact
baseline definition ARNs before image staging, rejects a saved plan that deactivates them, and
deregisters only those owned revisions after all owned jobs finish or confirmed cancellation.
Terraform then destroys the current definitions with the remaining ephemeral stack. Failed or
unconfirmed controller/job/revision cleanup prevents qualification. Ordinary deployments retain
their existing lifecycle default.

No live qualification is claimed by unit, schema or workflow tests. The remaining execution
boundary is approved inputs → reviewed trunk dispatch → actual four-cell proof with teardown
and cost → signed candidate-bound receipt. The broader release/client, security, burn, Windows
and promotion gates remain independently required. Refs #483 (governed proof and Lambda GA
bill disposition remain) and #377 (other journey/operate scope remains).

## Coverage and remaining qualification

| Obligation | Local source controls | Actual execution still required |
| --- | --- | --- |
| Exact A/B provenance and lock identity | Real CLI adapters; attestation/index/lock mismatch negatives | Two approved attested builds; GA additionally two distinct certified signed locks |
| Before-mutation schema refusal | Read-only B-image probe; both journal directions; incompatible-schema negatives | Actual B startup against each A database, with zero pending/extra scripts |
| ECS/Lambda actuators | Supported API and saved Terraform-plan adapters; no fallback/backend-substitution controls | Real alias, ALB and task readbacks across A/B/A |
| In-flight exactly-once GP | Separate per-switch Running/identity/attempt/event/output controls | A genuine workload stays Running across each real switch and completes once |
| Redis-off nonexecution | Typed HTTP refusal, recursive hidden-job negatives, independent capability manifest | A/B/A refusals in both actual Redis-off topologies; never GP completion |
| Authored serving data | Expected IDs/names/coordinates from retained seed source, not candidate parity | Exact published fixture rows at actual A/B/A endpoints |
| Recovery and cleanup | Always-teardown controller; owned job cancellation/revision/probe cleanup; strict missing-cost refusal | No orphaned jobs, revisions, controllers or infrastructure; bounded real cost report |

Local tests inject external I/O; their synthetic records are never release evidence. This PR
implements the producer, but no actual AWS switch, fault/recovery run, real workload duration,
operator role/approval/telemetry binding, cleanup or cost measurement has been executed here.
Those remain owned by #483; broader journey/operate requirements remain in #377. The retained
Batch source prerequisite is honua-iac#258, and the first-release coordination issue remains #376.
