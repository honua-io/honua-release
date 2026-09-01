# First-publication preflight for release issue 57

Preflight date: 2026-08-31 UTC. This record stops before every credentialed
registry push. It does not contain secret values, create tags, change repository
visibility, publish packages, or create releases.

## Readiness summary

| Channel | Exact candidate inspected | Credential-free proof | Publication credential and configuration boundary | Terminal state |
|---|---|---|---|---|
| NuGet (`Geospatial.Grpc`, then `Honua.Sdk.*`) | `geospatial-grpc` merge `1a6e3a501c948a44e5196028c449601265145543`; SDK PR 305 inspected head `007aa9f3817e5aac9fd247dcb9a50ad6fdd434de` (`1.6.1`), which is evidence only and is not an eligible publication target | gRPC merged-trunk validation-only run 32480858847 passed its real pack, package verification, clean local consumer, conformance packaging, and immutable artifact upload. SDK issue 263 records 16 `.nupkg` plus 16 `.snupkg`, 27 publication-workflow tests, and 130 gRPC tests at the inspected head. A fresh workflow dispatch was attempted twice and rejected before run creation by GitHub API rate limiting (HTTP 403 at 20:09:25 and 20:13:58 UTC); these results do not satisfy the publication gate. | gRPC: environment `production`, secret `NUGET_API_KEY`, scoped to create/push `Geospatial.Grpc` and symbols on nuget.org. SDK: environment `public-nuget`, secrets `NUGET_API_KEY`, `NUGET_SIGNING_CERTIFICATE_BASE64`, and `NUGET_SIGNING_PASSWORD`; optional environment variable `NUGET_SIGNING_TIMESTAMP_URL` defaults to DigiCert. Configure under each repository's **Settings -> Environments**, never in source or dispatch inputs. | `BLOCKED-ON-TRAIN-BINDING`: before either publication, update and validate `platform-manifest.yaml` and `compatibility-matrix.yaml` together with the exact gRPC and SDK release candidates. The SDK change must merge to its protected default branch; record that merge SHA, prove the tag target is reachable from the protected branch, and bind the manifest and tag to it. After `Geospatial.Grpc 1.0.0` is anonymously available, rerun the complete SDK gate at that exact merge SHA against the public package. Only a successful run permits the SDK 1.6.1 publication transaction. |
| Buf Schema Registry | `geospatial-grpc` merge `1a6e3a501c948a44e5196028c449601265145543`, stable contract `1.0.0` | The same validation-only run 32480858847 exercised Buf 1.66 lint/format/breaking and the release contract without entering the tag-only registry job. Manual `workflow_dispatch` is explicitly validation-only. | An owner must first create BSR organization `honua-io`. Then configure environment `production` secret `BUF_TOKEN` in `honua-io/geospatial-grpc`, scoped to create/push the public module `buf.build/honua-io/geospatial-grpc`. | `BLOCKED-ON-CREDENTIAL`: the owner organization/module and scoped token are absent. The combined workflow also requires the NuGet credential before either registry is mutated. |
| Helm OCI | merged publication plumbing at `91c7026ce8aa249e88b79bb6353bdebb857c82cc` | The workflow has a non-publishing `workflow_dispatch`, but it requires a real chart SemVer and a real server SemVer and verifies `ghcr.io/honua-io/honua-server:v<semver>-aot`. No authoritative stable server SemVer/AOT candidate exists, so no truthful dispatch input can be supplied. Helm is not installed on this host; the merged PR's lint/render validation is the available source proof. | No long-lived registry secret. The tag workflow uses its job-scoped `GITHUB_TOKEN` with `packages: write`, `id-token: write`, and `attestations: write`. After the first push, an organization package administrator may have to set `ghcr.io/honua-io/charts/honua` Public. | `BLOCKED-ON-CANDIDATE`, not yet credential-only: first publish the exact stable server SemVer and matching public AOT image. Then dispatch the dry-run, sign `chart-v<version>`, and stop for the operator-controlled tag push. |
| QGIS | PR 29 head `718e2180c6370c71483e155505ef3d64859ad5ab`, plugin `0.1.0` | `scripts/validate_metadata.py` passed. Two consecutive `scripts/package.py --check` builds produced the same 77,073-byte, 45-file ZIP with SHA-256 `ca547c100982d6b99d227245fbc5bb1b888eca3605eb78408b7a6a3781f33e5e`. PR evidence records 191 tests passed and 2 skipped; local pytest was not rerun because the repository-mandated `with-build-lock` wrapper is not installed on this host. There is no release-workflow dry-run trigger. | GitHub release creation uses the job-scoped `GITHUB_TOKEN`. The tag must be signed by a GitHub-verified maintainer identity. Marketplace submission requires the operator's OSGEO/QGIS account at `plugins.qgis.org`; it is configured only in that external account/browser session and must never be stored in the repository. | `BLOCKED`, before credential-only: restore Actions billing, review private history/issues/logs/metadata, make the repository Public, complete clean-profile QGIS smoke/screenshots, then use the verified signing identity and OSGEO account. Upload exactly the GitHub Release ZIP. |
| Mobile (NuGet and npm) | PR 361 head `efb840bd758935b817cdf3eb25ba75576b58ad28`; .NET packages `0.1.0-alpha.1`; `@honua-io/embed` `0.1.0` | Fresh embed proof passed: lockfile install, typecheck, 192/192 tests, build, `npm pack --dry-run`, and `npm audit --omit=dev` with zero production vulnerabilities. The tarball is `honua-io-embed-0.1.0.tgz` (6.2 MB packed, 480 files). The .NET dry-run cannot restore because this head still requires the unpublished SDK 1.6.0 graph. Both supported workflow dispatch attempts were rejected before run creation by GitHub API rate limiting (HTTP 403 at 20:13:59 UTC). | NuGet: environment `public-nuget`, package-scoped secret `NUGET_API_KEY`. npm first cut: environment `public-npm`, granular secret `NPM_TOKEN` scoped to `@honua-io/embed`; confirm npm owner control of the `@honua-io` scope. After the first cut, configure npm Trusted Publishing for this repository/workflow and remove the long-lived token path. | `BLOCKED-ON-DEPENDENCY` for Mobile NuGet: update the provisional SDK 1.6.0 pin to the actual public 1.6.1 train after SDK publication. npm packaging is `BLOCKED-ON-CREDENTIAL`, but the coordinated Mobile channel is not ready until the .NET half is corrected. |
| Console | focused Console PR 338 head `49695d2b26a5b52d3a75d5405a4dcd9e80a2b06a`; stacked public-restore PR 356 head `1e81f24e2658bae43d5cdc3d8b231afd7d8a7a01` | PR 338's exact head has green Console validation, container build/smoke, Playwright, live integration, CodeQL, and security checks. PR 356 correctly removes private feeds and fails closed on anonymous nuget.org restore, but still pins `Honua.Sdk.Studio 1.6.0`; no credential-free build can succeed while that coordinate is absent. This host has no .NET SDK, so no weaker local substitute was used. | None. Console must restore from its checked-in nuget.org-only configuration with empty caches and no package credential. | `BLOCKED-ON-DEPENDENCY`, not credential: rebase the public-restore stack onto the final Console head, change every direct SDK pin to public 1.6.1, regenerate locks through nuget.org only, and pass restore/build/test. |

## Operator boundary

The credentialed sequence is dependency ordered and must remain fail-closed:

1. Merge each reviewed release candidate to its protected default branch and
   record the resulting merge SHA. A PR head is evidence only and must never be
   tagged or published directly.
2. Update `platform-manifest.yaml` and `compatibility-matrix.yaml` together to
   name the exact gRPC and SDK versions and protected-branch SHAs selected for
   this train. Run the repository's manifest/matrix validation and retain its
   successful receipt before configuring publication credentials.
3. Create the BSR owner boundary and configure `BUF_TOKEN` plus the gRPC
   `NUGET_API_KEY` in `geospatial-grpc`'s protected `production` environment.
4. Prove the `v1.0.0` tag target is the manifest-bound gRPC protected-branch
   commit, push the immutable tag, and retain the public BSR/NuGet receipts.
5. Wait until `Geospatial.Grpc 1.0.0` restores anonymously from nuget.org, then
   run the complete SDK gate against that public dependency at the exact
   manifest-bound SDK merge SHA. The tag target must be that SHA and reachable
   from the SDK's protected default branch; retain the successful run receipt.
6. Only after that receipt exists, configure the three SDK signing/push secrets
   in its protected `public-nuget` environment and publish the exact 1.6.1 SDK
   package set.
7. Re-pin Mobile and Console to those public 1.6.1 coordinates and rerun their
   anonymous dry-runs. Configure Mobile's `public-nuget` and `public-npm`
   secrets only for the tag-triggered publication jobs.
8. Supply Helm inputs only after a stable server SemVer and matching public AOT
   image exist. Helm uses scoped workflow identity rather than a long-lived
   registry secret.
9. Complete the QGIS visibility/history/billing/signing review, create the
   immutable GitHub ZIP, smoke that exact byte sequence in a clean QGIS profile,
   and submit it with the operator's external OSGEO account.

No channel is marked ready merely because its packaging script passes. Registry
coordinates, dependency versions, workflow identity, anonymous consumption, and
the exact artifact bytes must all agree before a public receipt is claimed.
