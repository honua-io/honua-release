# Reviewed client artifact adoption

The trunk resolver verifies the package versions already declared in
`platform-manifest.yaml`. Its PLAN mode does not discover or adopt newer SDK
versions. A published package therefore needs a reviewed artifact proposal before
normal candidate selection can use it.

For an existing npm, PyPI wheel or public NuGet client coordinate, generate a
proposal for an exact published version with:

```sh
python3 tools/propose_client_artifact.py --client honua-sdk-dotnet --version 1.11.1
```

The tool reads the exact version from the public registry, derives its source
revision and package digest, then applies the existing complete package verifier.
PyPI source discovery validates the signed publish attestation. An npm publisher
bundle can supply an untrusted source hint when gitHead is absent; that hint earns
no proposal until the existing verifier authenticates the complete publisher
bundle and exact package bytes.
That verifier checks listing, catalog identity, SHA-512, downloaded package
SHA-256, archive identity, and repository URL/commit in both catalog and nuspec.
An unavailable package or any mismatch refuses the proposal. Output includes the
input manifest hash, previous artifact, proposed artifact and verified identity.
The command writes JSON to stdout and does not change the manifest or candidate.

Review the proposal against the maintained publication and source changes before
adopting it. The checked-in
[.NET 1.11.1 proposal](../certification/artifact-adoption-proposals/dotnet-1.11.1.json)
records the independently downloaded public package. It leaves candidate
qualification pending and does not select that package for shipment.

After review, adopt the compatible published cohort through the normal governed
manifest and requirements automation. A new server image and full matrix must
precede deriving a matching SDK fixture tuple. Normal resolver, NIGHTLY-STAGE,
exact producer receipt joins, and NIGHTLY-BIND still establish the candidate.
A proposal cannot bind the ledger, mint a release, establish client compatibility,
or replace exact candidate execution. Issue #181 remains open for that complete
convergence.

## Generate a review preview

After reviewing the proposal, generate a separate preview directory:

```sh
python3 tools/generate_client_artifact_preview.py \
  --proposal certification/artifact-adoption-proposals/dotnet-1.11.1.json \
  --output /tmp/honua-artifact-adoption-preview
```

The output directory must be new and outside the source repository. Multiple
`--proposal` arguments support a reviewed JavaScript/Python/.NET cohort. Each
proposal must match the exact input manifest hash and previous artifact. Registry
reverification must reproduce the reviewed identity. Primary SDK component
selection and declared versions use the normal resolver, including its real source
CI verdict; a failed certification check never becomes green through this tool.

The generator changes mapping blocks in its isolated copy, then runs normal
read-only convergence preparation. The preview contains the generated manifest,
source/catalog payloads, an inventory of exact hashes and an identity-level review
report. It preserves the original server, ledger and customer rehearsal binding.
Original obligations cannot disappear behind a larger net row count. New rows,
changed facets/policies and .NET source release-tier changes remain explicit and
unqualified until reviewed and executed. A source calling an operation excluded
because its fixture is absent does not establish unsupported behavior.

The preview is not a candidate, a receipt, a bound ledger or an automatic approval.
It writes no tracked source files and performs no APPLY, FINALIZE, BIND or mint.
Review the exact generated source and semantic differences before a normal
protected adoption PR. All original 3,008 obligations remain the minimum scope;
new applicable source coverage must be accounted for too.

MCP producer source convergence additionally needs a maintained, reviewed source
preparation stage to break the stale-catalog/certification cycle. Literal package
and lock snapshots must come from one authenticated producer source. The normal
resolver's certification verdict remains enforced. A source-only projection may
stage a coherent catalog for actual certification, but cannot grant candidate
admission. That bootstrap and all 82 actual MCP authorization/cohort observations
remain open under issues #181 and #695.
