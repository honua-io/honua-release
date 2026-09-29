# First-publication wave: anonymous preflight for #57

Observed 2026-09-26 UTC; receipt last refreshed 2026-09-29T19:34Z UTC (the probe observed `@honua/sdk-js@0.1.11-beta.0` published and nuget.org `Geospatial.Grpc` 1.0.2; earlier refresh 2026-09-29T10:54Z, when the manifest re-pinned
honua-iac to v0.2.0 and the probe observed `create-honua-app@0.1.5` and `honua-migrate` 0.8.0). **BLOCKED; #57 remains open.** This supersedes the 2026-09-06
checkpoint for registry availability. The files under
`certification/first-publication/` from that date stay as historical evidence. The
machine receipt is
[preflight-2026-09-26.json](../certification/first-publication/preflight-2026-09-26.json).

```bash
python3 tools/first_publication_preflight.py --check
```

The probe sends no `Authorization` header. `manifest-validate` runs it. A `published`
row is the sha256 of bytes the probe downloaded. A `listed` row is an index only.
Drift from the committed receipt fails the check. A missing coordinate is a named
blocker, not a fabricated digest, chart, or plugin ZIP.

## Satisfied on this probe

| Publication | Evidence |
|---|---|
| nuget.org `Geospatial.Grpc` 1.0.0 | Downloaded nupkg sha256 `69ab1ae0212a81bba6018bbd01789698f8e2f0c1d5134fec1eab3cefe841979f` (matches the retained receipt). The index now also lists 1.0.2 (geospatial-grpc v1.0.2); the manifest pin is unchanged. |
| `buf.build/honua-io/geospatial-grpc:f52df33b3b5d4723881ad0bacaf8a754` | Downloaded archive sha256 `7f68c40e1308dc47aff5cf87eb30ba220b513830e0c7677287687744adc970ef`. |
| PyPI `honua-sdk` 0.1.11 and `honua-admin` 0.1.8 | Downloaded wheel and sdist sha256 match `clientArtifacts`. |
| PyPI `honua-migrate` 0.8.0 | Downloaded `honua_migrate-0.8.0-py3-none-any.whl` sha256 `6d4b6b8ba0168dd6a875c830353ee9f671a7850f37100fb9d28ec1e2547b77a9` and sdist sha256 `9c5b7f4acf0311f805149f9b9ee18b221a456130f9e488643063d58bc0417a44`. Not a `clientArtifacts` pin. |
| npm `@honua/sdk-js` 0.1.9-beta.0 | Version metadata integrity matches `clientArtifacts`. Byte install stays on `verify_client_artifacts.py`. |
| honua-iac v0.2.0 | `registry.terraform.io/v1/modules/honua-io` is HTTP 404. The supported coordinate is the manifest Git archive; the downloaded tag tarball (sha256 `c27d26acdb70717bb9e54c3946708f59f2d109d2ce4807b445e1fa00cf849f89`) matches `artifactSha256`. Git-URL sourcing is the path this repo already publishes. |
| PyPI `honua-esri-assess` | Still HTTP 404. The retired name is not the migrate coordinate. GitHub still has the old `honua-esri-assess-*` releases; `honua-migrate-v0.7.1` also exists. |

nuget.org lists all 16 `Honua.Sdk*` IDs, including `Honua.Sdk.Studio`, at `1.6.4`, `1.7.0`,
`1.8.0`, `1.9.0`, and `1.10.0`. That is an index. The only SDK nupkg this probe hashed is
`Honua.Sdk` 1.10.0 (`dcc6bb0477e64982854f38ee704709abafae43e373d7f963af15b23c540859aa`)
and its symbol package
(`c1e8d581611bf161942adef4858214466a83247f0c12eca452926b882f010cb2` from
`globalcdn.nuget.org`; the flat-container `.snupkg` URL returned 404). Those bytes are
not the manifest pin.

## Still blocked

| Publication | Kind | Boundary |
|---|---|---|
| nuget.org `Honua.Sdk` **1.6.0** (and `Honua.Sdk.Studio` 1.6.0) | `blocked-on-train-binding` | `clientArtifacts.honua-sdk-dotnet` pins `1.6.0` on `github-packages`. Both pinned nupkg URLs returned 404. `components.honua-sdk-dotnet.version` is `1.6.2`, which nuget.org also does not serve. Later public versions are not recorded as the train. honua-console#356 cannot anonymously restore this pin. Rebinding is an operator decision. No nuget.org credential is required to see the gap. |
| nuget.org `Honua.Mobile.Sdk`, `Honua.Mobile.Offline`, `Honua.Mobile.Maui` | `blocked-on-operator` | honua-mobile `publish-dotnet-mobile.yml` pushes only to `https://nuget.pkg.github.com/honua-io/index.json` with `secrets.GITHUB_TOKEN`. It has no nuget.org Trusted Publishing step. This repo has no credential that can publish those IDs. |
| npmjs `@honua-io/embed` | `blocked-on-operator` | honua-mobile `publish-npm-embed.yml` publishes only to `https://npm.pkg.github.com` with `NODE_AUTH_TOKEN` from `secrets.GITHUB_TOKEN`. It has no npmjs publish. |
| `oci://ghcr.io/honua-io/charts/honua` | `blocked-on-candidate` | Anonymous GHCR pull token was denied (HTTP 403). honua-helm `release.yml` refuses `appVersion` `0.0.0` and requires a published `ghcr.io/honua-io/honua-server:v<semver>-aot` image. This repo does not invent that SemVer. |
| QGIS plugin `honua` | `blocked-on-operator` | Unauthenticated GitHub API for `honua-io/honua-qgis-plugin` returned 404, and `https://plugins.qgis.org/plugins/honua/` returned 404. CI builds a ZIP only. Visibility, signing review, the release ZIP, and OSGeo submission remain honua-qgis-plugin#29. No signing secret is recorded here. |
| `create-honua-app@0.1.5` template pins | `blocked-on-republish` | The tarball was downloaded (sha256 `a8aa82304bb1797150772540e62e2f90a41ba9e81a69746727881d5f2646d601`). Both templates pin `@honua/sdk-js@0.1.11-beta.0`, whose npm version metadata is now HTTP 200 (published, but the platform manifest pins 0.1.9-beta.0, so the template SDK pin is still off-train), and `maplibre-gl@6.4.1`, still below the GHSA-jrc7-96c5-q579 fix in 6.9.0. A replacement release has to come from honua-sdk-js. |

Do not close #57 until the blocked publications above have their own anonymous receipts.
The Helm receipt still waits on the immutable server image.
