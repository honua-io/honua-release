# Issue 233 recovery evidence

Disposition: BLOCKED. Issue 233 remains in must-fix-before-cut; this audit
releases no acceptance criterion and makes no certification claim.

Implementation inspected: release trunk `0150f767aa000aa1b029bddb8a058d60385954c9`.
The retained issue-233 WIP was fetched and compared with trunk. Trunk includes
the ledger/resolver, source verification, first-release derivation and subsequent
review fixes from merged PRs 279, 289, 291 and 296. The newer issue-236 recovery
backup was also fetched; its independent signing changes belong to issue 236.
No duplicate implementation PR is needed for the merged issue-233 subgroups.

## Verification

- `focused-tests.txt`: 122 existing regressions pass, zero skips, including real
  Git, HTTP and package-archive fixtures and literal expected introduction floors.
- `strict-check.txt`: `python3 tools/generate_compatibility_table.py
  docs/platform-lock.v1.draft.yaml --check` exits 1 for the four absent consumed
  protocol/capability manifest pins. This is a failing qualification audit.
- The same command with `--check-output` passes documentation freshness.
- `server-publication-history.json`: freshly collected complete empty tag,
  release and tag-ref namespaces, verified with `--max-age-days 1`.
- `publisher-sources.json` binds each fetched SDK declaration/MCP manifest to
  its current trunk commit, repository path and independently computed SHA-256.
  The adjacent source files retain the exact fetched bytes.

The lock still lacks `components.honua-server.releaseVersion` and the consumed
manifest pins for all four entries. Current upstream declarations still read
JavaScript 1.0.0, .NET 0.1.0, and Python 1.0.0 plus legacy 2026.3.0. The MCP
reference manifest still lacks introduction floors. The operator has been asked
to name the first server component SemVer; no value has been selected or inferred.

Remaining pre-cut work: name that version, bind first-release introduction
metadata and consumed capability requirements, correct and gate each SDK's
declarations, and bind published artifacts. The customer-table PR
https://github.com/honua-io/honua-site/pull/274 is open, non-draft, at
`b27ce1f6236c6bc4e1b98366ee32d4da687f84f0`, with validate and CodeQL green;
its publication is not complete. Exact-candidate ledger and upgrade/rollback
receipts retain only the candidate-dependent disposition in earlier PRs.

Recovery branch: `fix/233-finalize`; evidence is pushed only to
`wip/fix/233-finalize`. No implementation or test was changed in this audit.
