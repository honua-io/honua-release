# GP process releases: build once, promote by digest, deploy serialized, roll back the same way

Status: reference implementation for stage 4 of the GP developer-experience plan
(honua-release#613, honua-release#616). This is **follow-on work, not a 2026.1 RC gate**: nothing
here is in the release train's required set, and the existing platform-level rollback proof
(release#483, `rollback-certification.yml`) is unchanged.

The server binding/admission API is merged. Live deployment uses that API and the
release-bound job/artifact routes; its minimum server includes the GP HTTP integration and
credential-free artifact broker fixes. The checked-in `dev` environment remains **reference-only**.
No live AWS or installed-tool qualification is implied by fixture tests or reference receipts.

## The objects

| Object | Schema | Where | Who writes it |
|---|---|---|---|
| Release lock | `schemas/gp-process-release-lock.v1.schema.json` | run artifact, then `release/gp-process-locks/<process>/<12 hex>.json` | `honua-gp-devkit release` (reference mode: a committed fixture) |
| Environment state | `schemas/gp-environment-state.v1.schema.json` | `release/gp-environments/<env>/environment.json` | reviewed policy; versions/capabilities read from the live binding API |
| Environment pin | `schemas/gp-environment-pin.v1.schema.json` | `release/gp-environments/<env>/<process>.pin.json` | `gp-promote-process.yml`, merged by a reviewer |
| Deployment receipt | `schemas/gp-deployment-receipt.v1.schema.json` | run artifact, OIDC-attested | `gp-deploy-process.yml`, after a structured preflight decision |

A release is identified by the digest of its lock's canonical JSON (`tools/gp_process_release.py
lock-digest`). The lock names the source SHA, dependency-lock digest, runtime image digest,
workflow version, configuration references (references only, never values), targets with worker
image digests, artifact digests, and the validation receipts that passed. Promotion and deployment verify the packaged bytes. Custom workers still fetch the pinned source
and restore/build at execution time; the lock is not an independently enforced dependency closure.

## The flow

```
gp-release-process ──lock + artifacts + attestation──▶ run artifact
        │
gp-promote-process ──verifies attestation and digest──▶ PR: pin + committed lock bytes
        │  (human review, merge to trunk)
gp-deploy-process  ──live preflight ▶ verify IaC revision ▶ binding ▶ real job/output smoke ▶ receipt
        ▲
gp-rollback-process ──proves target == pin.previous──▶ same deploy path, operation=rollback
```

### 1. Release (`gp-release-process.yml`)

Dispatch with the process repository, the **full commit SHA**, and the descriptor path. The
workflow checks out that SHA, runs `honua-gp-devkit release`, validates the lock, verifies every
built artifact's bytes against the lock, uploads `out/release/` as
`gp-process-release-<12 hex of the digest>` and attests the lock with the workflow's OIDC
identity. The same inputs produce the same digest; a changed input produces a new release.

Reference mode: pass `lock_fixture` (a committed lock path such as
`tools/fixtures/gp-process-release/lock-v2.json`). The lock is validated, uploaded and attested
exactly as above; the summary says no process was built.

### 2. Promote (`gp-promote-process.yml`)

Dispatch with the environment, process, release run id, artifact name and the digest you expect.
The workflow downloads the artifact, verifies its attestation, refuses if the bytes digest to
anything else, copies the lock to `release/gp-process-locks/<process>/<12 hex>.json`, writes the
pin with `previous` = the verified live release and `expectedBindingVersion` = its current
native version, validates pin against
lock, and opens a pull request. The PR body carries the digests. Set the `RELEASE_GH_TOKEN`
secret so the PR gets its `validate` check (a PR opened with the default token triggers no
workflows).

Merging the PR is the approval. A reviewer checks that the digest in the PR is the digest the
release run attested, and that `previous` is the release they would want back.

### 3. Deploy (`gp-deploy-process.yml`)

Runs on push to `trunk` for every changed `*.pin.json`, or on dispatch for one environment and
process. One deployment at a time per environment (`concurrency`, never cancelled), so a queued
attempt waits for the preceding deployment.

Preflight (`tools/gp_process_release.py preflight`) refuses, with the reason in the receipt:

| Reason | Meaning |
|---|---|
| `LOCK_DIGEST_MISMATCH` | the committed lock is not the digest the pin names |
| `ARTIFACT_UNAVAILABLE:<name>` / `IMAGE_UNAVAILABLE:<digest>` | the registry or artifact store cannot serve the release |
| `INCOMPATIBLE_SERVER_CONTRACT` | the environment's server execution contract is outside the lock's range |
| `TARGET_UNSUPPORTED:<kind>` | the lock targets a backend this environment cannot execute |
| `STALE_BINDING_VERSION` | the environment moved since the pin was written: a concurrent attempt won |
| `ENVIRONMENT_MISMATCH` | pin and environment state disagree |

Live custom-code deployment supports `deploymentTooling.kind: aws-batch` (ADR-0063).
IaC must first provision the exact immutable revision and register its image/revision in the
server's custom-code workload configuration. The workflow **describes and verifies** this
revision; it never creates an image-only replacement that loses compute, role or retry settings.
It requires Fargate, no task IAM role, empty definition environment/secrets, and no
mounted/shared storage or additional registry/logging secret channels. These checks match
the maintained IaC custom-code pool, including its scratch-storage tiers; the native server
still supplies its job-scoped broker parameters at submission. The provider execution role
remains provider-owned. Built-in Helm worker deployment is a separate
path; a custom-code release with Kubernetes targets is refused before activation.

For a real environment, configure its GitHub Environment with `HONUA_GP_SERVER_URL` (HTTPS),
`HONUA_GP_AWS_REGION`, `HONUA_GP_VERIFICATION_ROLE_ARN` (OIDC role with Batch describe and ECR pull permissions when using private ECR),
and secret `HONUA_GP_ADMIN_API_KEY`. The runner uses the repository's Python client; no devkit
installation or unsupported `test --server` command is involved. Private ECR authentication is performed before image verification; other private registries
need separately configured runner access and are not qualified by this workflow. No credentials are stored in locks, pins or receipts.

The reviewed environment's `deploymentTooling.releases` maps each lock digest to
`jobDefinitionArn` and `smokeFixture` (a repository-relative JSON path). Keep both current and
previous registrations for rollback. Example smoke fixture:

```json
{
  "params": {"distance": 5},
  "declaredScope": [],
  "expectedArtifacts": [
    {"label": "summary.json", "sha256": "sha256:<64 hex digest of independently expected bytes>", "maxBytes": 4096}
  ]
}
```

Choose deterministic, non-destructive smoke inputs and independently calculate expected output
hashes; never copy the observed result into its own expectation. Each output is limited to 4 MiB
for this smoke. The client refuses another origin, redirects, missing/ambiguous artifacts,
wrong bytes, wrong release/source/binding version, failure and timeout. It retrieves bytes through
the same-origin authorized custom-artifact route, not by forwarding an admin token to storage.

Binding: the client reads live state, requires the reviewed predecessor and expected version,
publishes the lock, reads its exact bytes back, and activates with compare-and-set. It reads the
new digest/version/provider identity back before submitting a smoke job. This ordering is
necessary because new jobs resolve the active binding. A failed smoke means the **binding may
already be active**: the receipt reports `failed` with `binding.status: activated`. It does not
silently undo data or overwrite a concurrent promotion. Inspect the job, then explicitly roll
back through the same reviewed path. A lost activation response is resolved by exact version
readback, never by retrying with a newly observed version; unresolved outcomes remain `unknown`.

The smoke submits `POST /api/v1/admin/gp/process-releases/bindings/{process}/jobs` with a stable
run/attempt/binding idempotency key, polls the actual job, verifies diagnostic release identity,
and reads the expected artifact bytes. Terminal success alone is insufficient. On timeout the
job can still be running; the client reports failure and requires operator inspection.

Activation does not reconcile schedules. Receipts say `pending-orchestration-integration`, even
on success. Running and queued jobs keep the release they were admitted with; promotion affects
subsequent direct admissions. Whole-workflow/schedule pinning remains separate.

Receipt: attempted even after failure (`if: always()`), validated against its schema, uploaded as
`gp-deployment-receipt-<env>-<process>-<run>-<attempt>` and attested. A refused attempt's receipt
lists its reasons; a failed smoke is `failed`; `activated` is reserved for a binding the run
actually changed. Receipt generation requires a structured preflight decision. Runner setup,
invalid input, or registry/artifact-index failures before that decision can leave only workflow
logs and available error artifacts; these paths are not qualified as signed deployment receipts.

### 4. Roll back (`gp-rollback-process.yml`)

Dispatch with the environment and process. The plan job proves the target is the pin's
`previous` release (or refuses: `ROLLBACK_TARGET_MISSING` for a first release,
`ROLLBACK_TARGET_MISMATCH` for any other lock), then calls the deploy workflow with
`operation: rollback`. The deploy path repeats preflight against resolved availability, so a
previous release whose artifacts are gone or whose server contract the environment has outgrown
is refused, not attempted. A rollback does not undo committed data changes and does not change
the release of jobs already admitted.

Rollback additionally requires the live current and previous digests to match the reviewed pin,
then uses the freshly read live version for compare-and-set. After a successful rollback, promote
again from the observed live state. Promotion accepts the reviewed previous release only
when the native reverse lineage points back to the reviewed forward release, and records
that live predecessor/version in the next pin PR. Unknown lineage is refused; later concurrent
changes still fail deployment CAS. Server binding history records the activation.

## Reference environment

`release/gp-environments/dev/` is a reference environment (`deploymentTooling.kind: reference`)
pinning the sample process `buffer-features` to the fixture release `lock-v2` with `lock-v1`
as its previous release. Merging a change to its pin runs the whole deploy path on trunk and
produces an attested `reference-only` receipt. It is for proving the workflows, not for serving.

## Validating locally

```bash
python -m pytest tools/test_gp_process_release.py -q          # every refusal fires; workflow contracts hold
python3 tools/gp_process_release.py validate-lock tools/fixtures/gp-process-release/lock-v2.json
python3 tools/gp_process_release.py validate-pin release/gp-environments/dev/buffer-features.pin.json --locks-root .
python3 tools/gp_process_release.py preflight \
  --lock tools/fixtures/gp-process-release/lock-v2.json \
  --pin tools/fixtures/gp-process-release/pin-dev-v2.json \
  --environment tools/fixtures/gp-process-release/environment-dev.json \
  --artifact-index tools/fixtures/gp-process-release/artifact-index.json --out /tmp/decision.json
```

## What remains for #616

- Replace the binding seam with the honua-server#6221 API call and read the live binding version
  and server contract instead of `environment.json`.
- Run the v1 → v2 → rollback certification scenario with queued and running jobs once admission
  pinning exists, and prove from receipts and job diagnostics which release each job executed.
- Exercise the `helm` and `aws-batch` tooling kinds against a real environment and record the
  honua-helm and honua-iac touch points as executed, not described.
