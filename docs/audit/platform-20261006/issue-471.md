# Issue 471 audit record

| Finding id | Outcome | Evidence |
|---|---|---|
| REL-001 | fixed | `test_rel_001_dependency_security_receipts_are_not_excluded_without_coverage` proves dependency-security check-runs are no longer excluded; `gate-security.yml` also scans both pinned server and console images. |
| REL-002 | fixed | `test_image_resolvers_use_child_digest_instead_of_tag_or_index` executes both gates' resolvers against distinct index/child digests and a mutable tag. Security scans cover server/console amd64/arm64; server SBOMs cover both child digests. `test_sbom_scans_each_exact_child_with_explicit_platform` executes the scanner command boundary for both architectures. |
| REL-003 | fixed | `test_finalize_after_sixty_hour_burn_preserves_freshness_and_findings_policy` executes finalization after a 60-hour burn: a fresh-at-minting report passes, while stale-at-minting or failed/blocked findings receipts still refuse without writing release files. |
| REL-004 | fixed | `test_rel_004_security_findings_is_a_required_train_gate` proves the findings workflow is wired into the train and required receipt set; `test_security_gate_verifies_each_fixed_row_against_its_merged_pr` proves open findings make the gate fail. |
| REL-005 | fixed | `test_incompatible_forward_schema_never_claims_rollback` proves a known-incompatible forward schema produces no provider mutations. |

The followup review reproduced a false green from a passing subset of security scan fragments.
`test_scan_reports_cannot_pass_partial_duplicate_or_failed_coverage` executes the workflow assembly
under strict and bootstrap enforcement, removing each required fragment, duplicating/substituting
receipts, supplying an empty set, and failing each producer. Both gates require complete receipt
sets and successful producers. The findings gate's declared read permission is also propagated
through both train callers; `test_findings_permissions_are_available_through_both_train_callers`
checks that reusable workflow contract.

The [security-findings job on the previous PR head](https://github.com/honua-io/honua-release/actions/runs/37674354578/job/112973887095)
correctly refused with `GA-blocking security findings remain open`. All ten public decision rows
remain open: SEC-4, SEC-5, SEC-9, SEC-13, SEC-14, SEC-16, SEC-18, SEC-21, SEC-23, SEC-28.
Each row identifies `honua-io/honua-server` as implementation owner. Those owners must complete
the repairs; the [release decision owner](https://github.com/honua-io/honua-release/issues/274)
must reconcile each row with its merged default-branch fix PR citing the SEC-N id. No per-finding
assignee is recorded in these public rows. The refusal remains required while any row is open;
this repair does not change findings, credentials, or enforcement.

Nonblocking P2: [QGIS fallback observation provenance](https://github.com/honua-io/honua-release/issues/477).
An API rate-limit denial followed by a public-page 404 is currently attributed to the API URL.
The QGIS row remains blocked-on-operator; correcting that observation source is a separate repair.

Local verification on 2026-10-08: `python3 -m pytest tools/ e2e/test_licensing.py
e2e/test_upgrade_chaos.py e2e/test_cloud.py -q` passed all 3,071 tests with no skips or failures
(two existing tarfile deprecation warnings), under shared build slot 3. The 115 focused workflow,
binding and finalizer tests also passed. Actionlint passed for every changed workflow, and
`git diff --check` passed. Independent review found no remaining supported P0/P1 implementation
findings. The live findings verifier still exits 1 for the open SEC-N rows listed above.
