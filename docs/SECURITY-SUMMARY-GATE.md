# Private security summary gate

The `security-open-high` release promise is produced on the lane host by the lane-host
security summary script using the founder `gh` CLI login. There is no
`gate-security-private-summary.yml` CI workflow and no private tracker token or secret
stored in `honua-release` (founder ruling, 20 November 2026).

The lane-host script queries the private security tracker (the lane-host script's default)
via `gh api graphql`. The query verifies private visibility, enabled issues,
and the `severity/high` label, then reads only the total count of open issues with that
label. Maintain that label on all high findings. The script never requests finding
identifiers, titles, or bodies, suppresses API error content, and writes the sanitized
receipt (`security-open-high.json`, mode `0600`) to the private artifacts directory.

The receipt contains only the allowed public fields (`schema`, `row`, `status`,
`serverSha`, `imageDigest`, `platformLockDigest`, `observedAt`, `assertions`, and
`openHighFindings`). Zero produces `PASS`; a positive count produces `FAIL` and exits
non-zero. Invalid candidate identity, inaccessible or public sources, missing labels, and
malformed or partial API responses fail without leaving a receipt (any earlier receipt is
removed before collection).

Wire the receipt into `tools/release_promise.py` (or `gate-release-promise.yml` / `promote.yml`) as a
manifest input:

- Locally on the lane host: pass `--security-summary <path>` (or `SECURITY_SUMMARY_FILE`)
  pointing to the private artifacts directory file, together with `--security-summary-actor`
  (`SECURITY_SUMMARY_ACTOR=mikemcdougall`).
- In `gate-release-promise.yml`: pass `--dispatch-gate` to the lane-host script (which
  dispatches `gate-release-promise.yml` under the founder `gh` login ONLY when
  `openHighFindings == 0`, so nonzero counts are never placed in public Actions logs), or
  supply `security_summary_json` when dispatching as the founder. The workflow passes
  `SECURITY_SUMMARY_ACTOR: ${{ github.triggering_actor }}`.
- In `promote.yml` (GA promotion): passes `--reuse-admitted-security-summary` so the
  independent promotion actor does not deadlock with the `release-promotion` approval
  check (`check_release_promotion_approval.py`); `release_promise.py` reads the latest
  completed `gate-release-promise.yml` trunk run and rechecks its founder-attested
  `security-open-high` row against the promoted candidate. Any newer gate dispatch,
  whether for another candidate or by a non-founder, blocks GA promotion until the
  founder re-dispatches for this candidate.

`tools/release_promise.py` enforces exact canonical candidate identity, seven-day freshness,
`assertions: {"privateScoped": true}`, integer-zero `openHighFindings`, founder actor
authentication (`mikemcdougall`), and the strict public-field allowlist (rejecting any
unexpected keys or ambiguous dual file+JSON inputs). The aggregate report records `actor`
and `summarySha256`, which `rc_gate_window.py` rechecks against the Actions run's
`triggering_actor` / `actor` at admission. Missing, stale, oversized, unauthenticated, or
malformed inputs fail closed.

Trust boundary note: because `honua-release` CI holds no private tracker token, the actor
check verifies which `gh` login dispatched `gate-release-promise.yml`, not an independent
CI query of the private tracker. Only the founder login may attest this input.
