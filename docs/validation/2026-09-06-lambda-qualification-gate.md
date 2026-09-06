# PR #290 Lambda qualification gate validation

This supersedes the qualification-gate observations in
[the original promotion validation](2026-09-06-lambda-ga-promotion.md).
The branch was rebased onto trunk `ffc92bc`; trunk's generated decision files
were retained, all five Lambda bill inputs and ruling-A overrides were preserved,
and `python3 tools/release_decision_record.py --refresh --apply` refreshed the
retained inventory. Trunk's compatibility receipts remain intact.

Lambda is **GA target, qualification pending**. Ordinary validation permits
preparation. A `supported` architecture requires `qualification: passed`, an
HTTPS `qualificationReceipt.url`, and `candidateManifestDigest` matching the
canonical parsed manifest. The binding covers every manifest field, including
Lambda source/ECR digests and IaC revision. Exact-candidate validation also
rejects an unqualified GA target, so the existing live release-train freeze
cannot bypass the pending declaration. The cut checklist uses the same rule.

No artifact digest or execution receipt was invented. Unit-test receipt references
are explicitly synthetic. The gate validates a receipt reference and its candidate
binding; the full serving, authorization, cold-start, two-revision backend-driven
upgrade/rollback and teardown evidence bill remains the responsibility of the
owning live lanes in release#282. This change does not certify Lambda.

Focused regression coverage in `tools/test_deploy_qualification.py`:

- `test_supported_without_passed_qualification_rejected` (pending, failed, fabricated, absent)
- `test_ga_target_pending_accepted`
- `test_supported_passed_receipt_accepted`
- `test_supported_without_receipt_rejected`
- `test_supported_wrong_candidate_receipt_rejected`
- `test_supported_invalid_receipt_reference_rejected`
- `test_exact_candidate_pending_ga_target_rejected`
- `test_exact_candidate_passed_ga_target_clears_qualification_gate`
- `test_receipt_binding_covers_other_candidate_components`

The existing committed-pin test still checks all exact pin rules. Full-validator
coverage separately asserts that the committed pending deployment cannot qualify.

```text
$ python3 -m pytest tools/test_deploy_qualification.py tools/test_platform.py -q
67 passed in 1.80s

$ python3 tools/validate_platform.py --baseline origin/trunk
OK    platform manifest + compatibility matrix valid (structure + coherence + drift)

$ python3 tools/generate_compatibility_table.py docs/platform-lock.v1.draft.yaml --check-output
PASS: documentation matches lock

$ python3 tools/validate_compatibility_ledger.py compatibility-ledger.v1.yaml
PASS: compatibility-ledger.v1.yaml is a coherent compatibility-ledger.v1

$ python3 tools/check_evidence_map.py --self-test
== evidence-map self-test — PASS (3 schema defects, 1 shape defect, 1 invented packet and 2 packet-split defects rejected)

$ python3 tools/check_evidence_map.py
== evidence-map gate — PASS (104 rows validated against schemas/2026.1-evidence-map.schema.json; part1=62, part2=42)

$ python3 tools/check_action_pins.py
All external GitHub Actions references are immutable and consistently version-documented.

$ python3 certification/validate-protocol-requirements.py
Validated 2123 complete, unique protocol certification cells.

$ python3 tools/validate_platform.py --exact-candidate
SKIP  trunk-reachability checks unavailable offline (not running in CI and GITHUB_TOKEN is unset)
ERROR exact-candidate: deploy.honua-server.awsLambda.architectures.x86_64: GA target, qualification pending; requires supported + passed + candidate-bound qualificationReceipt

FAILED: 1 error(s), 0 warning(s)
```

The last command's exit 1 is the expected fail-closed result, not release qualification.
JSON Schema validation passes for the platform manifest. Added/changed relative
Markdown links and anchors resolve; all seven distinct immutable IaC source
file/line references in the Lambda section exist at the shipped revision.
`git diff --check` passes. Parsed manifest data equals trunk; only comments change.

The first full suite attempt in the restricted sandbox reported
`1 failed, 752 passed, 2 warnings in 135.47s`: the existing release-inspect HTTP
server test could not create a loopback socket (`PermissionError: [Errno 1]`).
The same full suite was rerun with socket access; no test was skipped or weakened.

```text
$ python3 -m pytest tools -q
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 28%]
........................................................................ [ 38%]
........................................................................ [ 47%]
........................................................................ [ 57%]
........................................................................ [ 66%]
........................................................................ [ 76%]
........................................................................ [ 86%]
........................................................................ [ 95%]
.................................                                        [100%]
=============================== warnings summary ===============================
tools/test_contract_surface.py::test_extract_js_reads_entry_points_from_the_committed_package_json
tools/test_contract_surface.py::test_extract_js_falls_back_when_no_entry_point_resolves
  /usr/lib/python3.12/tarfile.py:2301: DeprecationWarning: Python 3.14 will, by default, filter extracted tar archives and reject files or modify their metadata. Use the filter argument to control this behavior.
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
753 passed, 2 warnings in 128.15s (0:02:08)
```
