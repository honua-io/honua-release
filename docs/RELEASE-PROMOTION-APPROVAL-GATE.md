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
| `release-promotion` environment protection rules | a named human must approve before the promote job runs |
| `deployment_branch_policy: protected_branches` | promotion may only run from a protected branch |
| `prevent_self_review` | the actor that started the deployment cannot approve it |
| `tools/check_release_promotion_approval.py` (promote preflight) | the live configuration still matches the attested settings-as-code, promotion ran from the protected default branch, and the promoting actor is not the required reviewer |
| `tools/candidate_binding.py validate-run` | the exact certified train identity: a successful live `release-train` run from the protected default branch of this repository |
| `tools/finalize_release.py` | every gate green (or an explicitly allow-listed skip) before anything is tagged |

Everything the preflight refuses on:

- environment **missing** or metadata **unreadable** (HTTP error / no permission) — never read as "fine";
- **no required-reviewer rule**, more than one rule, or more than one reviewer;
- **wrong reviewer** (id or login differs from the attested reviewer);
- reviewer that is not a `User`, whose login ends in `[bot]`, or that is a listed **automation
  principal** — an automation-only approver is not an approver;
- **self-review permitted** (`prevent_self_review` not enabled);
- an **unreviewed protection-rule type** (for example a custom deployment-protection-rule app, which
  could approve automatically);
- deployment branch policy that is **not protected-branches-only**;
- the repository default branch **not protected**, or promotion dispatched from another branch;
- promotion **started by the required reviewer** (approval would not be independent of the request);
- the attestation **expired**, or the environment **modified after** the last attestation.

Each check is emitted to `release-promotion-approval-evidence.json` (uploaded by the promote run) as
`{check, status, why}`. The evidence records names, ids, and outcomes only — never a token, a secret,
or an approval credential, and the workflows echo HTTP statuses rather than API response bodies.

## Admin runbook — configuring the environment (reproducible)

Run once, as a repository admin. `<repo>` is `honua-io/honua-release`; the reviewer id and the
protection settings must match `certification/release-promotion-approval.yaml` exactly, or the preflight and
the drift check will refuse.

```bash
# 1. Create the environment with protected-branch-only deployments, a single required human
#    reviewer, and self-review disabled.
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

`prevent_self_review` plus the preflight's independent-actor check means **the required reviewer must
not be the one who dispatches `promote`**. In steady state the release automation (or a second human)
dispatches and the reviewer approves — "AI proposes, the pipeline disposes".

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

**The gate requires exactly ONE reviewer.** `certification/release-promotion-approval.yaml` declares a single
`required_reviewer`, and the checker refuses when the environment names any other set — more than one
reviewer, a different login, a Team, or an app. A second concurrent reviewer is therefore **not**
supported today; if a human needs to dispatch promote personally, **rotate** the required reviewer to
another human (below) rather than adding one alongside, and never disable `prevent_self_review`.
Supporting a declared multi-reviewer roster for continuity is tracked in honua-release#93.

## Reviewer rotation

1. Add the new reviewer to the `release-promotion` environment (step 1 above, with the new id).
2. Update `approval.required_reviewer.{id,login}` in `certification/release-promotion-approval.yaml` and
   refresh `attestation.attested_at` in the same pull request.
3. Remove the outgoing reviewer from the environment.
4. Confirm `repo-control-drift` is green.

The order matters: the environment and the declaration must never disagree for longer than one
review — the drift check reports the mismatch, and promotion refuses while it lasts.

## Emergency release (break-glass)

There is no bypass in the workflow, and none should be added. If a release must be cut while the
approval boundary is unavailable, the only legitimate path is an **admin change to the environment**,
which leaves durable evidence:

1. File an incident issue in `honua-io/honua-release` **before** changing anything, recording who, why,
   and the intended window.
2. **Rotate the required reviewer to an available human** — the only change the gate accepts. Because
   the checker requires exactly the attested reviewer, this is *two* coordinated changes and both are
   mandatory:
   - an admin replaces the reviewer on the environment (`gh api -X PUT ... reviewers[][id]=<new id>`);
   - a pull request updates `approval.required_reviewer.{id,login}` and `attestation.attested_at`,
     linking the incident issue.

   Do **not** add a second reviewer (the gate refuses more than one — see *Two-actor operation*),
   delete the environment, disable `prevent_self_review`, or weaken the branch policy. Those are the
   properties the gate exists to protect, and the preflight refuses them anyway; a mid-incident
   attempt would simply produce a hard refusal.
3. Promote as normal, with the new reviewer approving and someone else dispatching. The approval, the
   deployment record, and `release-promotion-approval-evidence.json` are the audit trail.
4. **Re-lock immediately after:** restore the environment to the declared settings, run the read-back
   in step 2 of the runbook, refresh `attestation.attested_at` in a pull request that links the
   incident issue, and confirm `repo-control-drift` is green. The gate is not re-locked until that
   check passes.

Never weaken `tools/check_release_promotion_approval.py`, `tools/candidate_binding.py`, or
`tools/finalize_release.py` to make a release pass — a gate that can't fail is worse than no gate.

## Current state

`certification/release-promotion-approval.yaml` records `attestation.applied: false`: the `release-promotion`
environment **does not exist yet** in this repository, so promotion is blocked. That is the intended
safe state (#43, #44). Applying the runbook above and committing the attestation is the remaining
owner action; the repo-side gate, evidence, drift check, and tests are in place and enforcing.

While `applied` is false, `repo-control-drift` reports the gap as a warning rather than a permanent
red — it is an early-warning monitor, and `promote.yml` runs the identical check and *refuses*, so the
unapplied state can never let a release through. Once `applied` is true, any drift is hard red.

## Verify locally

```bash
python3 -m pytest tools/test_check_release_promotion_approval.py -q
# --mode defaults to the enforcing `promote`; the read-only monitor's reduced check set must be
# asked for explicitly with --mode drift.
python3 tools/check_release_promotion_approval.py --policy certification/release-promotion-approval.yaml \
  --mode drift --unreadable-reason "environment not configured"   # expect REFUSED, exit 1
```
