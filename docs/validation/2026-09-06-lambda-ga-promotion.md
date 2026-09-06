# Lambda GA documentation validation — 2026-09-06

Scope: release#282 ruling A, documentation/data only. The decision generator
changes only its rendered scope prose; classification logic is unchanged.
No AWS access, deployment, candidate certification or artifact re-pin occurred.

The full tools suite completed with one sandbox-only socket failure. The test
creates an HTTP fixture on `127.0.0.1`; sandbox socket creation returned
`PermissionError: [Errno 1] Operation not permitted`. Re-running that exact test
with local socket access passed. Thus all 724 collected tests passed across the
full run and targeted rerun; this is not a claim of one wholly green full run.
The two warnings are existing Python tarfile deprecation warnings.

```text
$ python3 -m pytest tools/ -q
FAILED tools/test_release_inspect.py::test_server_endpoint_uses_well_known_lock
1 failed, 723 passed, 2 warnings in 256.21s (0:04:16)

$ python3 -m pytest tools/test_release_inspect.py::test_server_endpoint_uses_well_known_lock -q
1 passed in 0.81s

$ python3 -m pytest tools/test_release_decision_record.py tools/test_platform.py -q
63 passed in 2.68s

$ python3 tools/release_decision_record.py --check
{"2026.2": 20, "must-fix-before-cut": 167, "post-cut-hardening": 50, "prove-against-candidate": 28}

$ python3 tools/validate_platform.py
OK    platform manifest + compatibility matrix valid (structure + coherence)

$ python3 tools/validate_compatibility_ledger.py compatibility-ledger.v1.yaml
PASS: compatibility-ledger.v1.yaml is a coherent compatibility-ledger.v1

$ python3 tools/generate_compatibility_table.py docs/platform-lock.v1.draft.yaml --check-output
PASS: documentation matches lock

$ python3 certification/validate-protocol-requirements.py
Validated 2123 complete, unique protocol certification cells.

$ python3 tools/check_evidence_map.py
== evidence-map gate — PASS (104 rows validated against schemas/2026.1-evidence-map.schema.json; part1=62, part2=42)

$ python3 tools/check_evidence_map.py --self-test
== evidence-map self-test — PASS (3 schema defects, 1 shape defect, 1 invented packet and 2 packet-split defects rejected)

$ git diff --check
(no output; exit 0)
```

Additional review checks:

- All 7 relative links/anchors and 25 GitHub source links/anchors in added or
  changed Markdown lines resolved. GitHub requests used same-request transient
  backoff; one transient failure recovered. AWS limit sources were read from
  the official documentation linked by the operating envelope.
- Parsing the manifest before/after with PyYAML yielded equal data: all artifact
  pins, `awsLambdaEcrDigest: pending-ecr-mirror`, and `contractVersions` values
  are unchanged. Only support-scope and pending-verification comments changed.
- Repository-wide Lambda/serverless search found no current scope claim placing
  Lambda below Supported / GA target. Historical upstream issue titles and
  literal external workflow/script filenames retain their original names.
- `python3 tools/validate_platform.py --exact-candidate` also passed structure
  and coherence, but explicitly skipped trunk-reachability offline because
  `GITHUB_TOKEN` was unset. This is not a live reachability or candidate receipt.
- Decision counts are the retained cohort plus the supplemental release#282
  observation, not a fresh org-wide inventory. Bill items 1/2/5 are pre-cut;
  3/4 require exact-candidate proof. Qualification remains false.
