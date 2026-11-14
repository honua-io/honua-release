# honua-release: agent instructions

Platform release engineering: `platform-manifest.yaml` (pinned, certified component set per
`Honua YYYY.N`), `compatibility-matrix.yaml` (interop rules), the release train
(`release-train.yml`) and environment-gated promotion (`promote.yml`), cross-repo
certification (`certification/`, `e2e/`), BOM and release notes. Overview: `README.md`.

## Commands (from `.github/workflows/manifest-validate.yml`, the required `validate` check)
```bash
python -m pip install -r tools/requirements-validation.txt
python3 tools/check_action_pins.py                     # every action pinned to a SHA
python tools/validate_platform.py                      # manifest + matrix structure, coherence, drift
python3 tools/validate_customer_install_manifest.py customer-install-manifest.json
python3 tools/validate_compatibility_ledger.py compatibility-ledger.v1.yaml
python3 tools/coverage_audit.py --check
python3 tools/release_decision_record.py --check
python -m pytest tools/ -q                             # local tools subset
python -m pytest certification/agent-ops -q            # operate harness; own process, not mixed with e2e/
```
CI runs the tools and selected e2e self-tests through six `tools/self_test_partitions.py run`
jobs, then requires `tools/self_test_partitions.py verify` to conserve their execution receipts.
Run the steps that cover what you changed; the workflow lists the rest.

## Invariants
- **AI proposes, the pipeline disposes.** Release gates live in GitHub Actions and must be
  able to fail; no human or AI overrides them. A gate that can't fail is worse than no gate.
- **Do not grant AI release autonomy until the gates can't lie** (real gates and real
  telemetry first; `docs/RELEASE-ENGINEERING-PLAN.md`, Phase 1).
- **`platform-manifest.yaml` and `compatibility-matrix.yaml` are the source of truth** for
  which versions ship together. Narrowing a matrix range without a contract-version bump fails.
- **`customer-install-manifest.json` is published, not internal.** honua-site copies it
  byte-for-byte to `https://honua.io/data/customer-install-manifest.json`. Its pins must match
  `platform-manifest.yaml` (release-candidate profiles always use current pins; only the
  preserved pre-cut rehearsal uses its integrity-bound historical record, owner decision 19).
  It validates against `schemas/customer-install-manifest.v1.schema.json`.
- The `Commit hygiene` workflow (org reusable `commit-hygiene.yml`) rejects any commit not
  authored by the repository owner or carrying a `Co-Authored-By:` or "Generated with" line.

## Read before you act
- Cutting or promoting a release: `docs/RELEASE-CUT-CHECKLIST.md`, `docs/HYBRID-TRAIN.md`,
  `docs/RELEASE-PROMOTION-APPROVAL-GATE.md`.
- Component version declarations and SDK/server baselines: `docs/COMPONENT-VERSION-DECLARATIONS.md`,
  `docs/SDK-SERVER-BASELINE-RULE.md`.
- Adding or changing a workflow action: `docs/ACTION-PINNING.md`.
- Test layers and what each gate proves: `docs/TEST-STRATEGY.md`.

## Review guidelines
Reviews (people, Codex, Claude) judge the lines a PR adds or changes by Robert C. Martin's *Clean Code*
and cite smell codes ([rubric](https://github.com/honua-io/.github/blob/main/clean-code/RUBRIC.md)). The
`Clean code` PR check enforces what a tool can decide; `quality-exception: <reason>` in the PR body is logged.

BLOCKING:
- G5 duplication: search first; reuse or consolidate, never copy a handler, parser or validator.
- C1 history, ticket/PR refs or reviewer notes in code; C2-C4 obsolete, redundant or rambling comments;
  C5 commented-out code. Comment only what the code cannot say.
- XML doc comments (`///`) in application code; published SDKs keep at most a one-line `<summary>`
  on public members.
- N1 names that hide intent; F1 more than 3 parameters; F2 output arguments; F3 boolean flag arguments.
- Over budget with no reason in the PR: functions ~40 lines or 2 nesting levels; classes, files ~400 lines.

WARNING: the rubric's other smells (N2-N7, G6-G36, T1-T9), e.g. G25 magic numbers or G12
single-implementation interfaces.

<!-- fleet:begin v3 sha256=4faeba041ae102508d105847af58b407641cbfdf1ae506488819a2ccdbe73e29 -->
## Fleet rules (all Honua repos)

Generated from the org fleet-rules source; do not edit this block. Start with org skill `honua-system-map`.

1. Author commits as the repository owner's configured git identity. No AI attribution in commits or PR bodies.
2. Use a separate worktree per task, off the default branch, on durable disk. Include current trunk before opening a PR. No shared-checkout edits or git stash; preserve work with commits.
3. Run .NET builds and tests only through the approved remote build-host shims. Never fall back to a local build. See org skill `honua-build-host`.
4. Commit and push completed work; keep existing branches, commits and worktrees until their retention rules permit cleanup.
5. Claim work before starting, reread the issue and current lane rulings, and release the claim after completion. See org skill `honua-dev-loop`.
6. Use a PR slot and bundle at least three issues, a plan row, or a permitted single-work reason. Current caps and exceptions come from current lane rulings.
7. Target the default branch with the repository's PR template and conventional title. Never force-push or use an admin merge.
8. Run pre-PR checks, obtain one independent review of the final head, and resolve every review thread. Merge on green required checks; arm auto-merge where enforced checks support it. See org skill `honua-pr-flow`.
9. Read failed required-check logs and fix in one push. For red trunk, fix forward or revert within the repository's current ruling deadline; see org skills `honua-red-pr` and `honua-trunk-red`.
10. Never commit secrets, deployment identifiers, private repository names or infrastructure details. Security findings belong in the private tracker. Archived repos get no issues or PRs; public instructions name org skills only.
<!-- fleet:end -->
