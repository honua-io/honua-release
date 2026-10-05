# First-publication wave: anonymous preflight for #57

Observed 2026-10-05T07:01:30Z UTC (receipt
[preflight-2026-10-02.json](../certification/first-publication/preflight-2026-10-02.json),
after the R25 advances of `clientArtifacts` to the newest published client identities: npm
`@honua/sdk-js` 0.1.13 and `@honua/mcp-server` 0.1.13, PyPI `honua-sdk` 0.1.13 and `honua-admin`
0.1.10, and nuget.org `Honua.Sdk` 1.10.2. The receipt was first written 2026-10-03T00:03:58Z
for the 0.1.12 / 1.10.1 set, which cleared the former `blocked-on-train-binding` row). The previous
receipt, [preflight-2026-09-26.json](../certification/first-publication/preflight-2026-09-26.json),
stays as historical evidence; it was observed 2026-09-26 UTC and last refreshed 2026-09-29T22:36Z UTC (operator ruling 2026-09-29: mobile is Experimental and its public publication is deferred out of 2026.1, so the mobile channels are now `deferred-experimental`; the probe also observed nuget.org `Geospatial.Grpc` 1.0.3; earlier refresh 2026-09-29T19:34Z, when the probe observed `@honua/sdk-js@0.1.11-beta.0` published and nuget.org `Geospatial.Grpc` 1.0.2; earlier refresh 2026-09-29T10:54Z, when the manifest re-pinned
honua-iac to v0.2.0 and the probe observed `create-honua-app@0.1.5` and `honua-migrate` 0.8.0). **BLOCKED; #57 remains open.** This supersedes the 2026-09-06
checkpoint for registry availability. The files under
`certification/first-publication/` from that date stay as historical evidence. The
machine receipt `--check` compares against is
[preflight-2026-10-02.json](../certification/first-publication/preflight-2026-10-02.json).

```bash
python3 tools/first_publication_preflight.py --check
```

The probe sends no `Authorization` header. `manifest-validate` runs it. A `published`
row is the sha256 of bytes the probe downloaded. A `listed` row is an index only.
Drift from the committed receipt fails the check. A missing coordinate is a named
blocker, not a fabricated digest, chart, or plugin ZIP. A `deferred-experimental` row is a
channel whose component `platform-manifest.yaml` lists under `experimental:` (status
`experimental`); see [Deferred: experimental components](#deferred-experimental-components).

## Satisfied on this probe

| Publication | Evidence |
|---|---|
| nuget.org `Geospatial.Grpc` 1.0.0 | Downloaded nupkg sha256 `69ab1ae0212a81bba6018bbd01789698f8e2f0c1d5134fec1eab3cefe841979f` (matches the retained receipt). The index now also lists 1.0.2 and 1.0.3; the manifest pin is unchanged. |
| `buf.build/honua-io/geospatial-grpc:f52df33b3b5d4723881ad0bacaf8a754` | Downloaded archive sha256 `7f68c40e1308dc47aff5cf87eb30ba220b513830e0c7677287687744adc970ef`. |
| nuget.org `Honua.Sdk` **1.10.2** train (`train:honua-sdk-dotnet`, `published`) | `clientArtifacts.honua-sdk-dotnet` pins `1.10.2` on `nuget.org`. Every one of the 16 `Honua.Sdk*` indexes lists 1.10.2 (the umbrella pins exact same-version dependencies on the set). `Honua.Sdk` and `Honua.Sdk.Studio` nupkgs were downloaded and bound to their NuGet catalog SHA-512 and nuspec id/version: `honua.sdk.1.10.2.nupkg` sha256 `f699463e9dc1119024b216478654a7d91a5683cc6addcf494fc8263d2ee6f88c` (matches the manifest digest) and `honua.sdk.studio.1.10.2.nupkg` sha256 `7b3c7bf94e4e0c8c23bc3acfa35e6d269dd6f407620c6bf9058ec2ae5a1807a5`. |
| PyPI `honua-sdk` 0.1.13 and `honua-admin` 0.1.10 | Downloaded wheel and sdist sha256 match `clientArtifacts`. |
| PyPI `honua-migrate` 0.8.0 | Downloaded `honua_migrate-0.8.0-py3-none-any.whl` sha256 `6d4b6b8ba0168dd6a875c830353ee9f671a7850f37100fb9d28ec1e2547b77a9` and sdist sha256 `9c5b7f4acf0311f805149f9b9ee18b221a456130f9e488643063d58bc0417a44`. Not a `clientArtifacts` pin. |
| npm `@honua/sdk-js` 0.1.13 | Downloaded `sdk-js-0.1.13.tgz` sha256 `b9289058f6e035604d491edb94eef0018171bb15f05fe7af8ce0a8dee8325553`; its SRI matches `clientArtifacts` integrity, registry `gitHead` is the pinned `sourceSha` `718f5391` (the branch-recovery publish source, not tag `js-sdk-v0.1.13` `33c0b702`), and `package/package.json` names `@honua/sdk-js` 0.1.13. |
| npm `@honua/mcp-server` 0.1.13 | Downloaded `mcp-server-0.1.13.tgz` sha256 `aa18a3487697d745f42c0bc96c8788fa90085b03780483fddbcbccf088a72c62`; SRI, `gitHead` `18494f45` and package name/version match `clientArtifacts.honua-mcp-server`. |
| honua-iac v0.2.0 | `registry.terraform.io/v1/modules/honua-io` is HTTP 404. The supported coordinate is the manifest Git archive; the downloaded tag tarball (sha256 `c27d26acdb70717bb9e54c3946708f59f2d109d2ce4807b445e1fa00cf849f89`) matches `artifactSha256`. Git-URL sourcing is the path this repo already publishes. |
| PyPI `honua-esri-assess` | Still HTTP 404. The retired name is not the migrate coordinate. GitHub still has the old `honua-esri-assess-*` releases; `honua-migrate-v0.7.1` also exists. |

nuget.org lists all 16 `Honua.Sdk*` IDs, including `Honua.Sdk.Studio`, at `1.6.4`, `1.7.0`,
`1.8.0`, `1.9.0`, `1.10.0`, `1.10.1`, and `1.10.2`. The newest `Honua.Sdk` is the manifest pin; its
symbol package hashed `47aaaa30994e9ec7f93b9063a066f5c59a1e8737c8362e8c4017aaf6990248ef`
from `globalcdn.nuget.org` (the flat-container `.snupkg` URL returned 404).

## Still blocked

| Publication | Kind | Boundary |
|---|---|---|
| `oci://ghcr.io/honua-io/charts/honua` | `blocked-on-candidate` | Anonymous GHCR pull token was denied (HTTP 403). honua-helm `release.yml` refuses `appVersion` `0.0.0` and requires a published `ghcr.io/honua-io/honua-server:v<semver>-aot` image. This repo does not invent that SemVer. |
| QGIS plugin `honua` | `blocked-on-operator` | Unauthenticated GitHub API for `honua-io/honua-qgis-plugin` returned 404, and `https://plugins.qgis.org/plugins/honua/` returned 404. CI builds a ZIP only. Visibility, signing review, the release ZIP, and OSGeo submission remain honua-qgis-plugin#29. No signing secret is recorded here. |
| `create-honua-app@0.1.6` template pins | `blocked-on-republish` | The tarball was downloaded (sha256 `0dbb32e7432ef2692e5c9a30e8fddf36ea3fec6451d7873a38a92ad37b6e0a1f`). Both templates pin `@honua/sdk-js@0.1.12`, which is published (HTTP 200) but off-train now that the platform manifest pins 0.1.13, and `maplibre-gl@6.4.1`, still below the GHSA-jrc7-96c5-q579 fix in 6.9.0. A replacement release has to come from honua-sdk-js ([honua-sdk-js#1943](https://github.com/honua-io/honua-sdk-js/issues/1943)). |

## Deferred: experimental components

Operator ruling (2026-09-29): Honua mobile is Experimental and its public publication is
deferred out of 2026.1. The preflight derives this from the manifest, not from a hard-coded
exception. Every channel whose component is listed under the top-level `experimental:` block
with `status: experimental` is recorded as `deferred-experimental` with the component and
the manifest reason (`experimental.<name>.reason` when present, otherwise a reason naming
the block). Those rows stay in the receipt so the deferral is visible, are not in the
required channel set, and produce no blocker. The receipt's
`deferred_experimental_components` must match the manifest; `--check` fails otherwise.

| Publication | Component | Probe |
|---|---|---|
| nuget.org `Honua.Mobile.Sdk`, `Honua.Mobile.Offline`, `Honua.Mobile.Maui` | `honua-mobile` | Flat-container index HTTP 404. |
| npmjs `@honua-io/embed` | `honua-mobile` | Registry HTTP 404. |

A `deferred-experimental` row never carries package bytes or an `evidence_class`, and an
experimental component's channel cannot be recorded as `published`; the audit fails on
either. If the registry starts listing one of these packages, the listing is recorded as
`registry_versions` (drift), not as GA evidence. The honua-mobile publishing workflow
(honua-mobile#361) stays merged for later; no nuget.org Trusted Publisher or npm scope
decision is needed for 2026.1.

When `honua-mobile` moves out of `experimental:`, these channels become required again with
no code change: a missing registry coordinate is `blocked-on-operator` (honua-mobile
`publish-dotnet-mobile.yml` pushes only to GitHub Packages and has no nuget.org Trusted
Publishing step; `publish-npm-embed.yml` has no npmjs publish), and a listed package must be
downloaded before it is recorded.

Do not close #57 until the blocked publications above have their own anonymous receipts.
The Helm receipt still waits on the immutable server image.
