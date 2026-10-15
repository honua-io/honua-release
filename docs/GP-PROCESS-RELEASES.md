# GP process releases: build once, promote by digest, deploy serialized, roll back the same way

Status: reference implementation for stage 4 of the GP developer-experience plan
(honua-release#613, honua-release#616). This is **follow-on work, not a 2026.1 RC gate**: nothing
here is in the release train's required set, and the existing platform-level rollback proof
(release#483, `rollback-certification.yml`) is unchanged.

The server half (`honua-gp.yaml`, `honua-gp-devkit release`, the environment binding API and
admission-time pinning) is honua-io/honua-server#6221. Until it ships, the workflows below run
against the same contract in **reference mode**; every seam that depends on the server is marked
in the workflow and in the receipt, and nothing pretends to have changed a binding it did not.

## The objects

| Object | Schema | Where | Who writes it |
|---|---|---|---|
| Release lock | `schemas/gp-process-release-lock.v1.schema.json` | run artifact, then `release/gp-process-locks/<process>/<12 hex>.json` | `honua-gp-devkit release` (reference mode: a committed fixture) |
| Environment state | `schemas/gp-environment-state.v1.schema.json` | `release/gp-environments/<env>/environment.json` | reviewed by hand; later reconciled against the live binding API |
| Environment pin | `schemas/gp-environment-pin.v1.schema.json` | `release/gp-environments/<env>/<process>.pin.json` | `gp-promote-process.yml`, merged by a reviewer |
| Deployment receipt | `schemas/gp-deployment-receipt.v1.schema.json` | run artifact, OIDC-attested | `gp-deploy-process.yml`, on every attempt |

A release is identified by the digest of its lock's canonical JSON (`tools/gp_process_release.py
lock-digest`). The lock names the source SHA, dependency-lock digest, runtime image digest,
workflow version, configuration references (references only, never values), targets with worker
image digests, artifact digests, and the validation receipts that passed. Nothing downstream
rebuilds: promotion and deployment copy and verify bytes.

## The flow

```
gp-release-process ──lock + artifacts + attestation──▶ run artifact
        │
gp-promote-process ──verifies attestation and digest──▶ PR: pin + committed lock bytes
        │  (human review, merge to trunk)
gp-deploy-process  ──preflight ▶ tooling ▶ smoke ▶ binding ▶ receipt (always)
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
pin with `previous` = the pin currently merged (the rollback target) and
`expectedBindingVersion` = the environment's recorded binding version, validates pin against
lock, and opens a pull request. The PR body carries the digests. Set the `RELEASE_GH_TOKEN`
secret so the PR gets its `validate` check (a PR opened with the default token triggers no
workflows).

Merging the PR is the approval. A reviewer checks that the digest in the PR is the digest the
release run attested, and that `previous` is the release they would want back.

### 3. Deploy (`gp-deploy-process.yml`)

Runs on push to `trunk` for every changed `*.pin.json`, or on dispatch for one environment and
process. One deployment at a time per environment (`concurrency`, never cancelled), so a queued
attempt waits and still writes its receipt.

Preflight (`tools/gp_process_release.py preflight`) refuses, with the reason in the receipt:

| Reason | Meaning |
|---|---|
| `LOCK_DIGEST_MISMATCH` | the committed lock is not the digest the pin names |
| `ARTIFACT_UNAVAILABLE:<name>` / `IMAGE_UNAVAILABLE:<digest>` | the registry or artifact store cannot serve the release |
| `INCOMPATIBLE_SERVER_CONTRACT` | the environment's server execution contract is outside the lock's range |
| `TARGET_UNSUPPORTED:<kind>` | the lock targets a backend this environment cannot execute |
| `STALE_BINDING_VERSION` | the environment moved since the pin was written: a concurrent attempt won |
| `ENVIRONMENT_MISMATCH` | pin and environment state disagree |

Then, by `deploymentTooling.kind` in the environment state:

- `helm`: `helm upgrade --install` of the pinned chart version with the worker image **by
  digest** (`geoprocessing.kubernetesJobs.image.digest`, honua-helm#100 refuses a native profile
  without an explicit worker image).
- `aws-batch`: `aws batch register-job-definition` with the pinned image, producing a new
  immutable revision tagged with the lock digest. honua-iac owns the compute environment, role
  and retry settings; the deployment never edits them.
- `reference`: no tooling runs; the committed `dev` environment is this kind.

Smoke: `honua-gp-devkit test <process> --server $HONUA_GP_SERVER_URL`, recorded pass/fail; the
reference environment records `skipped` with that reason.

Binding: **pending honua-server#6221**. The step is the seam where the binding API call goes; the
receipt records `binding.status = pending-server-6221` and `outcome = reference-only` until then.
When the API exists: activate only after the worker path is ready, under the pin's
`expectedBindingVersion` (the server refuses a stale version), then reconcile schedules
explicitly. Running and queued jobs keep the release they were admitted with; a new binding
affects subsequent admissions only.

Receipt: written on every path (`if: always()`), validated against its schema, uploaded as
`gp-deployment-receipt-<env>-<process>-<run>-<attempt>` and attested. A refused attempt's receipt
lists its reasons; a failed smoke is `failed`; `activated` is reserved for a binding the run
actually changed.

### 4. Roll back (`gp-rollback-process.yml`)

Dispatch with the environment and process. The plan job proves the target is the pin's
`previous` release (or refuses: `ROLLBACK_TARGET_MISSING` for a first release,
`ROLLBACK_TARGET_MISMATCH` for any other lock), then calls the deploy workflow with
`operation: rollback`. The deploy path repeats preflight against resolved availability, so a
previous release whose artifacts are gone or whose server contract the environment has outgrown
is refused, not attempted. A rollback does not undo committed data changes and does not change
the release of jobs already admitted.

After a successful rollback, promote again to record the new `previous`; the pin is the history.

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
