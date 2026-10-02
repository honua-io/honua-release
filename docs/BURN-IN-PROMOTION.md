# Burn-in promotion evidence

Promotion publishes the exact bytes of a signed, certified platform lock. It does not rebuild the
candidate. The governing requirements are [the canonical 2026.1 plan](https://github.com/honua-io/agent-delivery-spec/tree/trunk/.specifica/2026-1-release-plan-ai-cloud-to-maps/),
rulings R18–R21, and [the cut checklist](RELEASE-CUT-CHECKLIST.md). Gates can fail and cannot be overridden.

## Select a lock and start its burn

An all-green nightly strict train resolves component `origin/trunk` heads, produces its own nightly
evidence and mints a signed `2026.1-rc.N` lock. Its journey requirement is the deterministic driver on
all four GA cells, plus the genuine model on ECS with Redis off. A red night mints nothing.

The release captain selects one certified lock as the promotion candidate, deploys its exact artifacts
to the demo and records its byte digest, minting run ID, burn-start commit and UTC deployment time in
`certification/promotions/<rc-label>.json`. The record follows
[`promotion-evidence.v1.schema.json`](../certification/promotion-evidence.v1.schema.json).
The lock bytes supplied to the checker must be the selected lock's retained artifact, even when the
repository's current `platform-lock.json` names a newer lock.

The burn belongs to that digest. New trunk commits, newer nightly locks and failures of another lock
do not reset it. A failure of the burning lock ends that burn; diagnose it and record a new deployment
and burn before trying again. Never hide a failed observation by selecting only passing run IDs.

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
| Nightly | `build-test`, `contract`, `sbom`, `security`, `upgrade`, `capacity-soak`, `dr`, `lambda-certification`, `protocol-ledger`, `deterministic-journey`, `nightly-model-journey` |
| Qualifying | `genuine-model-journey`, `update-rollback`, `esri-bundle`, `cite` |

Nightly classes come from the minting train and appear in its report's `evidenceClasses` array.
Qualifying classes are produced against the selected lock during its burn. Every retained receipt must
bind the selected digest; evidence from another lock cannot be carried forward. Additional consumed
classes require an explicit tier declaration and a retained receipt too.

GA cells are `aws-ecs/redis-off`, `aws-ecs/redis-on`, `aws-serverless/redis-off` and `aws-serverless/redis-on`:

- `deterministic-journey`: deterministic passes on all four GA cells.
- `nightly-model-journey`: a genuine-model pass on ECS with Redis off.
- `genuine-model-journey`: genuine-model passes on all four GA cells during the burn.
- `update-rollback`: passing update and rollback proof covering all four GA cells, using signed
  rc.3 → rc.N → rc.3 locks, a GP job in flight that finishes exactly once, and the schema rollback boundary.

Each required journey cell passes within two attempts. A cell records `mode`, `attemptCount` and the
complete ordered `attempts` array. Every attempt records its consecutive number, completion time,
status and lock digest. A first-attempt failure must have `failureAttribution: model` or
`failureAttribution: infrastructure`; the last attempt must pass. Skipped, missing, out-of-period or
wrong-lock attempts fail. Mixed ECS + Batch and EKS remain informational Preview cells: their failures
cannot block the GA cells, and their passes cannot replace GA evidence. The journey producers enforce
the per-run cost ceiling before teardown; a run over the ceiling fails and tears down.

## Canary coverage and promotion time

The burn lasts at least 48 hours. `demoCanaries` names the latest seven consecutive passing scheduled
six-hour canaries for this lock. Each has a distinct run ID and the same lock digest. Intervals must be
5h30m–6h30m, allowing schedule jitter without accepting a missing slot. The latest canary must be no
more than 6h30m old. Continue canaries after hour 48 until promotion.

The fetcher also supplies `canary-sequence.json`, a complete ledger of observed canary runs from burn
start through evaluation, including failures and lock bindings. Its shape is
`{"lockDigest":"sha256:…","runs":[…]}`; each run uses the same fields as a `demoCanaries` entry.
The selected seven must be the final seven observations for this lock. Any failed or incomplete
observation of this lock ends the burn, even if seven later canaries pass.

Promotion is allowed from hour 48 until any evidence passes its freshness bound. There is no
72-hour deadline and no requirement for later strict trains against newer trunk heads. Every retained
evidence receipt supplies a workflow-produced `freshUntil` UTC timestamp. The checker refuses missing,
expired or invalid bounds, future receipts, and Esri/CITE bounds longer than 14 days. These bounds come
from the producing workflow's gate policy; the promotion record cannot extend them. If evidence expires,
obtain fresh workflow evidence for the same lock; a newer lock never substitutes for it.

## Retained artifact contract and integration

The checker reads these paths under `--evidence-dir`:

| Path | Contents |
| --- | --- |
| `trains/<runId>/` | `gate-report.json`, selected `platform-lock.json`, Actions `run.json` |
| `canaries/<runId>/` | `live-canary-evidence.json` with `candidateLock.digest`, Actions `run.json` |
| `evidence/<class>/<runId>/` | Workflow-produced `receipt.json`, Actions `run.json` |
| `canary-sequence.json` | Complete canary ledger, including failed runs |

A class receipt repeats the record reference fields and adds `kind` and `freshUntil`. Journey receipts
add `cells` with the attempt ledger above. The update/rollback receipt adds `cells` (the four GA cell
names), `updateStatus` and `rollbackStatus`. Successful Actions metadata must match each recorded
completion time. These are retained workflow artifacts, never hand-minted summaries. The fetcher must
verify producer workflow identity, successful run metadata, artifact integrity and complete sequence
coverage before invoking the checker. Lock signature verification remains a separate mandatory gate.

`tools/check_promotion_readiness.py` emits `promotion-readiness.json` and refuses before publication
when any condition fails. `--lock-history` is retained for caller compatibility and does not affect the
burn: repository lock history describes other candidates too.

**Integration remaining in #386/#381:** the promotion fetcher and producers must supply the class
receipts and complete canary ledger above, resolve the selected retained lock instead of the current
trunk lock, and accept scheduled minting runs. The existing workflow still fetches the older evidence
layout; until it is updated, missing evidence makes the checker refuse. This checker/docs change does
not certify a live burn or move a channel.

After readiness passes, `request-promotion.yml` requests protected promotion as the scoped App identity.
The independent human approval and signature gates remain mandatory. `promote.yml` publishes the
minting train's exact signed lock and artifacts as `honua-2026.1.0` (then `honua-2026.1.Z`) and moves
npm, container, Helm and install-manifest channels. Only promotion may move those pointers.
