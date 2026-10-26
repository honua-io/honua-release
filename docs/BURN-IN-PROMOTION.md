# Burn-in promotion evidence

Promotion publishes the exact bytes of a signed, certified platform lock. It does not rebuild the
candidate. The governing requirements are [the canonical 2026.1 plan](https://github.com/honua-io/agent-delivery-spec/tree/trunk/.specifica/2026-1-release-plan-ai-cloud-to-maps/),
rulings R18–R21, and [the cut checklist](RELEASE-CUT-CHECKLIST.md). Gates can fail and cannot be overridden.

## Select a lock and start its burn

An all-green nightly strict train resolves component `origin/trunk` heads, produces its own nightly
evidence and mints a signed `2026.1-rc.N` lock. Its journey requirement is the deterministic driver on
both GA cells (AWS ECS, Redis off and on), plus the genuine model on ECS with Redis off. A red night mints nothing.

The release captain selects one certified lock as the promotion candidate, deploys its exact artifacts
to the demo and records its byte digest, minting run ID and UTC deployment time (`burnStartedAt`) in
`certification/promotions/<rc-label>.json`. The record follows
[`promotion-evidence.v1.schema.json`](../certification/promotion-evidence.v1.schema.json).
The lock bytes supplied to the checker must be the selected lock's retained artifact, even when the
repository's current `platform-lock.json` names a newer lock.

The burn belongs to that digest. New trunk commits, newer nightly locks and failures of another lock
do not reset it. A failure of the burning lock ends that burn, and the lock can no longer be promoted:
the checker counts every failure of the lock after its minting train completed, so a later
`burnStartedAt` cannot hide one. Fix forward and select a newer certified lock. Never hide a failed
observation by selecting only passing run IDs.

## Required evidence and journey pass rule

The record names exactly one `strictTrains` entry: the successful, non-dry-run minting train, completed
no later than burn start. `rcTrainRunId` names that same run. Its retained `gate-report.json`,
`platform-lock.json` and `run.json` must agree with the record. The report must contain all required
passing gate receipts; its original freshness is checked at minting time.

`evidenceClasses` declares every consumed class as `nightly` or `qualifying`. `evidence` contains exactly
one run reference per declared class, with `class`, `runId`, `completedAt`, `status` and `lockDigest`.
The checker requires these classes and refuses an undeclared, missing, duplicate or reclassified class:

| Tier | Required classes |
| --- | --- |
| Nightly | `build-test`, `contract`, `sbom`, `security`, `upgrade`, `capacity-soak`, `dr`, `lambda-certification`, `protocol-ledger`, `deterministic-journey`, `nightly-model-journey`, `executable-docs` |
| Qualifying | `genuine-model-journey`, `update-rollback`, `esri-bundle`, `cite` |

Nightly classes come from the minting train and appear in its report's `evidenceClasses` array.
Qualifying classes are produced against the selected lock during its burn. Every retained receipt must
bind the selected digest; evidence from another lock cannot be carried forward. Additional consumed
classes require an explicit tier declaration and a retained receipt too.

GA cells are `aws-ecs/redis-off` and `aws-ecs/redis-on`. Under the 2026-10-25 founder re-tier, AWS ECS is
the only GA cloud target; Lambda (`aws-serverless/*`) and EKS (`aws-eks/*`) cells are informational
Preview, are never counted and never block. (The `lambda-certification` class name is historical: its
verdict is the `cloud-parity` gate, which now certifies the ECS cells only.)

- `deterministic-journey`: deterministic passes on both GA cells.
- `nightly-model-journey`: a genuine-model pass on ECS with Redis off.
- `genuine-model-journey`: genuine-model passes on both GA cells during the burn.
- `update-rollback`: passing update and rollback proof on each GA cell, using signed
  rc.3 → rc.N → rc.3 locks, a GP job in flight that finishes exactly once, and the schema rollback
  boundary. Each cell records its own `updateStatus` and `rollbackStatus`, and both must pass.

Redis-on GA cells execute the complete eight-stage workflow. Redis-off GA cells qualify through
an explicit `redis-off-serving-refusal` profile: a nonempty feature query must match the authored
cell seed data through independent serving adapters, and a valid authored durable buffer request
must actually receive the typed Redis dependency refusal, corroborated by the live capability
manifest. The profile also verifies authentication, anonymous/viewer denials, exact client catalog
parity, and the candidate image before and after execution. The controller binds the seed manifest
to its encrypted provision handoff before candidate packages run.

The deterministic profile retains its original eight-stage receipt unchanged, including blocked
stages. A separate hashed sidecar records `executionOutcome: refused` and
`functionalGpExecuted: false`; it cannot supply genuine-model attribution or completed GP evidence.
A failed first profile remains attributed history and may use the existing second attempt. Missing,
failed, stale or mismatched sidecar, fixture, image, principal cleanup or run binding fails
qualification. This limited serving/refusal proof does not establish persistence, publication or
approval completion. Redis-on retains those full-workflow requirements. Background file
imports are not a governed-control-plane capability (ruling `redis-off-background-import-follows-job-2026-10-12`,
learned on run 38192851328): the import job service runs in-process without Redis, so the S10
large-import scenario on a Redis-off cell follows the job like a Redis-on cell, and the typed 503
counts only from a host without the import job service.

Each required journey cell passes within two attempts. A cell records `mode`, `attemptCount` and the
complete ordered `attempts` array. Every attempt records its consecutive number, completion time,
status and lock digest. A first-attempt failure must have `failureAttribution: model` or
`failureAttribution: infrastructure`; the last attempt must pass. Skipped, missing, out-of-period or
wrong-lock attempts fail. The mixed ECS + Batch cell is out of the rc.3 matrix. The two aws-eks cells are informational Preview under the 2026-10-16 owner amendment in #376,
superseding decisions 12/18. Their unknown/failed observations remain visible; no EKS success
replaces an ECS/Lambda requirement or establishes GA qualification. The journey producers enforce
the per-run cost ceiling before teardown; a run over the ceiling fails and tears down.

The cost reading is cell evidence, not a promotion class (owner decision 9, honua-release#519). Each
cell's gate report carries `cost`, the run carries `finalCost`, and earlier runs appear under
`priorRunCosts`: a live resource-hour estimate from the cell's Terraform state priced by
`e2e/cost-prices.json`, and the Cost Explorer actual for earlier runs grouped by the
`honua-release:run-id` tag once it is settled. A missing, stale, wrong-run or over-ceiling reading
fails the run. The Cost Explorer half reports `tag-not-activated` until the owner activates the
cost-allocation tags; see [Cost evidence](../e2e/README.md#cost-evidence-owner-decision-9-2026-10-10).

Every cell is reached on its own HTTPS name. Certification cells use per-run names under
`*.cert.demo.honua.io`; that is a certification artefact. The product must work on any customer
FQDN, with `Public__BaseUrl` derived from the module's `domain_name` (ruling
`customer-domains-2026-10-11`), so no journey check may assume a honua.io host.

## Canary coverage and promotion time

The burn lasts at least 48 hours. `demoCanaries` names the latest seven consecutive passing scheduled
six-hour canaries for this lock. Each has a distinct run ID and the same lock digest. Intervals must be
5h30m–6h30m, allowing schedule jitter without accepting a missing slot. The latest canary must be no
more than 6h30m old. Continue canaries after hour 48 until promotion.

The fetcher also supplies `canary-sequence.json`, a complete ledger of observed canary runs from the
minting train's completion through evaluation, including failures and lock bindings. Its shape is
`{"lockDigest":"sha256:…","runs":[…]}`; each run carries `runId`, `completedAt`, `status` and
`lockDigest`. Runs are matched to `demoCanaries` on those four fields, so an integer `runId` (as the
GitHub API returns it), fractional seconds and extra fields do not refuse. The selected seven must be
the final seven observations for this lock. Any failed or incomplete observation of this lock since
minting ends the burn, even if seven later canaries pass. A canary that fails before it reads its lock
records a null `lockDigest`; it cannot be attributed to another lock, so it refuses every lock minted
before it ran.

`burnStartedAt` is bounded by observation too. It cannot precede the minting train's completion, and
the lock's first observed canary must complete no more than 6h30m after it. A record cannot claim a
burn that started before the lock was deployed.

Promotion is allowed from hour 48 until any evidence passes its freshness bound. There is no
72-hour deadline and no requirement for later strict trains against newer trunk heads. Every retained
evidence receipt supplies a workflow-produced `freshUntil` UTC timestamp. The checker refuses missing,
expired or invalid bounds and future receipts. It also caps each bound at a policy maximum measured
from the receipt's completion, so no receipt can hold the promotion window open:

| Classes | Maximum `freshUntil` after completion |
| --- | --- |
| All nightly classes, `genuine-model-journey`, `update-rollback` | 7 days |
| `esri-bundle`, `cite` | 14 days |

A producer may set a shorter bound. A declared class with no maximum in the checker refuses. Nightly
evidence completes at minting, so a lock must start its burn within five days of minting to reach hour
48 before its nightly evidence expires. If qualifying evidence expires, obtain fresh workflow evidence
for the same lock; a newer lock never substitutes for it.

## Retained artifact contract and integration

The checker reads these paths under `--evidence-dir`:

| Path | Contents |
| --- | --- |
| `trains/<runId>/` | `gate-report.json`, selected `platform-lock.json`, Actions `run.json` |
| `canaries/<runId>/` | `live-canary-evidence.json` with `candidateLock.digest`, Actions `run.json` |
| `evidence/<class>/<runId>/` | Workflow-produced `receipt.json`, Actions `run.json` |
| `canary-sequence.json` | Complete canary ledger, including failed runs |

A class receipt repeats the record reference fields and adds `kind` and `freshUntil`. Journey receipts
add `cells` with the attempt ledger above. The update/rollback receipt adds top-level `updateStatus` and
`rollbackStatus`, `fromLockDigest` (retained lock), `toLockDigest` (selected candidate lock),
and `cells`, exactly one for each GA cell. The two lock digests must differ and
`toLockDigest` must equal the receipt's `lockDigest`. A first-release self-rollback does not qualify.
Each cell requires:

- `updateStatus` and `rollbackStatus`, both `pass`.
- `actuator`: `terraform-exact-apply` for ECS Redis-off, `honua-aws-ecs-alb` for the retained
  controller on ECS Redis-on, or `honua-gitops-aws-lambda` for either Lambda cell.
- Different immutable image `fromDigest` and `toDigest` values.
- `inFlightJob`: a nonempty `jobId`, `initialState: running`, and `terminalStates`
  containing exactly one of `succeeded`, `failed`, or `cancelled`. This records terminal observations;
  it does not alone prove exactly-once effects.
- `schemaBoundary`: `status: pass`, the observed candidate `schemaVersion`, and
  `imageDigest` equal to the rollback image `fromDigest`. `outcome` is `serving` or
  `refused-cleanly`; a clean refusal also requires its `reasonCode`.

The receipt shape is `#/$defs/updateRollbackReceipt` in
`certification/promotion-evidence.v1.schema.json`; promotion record entries remain artifact
references. The checker enforces cross-field equality/inequality and the four distinct cells,
which standard JSON Schema cannot express by comparing arbitrary sibling values.
Target image pins are resolved from the selected lock and the retained minting train's
`platform-manifest.yaml`; its bytes must match `sourceInputs.platformManifest.sha256` in that
lock. ECS `toDigest` is the generic server image digest, also checked against the lock's sole
server image artifact. Lambda `toDigest` is `awsLambdaEcrDigest`, the deployed mirror pin, not
the GHCR source digest. Missing or pending mirror pins refuse. Producers must include the shared
`runId`, `completedAt`, `status`, `kind` and `freshUntil` receipt envelope.

These requirements do not create evidence: release#483 still needs the actual controller,
Terraform update/rollback phase, in-flight GP observations, and two certified-lock cloud runs.
 Successful Actions metadata must match each recorded qualifying
completion time; nightly receipt production must fall within the minting run lifetime. These are retained workflow artifacts, never hand-minted summaries. The fetcher must
verify producer workflow identity, successful run metadata, artifact integrity and complete sequence
coverage before invoking the checker. Lock signature verification remains a separate mandatory gate.

`tools/check_promotion_readiness.py` emits `promotion-readiness.json` and refuses before publication
when any condition fails. Its `--lock` is the minting train's retained `platform-lock.json`, never
trunk's current lock. `--lock-history` is retained for caller compatibility and does not affect the
burn: repository lock history describes other candidates too.

`tools/fetch_promotion_evidence.py fetch` builds this layout from Actions. It checks each run against
Actions metadata: the minting train is a successful default-branch run of `release-train.yml` or the
scheduled `nightly-certification.yml`, and each recorded canary is a successful scheduled `demo-canary.yml`
run. It downloads the minting train's `certified-candidate` artifact, which carries the selected lock's
exact bytes, and each class receipt from the artifact `promotion-receipt-<class>` on the run the record
names. That run must be a successful default-branch run of the class's allowlisted producer
(`RECEIPT_PRODUCERS`): nightly classes come only from the minting workflows, and a class with no
allowlisted producer refuses, so qualifying classes refuse until their producers are registered. Each
artifact is extracted in isolation and only its expected file is kept (the whole candidate bundle for the
train), so artifact contents never replace the Actions `run.json`. It builds `canary-sequence.json` from every completed canary (scheduled or dispatched) since a day before
minting, reading each run's `candidateLock.digest`. A run that failed before binding a lock is recorded
with a null digest. An earlier failed attempt of a re-run canary is kept as an unattributed failure, so a
successful re-run cannot erase it. A missing receipt is left missing, and the checker refuses it.

The nightly report declares the eleven nightly class names in `evidenceClasses`, and all fifteen
classes in `evidenceDeclarations`. Each nightly declaration names its retained receipt and seven-day
`freshUntil`; qualifying declarations have a null receipt and freshness bound. The report retains the
nightly receipt payloads in `evidenceReceipts`. Minting checks every payload against the minted lock's
exact byte digest before signing. Missing, wrong-lock or failed evidence mints nothing; a deterministic
journey cannot stand in for the nightly genuine-model observation.

The train retains its pre-mint inputs as `qualified-candidate`. Only a successful mint uploads the
final `certified-candidate`, combining those exact manifest/matrix bytes with the signed minted lock
and report, and the eleven `promotion-receipt-<class>` artifacts containing `receipt.json`. Journey
receipts preserve the workflow's recorded attempt timestamps, driver and failure attribution.

**Producers remaining in #386/#381:** qualifying class producers must upload their receipts during burn, and
`demo-canary.yml` binds its evidence to trunk's committed `platform-lock.json` rather than the deployed
selected lock. Until they do, the checker refuses every record, so promotion fails closed. The fetcher
and checker do not certify a live burn or move a channel.

`request-promotion.yml` runs hourly. `fetch_promotion_evidence.py candidates` lists committed records
at or past hour 48 that have no published release and no promotion request that is pending or was made
in the last 24 hours. For each one, the workflow fetches the evidence and runs the same readiness
check. Only a passing record is dispatched to `promote.yml` as the scoped App identity, where readiness
is checked again behind the protected environment. A record file that is not a readable JSON object is
skipped with a warning rather than stopping the check of every other candidate. `promote.yml` verifies
the lock signature against the validated minting workflow: `gh attestation verify` for a
`release-train.yml` attestation, `cosign verify-blob` bound to `nightly-certification.yml` at the source
commit for a nightly mint.
The independent human approval and signature gates remain mandatory. `promote.yml` publishes the
minting train's exact signed lock and artifacts as `honua-2026.1.0` (then `honua-2026.1.Z`) and moves
npm, container, Helm and install-manifest channels. Only promotion may move those pointers.

Preview qualification is informational; failed or unknown cleanup, leaked run-owned resources and run cost overruns remain fatal. EKS teardown always executes and its independent workflow/aggregation checks retain the pre-amendment safety bar.

## Owner-approved patch profile (2026-10-17)

First mint stays on trusted trunk. Protect maintenance branches only after an accepted RC and before GA. A patch lock declares `profile: patch`, exact `changedComponents`, and `baselineLockDigest`; its promotion record repeats `profile: patch` and names the authenticated prior certified artifact in `baseline: {runId, lockDigest, releaseTag}`. The baseline must match the exact lock asset of an actual published GA/maintenance release, in the same major/minor series and with a strictly lower patch version (the first patch starts from `honua-2026.1.0`). The checker compares complete component identities, not merely version strings. A successful older nightly is insufficient. Pre-GA patch rehearsals are explicitly non-promotable diagnostics; no short-burn promotion is possible before GA. Require one strict train, at least 12 hours on the actually deployed selected lock, two consecutive scheduled six-hour canaries and mandatory actual AWS update/rollback. GA retains 48 hours and seven canaries. Human approval and App dispatch remain unchanged; promotion publishes the same bytes.

Required evidence classes are unchanged. Changed components need fresh evidence. The conservative initial reuse policy admits only `cite` (server) and the existing client compatibility bundle (server plus JS, .NET and Python SDKs): an evidence row declares the exact policy `reusedComponents`, and its original authenticated receipt carries `componentIdentities` equal to both signed locks. Shared fixtures/content digests must be identical, the receipt must bind the authenticated baseline, and the original completion/expiry and class maximum apply. No security, build, journey, rollback or arbitrary class is reusable. Missing producer allowlists still refuse; this patch does not invent live qualifying producers. Reuse cannot renew freshness.

The patch mint producer is not implemented by this consumer slice. An end-to-end non-promotable pre-GA patch rehearsal, baseline-certified artifact retention, deployed-lock canary producer binding, and maintenance branch protection remain execution obligations under #380/#386. Code tests do not perform them. Before first public RC, use two distinct immutable attested rehearsal builds for actual AWS transitions; GA still requires two distinct certified signed locks. A self-loop never qualifies.

**Current consumer limitation:** certificate reuse eligibility is not complete client-only or Helm-only patch promotion proof. The standing real-AWS rollback contract requires different server images in every GA cell. Patch rollback now binds every source image to the authenticated published baseline and every destination image to the selected lock; a self-image transition cannot qualify. Consequently unchanged-runtime patches are not promotable through this consumer today. Generalizing that transition proof and implementing the patch mint producer require separate reviewed work; this slice does not claim a generic operational patch path.

## Selected deployed-lock demo canary (initial ECS adapter)

The existing `demo-canary.yml` remains a daily monitor at 06:23 UTC. Without a selected record its envelope has no `candidateLock` and cannot satisfy a candidate burn. A governed repository variable, `DEMO_CANARY_PROMOTION_RECORD`, selects an already committed `certification/promotions/<label>.json` on protected trunk. While set, scheduled slots at 00:23, 12:23 and 18:23 UTC complete the six-hour cadence. Clearing the selection returns to daily monitoring. Manual runs remain diagnostic: promotion still requires the existing consecutive scheduled observations and failure ledger.

Bound mode obtains the record's exact `certified-candidate` from its successful trusted-trunk mint. It checks retained lock bytes against the record, verifies the minting workflow's signature/attestation and the existing derived-bundle contract, and passes that lock's server revision to the probes. It does not accept a supplied JSON lock or bind the current checkout's lock. The committed expected-GA fixture must still match the selected server revision; fixture drift refuses rather than rewriting expectations.

Activation requires operator-approved variables `DEMO_CANARY_READ_ROLE_ARN`, `DEMO_CANARY_REGION`, `DEMO_CANARY_CLUSTER`, `DEMO_CANARY_SERVICE`, `DEMO_CANARY_CONTAINER` and `DEMO_CANARY_ZONE`. The role must be restricted to read-only observation of the intended ECS service/tasks, ELB routing/target health and Route53 records, with OIDC trust restricted to this repository's protected trunk workflow. This code does not create the role, choose infrastructure identifiers, change variables or claim that this configuration exists.

The initial adapter requires a stable ECS service, one IP target group, direct public Route53 aliases to its active ALB and an HTTPS listener whose forwarding rules all select that group. Live endpoint DNS must match the ALB. Every healthy target must be a running service task; the named server container must report the selected immutable image index or its verified architecture child digest. Bound public and authenticated probes refuse redirects, preventing responses or admin headers from reaching another origin. The workflow repeats observation after the probes and refuses a changed deployment. Missing inputs, unsupported topology, incomplete observations, mismatched images or unavailable AWS reads fail closed. The envelope preserves passing, blocked and failed probe verdicts and records the observed deployment identity with the selected lock. A bound blocked/quarantined or failed observation also fails the workflow after writing that truthful envelope, so the complete burn ledger cannot erase the interruption after later green slots. Unbound daily diagnostics retain their existing behavior.

This ECS demo producer is burn monitoring, not acceptance of every AWS feature or cell. It cannot replace Lambda/Batch qualification, Redis-off serving/refusal proof, actual update/rollback, CITE/client compatibility receipts or the full AWS functional acceptance required before public RC. Existing demo failures remain owned by #475; this change claims no successful deployment, burn or promotion.
