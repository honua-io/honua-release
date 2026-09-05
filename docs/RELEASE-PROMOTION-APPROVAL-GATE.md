# Release-promotion approval gate

*honua-release#44. Settings-as-code: `certification/release-promotion-approval.yaml`. Decision core:
`tools/check_release_promotion_approval.py`. Enforced by `promote.yml`; monitored by
`.github/workflows/repo-control-drift.yml`.*

## Why the environment is called `release-promotion`

The GitHub environment behind `promote.yml` gates **tagging, signing, and publishing a release** — it
is an authorisation boundary, not a deployment target. Nothing is ever deployed to it. Honua is not a
hosted service: customers run Honua in their own environments, so this repository has no production
estate for an environment to represent. It was originally named `production`, which implied one. The
name is `release-promotion` so that what the approval actually authorises is what it is called.

## What must be true

`promote.yml` is the only path that turns a certified candidate into a signed, tagged, published
platform release, and it is callable by humans **and** by automation. Artifact integrity (#42) proves
*what* is being released. This gate proves *who authorised it*, with an authority the release
automation cannot grant itself.

## Chosen design: GitHub protected-environment required reviewers

This repository is **public**, and environment protection rules — required reviewers, protected-branch
deployment policy, prevent-self-review — are available to public repositories on every plan. The
blocker recorded when this issue was filed (required reviewers being Enterprise-only for **private**
repositories on GitHub Team) no longer applies, so no external approval system is needed.

Approval authority therefore sits in GitHub's deployment-protection layer:

- `GITHUB_TOKEN` and the release App can *dispatch* `promote`, but **neither can approve the
  deployment**. Approval is a repository-permission action performed by a named human in the GitHub
  UI/API, outside the workflow's token.
- Tagging, signing, and release creation remain the workflow's own OIDC identity — the AI never holds
  keys (AGENTS.md).

The alternative — an external approval service or detached signature — was rejected: it would add a
second trust root and a second set of credentials to protect, for an authority GitHub already provides
and already refuses to delegate to a workflow token.

**If this repository ever becomes private again**, the required-reviewer rule stops being enforceable
on a Team plan. The gate does not silently degrade — the environment read would show the rule gone and
promotion fails closed — but the design must be re-chosen (Enterprise, or an external signature)
*before* re-privatising.

## The enforcement chain

| layer | enforces |
|---|---|
| `release-promotion` environment protection rules | one human from the declared reviewer roster must approve before the promote job runs |
| `deployment_branch_policy: protected_branches` | promotion may only run from a protected branch |
| `prevent_self_review` | the actor that started the deployment cannot approve it |
| `tools/check_release_promotion_approval.py` (promote preflight) | the live configuration still matches the attested settings-as-code, promotion ran from the protected default branch, and at least one roster member other than the promoting actor can approve |
| `tools/candidate_binding.py validate-run` | the exact certified train identity: a successful live `release-train` run from the protected default branch of this repository |
| `tools/finalize_release.py` | every gate green (or an explicitly allow-listed skip) before anything is tagged |

Everything the preflight refuses on:

- environment **missing** or metadata **unreadable** (HTTP error / no permission) — never read as "fine";
- **no required-reviewer rule**, more than one rule, or an empty or over-six reviewer roster;
- **roster drift** (the live reviewer id/login pairs differ from the attested roster);
- any reviewer that is not a `User`, whose login ends in `[bot]`, or that is a listed **automation
  principal** — every roster entry must be a human approval authority;
- **self-review permitted** (`prevent_self_review` not enabled);
- an **unreviewed protection-rule type** (for example a custom deployment-protection-rule app, which
  could approve automatically);
- deployment branch policy that is **not protected-branches-only**;
- the repository default branch **not protected**, or promotion dispatched from another branch;
- promotion started by the **only** roster member (no different member could approve independently);
- the attestation **expired**, or the environment **modified after** the last attestation.

Each check is emitted to `release-promotion-approval-evidence.json` (uploaded by the promote run) as
`{check, status, why}`. The evidence records names, ids, and outcomes only — never a token, a secret,
or an approval credential, and the workflows echo HTTP statuses rather than API response bodies.

## Admin runbook — configuring the environment (reproducible)

Run once, as a repository admin. `<repo>` is `honua-io/honua-release`; the reviewer id and the
protection settings must match `certification/release-promotion-approval.yaml` exactly, or the preflight and
the drift check will refuse.

```bash
# 1. Create the environment with protected-branch-only deployments, the exact declared human
#    reviewer roster, and self-review disabled. Repeat both reviewers[] fields for each roster member
#    (GitHub supports one to six); this one-member command matches the current declaration.
gh api -X PUT repos/<repo>/environments/release-promotion \
  -F "prevent_self_review=true" \
  -F "reviewers[][type]=User" -F "reviewers[][id]=12301237" \
  -F "deployment_branch_policy[protected_branches]=true" \
  -F "deployment_branch_policy[custom_branch_policies]=false"

# 2. Read it back and confirm it matches the declaration (this is exactly what CI does).
gh api repos/<repo>/environments/release-promotion > /tmp/environment.json
gh api repos/<repo> > /tmp/repository.json
gh api repos/<repo>/branches/trunk > /tmp/branch.json
python3 tools/check_release_promotion_approval.py \
  --policy certification/release-promotion-approval.yaml \
  --environment-metadata /tmp/environment.json \
  --repository-metadata /tmp/repository.json \
  --branch-metadata /tmp/branch.json \
  --promotion-ref trunk --promotion-actor github-actions[bot]

# 3. Record the attestation: in ONE commit set attestation.applied: true and refresh
#    attestation.attested_at to the moment you reviewed the live settings above.
```

The default branch must also be protected (`branches/trunk.protected == true`) with administrator
enforcement, required pull requests, and the required `validate` check — see
`docs/RELEASE-ENGINEERING-PLAN.md` § *Repository configuration prerequisites*.

## Two-actor operation

GitHub requires one approval from any member of `approval.required_reviewers`. `prevent_self_review`
means a roster member who dispatches `promote` cannot supply that approval; when that happens, a
different roster member must approve. The preflight independently requires such a different roster
member to exist. In steady state the release automation dispatches and any available roster member
approves — "AI proposes, the pipeline disposes".

A single-seat owner therefore cannot both dispatch and approve. That is the intended property, not a
defect.

### Automated dispatch through the org-installed `claude` App

`.github/workflows/request-promotion.yml` listens only for a successful, default-branch, manually
dispatched live `release-train` completion. It validates the immutable certified-candidate binding,
mints an installation token for the org-installed `claude` GitHub App scoped to this repository, and
dispatches `promote.yml`. The promotion run is therefore started by `claude[bot]`, while the named
human remains solely responsible for approving the protected environment.

This path requires an organization owner to complete the following setup. It must not be tested with
`RELEASE_GH_TOKEN`, the repository `GITHUB_TOKEN`, or another App as a workaround: those identities
either preserve the self-review deadlock, cannot trigger a new workflow, or change the reviewed trust
boundary.

1. In the existing `claude` GitHub App settings, change repository permission **Actions** from
   **Read-only** to **Read and write**, then have an organization owner accept the installation's new
   permissions. Keep its existing installation on `honua-io/honua-release`.
2. Generate a private key for that App and store the exact configuration names consumed by the
   workflow:

   ```bash
   gh variable set CLAUDE_APP_ID --repo honua-io/honua-release --body "1236702"
   gh secret set CLAUDE_APP_PRIVATE_KEY --repo honua-io/honua-release < /path/to/claude.private-key.pem
   ```

   Delete the downloaded private-key file after GitHub confirms the secret was stored. Never paste
   the PEM into an issue, pull request, workflow, log, or shell history.
3. Verify the installed App reports `actions: write` before attempting the acceptance dispatch:

   ```bash
   gh api orgs/honua-io/installations \
     --jq '.installations[] | select(.app_slug == "claude") | {app_id, repository_selection, actions: .permissions.actions}'
   ```

After those owner actions, complete acceptance with a successful live `release-train` promotion
request. Its `request-promotion` child must dispatch `promote.yml` as `claude[bot]`; the run must reach
the `release-promotion` approval prompt, and the preflight evidence must contain
`independent-promoting-actor: pass`. Reaching the prompt proves this path without approving it or
creating a tag.

The policy declares an exact `required_reviewers` roster of one to six human `User` accounts. GitHub
needs only one member's approval, so a standby can approve when the primary is unavailable. The
checker compares the live roster's id/login pairs to the declaration as a set; reordering is harmless,
but an added, removed, substituted, Team, app, or automation reviewer is refused. The current roster
contains one member because no second human repository member is presently available.

## Roster changes

1. Add or remove reviewers in the `release-promotion` environment (step 1 above), keeping one to six
   human `User` accounts and `prevent_self_review: true`.
2. Make the identical change to `approval.required_reviewers` in
   `certification/release-promotion-approval.yaml` and refresh `attestation.attested_at` in the same
   pull request.
3. If rotating a member, remove the outgoing reviewer only after the replacement is present.
4. Confirm `repo-control-drift` is green.

The order matters: the environment and the declaration must never disagree for longer than one
review — the drift check reports the mismatch, and promotion refuses while it lasts.

## Emergency release (break-glass)

There is no bypass in the workflow, and none should be added. If a release must be cut while the
approval boundary is unavailable, the only legitimate path is an **admin change to the environment**,
which leaves durable evidence:

1. File an incident issue in `honua-io/honua-release` **before** changing anything, recording who, why,
   and the intended window.
2. **Add or rotate an available human in the reviewer roster.** Because the checker requires the live
   roster to match the attested roster exactly, this is *two* coordinated changes and both are
   mandatory:
   - an admin updates the environment roster (`gh api -X PUT ... reviewers[][id]=<new id>`);
   - a pull request makes the identical `approval.required_reviewers` change and refreshes
     `attestation.attested_at`, linking the incident issue.

   Do not delete the environment, leave the roster empty, exceed six reviewers, disable
   `prevent_self_review`, or weaken the branch policy. Those are the properties the gate exists to
   protect, and the preflight refuses them.
3. Promote as normal, with the new reviewer approving and someone else dispatching. The approval, the
   deployment record, and `release-promotion-approval-evidence.json` are the audit trail.
4. **Re-lock immediately after:** restore the environment to the declared settings, run the read-back
   in step 2 of the runbook, refresh `attestation.attested_at` in a pull request that links the
   incident issue, and confirm `repo-control-drift` is green. The gate is not re-locked until that
   check passes.

Never weaken `tools/check_release_promotion_approval.py`, `tools/candidate_binding.py`, or
`tools/finalize_release.py` to make a release pass — a gate that can't fail is worse than no gate.

## Current state

`certification/release-promotion-approval.yaml` records `attestation.applied: true`. The live
`release-promotion` environment currently matches its one-member roster. `repo-control-drift` reports
any later roster or protection-setting drift as hard red, and `promote.yml` runs the identical check
and refuses promotion.

## Verify locally

```bash
python3 -m pytest tools/test_check_release_promotion_approval.py -q
# --mode defaults to the enforcing `promote`; the read-only monitor's reduced check set must be
# asked for explicitly with --mode drift.
python3 tools/check_release_promotion_approval.py --policy certification/release-promotion-approval.yaml \
  --mode drift --unreadable-reason "environment not configured"   # expect REFUSED, exit 1
```
