# Release engineering requirements

These requirements define the platform release contract. They describe what must be
proved before promotion; their presence in documentation is not evidence of a pass.
Use the [cut checklist](RELEASE-CUT-CHECKLIST.md) to operate a release and the
[test strategy](TEST-STRATEGY.md) to determine the required certification scope.

## Versions and compatibility

- Components use SemVer: breaking API, contract, or configuration changes require a
  major version; compatible additions use a minor version; fixes use a patch version.
  Breaking-change checks must reject an insufficient version bump.
- Wire contracts have independent versions. Implementation versions alone cannot
  establish client compatibility.
- `Honua YYYY.N` identifies a certified component set. Candidates use `-rc.N` and
  become a platform release only after the complete required gate stack passes.
- Deprecations require a documented window of at least two platform releases.

`platform-manifest.yaml` pins component versions, source revisions, artifact digests,
contract versions, and required database schema versions. `compatibility-matrix.yaml`
defines the supported interoperability ranges. Build checks, deployment preflight,
runtime compatibility handling, and upgrade preflight must enforce those contracts.
Servers must advertise `contractVersion`; SDKs must check it on connection and report
a clear warning or error on mismatch rather than silently accepting incompatibility.
Publish the matrix with the release so customers can validate their own pinned set.

## Candidate certification

A cut snapshots green component revisions into an immutable candidate. Run dependent
gates in dependency order: contracts, server, clients, deployment artifacts, then
cross-component certification. Every required gate must be able to fail.

Required proof covers:

- Contract compatibility, including deleted or renamed RPCs and public SDK APIs.
- Consumption of the published packages, images, deployment modules, and charts.
- Dependency, secret, SAST, container, IaC, and license checks, with critical security
  findings blocking promotion; artifact SBOMs, provenance, and signature verification.
- Performance and SLO behavior, including errors returned inside successful HTTP responses.
- Advertised capabilities and executable documentation examples.
- Seeded upgrades from the prior platform release, asserting zero downtime, data
  integrity, configuration migration, rollback, and supported old clients against
  the new server.
- Deployment validation and cross-component integration against the exact candidate.

Release-tier tests must provision their backing services. A skipped required test is
not passing evidence. Protocol and canonical-client requirements are defined in
[Protocol certification](PROTOCOL-CERTIFICATION.md); broader platform requirements
remain necessary even after protocol certification passes.

Every result must identify its candidate, source revisions, artifact digests, execution
mode, and durable evidence. Missing, stale, skipped, or mismatched required evidence
blocks promotion. Release notes and capability claims must derive from verified
metadata and evidence; unsupported claims must be identified as roadmap.

## Publishing and upgrades

Publish only the component set certified by the manifest and matrix. Include component
and platform SBOMs, signed provenance, the compatibility matrix, release notes, and
explicit breaking changes and upgrade actions. Preserve traceability from the platform
release through component versions, source revisions, changes, and gate evidence.

Database changes use expand-contract migrations with forward and rollback testing.
Schema compatibility is a deployment and startup precondition. Version configuration
schemas, provide migration paths, and retain deprecated configuration for its declared
window. Upgrade preflight validates compatibility, migration behavior, configuration,
and backups before applying changes. Deployment requires health/readiness checks,
post-apply validation, and a tested rollback strategy that automatically rolls back
on an SLO breach.

## Repository configuration

Protect the current default branch, enforce protection for administrators, require
pull requests, and require the stable `validate` check on every PR. Path filters must
not bypass or deadlock that check.

The `release-promotion` environment must allow protected branches only, require the
declared human reviewer roster, and prevent self-review. Settings-as-code in
`certification/release-promotion-approval.yaml` defines that roster and attestation.
Promotion compares live settings with the declaration and fails closed on missing,
unreadable, weakened, drifted, or stale configuration. Repository-control monitoring
checks the same protections between releases.

The dispatcher and approver must be independent actors. Follow the
[approval gate runbook](RELEASE-PROMOTION-APPROVAL-GATE.md) for reviewer rotation,
break-glass handling, and re-locking; never weaken protection to permit self-approval.

## Automation boundary

AI proposes, the pipeline disposes. Humans and automation use the same workflow
dispatch and certification path. Dispatch credentials must not grant signing,
publishing, or tagging rights; those operations use the workflow identity with least
privilege. Serialize competing cuts and retain the triggering actor's identity.

Gate reports must be machine-readable. Promotion verifies a successful live train
from the protected current default branch, binds the certified manifest and matrix
to their Actions run by digest, and consumes those exact files. A later checkout
cannot replace the certified candidate. No human or AI may override a failing gate.
AI release autonomy requires trustworthy gates and telemetry; production promotion
retains the human approval boundary enforced by the workflow.
