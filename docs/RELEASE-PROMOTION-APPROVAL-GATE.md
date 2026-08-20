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
| `release-promotion` environment protection rules | one human from the bounded, attested roster must approve before the promote job runs |
| `deployment_branch_policy: protected_branches` | promotion may only run from a protected branch |
| `prevent_self_review` | the actor that started the deployment cannot approve it |
| `tools/check_release_promotion_approval.py` (promote preflight) | every live reviewer belongs to the bounded, attested roster, promotion ran from the protected default branch, and the promoting actor is not in that roster |
| `tools/candidate_binding.py validate-run` | the exact certified train identity: a successful live `release-train` run from the protected default branch of this repository |
| `tools/finalize_release.py` | every gate green (or an explicitly allow-listed skip) before anything is tagged |

Everything the preflight refuses on:

- environment **missing** or metadata **unreadable** (HTTP error / no permission) — never read as "fine";
- **no required-reviewer rule**, more than one rule, or a reviewer count outside the declared bounds;
- **wrong reviewer** (a live id/login pair is absent from the attested roster), including duplicates;
- reviewer that is not a `User`, whose login ends in `[bot]`, or that is a listed **automation
  principal** — an automation-only approver is not an approver;
- **self-review permitted** (`prevent_self_review` not enabled);
- an **unreviewed protection-rule type** (for example a custom deployment-protection-rule app, which
  could approve automatically);
- deployment branch policy that is **not protected-branches-only**;
- the repository default branch **not protected**, or promotion dispatched from another branch;
- promotion **started by a declared reviewer** (approval would not be independent of the request);
- the attestation **expired**, or the environment **modified after** the last attestation.

Each check is emitted to `release-promotion-approval-evidence.json` (uploaded by the promote run) as
`{check, status, why}`. The evidence records names, ids, and outcomes only — never a token, a secret,
or an approval credential, and the workflows echo HTTP statuses rather than API response bodies.

## Admin runbook — configuring the environment (reproducible)

Run once, as a repository admin. `<repo>` is `honua-io/honua-release`; every reviewer id and the
protection settings must match `certification/release-promotion-approval.yaml` exactly, or the preflight and
the drift check will refuse.

```bash
# 1. Create the environment with protected-branch-only deployments, the currently active members of
#    approval.required_reviewers.roster, and self-review disabled. Repeat both reviewer flags for
#    each additional live roster member (GitHub allows at most six).
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

`prevent_self_review` plus the preflight's independent-actor check means **no declared reviewer may
dispatch `promote`**. In steady state the release automation (or a human outside the roster) dispatches
and any live roster member approves — "AI proposes, the pipeline disposes".

A single-seat owner therefore cannot both dispatch and approve. That is the intended property, not a
defect.

`approval.required_reviewers` declares a roster plus an explicit `minimum` and `maximum` (the maximum
may not exceed GitHub's service limit of six). The live environment may use one or more roster members
inside those bounds. Every live entry must be a `User` with the declared id/login pair; Teams, apps,
bots, duplicates, and undeclared humans are refused. GitHub requires approval from any one live member,
so adding a second live roster member provides standby continuity without weakening the two-actor rule.

The repository currently has only one organization member, so the code path supports continuity but
the live environment still has one reviewer. Actual standby continuity requires inviting a second
human, adding that person to the roster and environment, and re-attesting the read-back.

## Reviewer rotation

1. Add the new human to `approval.required_reviewers.roster` in a pull request. Keep the roster count
   within its declared maximum and never add a Team, app, or bot.
2. Apply the same id/login to the `release-promotion` environment. Promotion and drift checks refuse
   while live configuration is ahead of the attestation; that is the intended safe transition.
3. Read the environment back, refresh `attestation.attested_at`, and merge the reviewed declaration.
4. After the new reviewer is proven live, remove an outgoing reviewer from both the environment and
   roster in one reviewed transition, re-attest, and confirm `repo-control-drift` is green.

The order matters: the environment and the declaration must never disagree for longer than one
review — the drift check reports the mismatch, and promotion refuses while it lasts.

## Emergency release (break-glass)

There is no bypass in the workflow, and none should be added. If a release must be cut while the
approval boundary is unavailable, the only legitimate path is an **admin change to the environment**,
which leaves durable evidence:

1. File an incident issue in `honua-io/honua-release` **before** changing anything, recording who, why,
   and the intended window.
2. **Activate or add an available declared human reviewer.** If the human is already in the roster,
   add that id to the environment; otherwise a pull request must add the id/login to the roster first.
   In either case, read back the live environment and refresh `attestation.attested_at` before promote.
   The checker permits only live reviewers from the declared roster and still requires another actor
   to dispatch.

   Do **not** add an undeclared reviewer, exceed the maximum, delete the environment, disable
   `prevent_self_review`, or weaken the branch policy. Those are the properties the gate protects and
   every one produces a hard refusal.
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
`release-promotion` environment has protected-branch-only deployment, `prevent_self_review: true`, and
one live human reviewer (`mikemcdougall`, id `12301237`) from the declared roster. Scheduled
`repo-control-drift` runs are green. A second human account is the remaining external prerequisite for
real standby continuity; until then the gate remains secure and fail-closed but has a single approval
seat.

## Verify locally

```bash
python3 -m pytest tools/test_check_release_promotion_approval.py -q
# --mode defaults to the enforcing `promote`; the read-only monitor's reduced check set must be
# asked for explicitly with --mode drift.
python3 tools/check_release_promotion_approval.py --policy certification/release-promotion-approval.yaml \
  --mode drift --unreadable-reason "environment not configured"   # expect REFUSED, exit 1
```
