# Protocol compliance and certification plan

Status: normative implementation plan  
Program: [honua-release#157](https://github.com/honua-io/honua-release/issues/157)  
Guide inventory: [cloud-native-client-inventory.yaml](cloud-native-client-inventory.yaml)

## Outcomes and certification boundaries

Honua uses two release claims:

- **Protocol/API-certified** means every required, addressable protocol/client cell for a candidate passed with fresh, candidate-bound evidence.
- **Platform-certified** means protocol/API certification passed and the broader release gates for component suites, deployment parity, upgrades, security, provenance, rollback/recovery, and teardown also passed.

Protocol/API certification is necessary but not sufficient for platform release promotion. Experimental, preview, roadmap, deprecated, and internal surfaces are reported but do not silently enter the supported denominator.

## Capability maturity policy

| State | Public claim | Required gates | Transition rule |
|---|---|---|---|
| `supported` | Supported and release-blocking | PR smoke, nightly matrix, exact-candidate release | Promote only after two consecutive green nightlies and one exact-candidate run; demote or block on an unresolved required-cell failure |
| `preview` | Public preview, not a compatibility promise | PR smoke and nightly evidence | Promote through the supported rule; demote on persistent failure or unsafe behavior |
| `experimental` | Opt-in and unstable | Owning-repo tests; nightly where a lane exists | Requires an owner, capability key, and expiry/review date |
| `roadmap` | Not implemented or not certifiable | None | Cannot appear as passing or supported |
| `deprecated` | Supported only through the declared removal window | Same gates as supported for the stated window | Removal requires published notice and compatibility policy compliance |
| `internal` | Not part of the public API surface | Owning-repo tests | Excluded from external certification and public support counts |

Every manifest row carries one state. Missing state is a schema failure. A supported row cannot be marked not-addressable merely because Honua lacks a probe; that is a certification gap.

## Normalized certification cell

The atomic denominator is one row per:

`surface + operation + canonical_client + client_version + deployment_target`

Required fields:

```yaml
capability_key: serve.example
surface: ogc-api-features
operation: collections.items.filter
maturity: supported
canonical_client: GDAL
client_lane: gdal-ogr
client_version: 3.11.4
deployment_target: local-docker
required_tier: nightly
licensed: false
addressable_by_client: true
addressability_reason: null
result: pass # pass | fail | skip | not-addressable
skip_reason: null
scenario_facets: [positive, negative, auth, pagination, limit, metadata]
contract_revision: ogc-api-features-1.0
auth_policy_revision: anonymous-v1
source_sha: null
producer_source_sha: null
image_digest: null
fixture_revision: null
evidence_uri: null
started_at: null
completed_at: null
```

Plural clients or versions in one row are invalid. Required addressable cells must pass; `skip` is never green. `not-addressable` requires a client-version-specific reason and does not excuse a missing canonical client for a supported operation.

## Gate tiers and freshness

| Tier | Required scope | Freshness and binding | Promotion behavior |
|---|---|---|---|
| PR | Changed repository tests plus changed-surface/client smoke | Exact PR head and exact dependency pins | Blocks merge only for impacted required cells |
| Nightly | All supported addressable protocol, SDK, and canonical-client cells | Completed within 7 days; exact source SHAs, fixture revision, dependency locks, and target class | Any required failure prevents nightly evidence from being promoted |
| Licensed nightly/release | Licensed clients that cannot run in ordinary CI | Completed within 72 hours; exact client version and target configuration | Required for the corresponding vendor certification claim |
| Release | Full protocol/API matrix against the candidate, then all platform gates | Run after candidate cut; exact immutable image digest and component SHAs | Missing, stale, skipped, or mismatched required evidence fails closed |

Evidence is invalidated by a change to the server image digest, protocol contract, canonical client major/minor version, fixture revision, authentication policy, deployment class, or relevant capability maturity/requirement. Re-running an unrelated lane does not refresh a cell.

## Canonical client assignments

| Surface | Required canonical proof |
|---|---|
| GeoServices REST and SOAP discovery | ArcGIS Pro/arcpy, ArcGIS API for Python, ArcGIS Maps SDK for .NET, raw REST; explicit SOAP catalog, public URL, auth, service-open, and REST-handoff cells |
| OGC Features/WFS | OGC CITE plus GDAL/OGR and QGIS; first-party SDKs where shipped |
| OGC Maps/Tiles/Styles and WMS/WMTS | OGC CITE plus QGIS and OpenLayers or MapLibre according to advertised output |
| OGC Coverages/WCS | OGC CITE plus GDAL and OWSLib |
| OGC Processes/WPS | OGC CITE plus OWSLib for WPS and the shipped Honua SDK client for OGC API Processes |
| OGC Records, EDR, SensorThings | OGC CITE plus the shipped Honua SDK client; an external maintained client must be selected, version-pinned, and added before promotion to `supported` |
| STAC | PySTAC-Client plus the shipped Honua SDK clients |
| OData | Microsoft.OData.Client plus the shipped Honua SDK clients |
| COG | Rasterio and GDAL; rio-cogeo is a producer/validator, not a substitute for reads |
| HDF5/NetCDF | h5stat metadata/statistics plus h5py and xarray consumer reads |
| Zarr | zarr, xarray, and fsspec consumer reads; Dask is required for the parallel/chunked scenario |
| GeoParquet | GeoPandas, PyArrow, and GDAL consumer reads |
| FlatGeobuf | GDAL/OGR, GeoPandas/Pyogrio, and the JavaScript FlatGeobuf reader |
| PMTiles | Python `pmtiles` reader and a browser viewer/client |
| 3D Tiles | CesiumJS browser client |
| COPC | PDAL CLI/Python `readers.copc`; tracked by server issues #2442, #3289, and #3290 |
| Kerchunk | kerchunk + fsspec reference filesystem + xarray; tracked by server issue #3378 |
| gRPC | Generated .NET, Python, and JavaScript/TypeScript clients over versioned fixtures |
| MCP | Official MCP SDK/Inspector transport checks plus the versioned geospatial-mcp conformance corpus |

An external OGC client selection must record package/repository, maintained release, supported platform, owner, operation mapping, and replacement policy. Until selected, the cell is an explicit blocking gap for a `supported` surface, not a placeholder pass.

## Scenario depth and cloud-native budgets

Operation presence alone is insufficient. Each supported operation declares applicable facets from: positive, malformed input, boundary values, authorization, pagination/cursor, result limits, CRS/axis order, media type/schema, cancellation/idempotency, and recovery. Release coverage is reported as passed required scenario cells divided by all required scenario cells, with separate negative/auth/limit percentages.

Cloud-native fixtures must declare machine-checked values rather than prose:

- `max_requests`, `max_bytes`, and `max_full_object_fraction`
- expected HTTP range behavior and cache policy
- `coordinate_abs_tolerance`, CRS, dimensions, bounds, dtype, and nodata policy
- required metadata keys/statistics and expected chunk/tile/point counts
- client package/version and fixture checksum

Budgets are fixture-specific because formats and usage profiles differ. A release fixture missing a required budget is invalid; exceeding any declared budget fails the cell.

## Evidence and metrics

The release view must publish:

- required, passed, failed, skipped, and not-addressable cells by surface/client/target/tier
- supported-operation coverage and required scenario-cell coverage
- positive, negative, auth, pagination/limit, and metadata assertion coverage
- canonical-client operation depth, not only whether a client launched
- source SHA, image digest, client version, fixture revision, timestamps, and durable evidence URI
- all exclusions with maturity and reason

Aggregate percentages never override a failed required cell. The target for release certification is 100% of required addressable cells, zero required skips, and zero stale or mismatched evidence.

## Execution sequence

1. Make the denominator executable in [honua-release#158](https://github.com/honua-io/honua-release/issues/158).
2. Implement the normalized ledger and governed guide inventory in [honua-evidence#22](https://github.com/honua-io/honua-evidence/issues/22).
3. Repair the current cloud-native lane and add canonical consumers in [honua-server#3377](https://github.com/honua-io/honua-server/issues/3377).
4. Complete the Esri addressable matrix and SOAP discovery boundary in [honua-esri-compat#74](https://github.com/honua-io/honua-esri-compat/issues/74).
5. Deliver Kerchunk through [honua-server#3378](https://github.com/honua-io/honua-server/issues/3378) and COPC through [#2442](https://github.com/honua-io/honua-server/issues/2442), [#3289](https://github.com/honua-io/honua-server/issues/3289), and [#3290](https://github.com/honua-io/honua-server/issues/3290).
6. Join existing SDK, gRPC, MCP, CITE, deployment, upgrade, security, and provenance producers.
7. Run the exact-candidate release matrix, publish immutable evidence, and promote only after both certification boundaries pass.

## Owned work and exit artifacts

| Work | Owning repo | Tier | Dependencies | Exit artifact |
|---|---|---|---|---|
| Program and dependency map | honua-release #157 | all | all rows below | closed program checklist and release decision |
| Manifest and gate semantics | honua-release #158 | all | capability vocabulary | versioned schema, validator, aggregate gate |
| Evidence ledger and guide inventory | honua-evidence #22 | nightly/release | #158 | public normalized ledger and evidence index |
| Cloud-native lane repair/clients | honua-server #3377 | PR/nightly/release | #158, #22 | green pinned workflow and normalized receipts |
| Kerchunk | honua-server #3378 | nightly/release after promotion | raster/storage security design | implemented surface and client receipts |
| COPC | honua-server #2442/#3289/#3290 | nightly/release after promotion | 3D capability work | PDAL-verified range-serving receipts |
| Esri and SOAP discovery | honua-esri-compat #74 | PR/nightly/release | Windows licensed runner, #158 | zero required skips and exact-candidate cert JSON |
| SDK matrices | sdk-js #39/#1113, sdk-python #21/#197, sdk-dotnet #31/#294, server #3381 | PR/nightly/release | server candidate and #158 | versioned SDK coverage snapshots |
| gRPC | geospatial-grpc #88 (parent #18) | PR/nightly/release | generated clients and shared fixtures | fixture-versioned client receipts |
| MCP | geospatial-mcp #78 (parent #1/#5) | PR/nightly/release | conformance corpus | deterministic transport/eval receipts |

## Release handling

Published 2026.1-rc.2 evidence remains valid as historical evidence with its disclosed defects. Defect fixes target 2026.1. Adoption of this plan is forward-looking: no capability becomes certified under the new vocabulary until its normalized evidence exists and the applicable gates pass.
