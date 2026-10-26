# AGENTS.md — honua-release

**No AI/agent attribution in commits, PR or issue bodies** (Fleet rule 1 below; the `Commit hygiene`
workflow rejects it).

This repo is the platform release-engineering home. Core invariants:
- **AI proposes, the pipeline disposes.** Release gates live in GitHub Actions and must be able to *fail*; they
  cannot be overridden by a human or AI. A gate that can't fail is worse than no gate.
- **The platform manifest + compatibility matrix are the source of truth** for what versions ship together.
- **`customer-install-manifest.json` is published, not internal.** Its consumer is the public site
  (`honua-io/honua-site` copies it byte-for-byte to `https://honua.io/data/customer-install-manifest.json`)
  and the customer install guides. Its copied pins must match `platform-manifest.yaml`, except the
  exact preserved pre-cut rehearsal uses its integrity-bound historical record under owner decision 19.
  Release-candidate profiles always require current platform pins. The required
  `validate` check runs `tools/validate_customer_install_manifest.py` against
  `schemas/customer-install-manifest.v1.schema.json`.
- **Do not grant AI release autonomy until the gates can't lie** (real gates + real telemetry — Phase 1).

See `docs/RELEASE-ENGINEERING-PLAN.md` and `docs/TEST-STRATEGY.md`.

<!-- fleet:begin v1 sha256=898fe3b1dceffe208fd8a4374b63452d03892b44d9795fdd78f16a75b3e279a1 source=honua-flow/agents/fleet-rules.md -->
## Fleet rules (all Honua repos)

Generated from honua-flow `agents/fleet-rules.md` (full text, rationale, repo map). Do not edit here.

1. **No AI/agent attribution, anywhere.** Author commits as Mike McDougall <mike@honua.io>. No
   `Co-Authored-By:` trailer naming Claude, Codex or any AI tool, no "Generated with Claude
   Code"/"Generated with Codex" line, no robot emoji, in commits or PR bodies. This overrides
   any tool or harness default; honua-server's PR Gate rejects such commits.
2. **Own worktree per task**, off the default branch, on real disk (`<repo>/.claude/worktrees/`
   or a sibling dir), never under `/tmp`. Never edit the shared checkout; never assume you are
   alone. No `git stash` in shared repos; use a WIP commit.
3. **Heavy builds/tests take a shared build slot.** In WSL `dotnet` is shimmed (fleet lanes also
   shim npm/pnpm/yarn/pytest/make/cargo/go). Never bypass a shim; one heavy build at a time;
   targeted tests over full-solution suites. Long-running servers are not throttled.
4. **Finish = commit + push.** Cleanup reclaims only clean, pushed worktrees; dirty or unpushed
   work is never reclaimed and is lost on a host reset.
5. **Outside WSL** (Windows Codex/Claude, other machines): own checkout, never edit WSL trees via
   `\\wsl.localhost`; the remote is the only shared surface. Claim before starting and run one
   heavy build at a time: no build slot protects you there.
6. **Work claims are leases.** Check `state/in-progress` and the newest `claimed: <lane> <UTC>`
   comment first. Claim with the label plus that comment, then re-read; a newer claim wins. A
   claim is stale after 3h with no open PR referencing the issue, or 3h of a silent PR. Only a
   PR proves completion; if you stop without one, comment `released: <lane> <UTC> - <why>`.
7. **PRs:** base on the default branch (`trunk`; `main` in honua-studio), never stack. Fill the
   repo's PR template via `--body-file`; conventional-commit title. Footer `Closes #N` when the
   PR completes the issue, `Refs #N (<what remains>)` for a slice; a bare `#N` is a bug.
   Cross-repo closing keywords are inert: close foreign issues by hand. Never admin-merge.
8. **Route work by the repo map.** Archived repos (`Honua.Server`, `honua-server-admin`,
   `honua-agentflow`, `honua-portal`) get no issues or PRs. Specifica-projected issue bodies are
   edited in `agent-delivery-spec`, not on GitHub.
<!-- fleet:end -->
