# e2e/

Cross-component & cross-cloud integration/parity harness — the *executable compatibility matrix*. Real
server + DB + SDKs installed from a staging registry, NO mocks at the seams; deployed via the actual IaC to
real cloud targets for parity.

- `scenarios/canonical-scenarios.md` — the scenario list (seeded from audit findings).
- Tiers: **local-docker** (per-PR, full SDK × scenario) → **cloud parity** (nightly, slim canonical set per deploy target).
- Deploy targets: local docker, AWS {serverless, ECS, EKS}, Azure {ACA, AKS, Functions} — axis-decomposed (see docs/TEST-STRATEGY.md).
- Build order: AWS-first (you have credits). OIDC (no static creds), ephemeral envs, teardown reaper, cost guardrails.

## Phase A — local-docker seam tier (implemented)

The cheap, per-PR tier: bring up the **real** honua-server + DB and run the canonical scenarios with no
mocks at the seam. Parameterized by `../platform-manifest.yaml`, so a run is the executable form of one
compatibility-matrix row.

```
e2e/
  Makefile                      # `make e2e` — the single entrypoint (humans + release train)
  run.py                        # orchestrator: compose up -> install SDKs -> run scenarios -> gate-report.json
  requirements.txt              # runner deps (PyYAML)
  local-docker/
    docker-compose.yml          # honua-server (manifest-pinned image) + PostGIS
    .env.example                # ports + SDK staging sources (npm/pip/nuget)
  runner/
    manifest.py                 # load platform-manifest + compat-matrix (server image + SDK pins)
    harness.py                  # compose lifecycle, healthz wait, metric scrape, SDK install, probe runner
    report.py                   # Result/Status + gate-report.json (mirrors the train's {gate,status,why,evidence})
  scenarios/
    geoservices_error_surfacing/   # IMPLEMENTED end-to-end (runnable; BLOCKED until real images)
      scenario.py                  #   force 200+{error}; assert every SDK raises; assert error metric increments
      probes/{probe.py, probe.mjs, dotnet/Probe.cs+csproj}   # one per-language probe, shared exit-code contract
    sync_no_duplicates/            # STUB
      scenario.py                  #   edit->sync->edit->sync->restart->sync => exactly ONE server feature
```

### Run it

```bash
cd e2e
make check        # static gates only (validate compose + compile scenarios) — no images needed
make e2e          # bring up the stack and run the seam scenarios
make e2e-strict   # E2E_REQUIRE_REAL=1: BLOCKED/SKIPPED => FAIL (the real release gate)
```

CI: `.github/workflows/e2e-local-docker.yml` runs on PRs touching `e2e/`/manifest, on `workflow_dispatch`,
and is `workflow_call`-able by the release train's `gate_e2e`.

### Redis-on and Redis-off are two topologies, not one broken one

honua-server composes the operation proposal store, its gateway and the admin executors only when Redis
is connected and entitled (`Program.cs:628`). On a Redis-off install the 20 projected
`honua_admin_layer_*`/`honua_admin_services_*` MCP tools are not registered (the full view is 104 tools,
not 124), Studio draft mutations answer the typed 409 "requires a Redis-backed durable store", and GP
submission answers 503 `capability-unavailable` (`missingDependency=redis`).

The cell declares its topology through `E2E_REDIS` (`on` by default; the cloud runner sets it from the
cell's Redis dimension) and the server must confirm it. `harness/lib/common.sh` `resolve_topology` asks,
in order: the manifest capability `operations.proposals` (added by honua-server S1), then
`GET /api/v1/admin/proposals` (200 vs the typed 503), then the manifest capability `jobs.runner`
(`dependency-unavailable`/`license-required`). Only a declared **and** confirmed Redis-off cell gets the
Redis-off expectation:

- **S2** compares the full catalog against `drivers/mcp/expected-tools.json` `fullCatalog.tools` (the
  canonical Redis-on roster) exactly, in both directions. On Redis-off the 20 names in
  `fullCatalog.requiresDurableControlPlane` must be all present (after S1) or all absent (before it);
  every other name stays exact. The evidence records `topology` and which state was observed.
- **S3** on Redis-off asserts the typed refusal at `create-draft` for every family (query, analysis, map)
  and passes with `topology: redis-off`; Redis-on drives the full lifecycle for the same families.
- A topology the server contradicts (a Redis-on cell with no durable control plane, or a Redis-off cell
  the server does not confirm) fails both scenarios.

Diagnostic: `drivers/mcp/probe_topology.sh` prints one JSON summary of a running candidate —
`topology`, `toolCount`, `missingFromRoster`, `extraOverRoster`, and `GET /api/v1/operations` — so the
same pinned digest booted with and without `docker-compose.no-redis.yml` can be compared directly
(acceptance: the catalogs differ by exactly the 20 names). It writes no scenario rows.

```bash
E2E_BASE=http://localhost:8080 E2E_API_KEY=honua-console-dev-key bash e2e/drivers/mcp/probe_topology.sh
```

## Phase B — cross-cloud parity tier (AWS-first, scaffolded)

The "also run cloud integration" layer: deploy a **real** honua-server to a cloud target via the actual
honua-iac, run the **canonical (slim) parity set** against its public endpoint, and assert it behaves
identically to the reference (local docker). Per `docs/TEST-STRATEGY.md`, this does NOT re-run the full
SDK × scenario matrix per target — it runs the small, data-independent canonical set and compares.

**Matrix: all 3 AWS targets × Redis on/off** — so the platform is proven to behave identically across
deploy shapes and with/without its cache:

| target | how | endpoint | Redis |
|---|---|---|---|
| `aws-serverless` | Lambda + API GW + AWS Batch geoprocessing (`examples/aws-serverless`, ECR Lambda-AOT image; Batch runs the generic server image) | `honua_url` output | `redis_enabled` |
| `aws-ecs` | Fargate + ALB (`examples/aws`, container image) | `honua_url` output | `redis_enabled` |
| `aws-eks` | k8s + Helm + LoadBalancer (`examples/aws-eks`) — heaviest, run least often | LB hostname (Helm) | Helm value |

```
e2e/
  canonical_checks.py     # the target-agnostic parity set (health, GeoServices 200+{error}, catalog,
                          #   live capability-manifest check honua-release#61) — HTTP-level, no SDK/Prom
  canary_probes.py        # the wider canary probe set (STAC/EDR/OData/OGC-Features/tiles/per-service
                          #   WMS-WMTS-WCS reachability, report-only geocoding latency; honua-release#61)
  expected-ga-manifest.json  # committed expected-GA capability id set the manifest check asserts against
  demo_canary.py          # scheduled entrypoint: canonical + canary probes against a live target
                          #   (default https://demo.honua.io); writes gate-report + a versioned
                          #   live-canary-evidence.json envelope for honua-evidence#8's join
  parity.py               # compare(reference, other): identical verdicts across targets, else FAIL
  run_cloud.py            # provision(target, redis) -> canonical + canary probes -> teardown -> parity -> gate-report-cloud.json
  targets/
    base.py               # DeployTarget contract (availability / provision(redis_enabled) / teardown)
    terraform_target.py   # config-driven terraform cells (serverless + ECS): apply image+redis var -> honua_url -> destroy
    aws_eks.py            # the heavy EKS cell (cluster + Helm + LB); needs kubectl/helm + the chart
  test_cloud.py           # unit tests: parity comparator, canonical normalisation (incl. capability-manifest), all 3 targets × redis BLOCKED-without-infra
  test_canary_probes.py   # unit tests: the canary probe set (pass/fail/blocked, incl. seeded-data honesty)
```

```bash
make cloud-aws                                  # aws-serverless / redis-off (BLOCKED until AWS infra is wired)
python e2e/run_cloud.py --target aws-ecs --redis on
```

CI: `.github/workflows/e2e-cloud-aws.yml` runs the **target × redis matrix** (6 cells, fail-fast off)
**nightly** + on `workflow_dispatch`, and is `workflow_call`-able by the release train's
`gate_cloud_parity`. OIDC into AWS (no static creds); every apply is ephemeral + run-scoped and
`teardown()` always runs.

Each cell is three jobs (`.github/workflows/e2e-cloud-aws-cell.yml`, honua-release#381):
`provision` (OIDC + AWS: apply, probes, database seed) → `journey` (`contents: read` only: the
seam drivers and the manifest-pinned clients via `run_live`) → `teardown` (OIDC + AWS, `if: always()`:
cost meter, destroy, cell verdict). An `admit` job (OIDC + AWS) seals the cell's random application key
to an RSA key the journey runner generated, and opens the ECS ALB / EKS Service to the journey runner's
own /32. Job outputs and step env are printed in the public log, so no credential travels that way. The
Terraform working directory reaches teardown as a sealed artifact (ciphertext only; the passphrase,
digest and application key are in Secrets Manager under `honua-cloud-cell-state/`, deleted by teardown).
Teardown adopts no verdict the journey job reported about itself: it re-checks every receipt (path,
digest, run/candidate binding, and the server identity provision observed before any client ran) and
copies only verified files into the cell's evidence.
`python e2e/run_cloud.py --phase <provision|journey|admit|teardown> ...` runs one phase; without
`--phase` all three run in one process.

GA cells for 2026.1 are `{aws-ecs, aws-serverless} × {redis-off, redis-on}`; `aws-eks` runs as
informational Preview. The mixed ECS + Batch cell is not in the rc.3 matrix: honua-iac has no
`examples/aws-mixed` root yet, so the cell could only report a missing root. Restore it in
`run_cloud.py` and `e2e-cloud-aws.yml` when honua-iac#209 lands.

`HONUA_ADMIN_PASSWORD` must be set before a Terraform cell provisions. The `provision` phase mints a
random one per cell; a local `--phase all` run must export its own. The harness never derives one from
the run id (the Actions run id is public). Teardown on a fresh runner passes a throwaway value, which
`terraform destroy` evaluates but never applies.

### The Lambda + Batch cell, and its migration before serving
`aws-serverless` is the 2026.1 Lambda + AWS Batch GA cell. When the pinned honua-iac root declares
them (honua-iac `feat/lambda-batch-ga-cell`), the cell applies `enable_gp_batch=true`,
`use_batch_service_linked_role=true` and `gp_batch_image=$HONUA_GP_BATCH_IMAGE`. The workflow sets that
image from the manifest: `components.honua-server.image`'s repository at `platformDigests.amd64`
(honua-iac refuses a tag for the Batch image). If the root declares `gp_batch_image` and the env is
unset, the cell is BLOCKED (FAIL under `--require-real`) instead of applying with the module's fallback.

The serverless root boots the Lambda with `skip_migrations=true`, so a fresh cell would serve an
unmigrated database. Between `terraform apply` and the first probe, `provision` therefore runs a
`migrate` step: `docker run` of `HONUA_MIGRATE_IMAGE` (the same generic server image) on the provision
runner with `HONUA_SKIP_MIGRATIONS=false`, the cell's own `ConnectionStrings__DefaultConnection` and
connection-encryption master key (read in memory from Terraform state, the way `seed_database` reads
the connection secret; passed to docker through its environment, never argv), the cell's admin password
and `Licensing__Mode=Disabled`. It polls `http://127.0.0.1:18080/healthz/ready` until 200, then removes
the container. The runner reaches RDS through the same /32 ingress the PostGIS bootstrap uses. A
container that fails to start, exits, or never reports Ready fails the cell with `migration failed:
<reason>` (log tail redacted) and teardown still destroys the cell. The provision report records the
step under `migration`. A server-side `HONUA_MIGRATE_ONLY` exit mode would replace the poll; it is
tracked for 2026.1.x.

### Bedrock on the genuine-model cell
`e2e-cloud-aws.yml` input `genuine_model_bedrock` (default off, so scheduled and ordinary runs stay
free of model charges) sets `HONUA_ENABLE_BEDROCK_AI=true` on `aws-ecs/redis-off` only, which applies
`enable_bedrock_ai=true` and `bedrock_ai_region=us-east-1`. A pinned root that does not declare those
inputs fails the cell rather than silently running without the model.

### Operation key-ring certificate on the Redis-on ECS and Lambda cells
With Redis connected outside Development/Test, honua-server enables the durable operation secret
channel and exits at startup unless `Operations:SecretChannel:KeyRingCertificatePath` names a
certificate. honua-iac#216 (in the pinned iac trunk) has `examples/aws` inject it from an
operator-owned secret and refuse a Redis plan without `operation_key_ring_certificate_secret_arn`.
`examples/aws-serverless` takes the same input from honua-iac#226: Lambda cannot resolve Secrets
Manager into env, so the function receives an `aws:secretsmanager:<arn>` reference that the server
resolves with the function role, and the module grants that role read on exactly the secret.
`SERVERLESS_SPEC` carries the same `redis_env_vars` mapping as `ECS_SPEC`. Until the iac pin
includes honua-iac#226 the serverless root declares neither variable, so the harness passes nothing
and the Redis-on Lambda cell still fails readiness for this reason.
**Owner step:** create a Secrets Manager secret in us-east-1 holding the certificate (base64 PKCS#12,
or JSON `{pkcs12,password}`, including the private key; format per the honua-iac aws-ecs module
README) that the ECS execution role and the Lambda function role can read under their permissions boundary, and set the repository
variable `HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN` (plus `HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN`
for a customer-managed key). The cell workflow exports both to the provision and teardown steps; the
harness passes them only to Redis-on ECS and Lambda cells whose pinned root declares them, and a declaring root
with the ARN unset refuses provisioning with this step named.

### ECS readiness diagnostics
Before destroying every `aws-ecs` cell, the teardown job runs `run_cloud.py --phase diagnose`. It
writes `diagnostics-ecs.json` into the cell's evidence (uploaded with the gate report) and prints the
same to the job log: every service task's `stopCode`, `stoppedReason` and container exit reasons
from `aws ecs describe-tasks`; the last 300 CloudWatch log lines of each stopped task's containers
(and the newest task's), read from the awslogs group/stream prefix in the task definition; and each
task definition's environment and secret variable NAMES, never values. The log group is destroyed
with the cell, so this is the only record of an exit cause. Credentials, connection strings and AWS
key ids are redacted from every line. The step is informational and never changes the verdict. The
journey job holds no AWS credential, so it cannot read these.

### Cells leave nothing billing — including what `terraform destroy` cannot delete
Teardown removing a resource is not the same as the resource stopping costing money. The EKS cell's
one case of that is the cluster's secret-encryption CMK: `terraform destroy` can only *schedule* a KMS
key for deletion, AWS's minimum window is **7 days** and cannot be shortened, so a key minted per cell
kept billing (~$1/key/month) for a week after its cluster was gone — two per full matrix dispatch,
growing with release-train cadence (honua-release#127).

The parity suite asserts nothing about secret-at-rest encryption (not `canonical_checks.py`, not
`canary_probes.py`, not `certification/`, not `compatibility-matrix.yaml`), so the cells were paying
for a property they never certified. `aws_eks.py` therefore applies the honua-iac aws-eks root with
`cluster_secret_encryption_enabled=false` and no key is created at all. Production keeps envelope
encryption: the iac default is `true`, and only this harness turns it off.

**If the cells ever need to certify secret encryption**, do not go back to a key per cell — that
recreates the drip. Create ONE long-lived CMK outside the harness and pass its ARN as the root's
`cluster_secret_encryption_key_arn` (leaving `cluster_secret_encryption_enabled=true`): the encryption
path is exercised on every cell at a fixed one-key cost, with nothing scheduled for deletion at teardown.

honua-iac is pinned **by sha** (`platform-manifest.yaml` → `components.honua-iac.sha`), and terraform
hard-errors on a `-var` the root does not declare, so the cell emits the flag only when the
checked-out root actually declares the variable (`AwsEksTarget._root_declares`). That keeps the
harness working against an older pin or an older local `HONUA_IAC_DIR` instead of failing every EKS
cell until the pin moves.

### A gate that can FAIL — and is honestly BLOCKED until infra exists
Each cell reports **BLOCKED** (never a fake green) until ALL prerequisites are wired, each a real
dependency: the AWS OIDC role (repo var `HONUA_AWS_ROLE_ARN`), a deployable image (`HONUA_LAMBDA_IMAGE_URI`
= ECR Lambda-AOT for serverless; `HONUA_ECS_IMAGE` for ECS/EKS), the honua-iac tree (`HONUA_IAC_DIR`), and
for EKS also the aws/kubectl/helm CLIs, the chart (`HONUA_HELM_DIR`) and the runner's own /32
(`HONUA_AWS_RUNNER_CIDR`, the only address its API server and load balancer are opened
to). `--require-real` (the train on a real cut / a
real nightly) promotes BLOCKED / a parity divergence to a hard FAIL. The verdict + parity logic is
unit-tested (`make test`) so the gate is trustworthy with zero cloud.

### What BLOCKED means — and what it does not (honua-release#128)
BLOCKED means **a probe had no input to work with**: no admin API key, no seeded service/tile id, no
cloud harness image. The missing thing is ours to supply and its absence says nothing about the
candidate, so it is reported and does not gate.

An **unreachable endpoint is not that**. The deployment is the subject of the test, so a probe that
cannot reach it has found a defect, and it FAILS — on every target, whatever `--require-real` says. A
cell that provisioned an endpoint which then never served is failed as one fact ("terraform
provisioned X but it never served") rather than as twenty identical timeouts.

This distinction was not free: the `aws-ecs` cells reported a passing verdict in every run they ever
had. Their ALB's security group defaults to VPC-only ingress unless `allow_http_ingress_cidrs` is set
(honua-iac `modules/aws-ecs`), so nothing from the GitHub runner ever reached them — every canonical
check and every reachability probe timed out, said `blocked`, and the cell summarised itself as
"canonical set passed". The cell now opens the ALB to the ephemeral runner's own /32 (the same address
the PostGIS bootstrap already uses, and nothing wider), and unreachability can no longer be mistaken
for a skip.

## Phase B.1 — scheduled demo canary (honua-release#61)

`.github/workflows/demo-canary.yml` runs `demo_canary.py` every 6 hours (+ `workflow_dispatch`) against
the always-on public demo (`https://demo.honua.io` by default) — a HYBRID-train evidence producer, not a
`release-train.yml` gate job (see [`docs/HYBRID-TRAIN.md`](../docs/HYBRID-TRAIN.md)). It runs the
canonical set + the full canary probe set (`canary_probes.run_canary`, with the demo's real
service/tile ids configured) and writes:

- `gate-report-demo-canary.json` — the human/machine-readable report (workflow step summary + the
  single tracking issue opened/updated on a genuine FAIL).
- `live-canary-evidence.json` — a versioned `honua-evidence.live-canary-envelope/v1` envelope (honua-evidence#9 producer contract) for
  honua-io/honua-evidence#8's capability-matrix join. The scheduled workflow commits each envelope into the evidence repo's live-canary landing zone.

```bash
python e2e/demo_canary.py --base https://demo.honua.io          # unauthenticated (default)
HONUA_DEMO_API_KEY=... python e2e/demo_canary.py --base https://demo.honua.io   # asserts available=true too
```

`geocoding-latency` is REPORT-ONLY (honua-server#2948 — geocoding is known-broken pending VPC egress) and
never fails the run. Every other check/probe can genuinely fail; key-gated probes (`metrics-gated`,
`admin-metrics-health`, `deploy-preflight`, and the manifest check's `available=true` assertion) report
BLOCKED — not FAIL — when `HONUA_DEMO_API_KEY` isn't configured. A demo that does not answer at all is
a FAIL, not a blocked run (honua-release#128) — an unreachable site is the loudest thing a canary can
find, and it used to be the quietest.

### Probe exit-code contract (every language probe)

`0` = PASS (expected behaviour, e.g. the SDK raised on a 200+`{error}`) · `1` = FAIL (the bug: success
returned) · `2` = SKIP (SDK/toolchain unavailable).

### A gate that can FAIL (AGENTS.md)

- The static gates (`make check`) and the Python import/compile of every scenario **always** run — a
  broken compose file or scenario makes the gate red even with no images.
- Server-dependent scenarios report **BLOCKED** (not PASS) while `platform-manifest.yaml` carries
  placeholder (`:TBD`) pins — we never fabricate a green.
- `E2E_REQUIRE_REAL=1` promotes **scenario-level** BLOCKED/SKIPPED to FAIL, so once real images + the
  `honua_geoservices_error_total` metric exist, the gate genuinely fails on a regression.
- **A candidate stack that never boots FAILS Slice-1 on every trigger** — PR included, `require_real`
  or not (honua-release#303). This is the local-docker twin of the unreachable-endpoint rule above:
  the candidate is the subject of the test, so "the image would not pull" or "the container exited
  before binding a port" is a finding, not a missing input of ours. `boot.sh` records the reason,
  the container exit code and the first (scrubbed, bounded) error lines in `out/boot.json`;
  `lib/report.sh` turns that into an `S0-stack-boot` FAIL row plus a job-summary block, instead of
  thirteen identical BLOCKED rows under a green check. The rule is fixture-tested by
  `harness/test_report.sh`, which `run_all.sh check` runs in the static tier (no images needed).

### Wiring left as TODO (blocked on real artifacts — search the tree for `TODO(#7)`)

- **Real images/pins:** populate `platform-manifest.yaml` server image + SDK versions (Phase 0/2); then
  the BLOCKED scenarios become live and the dotnet probe's `PackageReference` is added.
- **Staging registries:** point `HONUA_NPM_REGISTRY` / `HONUA_PIP_INDEX_URL` / `HONUA_NUGET_SOURCE` at
  the candidate's staging artifacts and implement the real install commands in `harness.install_sdks`.
- **Exact error trigger:** pin the endpoint+params that deterministically yield a 200+`{error}`, and the
  real SDK call surfaces in each probe (constructor / query method).
- **Server config:** confirm honua-server health path, metrics port, DB env keys in `docker-compose.yml`.
- **`sync_no_duplicates`:** implement via the honua-collect sync client once it is installable from staging.
