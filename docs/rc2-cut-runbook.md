# Candidate evidence and release admission

A release proceeds through candidate selection, proof and publication. The
committed workflows and validators enforce admission; this guide explains the
identities and evidence they require.

## Adopt one candidate

Retain the exact manifest, compatibility matrix and platform-lock bytes used by
certification. Record these identities separately:

| Identity | Meaning |
| --- | --- |
| Server source SHA | Full revision that produced the server image |
| Server image digest | Immutable image consumed by the installed candidate |
| Platform-lock digest | SHA-256 of the exact `platform-lock.json` bytes used by the gates |
| Candidate snapshot | Frozen manifest, matrix and component artifact identities |
| Producer execution | Repository, workflow, run ID, attempt and retained artifact identity |

The [release train](../.github/workflows/release-train.yml) validates and certifies
candidate inputs. Resolver output and qualification-mode lock generation are
preparation steps. A locally generated lock does not establish release readiness.
Reformatting a lock changes its digest; changing candidate artifacts requires new
proof for the resulting candidate.

## Prove the candidate

The [release promise contract](../certification/release-promise.v1.json) declares
the required live journeys and measured outcomes. The
[promise gate](../.github/workflows/gate-release-promise.yml) evaluates authenticated
producer evidence against the server SHA, image digest and platform-lock digest.
Required evidence must satisfy the contract's identity, freshness and result checks.
Missing producers or receipts fail closed.

A merged change, template validation, successful local rehearsal or aggregate CI
badge does not prove an installed journey. Retain raw results and their producing
run identities. Failed, blocked and unavailable stages remain visible; another
candidate's results cannot discharge this candidate's requirements. Report the
measured release outcomes in the release notes.

## Publish after admission

The [RC admission workflow](../.github/workflows/rc-release.yml) evaluates the
candidate's gate window. Its default dry run computes evidence and notes without
publishing a release. Publication requires the same candidate identities and an
admissible result; this workflow reads gate history rather than executing missing
gates.

[Promotion](../.github/workflows/promote.yml) separately verifies the certified
candidate bundle, readiness evidence and protected approval environment before
publication. Preserve the certified bytes, receipts, signatures and publication
references. Operational scheduling and deployment credentials belong outside
this public guide.
