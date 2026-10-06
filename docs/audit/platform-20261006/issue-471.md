# Issue 471 audit record

| Finding id | Outcome | Evidence |
|---|---|---|
| REL-001 | fixed | `test_rel_001_dependency_security_receipts_are_not_excluded_without_coverage` proves dependency-security check-runs are no longer excluded; `gate-security.yml` also scans both pinned server and console images. |
| REL-002 | fixed | `test_rel_002_image_gates_bind_scans_to_manifest_digest` proves security scans bind to each architecture's digest and SBOM generation binds to the manifest index digest rather than a mutable tag. |
| REL-003 | fixed | `test_rel_003_promotion_passes_minting_time_to_freshness_check` proves promotion supplies the trusted train completion time for the freshness decision; candidate completeness and all-pass validation remain enabled. |
| REL-004 | fixed | `test_rel_004_security_findings_is_a_required_train_gate` proves the findings workflow is wired into the train and required receipt set; `test_security_gate_verifies_each_fixed_row_against_its_merged_pr` proves open findings make the gate fail. |
| REL-005 | fixed | `test_incompatible_forward_schema_never_claims_rollback` proves a known-incompatible forward schema produces no provider mutations. |
