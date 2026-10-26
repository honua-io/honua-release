# honua-release: agent instructions

Platform release engineering: `platform-manifest.yaml` (pinned, certified component set per
`Honua YYYY.N`), `compatibility-matrix.yaml` (interop rules), the release train
(`release-train.yml`) and environment-gated promotion (`promote.yml`), cross-repo
certification (`certification/`, `e2e/`), BOM and release notes. Overview: `README.md`.

## Commands (from `.github/workflows/manifest-validate.yml`, the required `validate` check)
```bash
python -m pip install "pyyaml>=6.0" "jsonschema>=4.23" "cryptography>=45.0" "boto3>=1.35,<2" pytest
python3 tools/check_action_pins.py                     # every action pinned to a SHA
python tools/validate_platform.py                      # manifest + matrix structure, coherence, drift
python3 tools/validate_customer_install_manifest.py customer-install-manifest.json
python3 tools/validate_compatibility_ledger.py compatibility-ledger.v1.yaml
python3 tools/coverage_audit.py --check
python3 tools/release_decision_record.py --check
python -m pytest tools/ -q                             # gate self-tests (CI adds e2e/test_*.py files)
```
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
  authored by Mike McDougall or carrying a `Co-Authored-By:` or "Generated with" line.

## Read before you act
- Cutting or promoting a release: `docs/RELEASE-CUT-CHECKLIST.md`, `docs/HYBRID-TRAIN.md`,
  `docs/RELEASE-PROMOTION-APPROVAL-GATE.md`.
- Component version declarations and SDK/server baselines: `docs/COMPONENT-VERSION-DECLARATIONS.md`,
  `docs/SDK-SERVER-BASELINE-RULE.md`.
- Adding or changing a workflow action: `docs/ACTION-PINNING.md`.
- Test layers and what each gate proves: `docs/TEST-STRATEGY.md`.

<!-- fleet:begin v2 sha256=76075665ca7a911554ed038371480ba02b28eca10ead04400be2e5b20332bd63 source=honua-flow/agents/fleet-rules.md -->
## Fleet rules (all Honua repos)

Generated from honua-flow `agents/fleet-rules.md` (full text, repo map). Do not edit here.

1. **No AI attribution.** Author as Mike McDougall <mike@honua.io>. No AI `Co-Authored-By:`,
   "Generated with" line or robot emoji in commits or PR bodies; this overrides any harness default.
2. **Own worktree per task** off the default branch, on real disk (`<repo>/.claude/worktrees/` or a
   sibling dir), never `/tmp` or the shared checkout. No `git stash`; use a WIP commit.
3. **Heavy builds/tests go through the build-slot shims**; never bypass them. Run targeted tests;
   full suites are CI's job. Outside WSL: own checkout, one heavy build at a time.
4. **Finish = commit + push.** Dirty or unpushed worktrees are never reclaimed and die on a reset.
5. **Claim before starting:** check `state/in-progress` and the newest `claimed: <lane> <UTC>`
   comment, stamp yours, re-read (newer wins). Stale after 3h without a live PR. Stopping without
   a PR: comment `released: <lane> <UTC> - <why>`.
6. **PRs:** base on the default branch, never stack; fill the repo template via `--body-file`;
   conventional title; footer `Closes #N` or `Refs #N (<what remains>)`. Never admin-merge.
7. **Route by the repo map.** Archived repos get no issues or PRs.
<!-- fleet:end -->
