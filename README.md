# honua-release

Honua's release contracts, candidate certification and publication workflows live
here. A release adopts one candidate, proves its required journeys against frozen
inputs, then publishes only after admission.

## Adopt a candidate

[`platform-manifest.yaml`](platform-manifest.yaml) declares component and client
artifact pins; [`compatibility-matrix.yaml`](compatibility-matrix.yaml) declares
which versions may work together. Retain the exact manifest, matrix and
`platform-lock.json` bytes used by certification. Track the full server source
SHA, server image digest and platform-lock digest separately.

The [release train](.github/workflows/release-train.yml) validates and certifies
candidate inputs. Resolver output and locally generated locks prepare a candidate;
they do not establish installed readiness. The manifest's `clientArtifacts` and
`evidenceSources` pin immutable package bytes and trusted producer revisions for
certification without adding non-shipping tools to the deployable component set.

## Prove the candidate

The [release promise](certification/release-promise.v1.json) declares required live
journeys and measured outcomes. Its [gate](.github/workflows/gate-release-promise.yml)
checks authenticated producer evidence against the candidate's server SHA, image
digest and platform-lock digest, including required freshness and result checks.
Missing evidence fails closed. A merged change, local rehearsal or aggregate CI
badge does not prove an installed journey, and evidence for another candidate
does not satisfy this candidate's requirements.

[`certification/`](certification/) and [`e2e/`](e2e/) contain certification contracts
and cross-component tests. Each component repository owns its version and producer
gates; this repository checks their evidence and compatibility together. Failed,
blocked and unavailable outcomes remain visible.

## Publish after admission

The [RC workflow](.github/workflows/rc-release.yml) evaluates candidate gate history
and defaults to a dry run. It does not execute missing gates. Publication requires
an admissible result for the same candidate identities.

[Promotion](.github/workflows/promote.yml) verifies the certified candidate bundle,
readiness evidence and protected approval environment before publication. Retain
the certified bytes, producer run identities, signatures and publication references.
Release notes report measured outcomes; BOM and provenance describe the published
artifacts. AI may propose a release action; the committed gates enforce admission.

See the [candidate evidence guide](docs/rc2-cut-runbook.md),
[release requirements](docs/RELEASE-ENGINEERING.md),
[protocol certification contract](docs/PROTOCOL-CERTIFICATION.md) and
[test strategy](docs/TEST-STRATEGY.md).
