docs(release): record the 2026-10-20 Lambda GA bill position
The Lambda GA bill ledger (`docs/2026.1-lambda-ga-bill.md`) was last updated on 2026-09-27. Since then it has named the wrong pin (`87966c3`) and the wrong blocker. This PR adds a dated status section. It is read-only evidence from GitHub plus the downloaded receipt artifact. No gate, manifest field or qualification changes.

What it records:

- **Items 1 and 2 are proven on the current manifest pin, pre-cut.** honua-server run 38239462033 (2026-10-12) produced a `result: pass` receipt. Its `serverRevision` is `0e03bdd`, and its source digest and ECR digest both equal the pinned `awsLambdaDigest` `sha256:3d6a38e3…`. Fixture 10/10 rows in all four phases, write round-trip 1/1/1/0, 403 and 401, cold start 6246.32 ms, alias `live` 9 → 13 → 9, full teardown. The ledger had never recorded this run. §1 rows 1 and 2 now point at it.
- **Every lane run since then has failed.** The 2026-10-13 run was refused at input validation (a digest-form image). The 10-16, 10-17, 10-18, 10-19 and 10-20 runs (the last is 39450966449) each stopped at `Secrets Manager read denied; serving noProof`.
- **The current blocker is an infrastructure apply, not code.** The source fix is honua-iac#255 (`3be6dfce`, merged 2026-10-17). It is not yet applied to the standing `aws-cert` stack. That apply needs a governed exact-plan executor and plan-bound approval, and the protected `terraform-live-approval` environment prevents self-review.
- **Items 3–5 are unchanged.** Items 3 and 4 are candidate-bound. Item 5's Supported flip is withheld behind the §5 sequence.

The ledger also states plainly that the lane uses its own serving driver and does not reuse the honua-devops L3 harness.

Verification (Windows, native):

- `python tools/check_vendor_terms.py lint --root . --repo honua-release`: ok
- `python tools/check_vendor_terms.py check-baselines --base-ref origin/trunk`: ok
- `PYTHONUTF8=1 python tools/release_decision_record.py --check`: ok. Without UTF-8 mode, Windows reports a false drift, and it does so on trunk too.
- `python tools/coverage_audit.py --check`: ok
- No tool or test in `tools/`, `e2e/` or `.github/` reads the ledger. All in-file anchors resolve.

Remaining for release#282 (not deliverable from this repository): apply honua-iac#255 to the standing `aws-cert` stack through the governed exact-plan path, rerun the lane, then run the §5 candidate sequence.

Refs #282 (released: the remaining acceptance criteria need the operator-approved honua-iac#255 apply on the standing aws-cert stack and the not-yet-cut 2026.1 candidate; this PR only brings the release-side ledger up to date)
