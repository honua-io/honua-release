# First-publication wave: verified Windows checkpoint for #57

Observed 2026-09-06 UTC. **BLOCKED; #57 remains must-fix-before-cut.** This
supersedes the 2026-08-31 preflight. Release promise: the adopted quality contract
sections 5.1 and 14 and release #57 require customer-accessible, immutable public
packages and anonymous installed-consumer evidence. A merged workflow or green
dry run does not satisfy publication.

## Verified public denominator

[Registry observations](../certification/first-publication/registry-observations-2026-09-06.json)
record anonymous HTTPS GETs, response codes, timestamps and response hashes.
No registry credentials were supplied. HTTP 404 means absent at observation time;
an index response is availability evidence, not consumer certification.

| Channel | Current evidence | Disposition / next concrete boundary |
|---|---|---|
| Geospatial.Grpc / BSR | Public NuGet `1.0.0`; BSR commit `f52df33b3b5d4723881ad0bacaf8a754`; both anonymous downloads match the independent release receipt. Fresh native Windows restore passes from nuget.org alone. | Publication prerequisite satisfied. Preserve the immutable receipts; candidate-bound certification still uses the exact selected bytes. |
| .NET SDK | **All 16 package IDs return 404**, including `Honua.Sdk.Cli`. The attempted first-publication version is now `1.6.2`, not the obsolete `1.6.1` proposal. | Finish the protected publication transaction and collect the complete public package/symbol receipts. Do not treat closed SDK #263 or a dry run as publication. |
| Mobile NuGet | `Honua.Mobile.Sdk`, `Honua.Mobile.Offline`, `Honua.Mobile.Maui`: 404. Publication PR #361 remains open at `efb840bd758935b817cdf3eb25ba75576b58ad28`, with provisional SDK `1.6.0` references. | Bind to the actual reviewed, public SDK train, regenerate locks from public packages, pass anonymous restore/tests, then publish through the protected workflow. A speculative version substitution cannot produce public restore evidence. |
| Mobile npm | `@honua-io/embed`: 404. | Complete the protected first publication and anonymous tarball/provenance comparison. Prior local packaging success is not a public receipt. |
| Console | PR #356 remains open at `1e81f24e2658bae43d5cdc3d8b231afd7d8a7a01`, provisionally consuming `Honua.Sdk.Studio 1.6.0`; the public package ID is absent. | Rebase onto the reviewed focused Console, bind the actual public SDK version, regenerate locks, and pass nuget.org-only restore/build/test with empty caches. No package credential should be needed. |
| Migrate | Both `honua-migrate` and `honua-esri-assess` PyPI coordinates return 404. | Complete the pending PyPI Trusted Publisher transaction from the reviewed immutable source. Install with pipx and execute fixture-backed assess/report plus the compatibility command; verify wheel/sdist and release-asset parity. No installed assess/report success is claimed while the wheel is absent. |
| Helm OCI | Publication plumbing remains at `91c7026ce8aa249e88b79bb6353bdebb857c82cc`; no final stable server SemVer/public AOT identity is supplied by this lane. | The exact stable-server-dependent chart publication/anonymous pull receipt is candidate-dependent. Release that receipt criterion until the matching server exists; do not invent an appVersion or repin to a nightly. Helm remains Preview under the adopted amendment. |
| QGIS | Repository is still **private**. PR #29 remains open at `718e2180c6370c71483e155505ef3d64859ad5ab`. | Complete visibility/history review, a verified signing identity, green CI, exact release ZIP, clean-profile QGIS smoke/screenshots, and OSGEO submission/approval. Historical billing failure is not evidence of the current account balance. No visibility change or marketplace submission was performed. |
| Python SDK/admin | PyPI resolves `honua-sdk 0.1.11` and `honua-admin 0.1.8`; filenames and hashes retained in the observation file. | Previously published prerequisites; this recheck does not claim a fresh installed Python test or candidate certification. |

The SDK denominator is the 16 package projects at
[`a07d918ee121668029fea6b4e8106fe8cb3daae1`](https://github.com/honua-io/honua-sdk-dotnet/tree/a07d918ee121668029fea6b4e8106fe8cb3daae1/src).
`Meta` is a CI project label, not a seventeenth NuGet package.

## SDK publication boundary, refreshed

[SDK PR #329](https://github.com/honua-io/honua-sdk-dotnet/pull/329) explains why
immutable `dotnet-sdk-v1.6.1` cannot be moved and advances the attempted first
publication to `1.6.2`. Its [tag run](https://github.com/honua-io/honua-sdk-dotnet/actions/runs/33599733064)
passed Release Smoke but failed Staging Configuration Preflight; publication was
skipped. The five staging variables were configured later on September 2, but the
September 6 repository secret-name inventory still lacks both
`HONUA_STAGING_API_KEY` and `HONUA_STAGING_BEARER_TOKEN`. The `staging` environment
secret list is empty. Supply the authorized staging credential through GitHub
settings, never in source, an issue, a dispatch input or chat.

The [latest green release dry run](https://github.com/honua-io/honua-sdk-dotnet/actions/runs/33946201526)
is at open [PR #337](https://github.com/honua-io/honua-sdk-dotnet/pull/337),
`78de4d4bbb19cf9691f2c7be009085dbfd548b7e`; both staging integration and publication
were skipped. It cannot certify an immutable tag at another SHA. Land any required
publication fixes through review, select the protected-source transaction, and
retain its complete gates before publishing. Existing tags must never be moved.

The merged SDK publication design uses **nuget.org Trusted Publishing**. The old
instructions to create `NUGET_API_KEY` and author-signing secrets are superseded;
do not reintroduce that path. `public-nuget` currently requires a human reviewer,
disables admin bypass and restricts deployment refs. The external nuget.org owner
policy must match its approved repository/workflow/environment/package scope;
this lane did not inspect or modify the private owner account.

## Independent gRPC checks

The immutable [v1.0.0 release](https://github.com/honua-io/geospatial-grpc/releases/tag/v1.0.0)
binds source `0f701ecc6b0c41a5ea43e2dff3c46ce654312576`. Original release receipts
are retained under `certification/first-publication/grpc-1.0.0/`.

- [Anonymous download verification](../certification/first-publication/grpc-download-verification-2026-09-06.json): NuGet SHA-256 `69ab1ae0212a81bba6018bbd01789698f8e2f0c1d5134fec1eab3cefe841979f`; BSR archive SHA-256 `7f68c40e1308dc47aff5cf87eb30ba220b513830e0c7677287687744adc970ef`. Assertions compare downloads to the independently published receipt. The NuGet repository-signed hash correctly differs from the unsigned local pack.
- [Fresh Windows restore](../certification/first-publication/grpc-restore-windows-2026-09-06.json): native Windows `dotnet restore`, exact `[1.0.0]` reference, a cleared nuget.org-only source list, fresh package/HTTP caches, `--no-cache -maxcpucount:4`. Assertions verify resolved package identity, the sole source, and restored nupkg SHA-256.
- These checks do not execute a live gRPC server or certify the platform candidate. The release's original BSR consumer receipt remains separate from the fresh archive-byte check.

## Issue disposition

No missing pre-cut package, credential, ownership, visibility review, signing
implementation or clean-consumer prerequisite is released by the absence of a
candidate. Only the exact-server-dependent Helm publication receipt and final
candidate-bound client certification must wait for their immutable candidate.
All other unresolved acceptance criteria remain owned by
`windows-release-publication-hardening` and blocked at the boundaries above.
Do not close #57 until every in-scope coordinate has its required public and
anonymous consumer receipts.
