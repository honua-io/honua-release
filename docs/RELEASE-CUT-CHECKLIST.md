# First release cut checklist

This is the maintained runbook for cutting **the first Honua platform release**.
`2026.1` has not shipped: there is no prior `honua-*` platform release to
upgrade from. The upgrade gate's first-release ruling is therefore applicable
only to this cut; every later cut must certify an upgrade from the preceding
release.

Use `origin/trunk` (not a shared working tree) when selecting source commits.
The platform manifest and compatibility matrix are the release source of truth.
Do not claim a pass from an unpinned artifact, a floating branch, or a skipped
strict-release gate.

## Roles and evidence

| Actor | Responsibility |
| --- | --- |
| **Release operator (operator-only)** | Performs credentialed dispatches, registry/ECR operations, protected-environment approvals, and the final promotion/tag action. Records run URLs and immutable digests; never places credentials in this repository. |
| Release engineer | Prepares and reviews the manifest, matrix, evidence pins, and release records. |
| Release CI | Runs the train and its fail-closed gates; it decides gate outcomes. |
| Component maintainers | Merge release-path fixes and publish the server image or other component artifacts. |

For every checklist item, attach the named verification to the release record
(run URL, command output, immutable SHA/digest, or published-artifact receipt).
`BLOCKED`, `skipped`, cancelled, or stale evidence is not a release pass under
strict enforcement.

## 1. Establish the cut

| Done | Actor | Input | Action | Verification |
| --- | --- | --- | --- | --- |
| [ ] | Release engineer; server/IaC/DevOps owners | [Lambda ruling A, release#282](https://github.com/honua-io/honua-release/issues/282), bill items 1, 2 and 5 | Before candidate cut, attach a green live Lambda x86_64 trunk-digest certification: exact-digest deploy/invoke/verify/teardown and true ECR digest; asserted fixture row count, staging write round-trip, authorization denial and cold-start duration. Verify GA-target scope and pending qualification alignment across matrix/docs/CLI/contract and the [serverless limits](2026.1-operating-envelope.md#5-aws-lambda-serverless-supported-target-and-limits). | Missing, skipped, stale, wrong-digest or health-only evidence **blocks candidate cut**. Writes use the isolated staging target, never the customer-facing demo. The release documentation PR alone does not discharge the server, IaC or DevOps bills. |
| [ ] | Release engineer | Open release-path issues/PRs, `origin/trunk`, current `platform-manifest.yaml` | Confirm that all required fixes are merged, or explicitly decide a surface is out of scope before selecting the cut. Select only trunk-reachable revisions. | Record each selected full SHA and the decision; `tools/trunk_reachability.py` accepts every manifest pin. |
| [ ] | Release engineer | Candidate server SHA from `origin/trunk`; component CI and package/image receipts | Select the server and component pins: a usable server pin has the required green CI and a published immutable image bound to that SHA. | The selected SHA is `origin/trunk`-reachable; check runs are terminal and green except for documented non-product governance exceptions; image provenance identifies the selected SHA. |
| [ ] | Release engineer | Candidate SHA, manifest, compatibility matrix, current UTC time | Start a cut-specific record, set the candidate identity consistently in the manifest, and set `protocolCertification.candidateCutAt` to the actual time this candidate is frozen. Do not reuse a hand-snapshot's timestamp, derived values, or evidence. | `candidate.ref`, `components.honua-server.sha`, and `protocolCertification.serverCertificationProducerSha` agree; `candidateCutAt` records this cut (not the working snapshot); `python tools/validate_platform.py --exact-candidate` passes. |

## 2. Run the batched server re-pin cycle

Each cycle costs about two hours. Batch all required changes into one cycle;
do not start a second cycle for individual follow-ups.

| Done | Actor | Input | Action | Verification |
| --- | --- | --- | --- | --- |
| [ ] | **Release operator (operator-only)** | Selected `origin/trunk` server SHA | Dispatch `nightly-container-build` for that exact SHA **alone**. Never run it concurrently with `ci.yml`; concurrent dispatches starve runners and invalidate the timing signal. | The build run is terminal/successful and publishes the immutable multi-architecture server image and Lambda image/digests for the selected SHA. |
| [ ] | **Release operator (operator-only)** | Successful image-build run; same server SHA | Only after the image build completes, dispatch `ci.yml` with `full_ci=true` for the same SHA. | The full-matrix run has the selected `head_sha`, completes after the image build, and all required product lanes are green. A cancelled run is diagnosed from `startedAt`/`completedAt`, not treated as the known cancel cascade. |
| [ ] | Release engineer | Successful build/CI receipts; image and Lambda digest receipts | Re-pin `platform-manifest.yaml` to the exact server SHA, immutable server image/digest, `awsLambdaImage`, and `awsLambdaDigest`. Re-pin the matching `compatibility-matrix.yaml` values **in the same change**, including `deploysServerImage` and `appVersion`. | `python tools/validate_platform.py --exact-candidate` passes; the matrix and manifest name the same image/version; the train resolves `image@digest`, not a floating tag. |
| [ ] | **Release operator (operator-only)** | Newly pinned Lambda source image/digest and release-account ECR access | Perform the ECR mirror bootstrap. On a new server re-pin, set `awsLambdaEcrDigest` to the literal `pending-ecr-mirror`; mirror the exact Lambda candidate, compare source and ECR config/rootfs, then record ECR's returned registry-specific digest and rerun. Never carry forward the previous pin's ECR digest. | The bootstrap proves source/ECR config equality; the final manifest contains ECR's exact `sha256:` digest (not the sentinel) and `e2e-cloud-aws.yml` passes with `HONUA_AWS_ROLE_ARN` configured. |

## 3. Rebind cut-specific facts and artifacts

| Done | Actor | Input | Action | Verification |
| --- | --- | --- | --- | --- |
| [ ] | Release engineer | Certified, bootable server `image@digest`; live capabilities endpoint | Re-read `components.honua-server.contractVersions` and derived server facts (including database-schema evidence) from the running certified image. Replace all carried-forward working-snapshot values with this cut's live observations. | Capture the endpoint response and image identity; the observed contract versions match the manifest and compatibility matrix. |
| [ ] | Release engineer | Registry publication receipts for every required SDK/MCP/package; source revisions | Refresh every required `clientArtifacts` entry from the **published bytes**: version, immutable integrity/digest, publication state, and source SHA. Do not substitute local builds or merely advance a source pin. | `python tools/verify_client_artifacts.py` passes against the candidate manifest; required artifacts are published/promoted and their recorded bytes match the registry receipt. |
| [ ] | Component maintainer | Newly pinned server image/digest; honua-esri-compat test environment | Regenerate the Esri certification bundle against this exact candidate at cut time and commit it under the candidate's certified evidence path. | Bundle provenance names the candidate image/digest and its required lanes pass. |
| [ ] | Release engineer | Committed Esri certified bundle SHA; `certification/conformance-evidence.yaml`; manifest evidence source | Rebind `esri.evidenceRef` (and its canonical evidence-source pin where applicable) to the newly committed bundle. This is an **AT-CUT** action, not evidence that may be carried forward. | The conformance binding check passes and rejects a deliberately wrong-image bundle; the evidence ref is an immutable full SHA. |
| [ ] | Release engineer | Current certification/evidence producers and exact candidate | Re-aggregate and bind the protocol-certification ledger only in its required order: candidate manifest change is merged, evidence aggregation is produced, then its immutable commit, requirements source revision, and digest are re-pinned. | `tools/convergence_rebind.py` post-merge instructions complete; the ledger is `bound` to the candidate and the certification gate passes. |

## 4. Certify and promote the first release

| Done | Actor | Input | Action | Verification |
| --- | --- | --- | --- | --- |
| [ ] | Release CI | Exact manifest/matrix and all immutable receipts | Run the live release train under strict enforcement. Investigate a gate result rather than changing timeouts or treating an environment failure as a pass. | Every required gate reports `pass` for the exact candidate; no strict gate is blocked, skipped, cancelled, stale, or bound to another SHA. |
| [ ] | Release engineer | Strict train receipt; prior-release lookup | Apply the first-release ruling: record that no prior `honua-*` platform release exists, so no cross-release upgrade path exists yet. The strict train still runs the required same-image lifecycle coverage plus the two-revision ECS and Lambda alias upgrade/rollback proofs below. | `gate-upgrade` reports the self-limiting first-release basis and the strict train has no skipped/blocked upgrade result. |
| [ ] | Release CI; cloud owners | ECS certification receipt; two candidate revisions and populated database | Attach live apply/handoff/serving-smoke/destroy evidence and RC-n → RC-n+1 → RC-n application upgrade/rollback, including migrations reversed or declared forward-only, restore/data readability, supported-client import/publish/query, health and graceful draining (2026-09-05 ruling b; release#65/#129). | Same-revision traffic shifting proves mechanics only. Missing, stale, wrong-candidate or skipped required cells **block the GA cut/promotion**; N-1 from published 2026.1 is a later-release gate. |
| [ ] | Release CI; server/IaC/DevOps owners | **Lambda certification receipt**, exact candidate lock digest and source/ECR image identities; standing certification function and alias | Discharge [release#282](https://github.com/honua-io/honua-release/issues/282) items 3 and 4: use `AwsLambdaGitOpsDeployBackend` for alias-shift application upgrade and rollback of the exact candidate, with serving verification before/after recovery, then tear down everything the lane created. Re-pin `awsLambdaEcrDigest` from the real mirror and re-read `contractVersions` from the booted candidate. | Receipt must identify candidate lock/digest, server SHA, x86_64 source and ECR digests, run URL/times, function/alias and before/after/rollback versions, executed serving assertions and cleanup inventory/outcomes. A manual alias flip, same-version shift, source-built substitute or health-only probe is insufficient. Missing, skipped, stale or wrong-candidate receipt, pending ECR sentinel, unverified contracts or failed teardown **blocks the GA cut/promotion**. Re-run after any candidate-affecting change; pre-cut trunk evidence cannot substitute for this receipt. |
| [ ] | Release engineer | [Contract §4.2](https://github.com/honua-io/honua-flow/blob/trunk/docs/2026.1-quality-contract.md#42-the-gate-graph-is-red-or-can-produce-false-green); full Lambda bill receipt | Keep `status: ga-target` / `qualification: pending` during preparation. After the bill passes, set `status: supported`, `qualification: passed`, and `qualificationReceipt` with the real HTTPS `url` and `candidateManifestDigest` computed by `qualification_candidate_digest` from the exact manifest (see [binding rule](2026.1-operating-envelope.md#5-aws-lambda-serverless-supported-target-and-limits)). | Ordinary validation rejects supported + pending or missing/wrong-candidate receipt references; `--exact-candidate` also rejects pending GA targets. The reference binds every manifest pin; changing any candidate input requires fresh proof and rebinding. A reference alone does not discharge the live assertions above. |
| [ ] | **Release operator (operator-only)** | Passing strict train, protected approval evidence, finalized manifest and release notes | Request/obtain the required protected-environment approval, then promote and create the first immutable release/tag through the approved workflow. | Promotion receipt names the certified manifest/matrix and release identity; tag, release notes, BOM/provenance, and published artifacts resolve to the exact recorded bytes. |
| [ ] | Release engineer | Release/tag URL and receipts | Archive the complete cut record and open follow-up work for any non-release observations. Do not leave a known gate defect as an undocumented exception. | The release record links every verification above; the next cut has this released manifest as its upgrade baseline. |

## Operating safeguards

- **Actor boundary:** Only the release operator performs credentialed dispatch,
  ECR mirroring, protected approvals, publishing, or tag/promotion actions.
  Engineers may prepare and verify records, but cannot override a CI gate.
- **Source freshness:** Read `origin/trunk:<path>` or an immutable SHA. Shared
  worktrees can be stale and are not evidence of the selected cut.
- **Workflow ordering:** `nightly-container-build` and `ci.yml` must never
  overlap. The build precedes full CI, and the manifest plus matrix re-pin
  remains atomic.
- **Identity first:** Check the running candidate's advertised revision before
  accepting live SLO, capabilities, contract, or conformance observations.
- **No stale carry-forward:** Contract versions, client package bytes, Esri
  evidence, ECR digest, and certification ledger are all re-established for
  the selected cut in their ordering above.
