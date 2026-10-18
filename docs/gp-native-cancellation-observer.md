# Native cancellation and filesystem effects on AWS

`tools/gp_native_cancellation.py` implements the cancellation/effects slice of
[release679](https://github.com/honua-io/honua-release/issues/679) for both
`aws-ecs/redis-on` and `aws-serverless/redis-on` (Lambda + Batch). It submits a
single filesystem-only native conversion through the installed official MCP
SDK, independently observes native entry, repeats `honua_cancel_job`, then
checks canonical terminal state, unavailable results, physical outputs and a
second observation after a 15-second settle interval. It creates no cloud
resources, changes no deployment, grants no permissions and releases no barrier.

Redis-off cannot enter this producer. Its typed nonexecution journey remains a
separate obligation. EKS remains Preview. This adds no scenario denominator or
release waiver. Every receipt sets `candidateQualified: false`; the existing
journey, release, candidate identity, update/rollback and full lifecycle matrix
obligations remain open under release123/381/652 and server3809. Local controls
and a source PR cannot replace retained executions on the selected candidate.

## Prepared deployment contract

Adopt the separately reviewed server6346 native image and configuration through
the normal deployment owner. The native image must have authenticated source
provenance from the maintained nightly workflow. An architecture child must
belong to that attested index; a registry mirror must preserve its manifest
bytes. The task definition must use the exact digest-addressed native image.
The serving endpoint and selected platform candidate still require the existing
independent release bindings; this producer does not authenticate a platform
lock or change its pins.

The worker remains non-root with a read-only root filesystem. Its actual task
definition/launch overrides must explicitly declare:

- `HONUA_GP_QUALIFICATION_BARRIER_ROOT` at the qualification EFS mount.
- `HONUA_GP_QUALIFICATION_PAUSE_AT=native-process-started`. This skips the earlier
  optional pauses while retaining actual execution and cancellation behavior.
- `Geoprocessing__OutputStaging__Provider=local`, `PersistenceClass=shared-persistent`,
  `StoreReference`, `LocalRootPath` and `KeyPrefix` matching the configuration below.
- `GdalWorker__ScratchRoot` matching its mounted scratch directory. The deliberate
  ignore-cancellation mode is refused.

For persistent ECS workers, place a separate diagnostic container in that same
`pidMode: task` task. Pin it to the same native image, override its entry point
to `["/usr/bin/python3"]` and command to
`["-I", "-B", "-c", "import time; time.sleep(86400)"]`.
Mount worker scratch, barriers and output at identical absolute paths, read-only
in the observer. ECS Exec needs a writable diagnostic root; never relax the
worker root to satisfy the Exec agent. Mount the observer policy read-only at
`/run/honua/observer-policy/observer-policy.json` on a separate volume that
is never mounted in the worker.
The observer drops Exec's root identity to the fixed native-image UID/GID 1001
before reading anything. Keep the worker at UID1001 and make policy and scoped
mounts readable by that user; neither container needs an added trace capability.
The diagnostic command uses the absolute, isolated interpreter. Give it no
additional environment, secret environment, inherited volumes or launch
overrides. The native image protects the observer script and `/app` directory
from replacement by UID1001. Neither container may shadow image executable or
import roots, or add overlapping ancestor/descendant mounts inside an observed
output, barrier or scratch scope. The observer admits only its declared mounts.

For Lambda + Batch, retain a separate running store-observer ECS task. It uses
the same native image and diagnostic command, reads the exact same EFS output
and barrier mounts, and has a store-observer policy with null `scratchRoot`.
The producer discovers the new Batch job's exact attempt/task from the
canonical provider ID after submission. It verifies the reserved
`HONUA_OPERATION_ID` and `HONUA_EXECUTION_ATTEMPT` launch variables against the
submitted job. Native-entry barrier evidence and RUNNING task observations
precede cancellation; that same attempt must later have a stop time, exit code
and an independently observed STOPPED ECS task. A cancelled durable job may
have a provider exit code of zero; this does not mean its GP result succeeded.
The separate EFS observer survives that task and inventories post-cancel bytes.

Both mounts require EFS filesystem ID, access point, root directory, encryption,
read-only flags and the existing store attestation to agree. The observer's
scope digest must match these exact paths and prefix. A copied marker on a
fresh empty volume cannot substitute for matching actual worker/observer mount
identity. S3, API Lambda temporary storage and the runner's filesystem are not
substitutes for this filesystem-backed store.

## Inputs and invocation

Prepare one reviewed JSON configuration for each cell. Values are selectors to
be checked against actual provider responses, never assertions of successful
observations:

```json
{
  "cell": "aws-ecs/redis-on",
  "endpoint": "https://owned-qualification.example",
  "region": "us-west-2",
  "cluster": "<exact cluster ARN>",
  "workerTask": "<exact persistent worker task ARN; omit for Batch>",
  "workerContainer": "worker",
  "observerTask": "<exact diagnostic task ARN>",
  "observerContainer": "qualification-observer",
  "workerImage": "<registry/repository>@sha256:<64 lowercase hex characters>",
  "source": "<40 lowercase hex characters>",
  "barrierRoot": "/run/honua/qualification",
  "scratchRoot": "/tmp/honua-gdal-worker",
  "outputRoot": "/var/lib/honua/gp-outputs",
  "keyPrefix": "gp/outputs",
  "policySha256": "<SHA-256 of exact mounted observer policy bytes>",
  "workloadSha256": "<SHA-256 of exact reviewed plan file bytes>",
  "store": {
    "reference": "qualification",
    "configurationDigest": "<existing store configuration digest>",
    "attestationSha256": "<SHA-256 of exact existing store marker bytes>"
  }
}
```

For Batch, use `cell: aws-serverless/redis-on`, omit `workerTask` (the producer
resolves it), and add exact `batchQueue` and revisioned `batchDefinition` ARNs.
Use the running independent store observer's policy digest. Its scope has the
same barrier/output roots and prefix and null scratch root.

The reviewed plan file is an MCP `honua_execute_plan` argument containing
exactly one `Geoprocess` step with `processId: gdal.ogr2ogr`. Use actual source
bytes and choose a real workload long enough for native observations. Early
completion or absence of a live child fails the ECS proof; no simulated delay,
PID substitution or relaxed assertion makes that failure pass. Workloads with
database or external sink effects need additional independent target oracles
and cannot qualify through this filesystem-only producer.

Install the repository's frozen official MCP SDK from
`certification/sources/geospatial-mcp/package-lock.json` with the normal locked
package installation. Supply the existing qualification API key through
`HONUA_GP_MCP_API_KEY`, then invoke the maintained producer:

```text
python tools/gp_native_cancellation.py --config CELL_CONFIG.json --plan REVIEWED_PLAN.json --output RECEIPT.json --live
```

This executes submission and cancellation. Do not invoke it before the reviewed
deployment, governed workload and credentials have been adopted. The command
requires the AWS CLI/Session Manager plugin, Docker buildx for immutable OCI
manifest reads, `gh` for native-image provenance, Python dependencies from
`e2e/requirements.txt`, Node and the installed locked SDK. No server/worker
source or SDK source is copied from a sibling checkout.

## Evidence and permissions

The producer validates provider responses and observer freshness/nonces itself;
it accepts no precomputed observation JSON or `verified: true` input. Missing,
stale, wrong-identity, partial, contradictory or oversized evidence blocks. The
native-only barrier budget is five minutes, with a stricter four-minute producer
budget. Output inventories must be empty at native entry, after cancellation
and at late readback. ECS also requires empty cleaned scratch inventories and
continuity of the original worker boot/PID namespace/start identity.

Grant the operator only reviewed exact-resource task/definition reads,
`ecs:ExecuteCommand` for the diagnostic container, and Batch job reads for its
queue/definition. Diagnostic tasks need the normal Exec control/data channels.
Restrict direct SSM sessions and retain Exec auditing. Normal deployment owners
must review EFS/mount and permission adoption; this source change applies none.
See the [ECS Exec requirements](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html)
and [Batch attempt fields](https://docs.aws.amazon.com/batch/latest/APIReference/API_AttemptDetail.html).

`Native GP cancellation producer controls` runs actual installed-SDK wire
controls plus producer refusal controls on source changes. These tests receive
no cloud credentials and never emit candidate qualification. Retain real run
receipts alongside the existing governed journey evidence only after both
required cells execute on the exact selected images and serving deployment.
