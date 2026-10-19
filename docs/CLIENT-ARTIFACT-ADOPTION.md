# Reviewed client artifact adoption

The trunk resolver verifies the package versions already declared in
`platform-manifest.yaml`. Its PLAN mode does not discover or adopt newer SDK
versions. A published package therefore needs a reviewed artifact proposal before
normal candidate selection can use it.

For an existing public NuGet client coordinate, generate a proposal with:

```sh
python3 tools/propose_client_artifact.py --client honua-sdk-dotnet --version 1.11.1
```

The tool reads the exact version from the public registry, derives its source
revision and package digest, then applies the existing complete package verifier.
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
