# SDK producer convergence

The supported platform set must be certified against the exact SDK revisions and
published artifacts frozen in its manifest, using one immutable requirements
catalog and evidence ledger. A source pin alone does not prove a published package.

For a bound protocol certification ledger, the .NET, Python and JavaScript catalog
producer revisions must equal both their manifest component revisions and
`clientArtifacts.*.sourceSha`. Missing or divergent identities fail validation.
A pending ledger may be structurally valid; exact-candidate certification refuses
it. Binding restores the evidence commit, ledger digest and requirements source
revision together. Convergence planning and mutation refuse divergent SDK package
provenance before catalog regeneration and leave the manifest unchanged.

## Candidate certification

Select published SDK revisions containing their certification contracts and bind
component pins and artifact identities together. Select the server image and its
source revision, including matching producer-side image pins. Producer execution
must use trusted workflow context for that exact candidate.

The `nightly-certification.yml` workflow owns staging and binding:

1. Resolve the candidate and stage its catalog with
   `tools/convergence_rebind.py --nightly-stage`. Sources and vendored contracts
   come from the candidate's pinned revisions. The staged requirements commit is
   published under `refs/nightly-requirements/<sha>`.
2. Dispatch the catalog's protocol producers with `tools/nightly_receipts.py`,
   passing the candidate identity and nightly correlation ID. Wait for every
   producer to succeed, then aggregate their evidence against the staged
   requirements revision.
3. Bind with `tools/convergence_rebind.py --nightly-bind`. Every receipt must name
   the dispatched repository, workflow, run ID and attempt in
   `evidence_receipt.identity.producer_run`. A different run cannot substitute,
   even if it used the same source pins. Failed, skipped or mismatched producers
   refuse certification.
4. Run `tools/validate_platform.py --exact-candidate` with candidate deployment
   qualification before publishing the immutable candidate ref under
   `refs/nightly-candidates/<sha>`. The bound image and source must still equal
   the resolved and qualified server identity.

`convergence-rebind.yml` provides planning only. Candidate gates read the ledger
bound into the candidate manifest; retired activation workflows and standalone
`PROTOCOL_CERTIFICATION_*` repository variables are not a binding mechanism.
Neither this guide nor a committed test fixture is a qualification receipt.

## Harness and catalog growth

The pinned server `protocol-harness-assignments.v1.json` defines the operation
set, including writes and capabilities without an SDK entrypoint. The minimum
operation count is a floor, not a ceiling or the denominator. Every implemented
assignment must generate its exact operation, test IDs, scenario facets, source
identity and execution-policy row. Unknown capabilities, duplicate operations,
omitted rows and stale generated rows fail validation.

Nonimplemented capabilities do not become required gaps merely because an older
applicability assignment remains. Existing applicability classifications retain
their meaning within the implemented-capability scope. Unknown classification
keys, missing implemented classifications and stale generated Preview decision
rows fail. Harness growth does not change the SDK entrypoint denominator.

Planning derives the official protocol capability list from the exact pinned
server matrix using the validator's implemented-protocol rule and reports added
and removed capabilities. It derives official client versions from pinned SDK
source declarations using identity and SemVer checks. Each derived version must
equal both its manifest component version and published artifact version; missing
or mismatched selections refuse. Client names, lanes, authentication policy and
roster membership remain governed independently.

Catalog staging includes the derived policy, source snapshots and generated
catalog. It cannot change the server matrix, waive a Preview decision or client
roster requirement, or fabricate an execution receipt. Static contract inputs
exercise generation and refusal behavior; they do not certify a deployed candidate.
