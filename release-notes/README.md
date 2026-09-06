# release-notes/

Aggregated release notes per platform label, generated from change metadata (conventional-commit type +
change-class label per PR across all component repos). Includes an explicit **Breaking changes + upgrade
actions** section and the traceability chain (release → component version → SHA → PRs → issues → SBOM →
gate evidence). The AI may draft these; the *facts* come from the pipeline.

**Wired:** `tools/finalize_release.py` (`render_release_notes`) generates the notes at promote time from
the certified candidate's digest-bound `platform-manifest.yaml` + `compatibility-matrix.yaml` — the
component set, versions/SHAs/images, contract versions, DB-schema floor, compatibility window, and a
breaking-changes/upgrade-actions section. `.github/workflows/promote.yml` attaches them to the
`Honua YYYY.N` GitHub Release.

**Still a stub:** the per-component breaking-change rollup from change metadata (PLAN §8) — until that
lands the breaking-changes section points operators at each component's own release notes.

## 2026.1 terminal-first workspace

The primary product path is one terminal workspace using the installed `honua` Admin CLI and bounded
MCP profile against the same server contracts. The authoritative control-plane target is 396
Admin REST/CLI operations: 385 policy-governed MCP projections plus 11 named secret/session
exclusions that remain CLI-only with a private secret sink. Console and browser Studio have separate
client receipts; neither defines terminal completeness. Live journey certification remains blocked
on the upstream operation/runtime, session/scope, proposal authorization, and evidence-posture work
recorded in `certification/terminal-journey/journey.v1.json`.

The current-trunk at-cut run is adjudicated in
`certification/terminal-journey/certification.issue-122.md`. It is a blocked release
receipt, not a promotion claim: all eight stages and the independent local/AWS,
browser-client, exact-client-matrix, and control-plane-roster AND gates must be green on
one joined candidate before release. That record also contains the verbatim operator
walkthrough; its presence does not substitute for live execution.
