# Withdrawing a published 2026.1 release

This is the withdrawal procedure that exists in the release workflows today. It does not add a
workflow, a manifest status, or a tag. `promote.yml` is the publication path. Nothing in
`.github/workflows/` unpublishes a release.

## What publication does

`promote.yml` signs the publication tag with gitsign under that workflow's OIDC identity, pushes
`refs/tags/<tag>`, and verifies the published tag against the certified commit
(`tools/tag_signing.py` `sign` and `verify-remote`). If `git ls-remote` already sees the tag, the
sign step verifies it instead of re-signing.

The job `Create the platform release for the verified signed tag` then runs `gh release view`.
When that release exists, the job prints `refusing to overwrite a published platform release` and
exits 1. Otherwise it runs `gh release create` with `--verify-tag` and the lock assets named in
the workflow: gate report, platform manifest, compatibility matrix, platform lock, compatibility
ledger, SDK compatibility note, platform-release document, signed-tag receipt, finalized manifest,
and CycloneDX BOM, each with its signature bundle where the workflow signs one.

`promote.yml` does not run `npm dist-tag`, and it does not move a container tag `:stable` or
`:2026.1` or a Helm channel. `docs/2026.1-release-decision-overrides.json` records an intent that
promotion moves those channels. That sentence is not a step in `promote.yml`. Do not perform those
moves by hand as part of this procedure.

`schemas/customer-install-manifest.v1.schema.json` allows `status` values `pre-cut-rehearsal` and
`release-candidate` only. There is no `withdrawn` status. Do not invent one in the published
manifest.

## What an operator can do

1. Do not dispatch `promote.yml` for a tag whose GitHub release already exists. The workflow
   refuses to overwrite it. Do not dispatch `rc-release`, `e2e-cloud-aws`, or `rc-channel-tag`
   from this procedure.
2. Do not delete the git tag or the GitHub release from this document. No workflow here does
   that, and deleting a tag is not a customer withdrawal.
3. Leave `customer-install-manifest.json` on its current status. A withdrawn record cannot be
   stored until the schema allows it.
4. Notify enrolled customers with the support policy published as revision **2026.1-r2**
   (`honua-site` `docs/policies/2026.1-support-policy.md`, section 3). Email **mike@honua.io**
   with subject `Honua support: <severity> <short impact>`. If that delivery fails, email
   **info@honua.io** with the original receipt time. The honua-support application is Preview
   and is not the required path.
5. Compose customers roll back with [UPGRADE-RUNBOOK.md](UPGRADE-RUNBOOK.md) section 6. AWS ECS
   customers use section 7. Lambda rollback in that section is Preview, not a GA topology.

## Advisory text

Send this as the manual email. It is not posted by a workflow.

```text
Subject: Honua support: critical withdrawal of <tag>

Honua is withdrawing published tag <tag> from customer use. Do not deploy it.
Keep the digest you recorded at install and follow the rollback runbook for
that topology. Docker Compose rollback is UPGRADE-RUNBOOK section 6. AWS ECS
rollback is section 7. Lambda rollback is Preview.

This notice does not move an npm dist-tag, a container floating tag, or a Helm
channel. promote.yml publishes a signed git tag and a GitHub release, and it
refuses to overwrite an existing GitHub release.

Contact: mike@honua.io
```

## What this document does not claim

- No withdrawal was rehearsed against a test lock. The overwrite refusal runs only when
  `promote.yml` is dispatched, and that dispatch belongs to the release session.
- Pricing and licensing while `Licensing__Mode=Disabled` stay as written in
  `release-notes/2026.1.md`. This file does not close the pricing ruling.
- The support-policy approval receipt and the honua-support rehearsal are not attached here.
- The AWS promise quickstart (install on ECS or Lambda, then publish a layer and a 2D map)
  was not run for this file. The Compose playbooks on honua-site are the customer quickstart
  this lane executed locally. See that pull request for the host port and the image tag.
