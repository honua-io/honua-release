# gRPC publication and compatibility

Canonical protocol definitions live in `honua-io/geospatial-grpc`. The server
consumes generated bindings through the published `Geospatial.Grpc` package;
consumer repositories must not vendor their own copies of the canonical protos.

Use the source repository's current release contract and
`.github/workflows/publish-dotnet-protocol.yml` for publication prerequisites,
tag validation, registry verification and release assets. Historical release
procedures do not authorize creating tags or republishing packages.

Before updating downstream pins, verify that the selected protocol revision,
versioned conformance fixtures and published generated packages agree. A source
revision or fixtures release alone does not establish package availability.
Regenerate affected consumers and update their package and conformance pins
together, with the owning repository's compatibility checks passing.

In this repository, `gate-contract.yml` validates the manifest-pinned protocol
revision against the highest version-sorted `v*` release tag using `buf breaking`.
Verify the selected revision against that baseline; do not treat a skipped
publication job or an unrelated green workflow as protocol qualification.

The proto package `geospatial.v1`, its schema distribution version and the
platform compatibility contract are separate identities. A schema distribution
change does not by itself change the protocol package or client version floors
in `compatibility-matrix.yaml`. Compatibility changes require their own explicit
review and validation.
