# Platform lock #231: pre-cut delivery and remaining acceptance

Release promise: the adopted 2026.1 quality contract §9.2 and §14 requires one
signed atomic candidate identity, with exact published artifact bytes and customer
records derived from that identity. Issue #231 remains open until its real
candidate acceptance is proven. This document does not change its release bucket.

## Delivered pre-cut machinery

A strict release train validates `platform-lock.json` before qualification. The
lock must match the requested platform ID (including an optional patch number),
the byte hashes of both frozen inputs, every declared component/artifact fact,
and the exact component denominator. Every `clientArtifacts` entry, including
secondary `@honua/mcp-server` packages, must resolve to exactly one artifact with
the same published version, source revision and hash. Pending published clients
cannot disappear.
Only honua-mobile and honua-collect may use the operator-approved experimental
source-only model. Published artifact source revisions remain independent of the
component source head.

`tools/platform_lock_bundle.py` writes canonical JSON with no trailing newline;
the file SHA-256 equals the canonical digest used by the compatibility resolver.
It derives the CycloneDX BOM, compatibility ledger, SDK compatibility Markdown,
and site publication record from that lock. It refuses to replace different
existing output bytes. Empty ledger certification arrays mean no qualification
has been asserted; identity generation does not produce a certification receipt.

After the existing manifest, client-artifact and evidence checks, the freeze job
attests the canonical lock using GitHub OIDC. The original Sigstore bundle and all
derivatives travel with the frozen inputs into the certified-candidate artifact.
Promotion verifies the signature against the release-train workflow, source SHA,
and source ref, then checks every derivative against the authenticated lock.
It publishes the original candidate bytes and signature bundle. The strict SBOM
gate also generates its BOM from the lock; the bootstrap manifest report remains
available for development.

Generate a complete, reviewed candidate input set:

```bash
python3 tools/platform_lock_bundle.py platform-lock.json \
  --manifest platform-manifest.yaml --matrix compatibility-matrix.yaml \
  --label 2026.1.0-rc.1 --out-dir frozen-lock
```

Commit `frozen-lock/platform-lock.json` as `platform-lock.json` before dispatching
the strict train. Freeze requires byte equality with this canonical serialization
so the committed burn-in lock hash and attested candidate hash cannot diverge.

A customer who has downloaded the certified candidate bundle can verify its
signature with `gh attestation verify platform-lock.json --bundle
platform-lock.sigstore.json --repo honua-io/honua-release --signer-workflow
honua-io/honua-release/.github/workflows/release-train.yml --source-digest <train
commit> --source-ref refs/heads/trunk --deny-self-hosted-runners`, then rerun the
command above with `--out-dir . --check` to detect altered derived files.
The expected source commit comes from the reviewed train record, not from an
untrusted bundle. Signature verification proves who attested the lock bytes; it
is not a substitute for independently downloading the component artifacts.

## Evidence and acceptance disposition

The regression fixture supplies separate published-artifact and component-head
revisions. Expected SHA-512 is computed directly from fixture bytes, independently
of the BOM builder. Tests assert package version, purl, integrity, both revisions,
image architecture/index metadata, complete ledger edges, retained RC identity,
and absence of invented certification. Executed CLI regressions reject modified
BOM bytes and refuse overwriting a different candidate. Negative cases reject
wrong label/version/source, missing components, edited manifest bytes, placeholders,
and omitted publication dependencies. Existing tests are retained.

Released criterion: manufacture and third-party verification of the actual
`honua-2026.1.0-rc.1` candidate cannot execute before that exact candidate and its
published component set exist. The original train-issued signature, actual
registry downloads, catalog/OKF/fixture identities, and candidate-bound
SBOM/provenance must still be proven at manufacture. No production lock was
signed by this PR, and no existing tag was moved.

Remaining pre-cut work is **not released**: reconcile public package facts,
complete consumed SDK baseline declarations, and supply all explicit input
metadata needed to complete the lock. The generator is still a draft/worklist
generator; this PR does not turn its current partial output into a signable lock.
A complete lock is separately supplied and must pass the binding checks. The
site record is supplied as a publication artifact; deployment into honua-site is
not evidence supplied by this PR.

## Current factual blockers

At the remote trunk baseline `d02d459587844c99942a4854fe9e705d67e0f61b`
(checked 2026-09-06 UTC), `gh release list` listed only the historical
`honua-2026.1` prerelease. Anonymous retrieval of
`https://api.nuget.org/v3-flatcontainer/honua.sdk/index.json` returned HTTP 404.
[SDK #263](https://github.com/honua-io/honua-sdk-dotnet/issues/263) is closed for
publication plumbing; that is not a public artifact receipt.
[Release #57](https://github.com/honua-io/honua-release/issues/57) still records the
SDK publication/receipts, Console dependency and stable server/chart prerequisites.
[gRPC #88](https://github.com/honua-io/geospatial-grpc/issues/88) remains open.

The generator also does not yet seed secondary client packages such as
`@honua/mcp-server`; the complete-lock binding now refuses that omission.

The unmodified authoritative manifest/matrix produce **43 refusals: 29 AT-CUT,
14 PUBLISH**. These are the generator's classifications, not a blanket release
of every AT-CUT line: non-candidate metadata must still be resolved before cut.
No registry value, lifecycle ruling, or source pin was invented to clear them.

```text
- [AT-CUT] $.components.honua-server.migrationJournalSha256: exact declared migration set is not bound
- [AT-CUT] $.components.honua-server.artifacts[0].version: source snapshot/pre-release is not a released artifact version
- [AT-CUT] $.components.honua-server.artifacts[0].architectures: registry architecture set is not declared
- [AT-CUT] $.components.honua-server.artifacts[0].platformDigests: platform-specific image digests are not declared
- [AT-CUT] $.components.honua-server.artifacts[0].sourceRevision: registry provenance must bind the artifact to its source revision
- [AT-CUT] $.components.honua-console.contractVersions: not declared
- [AT-CUT] $.components.honua-console.schemaVersions: not declared
- [PUBLISH] $.components.honua-console.artifacts[0].version: source snapshot/pre-release is not a released artifact version
- [AT-CUT] $.components.honua-console.artifacts[0].platformDigests: platform-specific image digests are not declared
- [AT-CUT] $.components.honua-sdk-dotnet.schemaVersions: not declared
- [PUBLISH] $.components.honua-sdk-dotnet.artifacts[0].sha256: package hash is not declared (blocked on https://github.com/honua-io/honua-sdk-dotnet/issues/263 for Honua.Sdk 1.6.1 publication)
- [PUBLISH] $.components.honua-sdk-dotnet.artifacts[0].sourceRevision: registry provenance must bind the artifact to its source revision (blocked on https://github.com/honua-io/honua-sdk-dotnet/issues/263 for Honua.Sdk 1.6.1 publication)
- [PUBLISH] $.components.honua-sdk-dotnet.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [AT-CUT] $.components.honua-sdk-js.schemaVersions: not declared
- [PUBLISH] $.components.honua-sdk-js.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [AT-CUT] $.components.honua-sdk-python.schemaVersions: not declared
- [PUBLISH] $.components.honua-sdk-python.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [AT-CUT] $.components.geospatial-grpc.schemaVersions: not declared
- [PUBLISH] $.components.geospatial-grpc.artifacts[python]: published package coordinate is pending https://github.com/honua-io/geospatial-grpc/issues/88
- [PUBLISH] $.components.geospatial-grpc.artifacts[typescript]: published package coordinate is pending https://github.com/honua-io/geospatial-grpc/issues/88
- [AT-CUT] $.components.geospatial-mcp.contractVersions: not declared
- [AT-CUT] $.components.geospatial-mcp.schemaVersions: not declared
- [PUBLISH] $.components.geospatial-mcp.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
- [AT-CUT] $.components.honua-iac.contractVersions: not declared
- [AT-CUT] $.components.honua-iac.schemaVersions: not declared
- [AT-CUT] $.components.honua-helm.contractVersions: not declared
- [AT-CUT] $.components.honua-helm.schemaVersions: not declared
- [PUBLISH] $.components.honua-helm.artifacts[0].version: source snapshot/pre-release is not a released artifact version
- [PUBLISH] $.components.honua-helm.artifacts[0].digest: immutable registry digest is not declared
- [PUBLISH] $.components.honua-helm.artifacts[0].architectures: registry architecture set is not declared
- [PUBLISH] $.components.honua-helm.artifacts[0].sha256: pulled chart package checksum is not declared
- [PUBLISH] $.components.honua-helm.artifacts[0].sourceRevision: registry provenance must bind the artifact to its source revision
- [AT-CUT] $.components.honua-mobile.contractVersions: not declared
- [AT-CUT] $.components.honua-mobile.schemaVersions: not declared
- [AT-CUT] $.components.honua-collect.contractVersions: not declared
- [AT-CUT] $.components.honua-collect.schemaVersions: not declared
- [AT-CUT] $.contentDigests.geospatialMcp: certified content digest is not declared
- [AT-CUT] $.contentDigests.catalog: catalog digest is not declared
- [AT-CUT] $.contentDigests.okf: OKF digest is not declared
- [AT-CUT] $.fixtures: fixture repository revisions are not declared
- [AT-CUT] $.notes: immutable release-notes content/reference is not declared
- [AT-CUT] $.sbom: immutable SBOM references and hashes are not declared
- [AT-CUT] $.provenance: immutable provenance references and hashes are not declared
```
