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

## Published-client reconciliation (2026-09-06, Windows)

The follow-up at trunk baseline `6774e00` fixes the generator's published-client
join. Primary packages match ecosystem and coordinate instead of an arbitrary
client row name. Secondary packages join their unique component repository.
`@honua/mcp-server` belongs to **honua-sdk-js**, not geospatial-mcp, and retains
its own published version, hash and revision. Conflicting component/package
versions, revisions or hashes, incomplete identities, duplicate coordinates and
ambiguous ownership remain explicit signing blockers. Binding refuses those
inventory errors even when supplied with an otherwise complete lock.

The obsolete special case that ignored the declared Honua.Sdk package has been
removed. No manifest version, registry, hash, source pin or lifecycle changed.
The following command downloaded all four declared artifacts on the native
Windows host, without building source, and returned exit 0:

```powershell
python tools/verify_client_artifacts.py
```

The existing GitHub credential was supplied through `GITHUB_TOKEN` for the
declared GitHub Packages NuGet feed. Public npm/PyPI downloads used no token.
The verifier checked downloaded bytes against the manifest hash and checked
the package name/version inside each archive:

| Artifact | Result |
| --- | --- |
| npm `@honua/mcp-server@0.1.4-beta.0` | SHA-512 integrity and package metadata match |
| GitHub Packages `Honua.Sdk@1.6.0` | SHA-256 `e5c3bf0a243822cb3d76cca6ef090b226f8502480c6a6699d7e34e24c1aeeadc` and package metadata match |
| npm `@honua/sdk-js@0.1.9-beta.0` | SHA-512 integrity and package metadata match |
| PyPI `honua_sdk-0.1.11-py3-none-any.whl` | SHA-256 `80ac6a25fa5fed0ee7d8dca4ca94d14c098d9f72a149c9e271b1a1d04b78c3e9` and package metadata match |

These downloads prove the current manifest's four client packages are accessible
with the declared access model. They do not prove their source provenance or
certify the future candidate's full artifact set. The generator preserves the
manifest's artifact source declarations separately from component source heads.

The new regression fixture creates real npm tarballs and a NuGet archive, computes
expected SHA-512/SHA-256 directly from their bytes, and asserts all three artifacts
reach the lock-derived BOM with their exact versions, hashes and source revisions.
It also challenges conflicting versions/revisions/hashes, missing or ambiguous
repository ownership, duplicate coordinates and incomplete identities. Existing
secondary-package tampering assertions now fail at the earlier generated-input
binding check. No rejection case was removed.

## Release-level candidate facts (2026-09-07)

Everything the signed lock carries outside `components` - the MCP/catalog/OKF content digests,
fixture revisions, SBOM and provenance references, and the release notes - is now declared by
`platformLockEvidence` in the platform manifest, exactly like the component facts.

Before this change those five fields had no input path at all. The generator hard-coded
`contentDigests: {}` and `fixtures: []` and refused all five unconditionally, so the worklist
could not burn down even when the facts existed, and `$.notes` was reported as undeclared even
when the manifest declared it. Candidate binding compared `sourceInputs`, `platform`,
`components` and `clientArtifacts` against the frozen inputs but never the release-level fields,
so a manufactured lock could introduce a content digest, a fixture revision, an SBOM or
provenance reference, or release-notes text that no reviewed input declared - and those values
travel into the derived BOM, the compatibility ledger and the customer `platform-release.v1.json`.

The rules are now one module, `tools/release_facts.py`, shared by the generator, the semantic
validator and candidate binding, so they cannot drift:

- a content digest is the byte SHA-256 of one file at an immutable 40-character revision;
- a fixture is a repository at an immutable revision, at most one revision per repository path;
- an SBOM/provenance reference must be immutable: an `oci://` reference pinned to `@sha256:...`,
  no floating tag such as `latest`/`nightly`, and no `/blob/<branch>/` or `refs/heads/` source;
- release notes enter the lock as `repository@revision:path#sha256:<digest>`, never as prose,
  a placeholder, or a page that can be edited after the candidate is signed;
- candidate binding compares all five fields for **equality** with the declarations the frozen
  manifest produces. A lock may neither add a release fact nor drop one.

`tools/verify_content_digests.py` re-reads each declared file at its pinned revision - through
the pinned GitHub contents API, or from git objects with `--source-root` - and recomputes the
digest. `manifest-validate` runs it on every pull request, so a moved or edited source reddens
the branch-protected job.

One fact is declared and verified today: the geospatial-MCP standard content digest
`sha256:595f0ac8...` for `spec/schemas/index.json` at `d5a09d13`, the schema-index artifact the
operator ruled is the public spec identity (2026-09-01 decision 3). The runtime catalog and OKF
digests, the fixture revisions, the SBOM/provenance references and the release notes are produced
by the candidate itself and remain AT-CUT; they now refuse against a real input path.

The generator's refusal list is **40: 28 AT-CUT, 12 PUBLISH** (was 41: 29 AT-CUT, 12 PUBLISH).

```text
[AT-CUT] $.components.honua-server.migrationJournalSha256: exact declared migration set is not bound
[AT-CUT] $.components.honua-server.artifacts[0].version: source snapshot/pre-release is not a released artifact version
[AT-CUT] $.components.honua-server.artifacts[0].architectures: registry architecture set is not declared
[AT-CUT] $.components.honua-server.artifacts[0].platformDigests: platform-specific image digests are not declared
[AT-CUT] $.components.honua-server.artifacts[0].sourceRevision: registry provenance must bind the artifact to its source revision
[AT-CUT] $.components.honua-console.contractVersions: not declared
[AT-CUT] $.components.honua-console.schemaVersions: not declared
[PUBLISH] $.components.honua-console.artifacts[0].version: source snapshot/pre-release is not a released artifact version
[AT-CUT] $.components.honua-console.artifacts[0].platformDigests: platform-specific image digests are not declared
[AT-CUT] $.components.honua-sdk-dotnet.schemaVersions: not declared
[PUBLISH] $.components.honua-sdk-dotnet.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
[AT-CUT] $.components.honua-sdk-js.schemaVersions: not declared
[PUBLISH] $.components.honua-sdk-js.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
[AT-CUT] $.components.honua-sdk-python.schemaVersions: not declared
[PUBLISH] $.components.honua-sdk-python.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
[AT-CUT] $.components.geospatial-grpc.schemaVersions: not declared
[PUBLISH] $.components.geospatial-grpc.artifacts[python]: published package coordinate is pending https://github.com/honua-io/geospatial-grpc/issues/88
[PUBLISH] $.components.geospatial-grpc.artifacts[typescript]: published package coordinate is pending https://github.com/honua-io/geospatial-grpc/issues/88
[AT-CUT] $.components.geospatial-mcp.contractVersions: not declared
[AT-CUT] $.components.geospatial-mcp.schemaVersions: not declared
[PUBLISH] $.components.geospatial-mcp.serverCompatibility: unqualified: no consumed protocol/capability manifest is pinned
[AT-CUT] $.components.honua-iac.contractVersions: not declared
[AT-CUT] $.components.honua-iac.schemaVersions: not declared
[AT-CUT] $.components.honua-helm.contractVersions: not declared
[AT-CUT] $.components.honua-helm.schemaVersions: not declared
[PUBLISH] $.components.honua-helm.artifacts[0].version: source snapshot/pre-release is not a released artifact version
[PUBLISH] $.components.honua-helm.artifacts[0].digest: immutable registry digest is not declared
[PUBLISH] $.components.honua-helm.artifacts[0].architectures: registry architecture set is not declared
[PUBLISH] $.components.honua-helm.artifacts[0].sha256: pulled chart package checksum is not declared
[PUBLISH] $.components.honua-helm.artifacts[0].sourceRevision: registry provenance must bind the artifact to its source revision
[AT-CUT] $.components.honua-mobile.contractVersions: not declared
[AT-CUT] $.components.honua-mobile.schemaVersions: not declared
[AT-CUT] $.components.honua-collect.contractVersions: not declared
[AT-CUT] $.components.honua-collect.schemaVersions: not declared
[AT-CUT] $.contentDigests.catalog: catalog digest is not declared
[AT-CUT] $.contentDigests.okf: OKF digest is not declared
[AT-CUT] $.fixtures: fixture repository revisions are not declared
[AT-CUT] $.sbom: immutable SBOM references and hashes are not declared
[AT-CUT] $.provenance: immutable provenance references and hashes are not declared
[AT-CUT] $.notes: immutable release-notes content/reference is not declared
```

## Current factual blockers

At the remote trunk baseline `6774e00` (checked 2026-09-06 UTC),
`gh release list` still listed only the historical `honua-2026.1` prerelease.
The earlier NuGet.org HTTP 404 is not a blocker for the manifest's declared
GitHub Packages feed; its exact Honua.Sdk 1.6.0 download passed above.
[Release #57](https://github.com/honua-io/honua-release/issues/57) still records the
SDK publication/receipts, Console dependency and stable server/chart prerequisites.
[gRPC #88](https://github.com/honua-io/geospatial-grpc/issues/88) remains open.

The generator and the authoritative manifest/matrix now produce
**40 refusals: 28 AT-CUT, 12 PUBLISH** (enumerated above), down from 41 after declaring and
verifying the geospatial-MCP standard content digest. These are the generator's classifications,
not a blanket release of every AT-CUT line: non-candidate metadata must still be resolved before
cut. No registry value, lifecycle ruling, or source pin was invented to clear them.

