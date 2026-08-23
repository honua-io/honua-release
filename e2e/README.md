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

### D9.3 AI delivery arc

`ai_delivery_arc.py` consumes the zero-to-map plan and executable driver from
the exact `honua-sdk-js` SHA pinned in `platform-manifest.yaml`. It does not copy
or reimplement that driver. Contract mode validates the ordered seven-stage
plan and emits an explicitly blocked SDK receipt. Live mode runs through the
publication proposals once, writes a candidate/source/plan/endpoint-bound
checkpoint, and pauses for the focused Console receipt. Resume atomically
claims that checkpoint, so approval cannot replay connection, GP, or Studio
side effects. The release
checker additionally requires both AI execution of the Esri-compatible GP task
and native direct analysis, plus the Studio/Console/final-URL receipt joins.
The Studio section is three distinct lifecycle families: map, app, and
dashboard. Each must save an immutable version, read that exact item/version
pair, and reopen it as a new draft through the server MCP lifecycle tools;
reading the original mutable draft does not count as reopen evidence. Each
family then records a publication intent, saves a distinct intent-bearing
version, joins the Console request/publication/audit identities, and probes its
own final HTTPS URL. The app URL remains the entry-point share URL.

The release train treats evidence production and the release verdict as three
separate jobs. `e2e-ai-delivery-arc-local.yml` produces the local-Docker SDK,
Studio, and Console receipts without consuming cloud artifacts. The AWS ECS
cell independently produces its receipts. Only after both reusable jobs finish
does `release-train.yml` download their exact named artifacts from the current
caller run and invoke the strict checker. A failed, blocked, or missing producer
therefore reaches the final aggregate as a non-green result instead of being
hidden by job ordering. The release-owned local installer uses the manifest
server image by digest; the pinned SDK continues to own every journey action.

`certification/ai-delivery-arc.yaml` names two certifying targets. Local Docker
must carry the candidate-pinned SDK journey. AWS ECS must supply both the
candidate-bound Terraform provision/handoff receipt and a second receipt proving
that the same full admin to GP to Studio to Console to public-share arc ran on
the ECS target. Both targets also require a dedicated real-model receipt over
the same endpoint. It must prove natural-language tool selection across Admin,
Esri GP, native analysis, and map/app/dashboard composition/publication, with
exact joins to deterministic runtime IDs. A generic Studio transcript or a
healthy ECS apply cannot satisfy the release gate.

Every external receipt must declare its target and an explicit set of `checks`,
all `passed`. The required checks include real-model map, app, and dashboard
compose/save/reopen evidence and, on ECS, the complete downstream journey. The
checker also rejects a live SDK action marked passed without kind-appropriate
execution evidence or without its planned identity captures. The terminal share
check must be HTTP 200 on a public HTTPS URL; loopback, private-address, and
plain-HTTP receipts cannot certify publication.

```bash
E2E_SDK_JS_DIR=../honua-sdk-js python e2e/ai_delivery_arc.py
# The live local producer also requires exact Studio/Console checkouts, the
# Console origin, distinct scoped prepare/Console credentials, and a configured
# real-model provider. It creates a pinned ephemeral HTTPS tunnel, verifies that
# the generated origin routes to its exact local candidate port, and tears it down.
python e2e/local_ai_delivery_arc.py
```

A strict live invocation also requires `E2E_AI_LOCAL_MODEL_RECEIPT` plus its
content-addressed `E2E_AI_LOCAL_MODEL_EVIDENCE`, `E2E_AI_AWS_RECEIPT` for the ECS
Terraform/readiness/handoff run and `E2E_AI_AWS_ARC_RECEIPT` for the separate
full journey executed against that still-live ECS endpoint, and the dedicated
`E2E_AI_AWS_MODEL_RECEIPT`/`E2E_AI_AWS_MODEL_EVIDENCE` pair. Both generic AWS
receipts also require `E2E_AI_AWS_EVIDENCE`, the exact final-evidence bytes
whose SHA-256 and Actions-run URL they declare. None is inferred from a
successful apply. `E2E_AI_AWS_SDK_RECEIPT` must point to the live
`sdk-journey.json` produced during that same ECS lifetime; every AWS model call
is hash-bound to its exact SDK action receipt, just as the local model receipt
is bound to the local SDK receipt.

In strict `aws-ecs` cloud cells, `run_cloud.py` owns one lifetime: it applies
one saved Terraform plan, waits for readiness, invokes the manifest-pinned
DevOps producer through deterministic prepare/pause, Studio model prepare/pause,
focused Console browser approval, Studio model resume, and deterministic SDK resume;
it then destroys and verifies empty Terraform state and seals the two final
release receipts. Studio runs `release:real-model-ai-arc prepare|resume`; Console
runs `npm --prefix e2e/playwright run receipt:console` against the separately
bound `HONUA_AI_ARC_CONSOLE_ORIGIN`. It validates the aggregate against the
manifest-pinned SDK schema, writes byte-identical aggregate bytes to the Studio
and SDK receipt paths, and emits Console-owned browser/runtime evidence to
`HONUA_AI_ARC_CONSOLE_EVIDENCE`. Console receives only
`HONUA_AI_ARC_CONSOLE_TOKEN`; the model/admin credential is never present in its
process environment. Studio prepare receives only the purpose-specific secret
referenced by `HONUA_AI_ARC_PREPARE_CREDENTIAL_SECRET_REF`; this reference must
not equal Terraform's bootstrap admin-secret reference. DevOps subprocesses
retain only the AWS/OIDC identity needed to resolve their own references and
explicitly scrub Studio, Console, broad Admin/API, and provider credentials.

## Phase B — cross-cloud parity tier (AWS-first, scaffolded)

The "also run cloud integration" layer: deploy a **real** honua-server to a cloud target via the actual
honua-iac, run the **canonical (slim) parity set** against its public endpoint, and assert it behaves
identically to the reference (local docker). Per `docs/TEST-STRATEGY.md`, this does NOT re-run the full
SDK × scenario matrix per target — it runs the small, data-independent canonical set and compares.

**Matrix: all 3 AWS targets × Redis on/off** — so the platform is proven to behave identically across
deploy shapes and with/without its cache:

| target | how | endpoint | Redis |
|---|---|---|---|
| `aws-serverless` | Lambda + API GW (`examples/aws-serverless`, ECR Lambda-AOT image) | `honua_url` output | `redis_enabled` |
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
`teardown()` + a backstop reaper (sweeping every example root) always run.

The ECS cell reuses the one DevOps provision handoff, runs the SDK, Studio, and
Console producers before teardown, and uploads their source artifacts together.
The release checker still remains red until those artifacts are explicitly
provided and candidate-bound; it never infers a journey pass from Terraform or
readiness alone.

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
- `E2E_REQUIRE_REAL=1` promotes BLOCKED/SKIPPED to FAIL, so once real images + the
  `honua_geoservices_error_total` metric exist, the gate genuinely fails on a regression.

### Wiring left as TODO (blocked on real artifacts — search the tree for `TODO(#7)`)

- **Real images/pins:** populate `platform-manifest.yaml` server image + SDK versions (Phase 0/2); then
  the BLOCKED scenarios become live and the dotnet probe's `PackageReference` is added.
- **Staging registries:** point `HONUA_NPM_REGISTRY` / `HONUA_PIP_INDEX_URL` / `HONUA_NUGET_SOURCE` at
  the candidate's staging artifacts and implement the real install commands in `harness.install_sdks`.
- **Exact error trigger:** pin the endpoint+params that deterministically yield a 200+`{error}`, and the
  real SDK call surfaces in each probe (constructor / query method).
- **Server config:** confirm honua-server health path, metrics port, DB env keys in `docker-compose.yml`.
- **`sync_no_duplicates`:** implement via the honua-collect sync client once it is installable from staging.
