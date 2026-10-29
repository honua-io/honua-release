# e2e/

Cross-component & cross-cloud integration/parity harness — the *executable compatibility matrix*. Real
server + DB + SDKs installed from a staging registry, NO mocks at the seams; deployed via the actual IaC to
real cloud targets for parity.

- `scenarios/canonical-scenarios.md` — the scenario list (seeded from audit findings).
- Tiers: **local-docker** (per-PR, full SDK × scenario) → **cloud parity** (canonical checks and live journeys, plus the full installed-client suite on the Redis-on GA cells).
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
    gp_batch_native/               # judge only; a missing native receipt stays blocked (honua-release#85)
      scenario.py                  #   pass only for a native GDAL-worker OGC job; busybox, managed geometry, and Redis-off fail
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
- **S10** (`large-import`) on Redis-off sends a `forceBackground` `upload-url` import of the small
  fixture. The server runs file imports in-process without Redis, so a 202 job is followed to
  completion, published and counted (`executor: in-process`, `topology: redis-off`); the typed 503
  passes only from a host without the import job service. Redis-on follows the large fixture's
  background job the same way (see "S10 large-import" below).
- **S9** (`top-demo`) `S9-demos-geoprocessing` on Redis-off drives the demo's execution and passes with `topology: redis-off` only when the server returns S5's typed 503 capability-unavailable (`missingDependency=redis`) and the page renders it as its job-store-unavailable state (a page that spins on the refusal is `blocked`, naming the site); Redis-on still requires a completed live job.
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
identically to the reference (local docker). The canonical parity checks are supplemented by live
journeys. ECS/Redis-on and Lambda+Batch/Redis-on also execute the complete published installed-client
matrix against the deployed endpoint, using trusted fixture preparation and the encrypted application
handoff. Redis-off retains serving and typed-refusal checks; it does not claim full SDK workflow coverage.
Missing prerequisites and matrix blockers remain non-passes. See
[`certification/installed-clients/README.md`](../certification/installed-clients/README.md#existing-aws-endpoint).

**Matrix: all 3 AWS targets × Redis on/off** — so the platform is proven to behave identically across
deploy shapes and with/without its cache:

| target | how | endpoint | Redis |
|---|---|---|---|
| `aws-serverless` | Lambda + API GW + AWS Batch geoprocessing (`examples/aws-serverless`, ECR Lambda-AOT image; Batch runs the generic server image) | `honua_url` output | `redis_enabled` |
| `aws-ecs` | Fargate + ALB (`examples/aws`, container image) | `honua_url` output | `redis_enabled` |
| `aws-eks` | EKS + the Helm chart + Service LoadBalancer (`examples/aws-eks`; RDS PostGIS, ElastiCache and GP as Kubernetes Jobs once the pins carry them) | per-run HTTPS name, else LB hostname | `enable_redis` (ElastiCache), else the chart's Redis |

```
e2e/
  canonical_checks.py     # the target-agnostic parity set (health, GeoServices 200+{error}, catalog,
                          #   live capability-manifest check honua-release#61) — HTTP-level, no SDK/Prom
  canary_probes.py        # the wider canary probe set (STAC/EDR/OData/OGC-Features/tiles/per-service
                          #   WMS-WMTS-WCS reachability, report-only geocoding latency; honua-release#61)
  expected-ga-manifest.json  # reviewed expected-GA POLICY (ids that must be GA) the manifest check asserts
  expected-ga-observation.json  # GENERATED per candidate by tools/expected_ga_snapshot.py from a boot of
                          #   its server; bound to the server sha + index/child digests, refused otherwise
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
live cost estimate, destroy, cell verdict). An `admit` job (OIDC + AWS) seals the cell's random application key
to an RSA key the journey runner generated, and opens the ECS ALB / EKS Service to the journey runner's
own /32. Job outputs and step env are printed in the public log, so no credential travels that way. The
Terraform working directory reaches teardown as a sealed artifact (ciphertext only; the passphrase,
digest and application key are in Secrets Manager under `honua-cloud-cell-state/`, deleted by teardown).
Teardown adopts no verdict the journey job reported about itself: it re-checks every receipt (path,
digest, run/candidate binding, and the server identity provision observed before any client ran) and
copies only verified files into the cell's evidence.

A cell verdict is `pass`, `blocked` or `fail`. Outside `--require-real`, a cell whose only gaps are
tracked, documented limitations is `blocked` (never `pass`) and its report names each blocker: a
journey whose every attempt is a BLOCKED receipt naming its issues (the stages' `blockedBy`, or the
honua-release#377 cloud-kind gap; `journeyBlockedBy`), a scenario BLOCKED on a named issue (for
example `top-demo` on honua-release#450), or a missing run cost meter reading (`cost.status:
unavailable`, written into the report with the path it looked for). A documented journey block is not
retried. An attempt whose driver raised, a failed stage or check, a blocked stage that names no issue,
or a cost reading that is over the ceiling, stale or bound to another run is still `fail`.
`--require-real` (the per-RC strict mode) turns every one of those blocks into `fail`, and the
full-scope aggregate certifies only cells that `pass`.

A second journey attempt is a real second try (owner decision 7 of 2026-10-10). The journey's stage 3
and 4 objects have fixed authored names: the `journey_source` secure connection and the
`journey-solid-red` style. Before this change, attempt 2 on every cell failed at
`CreateConnectionAsync` because attempt 1's connection still existed. The strategy is
delete-and-recreate. Before attempt N > 1, `cloud_journey.reset_prior_attempt` uses the cell's admin
key (sent only where the driver's credential rule would send it, never on a redirect) to delete
attempt N-1's connection by name and its style through the cell admin API. Attempt N then creates both
again through the journey's own SDK and CLI calls. The upload needs no reset (`OverwriteExisting=true`),
and Studio objects are keyed by the attempt's own workspace. Each retried receipt carries a
`Journey retry strategy: delete-and-recreate.` notice with every outcome. A deletion the cell refuses
(409 when attempt N-1 had already published a service on the connection) or a listing that fails is
reported there, never hidden.
`python e2e/run_cloud.py --phase <provision|journey|admit|teardown> ...` runs one phase; without
`--phase` all three run in one process.

GA cells for 2026.1 are `{aws-ecs, aws-serverless} × {redis-off, redis-on}` under the October 16
owner amendment, superseding decisions 12/18. EKS is informational Preview: its raw failures remain
visible and cannot replace required ECS/Lambda proofs. The
mixed ECS + Batch cell is not in the rc.3 matrix: honua-iac has no
`examples/aws-mixed` root yet, so the cell could only report a missing root. Restore it in
`run_cloud.py` and `e2e-cloud-aws.yml` when honua-iac#209 lands.

`HONUA_ADMIN_PASSWORD` must be set before a Terraform cell provisions. The `provision` phase mints a
random one per cell; a local `--phase all` run must export its own. The harness never derives one from
the run id (the Actions run id is public). Teardown on a fresh runner passes a throwaway value, which
`terraform destroy` evaluates but never applies.

### The Lambda + Batch cell, and its migration before serving
`aws-serverless` is the 2026.1 Lambda + AWS Batch cell. It is informational Preview under the
2026-10-25 founder re-tier; the two `aws-ecs` cells are the required GA cells. When the pinned honua-iac root declares
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

The cell stays closed until that migration is Ready (honua-server#6750: in run 40746787170 an internet
scanner cold-started the API Lambda before the migration finished, and the instance answered `503
schema-floor` until teardown). When the pinned serverless root declares `serving_closed` (honua-iac
v0.4.2+), `provision` applies with `serving_closed=true` (reserved concurrency 0 on the API and
control-plane event Lambdas), `migrate` runs, and only after Ready a second apply with the same inputs
and `serving_closed=false` opens the cell (`migration.servingOpened` in the report). A failed migration
leaves it closed for teardown. An older pin without the input applies once, open, as before.

### The EKS cell (bring-your-own Kubernetes, honua-release#203)
`aws-eks` is an informational 2026.1 Preview cell (October 16 amendment supersedes decisions 12/18; GP on Kubernetes is
honua-server#4719 and honua-helm#77). It runs the same provision → admit → journey → teardown chain as
the ECS and Lambda cells. Provision applies `examples/aws-eks`, installs the manifest-pinned honua-helm
chart with the manifest-pinned image by digest, and probes the Service's load balancer. Admit adds the
journey runner's /32 to the cluster API and to the Service's `loadBalancerSourceRanges`. The journey
job runs the seam drivers (S1/S2 MCP, S3 Studio, S5 GP, S9 demos) and the imported terminal journey
with target kind `aws-eks` (receipt evidence `live-aws-eks`, topology `eks-service-lb-pods`). The
teardown job decides the cell's verdict. Stage 1's candidate-image check reads the digest every server
pod of the release is running (`AwsEksTarget.observed_image`).

`AwsEksTarget` passes each root input only when the pinned root declares it, like the TfTargetSpec
cells, so the cell keeps working while the iac pin moves:

| input (when declared) | value |
|---|---|
| `enable_redis` (or `redis_enabled`) | the cell's Redis dimension; ElastiCache replaces the chart's Redis |
| `operation_key_ring_certificate_secret_arn` (+ `_kms_key_arn`) | Redis-on only, from `HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN`; a declaring root with it unset refuses the cell (never the destroy) |
| `audit_chain_key_secret_arn` (+ `_kms_key_arn`) | every cell, from `HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN`; never refused when unset |
| `enable_postgis=true`, `rds_deletion_protection=false` | RDS PostgreSQL with PostGIS instead of the in-cluster fixture |
| `db_publicly_accessible=true`, `db_additional_ingress_cidrs=[runner /32]` | only with RDS: the PostGIS bootstrap and the fixture seed reach it from the runner, as on ECS |
| `operations_policy_rules` | the cells' shared policy (`OPERATIONS_POLICY_RULES_VAR`, honua-release#517) |
| `licensing_mode=Disabled`, `cors_allowed_origins=["http://127.0.0.1:18099"]` | as on ECS |
| `domain_name`, `route53_zone_id` | the per-run `<run id>-aws-eks-redis-<on/off>.cert.<HONUA_AWS_CELL_DNS_PARENT>` in `HONUA_AWS_CELL_DNS_ZONE_ID`, the ECS cells' variables |
| `cluster_secret_encryption_enabled=false` | no per-cell KMS key (honua-release#127) |
| `kubernetes_namespace=honua-cert`, `server_service_account_name=honua`, `gp_job_service_account_name=honua-gp-job` | where the root's Pod Identity associations bind the server and GP job roles: the namespace and service accounts the chart is installed with (honua-iac#233) |

The chart install reads these root outputs, each only when the root declares it (`terraform output
-json`, held in memory):

- `db_connection_string` and `redis_connection_string` (sensitive) go into the cell's runtime Secret.
- `chart_config_env` is a map of plain environment names to non-secret values, for example
  `ControlPlane__Kubernetes__*`, the operation policy rules and Secrets Manager references. It goes
  into the chart's `config.env` through a values file.
- `server_service_account_annotations` and `gp_job_service_account_annotations` are the only
  annotations put on the server and GP job ServiceAccounts. Under `workload_identity_mode=pod_identity`
  (the root's default) they are empty, and a role-arn annotation there refuses the cell: the AWS SDK
  would try web identity first, and the role trusts no OIDC provider. Under `irsa` they must carry
  `eks.amazonaws.com/role-arn` for `server_role_arn` / `gp_job_role_arn`. The root's
  `kubernetes_namespace` and service account outputs must match what the chart is installed with.
- `certificate_arn` terminates TLS on the load balancer for the per-run name.

When the pinned chart declares `geoprocessing.kubernetesJobs`, the cell enables it in the cell
namespace with `HONUA_GP_BATCH_IMAGE`, the manifest's generic server image (the same image the Lambda
cell's Batch jobs run). A chart that declares it while that variable is unset is BLOCKED. The cluster,
not terraform, creates the load balancer, so the harness points the per-run name at it with its own
Route53 CNAME and deletes that record before the destroy. The certificate and its validation records
belong to the root. A root that does not declare `domain_name` keeps the plain-HTTP load balancer
endpoint, and the provision log says so. Setting one DNS variable without the other refuses, as on ECS.

A root without RDS keeps the legacy shape (in-cluster PostGIS without TLS, the chart's Redis), and the
provision log names it. That shape cannot pass the journey's TLS datasource stage, and the red verdict
is the honest one. The cell moves forward when the iac and helm pins do:

- `examples/aws-eks` with `enable_redis` + ElastiCache, the key-ring and audit-chain secrets, RDS with
  `enable_postgis`, `operations_policy_rules`, `domain_name`/`route53_zone_id` and the outputs above;
- a chart with `geoprocessing.kubernetesJobs.*`, its Job ServiceAccount/RBAC and the Redis wiring.

Any IAM the root creates must stay inside the release role's guardrail. Roles and policies must be
named `honuaeks*` (the cell's `name_prefix`). The root must not create an IAM OIDC provider:
`release-cicd-guardrails` denies that account-wide, and run 38057015781 failed provisioning on it
(see below). The root therefore defaults to EKS Pod Identity (honua-iac#233).

**Teardown.** The LoadBalancer Services, the release, the namespace and the cell's DNS record go
first. Then `terraform destroy` runs, retried once after sweeping the node ENIs that the VPC CNI leaks.
A completed destroy is then verified read-only, and any leftover or unreadable listing fails the cell:

- the cluster must answer `ResourceNotFoundException`;
- no VPC may carry the cell's Name tag;
- no ENI may carry the cluster's `cluster.k8s.amazonaws.com/name` tag or sit in the cell's VPC;
- no classic or v2 load balancer may sit in the cell's VPC;
- the cell's certificate and records must be gone. This is the ECS cells' check, and other
  `*.cert.<parent>` names are only a warning.

**Run 38057015781** (2026-10-10): both EKS provision jobs were green, but provisioning had failed.
`terraform apply` of `examples/aws-eks` was refused `iam:CreateOpenIDConnectProvider` (an explicit
deny, `DenyAccountControls` in `release-cicd-guardrails`) and `iam:CreateRole` for
`default-eks-node-group-*` (outside the `honuaeks*` IAM namespace). The provision handoff carried no
endpoint, so admit and the journey were skipped. Each teardown job destroyed what had been created and
then reported the recorded provision failure, which is what made teardown red. Nothing was stranded:
afterwards no EKS cluster, `honuaeks*` VPC or `default-eks-node-group-*` role remained. Both refusals
need fixes in the iac root (a node group role name under the cell prefix, and no OIDC provider), not
in this harness.

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
README) that the ECS execution role and the Lambda function role can read under their permissions boundary
(the release-cell boundary admits it only when honua-iac `bootstrap/aws-release-cells` lists it in
`runtime_operation_key_ring_certificate_secret_arns`), and set the repository
variable `HONUA_AWS_OPERATION_KEY_RING_SECRET_ARN` (plus `HONUA_AWS_OPERATION_KEY_RING_SECRET_KMS_KEY_ARN`
for a customer-managed key). The cell workflow exports both to the provision and teardown steps; the
harness passes them only to Redis-on ECS and Lambda cells whose pinned root declares them, and a declaring root
with the ARN unset refuses provisioning with this step named.

### Production operation policy on the ECS and Lambda cells
Every cloud cell runs the server image's default environment, Production (the iac modules set no
`ASPNETCORE_ENVIRONMENT`), whereas every local journey and e2e stack runs Development, where the typed
operation policy is inert. Production enables `Operations:Policy` with a fail-closed `Deny` default
(honua-server `src/Honua.Server/appsettings.Production.json`; the server refuses to boot in Production
without it), so the first cloud journey to reach stage 3 (run 38066103745, aws-serverless redis-on)
recorded `service.publish ... status=Denied; policyOutcome=Deny; message="Operations require an
explicit production policy rule."`. The harness therefore passes `operations_policy_rules`
(`CELL_OPERATION_POLICY_RULES` in `targets/terraform_target.py`) to both cells and both Redis modes:
two `Allow` rules on every operation, one for role `scoped-admin-key` (the journey operator and
approver keys) and one for role `admin` (the bootstrap admin and full-admin keys). The proposer
(`layer-write-key`) and viewer (`scoped-api-key`) get no rule and the default stays `Deny`, so the
cell remains an honest Production topology; the journey's governance proof (stages 7-8, the Studio
proposal and approval path) does not ride on these rules. The variable is passed only when the pinned
honua-iac root declares it, so the rules take effect once the iac pin carries `operations_policy_rules`;
until then nothing is passed and the receipt keeps recording the Deny.

Both cells also pass `studio_end_user_authorization=true` when the pinned root declares it. The server
admits non-admin principals to the Studio lifecycle API only with `Studio:EndUserAuthorization:Enabled`.
The local journey compose sets it, but the cells did not, so stage 6 on run 38091905679
(aws-ecs/redis-on) failed with a 403 for the proposer on `GET /api/v1/studio/package-families`.
The EKS cells set `Studio__EndUserAuthorization__Enabled=true` in the chart's `config.env` directly
(the EKS root has no such input); without it run 40742242038 hit the same 403 on both EKS cells.

An EKS cell advertises `deploymentRevisionSource=image-digest`: the honua-helm chart's release-info
ConfigMap sets `HONUA_IMAGE_DIGEST` from `image.digest`, and the server prefers it over the baked
commit. Stage 1's `1.3-candidate-identity` accepts an image-digest identity only when it is the
manifest's index digest or one of its `platformDigests`, and the receipt's `server.sourceSha` binds
the manifest sha only when the pods' running digest (read from the kubelet) agrees with it.

A journey retry imports its fixture into its own table (`journey_source_attempt<N>`): attempt 1's
published layer keeps the authored table, so re-importing into it made `service.publish` refuse the
retry at its layer-conflict check (run 40742242038, attempt 2, stage 3). The retry also publishes
to its own Studio route (`/journey-map-attempt<N>`, read back at the same published path): attempt
1's approved publication governs `/journey-map` for the life of the cell and no admin call releases
it, so a retry that re-proposed it failed stage 7 with "Route '/journey-map' is already claimed by
another Studio publication" (run 40947860102, aws-eks/redis-on, attempt 2). The genuine-model canary
runs on the same cell after the deterministic attempts, so its model attempt k uses attempt number
2 + k for its connection, table and route (`cloud_target.scope_execution`).

A run-signed bearer proposer (the EKS tenant fixture) is reported in the proposal's `requestedBy` as
the server's canonical actor, `<scheme>:subject:<issuer>:<subject>` with both parts percent-encoded.
Stage 8 accepts that form (`executor.PRINCIPAL`); an API-key proposer is `apikey:api-key:<guid>` or a
bare key id. Run 40947860102 failed `8.execution` on the percent-encoded issuer before this.

### Candidate image and two tasks on the Redis-on ECS cell
Stage 1's `1.1-candidate-image` on an ECS cell is satisfied by the image ECS reports running, read in
the provision phase through `cloud_journey.observed_ecs_image`. The Lambda cells already worked this
way (#512). The ECS cell deploys `repository@digest` with no tag. The match is therefore by
repository, and the reported digest must be the manifest's index digest or its child for the cell
architecture (`platformDigests.amd64` for `x86_64`). Matching the manifest's tagged `image` found no
server container on run 38110106995, so the check stayed blocked.
With two tasks, a task still starting is waited for, up to 12 reads 10 s apart. After that, only
RUNNING tasks count, and every one of them must report the candidate. If one does not, the check's
detail names the task, container and digest that disagree, or names the task states when no task is
RUNNING. On run 38119871941 the two-task Redis-on cell showed only "the pinned candidate stack was
not brought up".

The Redis-on ECS cell runs two tasks, so stage 6's `6.replica-map` can observe two `Honua-Instance`
ids; on one task, run 38110106995 recorded "topology served one instance". honua-iac allows more than
one task only in the MultiNode topology (Redis, plus AwsS3 file storage in an existing bucket). With
the repository variable `HONUA_AWS_CELL_FILE_STORAGE_BUCKET` set, and a pinned `examples/aws` root
that declares the inputs, the harness passes:
- `desired_count=2` and `max_capacity=2`;
- `deployment_mode=MultiNode` and `file_storage_provider=AwsS3`;
- the bucket name, with a per-cell key prefix `release-cells/<name prefix>`.

The Redis-off ECS cell stays one task. It is the serving-only topology, and MultiNode requires Redis.
Without the variable, or on an older root, the Redis-on cell also stays one task and `6.replica-map`
stays blocked on the topology. **Cost:** the live estimate prices Fargate vCPU and GB per task from
the state's `desired_count`, so the Redis-on ECS cell's Fargate line roughly doubles. Objects the
server writes to the bucket are not counted, like other S3 usage; the S10 fixture the harness stages
there is counted (see "Cost evidence").

The Redis-on EKS cell runs two chart replicas the same way (run 40832572972 recorded one instance on
one replica). The pinned chart refuses `replicaCount > 1` unless `config.env.Deployment__Mode` is
MultiNode, so with the same bucket variable, a root that declares `gp_job_data_bucket_arn` and
`gp_job_data_bucket_enabled` (the server's workload role then may use the bucket) and a chart that
declares `replicaCount`, the harness renders `replicaCount=2`, `Deployment__Mode=MultiNode` and the
`FileStorage__AwsS3__*` keys with the same per-cell key prefix. The pinned root renders no pool sizing
for EKS, so the harness sets `Limits__Connections__*` to 10 (two pools per process, three processes
in the worst case, inside db.t3.micro's connections); a root that declares `server_max_replicas` gets
`server_max_replicas=2` instead and sizes them itself. **Cost:** the replicas fit the two nodes the
root already runs, so the EKS line does not change.

The cloud fixture seed (`harness/cloud/seed.py`) publishes the demo and Studio fixtures through the
admin API from the provision runner. If a call fails, the `studio-authoring-seed` and `top-demo-seed`
rows say which call failed, its status, which `Honua-Instance` answered it, a bounded
credential-masked excerpt of the body, and which instances answered the earlier calls. Before this,
run 38128300695 recorded only `HTTPError` on the two-task cell.

The likely cause, from reading the server code (not yet observed on a run): honua-server caches the
metadata graph per process with a 10 s TTL (`MetadataV2GraphSnapshotCache`), and a write invalidates
it only on the node that wrote. The cache's own comments say other nodes see the write only when
their TTL expires. A `PUT /api/v1/admin/services/{service}/access-policy` that lands on the other
task within 10 s of the layer publish therefore answers `404 "Service '<name>' not found."`. That is
a server MultiNode defect: it needs cross-node invalidation of the snapshot. The harness does not
retry around it.

**Owner step:**
1. Create an S3 bucket in us-east-1 for cell file storage, with a lifecycle rule that expires objects
   under `release-cells/` after a day. `terraform destroy` does not empty a bucket the cell does not
   own.
2. Admit it in the release-cell permissions boundary. The aws-ecs module grants the task role
   `s3:ListBucket` on the bucket and object access on `bucket/*`.
3. Set the repository variable `HONUA_AWS_CELL_FILE_STORAGE_BUCKET`. The cell workflow exports it to
   the provision and teardown steps.

### S10 large-import: a background import from the cell's S3 bucket (honua-release#548)
Owner decision 24 (2026-10-11), fix unit J19. The journey's stage 3 proves only the small synchronous
multipart upload; every committed cloud receipt records `jobId: null`. S10 proves the other path on
every GA cell, as the `large-import` row of `scenarioCoverage` (`e2e/large_import.py`):

| cell | fixture | request | passes when |
|---|---|---|---|
| Redis-on | `s10-large-import.geojson`, 330,000 points, 85,688,964 bytes, sha256 `b1795475…c4d79ca9`, generated (never committed) | `upload-url` from a presigned GET URL | the job reaches `Completed`, the table publishes, and the layer's count is 330,000 |
| Redis-off | `e2e/fixtures/s10-redis-off.geojson`, 8 points, 2,109 bytes, committed | `upload-url` with `forceBackground: true` | either the 202 job (an in-process background import) reaches `Completed`, `honua_data.imported_s10_redis_off` publishes as `s10-redis-off`, and its count is 8 (`executor: in-process`, `topology: redis-off`); or the typed 503 `Background import service not available. File is too large for synchronous import.`, which the server sends only when the import job service is absent (`refusal` recorded, decision 3) |

`e2e/fixtures/large_import.py` generates both fixtures byte for byte from integer arithmetic and pins
their feature count, size and SHA-256; the harness refuses a file whose digest differs. The work
follows the cell's job split (honua-release#381):

- **provision** (AWS credentials) generates and verifies the fixture, then puts it at
  `s3://$HONUA_AWS_CELL_FILE_STORAGE_BUCKET/release-cells/<target>-redis-<on|off>/s10/<run id>-<attempt>/`.
  The handoff records the bucket and the key's SHA-256 only; the key is recomputed from the cell and run.
- **admit** (AWS credentials) mints a presigned GET URL valid for 1,800 s and seals it to the journey
  runner together with the application key. The URL is a bearer credential: it never reaches a job
  output, an artifact in the clear, a receipt or a log line.
- **journey** (no AWS credential) runs S10 before the seam drivers. It reads
  `GET /api/v1/admin/import/limits` first. If `max(BackgroundJobThresholdBytes, MaxMemoryBytes)` (the
  staging cap both upload paths enforce) does not exceed the threshold, the row fails
  `no-background-window` and names both limits; a fixture outside the window fails
  `fixture-outside-window`. It never falls back to `forceBackground` or a smaller file. Then it calls
  `POST /api/v1/admin/import/upload-url` and follows `GET /api/v1/admin/import/jobs/{jobId}` for up to
  1,200 s, recording each state change. It publishes `honua_data.imported_s10_large_import` through
  the seed's connection (`POST /api/v1/admin/connections/{id}/layers`) and counts it through
  `FeatureServer/{layer}/query?returnCountOnly=true`.
- **publish** follows the admin API's asynchronous 202 response when present. It validates the
  job id, pinned source row count, and exact relative same-connection `statusUrl` before polling
  `GET /api/v1/admin/connections/{id}/layers/jobs/{jobId}` every 5 s for at most 1,200 s. A completed
  job must report full materialization and a committed layer id; the scenario then queries that
  layer and still requires the pinned row count. Failed, cancelled, stalled, malformed and denied
  jobs fail the scenario. Job acceptance alone never passes it. Receipts keep the job id, status,
  bounded progress transitions, rows materialized/total, duration, reads and answering instances;
  raw responses and credentials are excluded. The harness never follows a foreign status URL or
  resubmits publication. Small 200/201 publications keep the synchronous path.
  Older candidates can answer 504 or nothing within 120 s (as run 38249563601 did). For those
  responses, S10 retains the existing read-only layer-list convergence window of 360 s, then
  requires the same pinned count. It records `publishAnswered`, `publishMs`, `publishReads` and
  `publishConvergedSeconds`; failure to find the layer remains `publish-not-converged`.
  A typed publish refusal still fails the positive Redis-on journey. Real cloud receipts are
  required after the asynchronous server implementation ships; harness tests do not qualify it.
- **teardown** (AWS credentials) deletes the object (`s10Cleanup` in the cell report). A failed delete
  fails the cell. The bucket's one-day lifecycle on `release-cells/` is only the backstop.

A 200 (imported synchronously), a 202 without a `jobId`, a failed or cancelled job, a stalled job, a
publish that does not converge, a wrong row count, or (on Redis-off) any answer other than a 202 job or the typed 503 is `fail`, never
`blocked`, on both topologies: the server accepted the work. A job still not terminal at the 1,200 s
deadline fails `job-stalled`, naming the last transition, its time, the seconds since it, the
`Honua-Instance` values that answered the status reads, and the server item (`honua-server#5916`:
the job runs in the host that accepted the upload, which stops advancing it, e.g. a Lambda invocation
frozen after its 202; cloud run 38192851328, `processing -> queued -> processing`). An unstaged fixture (no
bucket variable, a digest mismatch, an S3 refusal) or a missing presigned URL is `blocked` and names
honua-release#548; `require_real` fails it. The row's evidence keeps only the allowlisted fields
`jobId`, `fixtureSha256`, `bytes`, `thresholdBytes`, `maxMemoryBytes`, `stagingCapBytes`, `queuedAt`,
`completedAt`, `durationSeconds`, `durationMs`, `transitions`, `rowCount`, `executor`,
`executorBasis`, `jobRunner`, `jobsRunnerCapability`, `stall`, `refusal`, `topology`, `source`
(`s3://<bucket>/sha256:<key digest>`), `sourceUrlHost`, the answering `instances` and the
`statusInstances` that answered the job reads. The job
document's `sourceUrl` (the presigned URL) is dropped, and server messages are scrubbed of the URL and
every `X-Amz-*` parameter.

**Cell inputs.** Under the server defaults (100 MiB each), the staging cap equals the threshold, so a
default cell has no background window. The Redis-on cells lower `Import__Limits__BackgroundJobThresholdBytes`
to 64 MiB (67,108,864) under the default 100 MiB `MaxMemoryBytes`, which leaves the window
(64 MiB, 100 MiB] that the fixture sits in:

- `aws-eks/redis-on` sets it directly in the chart's `config.env`.
- `aws-ecs/redis-on` and `aws-serverless/redis-on` pass `import_background_job_threshold_bytes=67108864`
  (`TfTargetSpec.redis_on_declared_vars`), only when the pinned root declares it. Neither
  `examples/aws` nor `examples/aws-serverless` declares it yet, because both roots fix their
  `additional_env` inline. Until honua-iac adds the input and renders it as
  `Import__Limits__BackgroundJobThresholdBytes`, those two cells fail S10 with `no-background-window`.

A background import that the Redis-on Lambda accepts runs on AWS Batch only when the API function and
the Batch workers share S3 file storage (honua-server#5916). Otherwise the Lambda refuses it with the
typed 503 (`import.background` reports `reasonCode: shared-file-storage-required`). honua-iac#243 adds
the inputs to `examples/aws-serverless`. The cells pass:

| cell | input (only when the pinned root declares all three) | value |
|---|---|---|
| `aws-serverless/redis-on` | `file_storage_provider` | `AwsS3` |
| | `file_storage_aws_s3_bucket_name` | `$HONUA_AWS_CELL_FILE_STORAGE_BUCKET` |
| | `file_storage_aws_s3_key_prefix` | `release-cells/<name prefix>` (the cell's object grant is `<prefix>/*`) |
| | `file_storage_aws_s3_region` | not passed; the module renders the root region (us-east-1, the bucket's region) |
| `aws-serverless/redis-off` | none | without Redis the Lambda has no durable runner and answers the typed 503 whatever its storage, which is what the cell records |
| `aws-ecs/redis-on` | unchanged: the same three inputs as part of the MultiNode set (`REDIS_ON_SCALE_INPUTS`) | |

`TfTargetSpec.file_storage_inputs` and `file_storage_bucket_env` drive the serverless set. An older pin
that lacks any of the three inputs gets none of them and keeps `Local` storage. If the root declares
them but `HONUA_AWS_CELL_FILE_STORAGE_BUCKET` is unset, the Redis-on serverless cell refuses to
provision with an error naming the variable (destroy still plans). The S10 fixture is staged next to
the cell's prefix, not inside it: `release-cells/<target>-redis-<on|off>/s10/...` versus
`release-cells/<name prefix>/...`. The server reads it only through the harness-minted presigned URL,
never with its own role, and both sit under the bucket's one-day `release-cells/` lifecycle. The
release-cell permissions boundary must admit the module's grant on the bucket for the Lambda,
control-plane event and GP Batch job roles.

**Where the import job runs (`executor`, `jobRunner`).** honua-server queues file imports in
`UniversalImportJobService`, which runs the job inside the host that accepted the upload on every
topology, and registers it whether or not Redis is. Cloud run 38192851328 (server 93cad86ec) showed
both consequences: the Redis-off cell queued the forced job (202) instead of the typed 503, and on
`aws-serverless/redis-on` the job stalled after the Lambda invocation returned its 202. When the job
document names its runner (`jobRunner`, `runner` or `executor`; `ImportProgress` at 93cad86ec has none),
the receipt records it as `jobRunner` and `executor`. Otherwise the receipt **derives**
`executor: in-process` and says so in `executorBasis`, quoting the capability manifest's `jobs.runner`
entry (`jobsRunnerCapability`: `available`, `reasonCode`). That entry describes the Redis-backed
geoprocessing job runner, not file imports: on Redis-off it is `dependency-unavailable` while the import
still runs, which is why an accepted Redis-off import can only be in-process.

**Owner step (Cloud lane, honua-iac):** declare `import_background_job_threshold_bytes` on
`examples/aws` and `examples/aws-serverless`. The harness role also needs `s3:PutObject`,
`s3:GetObject` (presign) and `s3:DeleteObject` on `release-cells/*/s10/*` in the cell file-storage
bucket.

### Audit hash-chain key on every AWS cell
Without `AuditLog:ChainVerification:Key` the server still serves and writes audit rows, but its
scheduled hash-chain verification never succeeds (`Audit hash-chain integrity FAILED ... audit chain
key is not configured`) and the `audit-chain-integrity` health check is Unhealthy. honua-iac#227 adds
`audit_chain_key_secret_arn` (and `audit_chain_key_secret_kms_key_arn`) to `examples/aws` and
`examples/aws-serverless`: ECS resolves the secret through task secrets; Lambda and the GP Batch jobs
receive an `aws:secretsmanager:<arn>` reference that the server resolves at startup
(honua-server#5768). **Owner step:** create a Secrets Manager secret in us-east-1 whose value is a
base64 key of at least 32 bytes (`openssl rand -base64 32`), keep it for the lifetime of the cells'
audit data, admit it through the release-cell boundary (honua-iac `bootstrap/aws-release-cells`
`runtime_audit_chain_key_secret_arns`), and set the repository variable
`HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_ARN` (plus `HONUA_AWS_AUDIT_CHAIN_KEY_SECRET_KMS_KEY_ARN` for a
customer-managed key). The cell workflow exports both to the provision and teardown steps; the
harness passes them to every ECS and Lambda cell whose pinned root declares them
(`optional_env_vars`). The key is recommended, not required: an unset variable never blocks or
refuses a cell, and the root plans with a warning. Lambda caps the function environment at 4 KB:
the Redis-on cell with the key ring, GP Batch and the audit-key reference once measured 4118 bytes
and `CreateFunction` refused it (run 38046060497); honua-iac#230 (pinned from e218d17b) keeps that
environment near 2.9 KB and fails the plan with the measured size whenever a root would exceed the
cap, so the key is passed on both Redis modes.

### Per-run HTTPS hostname and the demo CORS origin on the ECS and Lambda cells (honua-release#450)
The pinned `honua` CLI and `honua-mcp-proxy` refuse to send a credential over plain HTTP to any
non-loopback host, so a journey against the ALB's `http://*.elb.amazonaws.com` endpoint stopped at
stage 2 on every aws-ecs attempt (e2e-cloud-aws run 38038433205). The reviewed demo pages' CSP
bootstrap also admits only `https://(*.)honua.io` backends (or loopback), so `top-demo` (S9) was
BLOCKED on every cell. `ECS_SPEC` therefore gives each aws-ecs cell its own HTTPS name:

    <GITHUB_RUN_ID>-aws-ecs-redis-<on|off>.cert.<HONUA_AWS_CELL_DNS_PARENT>
    e.g. 38038433205-aws-ecs-redis-off.cert.demo.honua.io

The label carries the whole run id plus the cell, so concurrent cells and concurrent runs never share
a certificate or record. It is lowercase LDH, at most 63 octets, and short enough for ACM's 64-octet
limit on the full name; an over-long value keeps a hash suffix instead of being truncated. The
harness passes `domain_name` and `route53_zone_id` to `examples/aws`. The aws-ecs module then issues
an ACM certificate, writes its DNS validation records in the zone, waits for issuance inside
`terraform apply` (a few minutes), aliases the name to the ALB, and `honua_url` becomes
`https://<name>`. The runner's /32 is passed as `allow_https_ingress_cidrs`, not
`allow_http_ingress_cidrs`. On this TLS path only, the harness also passes
`alb_enable_http_redirect=true` when the pinned `examples/aws` root declares it (owner decision 8 of
2026-10-10; the module input already defaults to true, the root variable is a parallel honua-iac
change), so the module serves a redirect-only listener on port 80, open to the same /32 only. A
plain-HTTP cell never gets it: there is no port 443 to redirect to. The canary's `https-redirect`
probe asserts it from the provision runner: on an HTTPS cell `http://<host>/healthz/live` must answer
301 or 308 with `Location: https://<host>/healthz/live` (a missing listener, a 302, or any other
target fails the cell); every other cell (plain-HTTP ECS, serverless, EKS) records the probe
`blocked` with the reason. The admit job resolves
the name to its ALB through the zone's alias record and opens 443 (the endpoint's scheme) to the
journey runner. The provision report records `transport: {scheme, host}`, and each journey receipt
records the same pair in its `Candidate transport` notice. `terraform destroy` removes the
certificate, its validation records and the alias. Teardown then lists the zone and ACM read-only:
any remaining record or certificate for this cell, or a listing that cannot be read, fails the
cell closed, and other
`*.cert.<parent>` names are reported as a warning (they may belong to a concurrent run's live cells).

Every ECS cell also passes `cors_allowed_origins=["http://127.0.0.1:18099"]` when the pinned root
declares it (honua-iac fix unit C4 renders `Cors__AllowedOrigins__0`). That is the origin
`e2e/drivers/demos/run.sh` serves the demo pages from (`E2E_SITE_PORT`), so the cell answers CORS for
the demos without a stub.

**Owner step (done for honua-io/honua-release, account 585192672263):** set the repository
variables `HONUA_AWS_CELL_DNS_ZONE_ID` (the public hosted zone id, `Z089181827C9GKIKHXUTT`) and
`HONUA_AWS_CELL_DNS_PARENT` (`demo.honua.io`). They are not secrets. The cell workflow exports both
to the provision and teardown steps, and the zone id to the admit step. The OIDC cell role needs ACM
and Route53 write in that zone (`honua-release-cicd` has PowerUserAccess). With both variables unset
(forks, other accounts) the cell keeps its plain-HTTP ALB endpoint and says so in the provision log.
If only one is set, or a value is malformed, provisioning refuses. Destroy never refuses.
aws-eks uses the same variables (see the EKS cell section).

The aws-serverless cells use the same path (honua-iac#232). When the pinned `examples/aws-serverless`
root declares both `domain_name` and `route53_zone_id`, `SERVERLESS_SPEC` passes the per-run name
`<GITHUB_RUN_ID>-aws-serverless-redis-<on|off>.cert.<HONUA_AWS_CELL_DNS_PARENT>` and the zone id. The
module issues a DNS-validated ACM certificate, creates a regional API Gateway custom domain mapped to
the `$default` stage, writes the Route53 alias, and adds the name to `HostValidation__AllowedHosts`.
The cell endpoint is read from `honua_url`, which is `https://<name>` in that case. A pin whose root
predates those inputs keeps the execute-api URL rather than refusing, because the release account's
DNS variables are already set. The demo CORS origin is passed whenever the root declares
`cors_allowed_origins`. There is no ALB, so there is no runner ingress input, no admit step and never
`alb_enable_http_redirect`, and the canary's `https-redirect` probe is `blocked` with that reason.
Teardown runs the same read-only leftover check as the ECS cells: this cell's records and certificate
must be gone. With an HTTPS honua.io endpoint the pinned demo CSP admits the backend, so `top-demo` (S9)
runs on the Lambda cells too. The Redis-off geoprocessing demo still passes only on its typed refusal
(#514).

### Cell readiness diagnostics (ECS, Lambda and EKS)
Before destroying every `aws-ecs`, `aws-serverless` and `aws-eks` cell, the teardown job runs
`run_cloud.py --phase diagnose`. For an ECS cell it writes `diagnostics-ecs.json` into the cell's
evidence (uploaded with the gate report) and prints the same to the job log: every service task's
`stopCode`, `stoppedReason` and container exit reasons from `aws ecs describe-tasks`; the last 300
CloudWatch log lines of each stopped task's containers (and the newest task's), read from the
awslogs group/stream prefix in the task definition; and each task definition's environment and
secret variable NAMES, never values.

For a serverless cell it writes `diagnostics-lambda.json`: every Lambda function carrying the cell's
name stem (the stem of the `lambda_function_name` output, matched against `aws lambda
list-functions`, plus the API and control-plane function-name outputs), each with its state, last
update status and environment variable NAMES (never values), and the last 300 lines of its log group
merged from every stream (one per execution environment, up to 40; CloudWatch's LastEventTime order
is updated lazily, so "the newest few" can miss the environment that served last). Each group also
gets `failureSignatures`: a whole-group `filter-log-events` sweep counting lines, streams and
first/last time for `53300:` connection exhaustion, `Runtime.ExitError`, unhandled exceptions, Lambda
timeouts (`Task timed out`, and `Status: timeout` on a REPORT or INIT_REPORT line), database timeouts, a failed startup preflight and unexpected MCP tool failures (the server logs
the exception class there; the tool envelope does not carry it), with a bounded sample. The API
Gateway access log (`/aws/apigateway/<api function name>`) is tailed and its 4xx/5xx lines counted
by status and by route (`accessErrors`): a response API Gateway produced itself carries no CORS
header, so a demo page reports it as a CORS refusal and only this log tells it from a server
response. For each 5xx API Gateway generated itself (`errorMessage` set, such as the 503 "Service
Unavailable" at the 30 s integration timeout; up to 10), `gatewayFailureWindows` keeps the API
function's `START`/`REPORT`/`INIT_*`/`Task timed out`/`Status: timeout` and server request lines
from 2 s before the request to 40 s after, with the request's latencies and Lambda RequestId when the
stage logs them: that is what tells a cold start, a queued invocation and a slow handler apart (run
40832719280). The platform lines are read in their own bounded pass, so a burst's request lines
cannot crowd out the stalled invocation's REPORT, and 5xx requests that started in the same second
share one window (`sameWindow` counts them; run 40865466550).
When `gp_batch_enabled` is true it also tails and sweeps every `/aws/batch/<stem>-*` job
log group. This is where a Lambda server's own startup and refusal reasons
(a `dependency-unavailable` capability, a GP job that never left `running`) survive the cell.

The log groups are destroyed with the cell, so this is the only record of an exit or refusal cause.
Credentials, connection strings, URI userinfo passwords and AWS key ids are redacted from every line.
The step is informational and never changes the verdict. The journey job holds no AWS credential, so
it cannot read these.

### Cost evidence (owner decision 9, 2026-10-10)
`cost_ceiling_usd` (default 20) is a per-run ceiling. Two readings back it, recorded side by side:

- **Live estimate (this run, gating now).** In each cell's teardown, before `terraform destroy`,
  `e2e/cost_meter.py` reads the cell's Terraform state (`terraform show -json`), prices every
  billable resource class for the hours since provision began plus a 0.5 h teardown allowance, and
  adds measured usage: Lambda GB-seconds and invocations and HTTP API requests (CloudWatch sums), and
  Batch job vCPU/GB-hours (`batch list-jobs` / `describe-jobs`). Prices come from
  `e2e/cost-prices.json`, a committed table of public us-east-1 on-demand list prices with a
  `pricesAsOf` date (read from the AWS Price List API). Classes billed "per hour or partial hour"
  are billed whole hours. Data transfer, NAT data processing, ALB capacity beyond one LCU-hour per
  hour, CloudWatch, S3/ECR (except the S10 fixture the harness stages: S3 Standard storage for the
  run's hours, one PUT and one GET, attributed to the run by its key prefix), Route 53/ACM, Bedrock
  tokens and Lambda INIT time are **not** counted;
  Fargate Spot is priced at the on-demand rate. The table's `excluded` list says so in every
  reading. A resource the table cannot price, or a usage reading that fails, makes the estimate
  `unavailable` (named, never `pass`). A metric with no datapoints yet is read again once after
  CloudWatch's publication delay (120 s); if it is still empty, it counts as zero and is listed
  under `noDatapointsAfterRetry`. The estimate lands in the cell gate-report's `cost`; the
  `cloud-report` job sums the cells into `e2e/cloud-evidence/run-cost.json`, and `aggregate` takes
  that sum as the run's cost: `pass` when it is at or under the ceiling, `fail` over it.
- **Cost Explorer actual (earlier runs, gating at the next run).** Every cell resource is tagged
  `honua-release:run-id=<GITHUB_RUN_ID>`, `honua-release:cell=<target>/redis-<on|off>` and
  `honua-release:cost-ceiling-usd=<ceiling>` through the IaC root's `tags` variable.
  - **Query.** `cloud-report` calls `ce:GetCostAndUsage` (daily UnblendedCost, every record type
    except `Credit` and `Refund`). Credits are excluded because they net this account's plain
    UnblendedCost to about $0. Commitment-covered usage still counts. The query groups by run id
    and ceiling over the last 3 days to find runs. It then reads each run's whole billed history,
    90 days back.
  - **Settling.** Cost Explorer lags about a day. A day is settled 24 h after it ends (UTC):
    - `settledUsd` sums the settled days and `actualUsd` sums every day seen.
    - A run reads as `pending` until all its days are settled, then as `measured`.
    - A run with history before the 90-day horizon reads as `incomplete`.
    - Cost Explorer's `Estimated` flag stays on for the whole open billing month. It is recorded
      as `estimatedByCostExplorer` and not waited on.
  - **Enforcement.** A full-scope run fails when an earlier run's `settledUsd` exceeds that run's
    own ceiling, and the failure names the earlier run id. Settled spend only grows, so an orphan
    that keeps accruing fails it as well. The ceiling is enforceable at the next run, not during
    the run that overspent.
  - **No usable reading.** If the tag is not active as a cost-allocation tag (`tag-not-activated`)
    or the query fails (`unavailable`), a full-scope run is `blocked`, and under `require_real` it
    fails.
  - **Rerun.** A rerun (`GITHUB_RUN_ATTEMPT` > 1) shares its run id with the earlier attempts,
    which the live estimate does not cover. It certifies only when those attempts are `measured`
    and this attempt's estimate plus their settled spend fits the ceiling.
  - **Scope.** Every dispatched cell, EKS included, must report its estimate, or report
    that it never provisioned. Otherwise the run's estimate is `unavailable`. The `iac-live` job
    is outside this ceiling: it dispatches a separate honua-iac workflow run with its own
    deployment, lifecycle and teardown, and that run is not tagged with this run id. The basis
    says so under `notCovered`.

Evidence object (per cell in `cost`, per run in `finalCost`; earlier runs in `priorRunCosts.runs[]`):
`{status, scope:"run", runId, runAttempt, currency:"USD", meter:"estimate", estimateUsd,
estimateBasis, actualUsd|null, actualAsOf|null, ceilingUsd, measuredAt, amountUsd}`. `amountUsd`
repeats the gated figure under the name the existing consumers read.

Owner steps (once):
1. **Activate the cost-allocation tags** `honua-release:run-id` and `honua-release:cost-ceiling-usd`
   (optionally `honua-release:cell`) under Billing and Cost Management > Cost allocation tags.
   A key appears there only after a tagged resource has billed (up to 24 h after the first tagged
   run), and activation applies from then on. Request a backfill there if earlier runs should count.
   Until then the prior-run reading is `tag-not-activated`, which blocks a full-scope run and fails a `require_real` one. Without the ceiling tag, an earlier
   run is judged against the current run's ceiling (`ceilingSource: current-run-input`).
2. **CI role permissions (read-only).** The `HONUA_AWS_ROLE_ARN` role needs `ce:GetCostAndUsage`,
   `ce:ListCostAllocationTags`, `cloudwatch:GetMetricStatistics`, `batch:ListJobs` and
   `batch:DescribeJobs`. `tag:GetResources` is optional and lets an operator list a run's resources.
   `honua-release-cicd` has `PowerUserAccess`. `iam simulate-principal-policy` (2026-10-10) allows
   all of these, but SCPs were not evaluated and the role was not assumed: its trust policy admits
   only GitHub OIDC from `refs/heads/trunk`.

Known gaps in the actuals (honua-iac, not this harness): ECS services and Batch job definitions do
not set `propagate_tags`, so Fargate task and Batch job spend is not attributed to the run tag. The
actual undercounts those lines, and the estimate still covers them. Public IPv4 charges for the
ALB, and the EKS cell's Helm-created load balancer, carry no run tag either.

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

### Run-owned tenant isolation fixture

The optional `HONUA_CLOUD_TENANT_FIXTURE=true` profile provisions an ephemeral static
OIDC issuer for the owned cell. The ECS and Lambda roots must expose the reviewed
`oidc_static_jwt` input; the Lambda image must include the early signing-secret
resolution from honua-server#5980. EKS injects the key through its existing external
Kubernetes Secret, never Helm values. Existing deployments and legacy runs keep
their current configuration when this flag is absent.

The bootstrap API credential still creates the narrow operator, approver and viewer
keys and role grants. The author and second tenant use fresh five-minute signed
JWTs with identical subject and role, differing only in tenant and token timing/
nonce. Stage 8 first reads the exact private map as its author, proves that the
other tenant can reach the general Studio surface, then requires denial for that
same private resource. A missing issuer, invalid bearer or blanket route denial
cannot count as tenant-isolation evidence.

The provision job creates the signing secret outside Terraform and passes only
its ARN to the typed ECS/Lambda configuration. The key travels to the isolated
journey job through the existing encrypted handoff and remains in Python memory;
it is not passed to npm or retained in a principal file, target document, receipt
or public artifact. Teardown deletes the run-owned secret. Public configuration
capture resolves immutable secret version identities using metadata-only DescribeSecret
requests. Runtime secret values use field-bound HMAC-SHA256 commitments with a fresh
256-bit key that exists only in producer memory and is never published or persisted.
Public validation verifies the captured configuration binding, not secret values or
offline guesses against a public password hash.

For prepared EKS qualification, `deployment-terraform-inputs.json` records exact
Terraform arguments before apply. `deployment-helm-inputs.json` chains that digest
and records the resolved values and exact command before Helm installation. Both
are bound by `deployment-execution-inputs.json`; `deployment-actuation.json` records
the actual consumption boundaries. `executed-manifest.yaml` preserves the original
manifest bytes. The teardown producer verifies the chain and actual journey and
cleanup results before emitting `deployment-cell-receipt.json`. A source change
or local control pass alone does not qualify a cloud target.
