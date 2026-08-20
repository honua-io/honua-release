# Public registry readiness

*honua-release#57. Audited against public registries and repository configuration on 2026-08-20.*

The first-publish wave is not complete. A successful source build or an authenticated install from
GitHub Packages is useful bootstrap evidence, but it is not proof that an external customer can use a
public artifact. `gate-artifact-consume.yml` therefore has two deliberately different modes:

- `bootstrap` may build a manifest-pinned source checkout when its registry artifact is absent. The
  evidence names that source as `local`.
- `strict`, used by a real release cut, accepts only `source: staging`, meaning the exact
  manifest-pinned artifact was installed from its customer-facing registry. A local fallback,
  `blocked`, or `skipped` result is a hard failure even when the locally built artifact works.

The public endpoints used by the strict gate are npmjs, nuget.org, PyPI, GHCR, GitHub Releases, and
the Buf Schema Registry. In particular, the .NET lane must use `https://api.nuget.org/v3/index.json`,
not the authenticated GitHub Packages feed, and the protobuf module coordinate is
`buf.build/honua-io/geospatial-grpc`.

## Live disposition

| Wave item | Public evidence | Disposition / remaining external gate |
|---|---|---|
| JavaScript SDK packages | npmjs carries the `@honua/*` packages, including manifest-pinned `@honua/sdk-js` `0.1.7-beta.0` | Published |
| Server container | GHCR and Docker Hub carry public server images; the platform manifest pins an immutable GHCR digest | Published |
| Python SDK + admin | PyPI carries `honua-sdk` `0.1.10` and `honua-admin` `0.1.7` | Published |
| .NET SDK | nuget.org has no `Honua.Sdk` package; the repository has no `NUGET_API_KEY` secret | Add a nuget.org-scoped API key, publish a stable public `Geospatial.Grpc`, then run the stable SDK tag workflow and retain the nuget.org-only install evidence |
| Protobuf module + .NET binding | BSR reports that `buf.build/honua-io/geospatial-grpc` does not exist; nuget.org has no `Geospatial.Grpc`; `geospatial-grpc` has neither `BUF_TOKEN` nor a NuGet publication credential configured | Create the BSR module/configure `BUF_TOKEN`; cut and publish a stable protocol version to both BSR and nuget.org before the stable SDK cut |
| Esri assessment CLI | PyPI returns 404 for `honua-esri-assess` | Configure/confirm PyPI Trusted Publishing for the current `honua-migrate` repository identity, cut the distribution, and run a clean `pipx install` smoke |
| Helm chart | The organization has no `charts/honua` GHCR package and `honua-helm` has no release | Cut a chart version, push `oci://ghcr.io/honua-io/charts/honua`, make it public, and pin that version in the platform manifest |
| QGIS plugin | `honua-qgis-plugin` has no GitHub release and its QGIS plugin page returns 404 | Cut the installable ZIP, then submit it with a QGIS plugin-repository maintainer account; GitHub credentials alone cannot perform QGIS repository approval |
| Mobile packages | `honua-mobile` has no release; its NuGet packages and `@honua-io/embed` workflow target authenticated GitHub Packages | The platform manifest marks mobile experimental and outside the certified 2026.1 set. Public NuGet/npm publication still remains on #57 and needs an explicit release disposition rather than being counted as shipped |
| Console anonymous restore | Public checkout still depends on `Honua.Sdk.*` from authenticated GitHub Packages | Unblocks when the SDK package graph, including stable `Geospatial.Grpc`, restores using nuget.org alone |

Registry publication is an external state transition and must not be inferred from a green dry run.
After each publish, update the immutable platform pin where applicable and rerun the strict release
train. Only that candidate-bound report can retire the corresponding release gate.
