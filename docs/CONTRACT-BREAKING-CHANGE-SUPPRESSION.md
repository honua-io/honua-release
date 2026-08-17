# Contract breaking-change suppression — release gate

*Release gate for honua-io/honua-release#71. Register: `certification/contract-suppression-policy.yaml`.
Decision core: `tools/check_contract_suppression.py`. Wired as the `contract-suppression` check inside
`gate-contract` (release gate `b`).*

## The hole this closes

`honua-io/honua-server` carries a repository variable, `OPENAPI_ALLOW_BREAKING_CHANGES`, that its
**OpenAPI Contract Governance** workflow reads. When it is true, breaking-change enforcement is off for
**every** pull request in that repository — not just the one that needed it.

It was set on 2026-07-29 and that was the correct call: the control-plane API is unpublished, so no
external consumer can be harmed, and the checker was flagging removal of routes that provably do not
exist at runtime (honua-server#3064, and #2822 before it).

The problem is the shape of the failure at publication: **silence, not a red check**. A genuinely
breaking change to a consumed contract would merge with no signal anywhere in CI, and nothing would
remind anyone at release time. That is why it is a release gate here rather than a note in a server
backlog — cheap to verify (one variable read), expensive to miss.

## What the gate does

`gate-contract` now runs `tools/check_contract_suppression.py` on every cut and nightly, and the
verdict is bound into the candidate's `gate-report.json` — so the check is **recorded in release
evidence**, not performed ad hoc.

| state | verdict | effect |
|---|---|---|
| variable absent, or set to a false value | `pass` | — |
| variable set to a true value | `blocked` | loud in `bootstrap`; **FAIL** under `strict` (a real cut) |
| variables unreadable (no cross-repo token, HTTP error) | `blocked` | fail-closed — "unknown" is never read as "off" |
| value is neither truthy nor falsy (a typo) | `blocked` | mirrors the server's own refusal to read a typo as `false` |
| no landed steady-state mechanism declared | `blocked` | the decision cannot stay a backlog note |
| window audit missing, adverse, or stale while the window is open | `blocked` | the unaudited tail cannot grow silently |

The gate deliberately does **not** reset the variable, and neither should anything else in this repo.
Pre-publication the current setting is correct; flipping it early just re-blocks honest dead-path
cleanup. The reset is the release lead's action immediately before the first published control-plane
API release — this gate is what makes forgetting it impossible.

Reading is cross-repo, so it needs `RELEASE_GH_TOKEN` (a release token with read access to
honua-server). Without one the gate reports `blocked`, never a fake pass.

## Steady-state mechanism (decided, and landed)

"Flip a global variable per incident" does not scale past the first occurrence. The replacement is
**per-PR acknowledgement**, and it has already landed in honua-server (#3065):

- `scripts/ci/openapi-breaking-change-policy.py` resolves the acknowledgement. Once the repo-wide
  variable is false, an intentional break must check the exact `OPENAPI_BREAKING_CHANGE_APPROVED`
  task in the PR template **and** update one of the breaking-change documents (control-plane migration
  guide, versioning-and-support, or the release checklist). The acknowledgement is scoped to the PR
  that introduces the break instead of globally pre-authorising every future one.
- `scripts/ci/validate-openapi-contracts.sh` makes suppression **loud** for both acknowledgement
  sources: an Actions `::warning` plus a job-summary section listing every suppressed finding and its
  source. So suppression is visible rather than silent, whichever path allowed it.
- The repository variable short-circuits ahead of the marker, which is exactly why it must be reset:
  while it is true, the per-PR mechanism never gets a say.

## Audit of the suppression window (2026-07-29 → 2026-08-16)

AC: *confirm no breaking contract change merged while the flag was on that should have been caught.*

**Method.** Replay honua-server's own checker with enforcement ON, for every trunk commit in the window
that touched the curated contract documents:

```bash
git clone --shared --no-checkout <honua-server> /tmp/hs-audit && cd /tmp/hs-audit
for sha in $(git log --format='%H' --since=2026-07-29T16:53:29Z origin/trunk -- docs/developer/api-specs/ | tac); do
  git checkout -q "$sha"
  OPENAPI_BASE_REF="$(git rev-parse "$sha^1")" OPENAPI_ALLOW_BREAKING_CHANGES=false \
    bash scripts/ci/validate-openapi-contracts.sh >/dev/null 2>&1 \
    && echo "$sha clean" || echo "$sha FLAGGED"
done
```

**Result.** 13 commits in scope; 12 clean; **1 flagged**, and it is not a genuine break:

- `7c54ebef` (honua-server#3077, *burn down the admin-api.json undocumented-route backlog to zero*) —
  the checker reports `Path '/connections/tables' was removed` and `Path '/connections/{path}' was
  removed`. Both are **error-only guard routes with no success path**: the first always answers 400
  ("Connection ID is required"), the second is the catch-all fallback that answers 400 or 404. The
  runtime routes were not removed; #3077 moved their documentation into the declared-exclusion list.
  No consumable response was ever advertised, so no consumer contract changed.

**Verdict: no genuine breaking contract change merged during the window.** The window is still open
(the variable is still true today), so the audit carries a `covers_through` date and the gate blocks
once that record goes stale — re-run the command above and update `window_audit` in the register.

**Scope.** The checker diffs the curated `docs/developer/api-specs/` documents, so this audit covers
exactly what it would have caught. Runtime changes that never touch those documents are the admin
OpenAPI drift gate's concern (honua-server#3051 / #3063), not this window.

## Closing the gate at the first published release

1. Re-run the audit command above; update `window_audit.covers_through` (and any new findings) in
   `certification/contract-suppression-policy.yaml`.
2. Delete or set `OPENAPI_ALLOW_BREAKING_CHANGES=false` in `honua-io/honua-server` repository
   variables. From then on, intentional breaks travel through the per-PR marker.
3. Re-run `gate-contract` (or the train) and confirm `contract-suppression` reports `pass`. That run's
   `gate-report.json` is the recorded evidence for the release.

## Verify locally

```bash
gh api repos/honua-io/honua-server/actions/variables --paginate > /tmp/vars.json
python3 tools/check_contract_suppression.py \
  --policy certification/contract-suppression-policy.yaml \
  --variable-listing /tmp/vars.json
python3 -m pytest tools/test_check_contract_suppression.py -q
```
