# First release cut checklist

The 2026.1 cut follows continuous certification on trunk, as adopted by
[the canonical plan](https://github.com/honua-io/agent-delivery-spec/tree/trunk/.specifica/2026-1-release-plan-ai-cloud-to-maps/)
(honua-release#376, rulings R18–R21). An all-green nightly strict train mints a signed lock. The release
captain selects a lock to burn and qualify; promotion publishes that lock and moves the public channels.
There is no freeze train or hand re-pin cycle. Gates can fail and cannot be overridden.

The M1 developer preview is not a cut. M2 requires the first certified lock `2026.1-rc.3` and the
genuine-model journey on all four GA cells against a certified lock. M3 promotes rc.N with signed
rc.3 → rc.N → rc.3 update and rollback proof. Read `origin/trunk` or immutable revisions, never a shared
checkout, when checking sources. The manifest and compatibility matrix describe each lock's exact scope.

## Roles and evidence

| Actor | Responsibility |
| --- | --- |
| Release captain | Selects the promotion candidate and requests promotion; records the selected digest and evidence. |
| Release operator | Performs protected-environment approvals and authorized credentialed publication operations; resolves operator rulings and access blockers. |
| Release engineer | Reviews lock, manifest, matrix, immutable workflow receipts and release records. |
| Release CI | Resolves trunk inputs, produces evidence, runs failable gates, signs green locks and executes approved promotion. |
| Component maintainers | Land fixes on trunk and provide immutable public package/image artifacts and evidence producers. |

Attach workflow URLs, immutable digests, source SHAs and retained receipts to the release record.
Missing, blocked, skipped, cancelled, stale or wrong-lock evidence is not a pass. No record or approval
can override a gate.

## 1. Make the nightly strict train self-contained

- [ ] Resolve component `origin/trunk` heads, including the imaged server/Console revisions and published
  client artifact identities. Verify trunk reachability and immutable package/image provenance.
- [ ] Produce the matching manifest and matrix within the train. Establish live contract/schema facts
  from the exact booted server image; verify published client bytes. Do not reuse snapshot-derived facts.
- [ ] Resolve the Lambda x86_64 image and actual registry-specific ECR mirror digest. Compare source/ECR
  config and rootfs. A floating tag, pending mirror sentinel or another revision's digest fails.
- [ ] Produce every nightly evidence class: build/test, contract, SBOM, security, upgrade, capacity soak,
  DR, Lambda certification, protocol ledger and journey evidence. Declare consumed classes as nightly
  or qualifying. No hand-supplied receipt URL or pin is part of the scheduled train.
- [ ] Bind protocol requirements, producer revisions and ledger digest to this train's candidate through
  the producer workflow; do not hand re-pin the ledger after a cut.
- [ ] Run the deterministic journey on {All-ECS, Lambda + Batch} × {Redis off, on}, plus the genuine model
  on ECS with Redis off. Each required cell passes within two recorded attempts; every failure is
  attributed to model or infrastructure. Preview mixed/EKS cells report informationally and cannot
  substitute for a GA cell. Enforce the cost ceiling before teardown; overruns fail and tear down.
- [ ] Run all strict automated gates. A green night mints and signs `2026.1-rc.N`; a red night mints
  nothing. Retain the exact lock, manifest, matrix, full gate report and workflow provenance.

The live Lambda proof must include exact-digest deploy/invoke/verify/teardown, true ECR digest,
asserted fixture row count, staging write round-trip, authorization denial and cold-start duration.
Use the isolated staging target. Health-only evidence cannot discharge
[release#282](https://github.com/honua-io/honua-release/issues/282); see the
[Lambda GA bill ledger](2026.1-lambda-ga-bill.md) and
[serverless operating limits](2026.1-operating-envelope.md#5-aws-lambda-serverless-supported-target-and-limits).

**Upgrade baseline ruling:** the operator must settle the prior-release lookup, which currently finds
the 2026-08-20 `honua-2026.1` engineering snapshot. Do not assert that no prior release exists. Either
exclude pre-release snapshots by a recorded ruling or certify against that baseline. Same-image
lifecycle coverage alone cannot replace the required two-revision application proof below.

## 2. Select, burn and qualify a promotion candidate

- [ ] Select a signed certified lock rc.N and verify its signature and minting strict train. Deploy its
  exact artifacts to the demo. Record its digest, deployment/burn-start UTC time and minting run in
  `certification/promotions/<rc-label>.json`; use the retained lock bytes for readiness.
- [ ] Burn that lock for at least 48 hours while trunk continues moving. New commits and newer locks
  never reset it. A failure of this lock ends the burn; diagnose and redeploy before starting another.
- [ ] Collect seven consecutive passing six-hour canaries bound to this digest. Retain every observed
  run, including failures, so readiness cannot omit a failure. Continue canaries until promotion; the
  latest seven must meet cadence and freshness bounds.
- [ ] During the burn, run the genuine-model journey on all four GA cells with the same complete
  two-attempt ledger and failure attribution rule. Check the cost ceiling before teardown.
- [ ] Prove signed rc.3 → rc.N → rc.3 application update and rollback on all four cells using the existing
  ECS and Lambda deploy backends and approved preflight/apply/rollback actuators. Keep a GP job in flight
  and prove it finishes exactly once, rc.3 serves on rc.N's schema or refuses cleanly, and no
  version-skew corruption occurs. Verify restored data, client import/publish/query, serving health,
  graceful draining and cleanup. Same-version traffic shifting or a manual alias flip is insufficient.
- [ ] For Lambda, attach the exact lock, server SHA, x86_64 source/ECR digests, run URL/times,
  function/alias and before/after/rollback versions, executed serving assertions and teardown inventory
  and outcomes. Re-read contracts from the booted candidate. A source-built substitute or health-only
  receipt cannot discharge the bill's application upgrade/rollback items.
- [ ] Produce fresh Esri and CITE evidence against this lock during the burn, with immutable provenance
  and bounds of at most 14 days. Every qualifying class is declared and bound to this digest; no class
  is carried forward from another lock or hand-minted outside a workflow.
- [ ] Verify Lambda GA qualification consistency. Preparation uses `ga-target` / `pending`; supported
  status requires the full passed bill and a real HTTPS `qualificationReceipt` bound by
  `qualification_candidate_digest` to the exact manifest. A receipt reference alone is insufficient.

The artifact layout and attempt-ledger contract are in [BURN-IN-PROMOTION.md](BURN-IN-PROMOTION.md).
The promotion fetcher/producers still need the integration described there under #386/#381; missing
artifacts fail closed. Documentation and checker tests do not substitute for live proof.

## 3. Check readiness and publish

- [ ] Run `tools/check_promotion_readiness.py` against the committed record, selected retained lock and
  verified immutable evidence. Require the minting train, 48-hour burn, seven canaries, nightly journey
  tier, qualifying journey tier, declarations, bindings and unexpired evidence. Later strict trains
  certify their own locks and are not prerequisites. Promotion remains open after hour 72 while all
  evidence stays within its freshness bound.
- [ ] Pass the GA documentation gates ([release#379](https://github.com/honua-io/honua-release/issues/379))
  and regenerate the release decision record with Decision: GO. Record the operator's disposition of
  every server security-review finding and the measured AWS spending ceilings.
- [ ] Rehearse fix-forward patching before GA ([release#380](https://github.com/honua-io/honua-release/issues/380)):
  fix on trunk → next certified lock → 12-hour patch burn and two canaries → `2026.1.1` promotion →
  rollback to the previous promoted lock. `release/2026.1` is a recorded Sev1 break-glass path only.
- [ ] Request promotion through the scoped App workflow and obtain independent protected-environment
  approval. Verify the lock signature and publication tag against the minting train's exact source.
- [ ] Promote `honua-2026.1.0`, publishing the selected lock's exact bytes and provenance. Only promotion
  moves npm `latest` / `2026.1`, container `:stable` / `:2026.1`, Helm and customer-install-manifest
  channels; each pointer resolves to immutable artifacts. No rebuild or unsigned update channel ships.
- [ ] Archive the readiness decision, lock, signatures, full receipts, approval and publication URLs.
  Preserve the previous promoted lock as rollback target and the next cut's upgrade baseline.

## Operating safeguards

Identity comes first: verify the running revision before accepting any live observation. Build and full
CI ordering must respect runner capacity; orchestration owns that sequence. Engineers prepare and
verify records; authorized workflows and operators perform credentialed operations. Never place
credentials in this repository. No skipped gate, stale receipt, manual pin cycle or gate override can
turn an incomplete lock into a promotion candidate.
