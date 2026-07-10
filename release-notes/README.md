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
