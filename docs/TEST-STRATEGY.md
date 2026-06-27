# Honua Platform Test & Certification Strategy

Companion to RELEASE-ENGINEERING-PLAN.md. Defines the layered test architecture, the deploy-target
matrix (and how to avoid its combinatorial explosion), the canonical scenarios, and an AWS-first build.

## The problem
The release "full test suite" gate means **every repo's tests + cross-component integration + cross-cloud
integration** — and today the cross-component and cross-cloud layers largely don't exist. Each repo tests in
isolation (SDK mocks server, server mocks clients, IaC never boots a server), so the seams — where almost
every audit bug actually lives — are untested. A naive full cross-product is days-long and bankrupting.

## Three layers (stacked, increasing cost, decreasing cache-ability)

1. **Per-repo suites — cache-able.** Deterministic, content-addressed; run only what changed since the last
   green manifest; reuse cached results for unchanged components. Kept green continuously (nightly), so the
   release train mostly *confirms*.
2. **Cross-component seam scenarios — the high-ROI gap.** Real server + real DB + SDKs installed from a
   staging registry, NO mocks at the seams. Tests the contracts *between* components (IaC→server, server↔SDK,
   SDK↔contract-version, app→server). **`docker-compose` tier is cheap, fast, per-PR-able and catches most
   seam bugs — build this first, on a laptop.**
3. **Cross-cloud integration & parity — real infra, expensive, un-cache-able.** Ephemeral real cloud
   environments via the actual IaC; proves "deploys + behaves correctly across environments." Nightly/release.

## Deploy-target matrix (7 targets)
| # | Target | Cost / spin-up | Module |
|---|---|---|---|
| 1 | local docker (compose) | ~free, seconds | docker-compose |
| 2 | AWS serverless (Lambda/API GW) | cheap, scale-to-zero | aws-serverless |
| 3 | AWS ECS (Fargate) | medium | aws-ecs |
| 4 | AWS EKS (k8s) | high + slow (~10-15m control plane) | aws-eks + helm |
| 5 | Azure ACA | medium | azure-aca |
| 6 | Azure AKS (k8s) | high + slow | azure-aks + helm |
| 7 | Azure Functions | cheap | azure-functions |

## Decompose the axes — do NOT run the full cross-product
Full product (7 targets × 3 SDKs × compat-window × scenarios) = hundreds of cells / days / $$$. Instead test
each axis independently:
- **SDK × scenario matrix → one reference target = local docker.** All 3 SDKs, full scenario set, compat
  window — here, free, per-PR. This is where SDK/contract bugs are caught cheaply.
- **Deploy-target parity → the *canonical* (slim) scenario set on each target, assert identical results.**
  ~7 cells, same scenarios; proves "behaves the same everywhere" without re-running the SDK matrix per target.
- **IaC→server + upgrade seams → per target** (or representative subset); these are target-specific.
- **Cross-cloud parity (AWS≡Azure)** lights up once Azure is in — same harness, more cells.

Net: a few dozen cells, not hundreds. Local docker does the heavy per-PR lifting; cloud cells run nightly and
the release train consumes "green within last N hours."

## Canonical scenarios (seed each from a real audit finding → permanent regression test)
- **Sync round-trip / no-duplicates** (catches honua-collect#102): edit → sync → edit → sync → restart → sync
  ⇒ assert exactly one server feature.
- **GeoServices error surfacing** (catches sdk-js#309, sdk-python#122): force a 200+`{error}` ⇒ every SDK must
  raise, not return success; the error metric must increment (ties telemetry gate).
- **gRPC-web authenticated call** (catches sdk-js#308): query over grpc-web with apiKey ⇒ authorized + timeout/retry honored.
- **Published-artifact consumption** (catches sdk-js#310, iac#81, grpc#45): npm/nuget/pip install from staging;
  `terraform init` the customer tarball; `docker pull && run && /healthz`; run codegen.
- **Contract-compat window**: SDK v(N-1) against server vN ⇒ pass within supported window.
- **Upgrade**: deploy prior platform release → apply candidate → migrate (forward + rollback) → old clients still work.
- **Deploy-target parity**: the canonical set runs identically on every target.

## Cadence & cost control
- Per-PR: per-repo (changed only) + the compose seam tier.
- Nightly: cloud parity matrix (continuous-green), so the train just labels the latest green.
- EKS/AKS least often (control-plane cost + slow); serverless/Fargate-spot preferred.
- **OIDC into AWS/Azure (no static creds)**, per-run isolated accounts/RGs, ephemeral tagging + a **teardown
  reaper** (guarantee nothing lingers on credits), time-boxes, cost-budget alarms.

## Build order (AWS-first — you have credits)
- **Phase A:** local docker seam tier — full SDK × scenario. Free, immediate, highest bug-catch-per-dollar.
- **Phase B:** AWS **serverless** parity + IaC-seam + upgrade (cheapest cloud; scale-to-zero ideal for ephemeral).
- **Phase C:** add AWS **ECS**, then **EKS** (EKS weekly, not nightly).
- **Phase D:** Azure ACA/AKS/Functions → cross-cloud parity assertion goes live.

## Where it lives
A neutral `honua-e2e` (or in the release repo), parameterized by the **platform manifest** so it tests the exact
pinned set. It is the **executable compatibility matrix**: a matrix row is only credible if a scenario actually
runs that pairing. No component repo owns cross-component tests (it would just mock the others again).
