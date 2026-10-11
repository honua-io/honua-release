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
identically to the reference (local docker). Per `docs/TEST-STRATEGY.md`, this does NOT re-run the full
SDK × scenario matrix per target — it runs the small, data-independent canonical set and compares.

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

GA cells for 2026.1 are `{aws-ecs, aws-serverless, aws-eks} × {redis-off, redis-on}`: owner decisions
12 and 18 of 2026-10-10 pulled honua-release#203 (bring-your-own Kubernetes) into 2026.1, so the EKS
cells are no longer informational Preview and a red EKS cell reddens the full-scope cloud report. The
mixed ECS + Batch cell is not in the rc.3 matrix: honua-iac has no
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

### The EKS cell (bring-your-own Kubernetes, honua-release#203)
`aws-eks` is the 2026.1 Kubernetes GA cell (owner decisions 12/18 of 2026-10-10; GP on Kubernetes is
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

The chart install reads these root outputs, each only when the root declares it (`terraform output
-json`, held in memory):

- `db_connection_string` and `redis_connection_string` (sensitive) go into the cell's runtime Secret.
- `chart_config_env` is a map of plain environment names to non-secret values, for example
  `ControlPlane__Kubernetes__*`, the operation policy rules and Secrets Manager references. It goes
  into the chart's `config.env` through a values file.
- `server_role_arn` and `gp_job_role_arn` annotate the server and GP job ServiceAccounts (IRSA).
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
(see below). The job and server roles therefore need EKS Pod Identity or an operator-created provider.

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
merged from the group's newest streams. When `gp_batch_enabled` is true it also tails every
`/aws/batch/<stem>-*` job log group. This is where a Lambda server's own startup and refusal reasons
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
  hour, CloudWatch, S3/ECR, Route 53/ACM, Bedrock tokens and Lambda INIT time are **not** counted;
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
