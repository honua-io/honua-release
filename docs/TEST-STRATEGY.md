# Honua Platform Test & Certification Strategy

Companion to RELEASE-ENGINEERING-PLAN.md. Defines the layered test architecture, the deploy-target
matrix (and how to avoid its combinatorial explosion), the canonical scenarios, and an AWS-first build.

## The problem
The release "full test suite" gate means **every repo's tests + cross-component integration + cross-cloud
integration** — and today the cross-component and cross-cloud layers largely don't exist. Each repo tests in
isolation (SDK mocks server, server mocks clients, IaC never boots a server), so the seams — where almost
every audit bug actually lives — are untested. A naive full cross-product is days-long and bankrupting.

## Three layers (stacked, increasing cost, decreasing cache-ability)

1. **Per-repo suites — cache-able.** Deterministic, content-addressed; run only what changed since the last
   green manifest; reuse cached results for unchanged components. Kept green continuously (nightly), so the
   release train mostly *confirms*.
2. **Cross-component seam scenarios — the high-ROI gap.** Real server + real DB + SDKs installed from a
   staging registry, NO mocks at the seams. Tests the contracts *between* components (IaC→server, server↔SDK,
   SDK↔contract-version, app→server). **`docker-compose` tier is cheap, fast, per-PR-able and catches most
   seam bugs — build this first, on a laptop.**
3. **Cross-cloud integration & parity — real infra, expensive, un-cache-able.** Ephemeral real cloud
   environments via the actual IaC; proves "deploys + behaves correctly across environments." Nightly/release.

## Deploy-target matrix (7 targets)
| # | Target | Cost / spin-up | Module |
|---|---|---|---|
| 1 | local docker (compose) | ~free, seconds | docker-compose |
| 2 | AWS serverless (Lambda/API GW) | cheap, scale-to-zero | aws-serverless |
| 3 | AWS ECS (Fargate) | medium | aws-ecs |
| 4 | AWS EKS (k8s) | high + slow (~10-15m control plane) | aws-eks + helm |
| 5 | Azure ACA | medium | azure-aca |
| 6 | Azure AKS (k8s) | high + slow | azure-aks + helm |
| 7 | Azure Functions | cheap | azure-functions |

## Decompose the axes — do NOT run the full cross-product
Full product (7 targets × 3 SDKs × compat-window × scenarios) = hundreds of cells / days / $$$. Instead test
each axis independently:
- **SDK × scenario matrix → one reference target = local docker.** PRs run changed-SDK and
  changed-surface seam smoke. The full three-SDK scenario and compatibility-window matrix runs nightly
  and for release candidates.
- **Deploy-target parity → the *canonical* (slim) scenario set on each target, assert identical results.**
  ~7 cells, same scenarios; proves "behaves the same everywhere" without re-running the SDK matrix per target.
- **IaC→server + upgrade seams → per target** (or representative subset); these are target-specific.
- **Cross-cloud parity (AWS≡Azure)** lights up once Azure is in — same harness, more cells.

Net: a few dozen cells, not hundreds. Local docker does the heavy per-PR lifting; cloud cells run nightly and
the release train consumes "green within last N hours."

## Canonical scenarios (seed each from a real audit finding → permanent regression test)
- **Sync round-trip / no-duplicates** (catches honua-collect#102): edit → sync → edit → sync → restart → sync
  ⇒ assert exactly one server feature.
- **GeoServices error surfacing** (catches sdk-js#309, sdk-python#122): force a 200+`{error}` ⇒ every SDK must
  raise, not return success; the error metric must increment (ties telemetry gate).
- **gRPC-web authenticated call** (catches sdk-js#308): query over grpc-web with apiKey ⇒ authorized + timeout/retry honored.
- **Published-artifact consumption** (catches sdk-js#310, iac#81, grpc#45): npm/nuget/pip install from staging;
  `terraform init` the customer tarball; `docker pull && run && /healthz`; run codegen.
- **Contract-compat window**: SDK v(N-1) against server vN ⇒ pass within supported window.
- **Upgrade**: deploy prior platform release → apply candidate → migrate (forward + rollback) → old clients still work.
- **Deploy-target parity**: the canonical set runs identically on every target.

## Cadence & cost control
- Per-PR: per-repo changed tests plus minimal changed-surface compose seam smoke.
- Nightly: cloud parity matrix (continuous-green), so the train just labels the latest green.
- EKS/AKS least often (control-plane cost + slow); serverless/Fargate-spot preferred.
- **OIDC into AWS/Azure (no static creds)**, per-run isolated accounts/RGs, ephemeral tagging + a **teardown
  reaper** (guarantee nothing lingers on credits), time-boxes, cost-budget alarms.

## Build order (AWS-first — you have credits)
- **Phase A:** local docker seam tier — changed SDK/surface smoke per PR; full SDK × scenario nightly.
- **Phase B:** AWS **serverless** parity + IaC-seam + upgrade (cheapest cloud; scale-to-zero ideal for ephemeral).
- **Phase C:** add AWS **ECS**, then **EKS** (EKS weekly, not nightly).
- **Phase D:** Azure ACA/AKS/Functions → cross-cloud parity assertion goes live.

## Where it lives
A neutral `honua-e2e` (or in the release repo), parameterized by the **platform manifest** so it tests the exact
pinned set. It is the **executable compatibility matrix**: a matrix row is only credible if a scenario actually
runs that pairing. No component repo owns cross-component tests (it would just mock the others again).

---

## Protocol compliance and certification program

> **Normative contract:** [PROTOCOL-CERTIFICATION-PLAN.md](PROTOCOL-CERTIFICATION-PLAN.md) resolves the
> independent-review findings and governs maturity, denominator, freshness, client assignment, tier
> promotion, and evidence semantics. If earlier prose conflicts with that contract, the plan takes precedence.

This section defines how Honua proves the supported public API and format surface without turning ordinary
pull-request CI into the full certification lab. It covers server protocols, official Honua SDKs, licensed GIS
clients, standards suites, and cloud-native formats.

### Proof vocabulary

These terms are intentionally distinct. Passing one class of proof must not be reported as another.

| Claim | Required proof |
|---|---|
| Implemented | Runtime behavior plus route or operation integration coverage |
| Conformant | Every applicable assertion in an official ETS or validator profile passes |
| Interoperable | A named, versioned canonical client consumes the live surface successfully |
| Honua-certified | All required release-tier evidence is bound to one immutable platform candidate |
| Vendor-certified | An external vendor or standards body has completed its formal certification process |

OGC CITE evidence is conformance evidence, not an OGC certification claim until the result is submitted
through the OGC Compliance Program. Licensed ArcGIS evidence is Honua's ArcGIS compatibility evidence, not
an Esri-issued certification.

### Required proof for each supported surface

Every supported public surface must have a row in the executable compatibility matrix and must declare the
following fields or an explicit, reviewed `not-applicable` rationale:

| Proof dimension | Requirement |
|---|---|
| Route coverage | Every registered endpoint is invoked by an integration test |
| Operation coverage | Every advertised logical operation and material parameter family is exercised |
| Scenario depth | Positive, negative, auth, paging, CRS, malformed-input, and limit behavior as applicable |
| Standards conformance | Official ETS or validator where one exists |
| Canonical-client interoperability | At least one independent ecosystem client consumes the live surface |
| Real-client certification | Licensed or desktop client where that is how customers consume the surface |
| Release binding | Evidence records the exact server SHA, image digest, fixture digest, and client version |
| Freshness | Evidence meets the tier's freshness window |
| Governance | Every skip, exception, and gap has an owner and tracking issue |

Where no meaningful independent client exists, the row must name the substitute proof, such as an official
validator or a generated protocol client. The matrix must not invent a nominal "canonical client" merely to
fill a cell.

### Three compliance levels

The tiers are cumulative. A release consumes PR and nightly evidence only when it is still valid for the
candidate inputs; it reruns any lane whose result cannot be safely reused.

| Level | Purpose | Blocking scope | Cost controls |
|---|---|---|---|
| PR | Prevent local contract and behavior regressions | Changed route/operation tests, Fast integration tests, architecture and matrix drift, client compile/contract tests, inexpensive canonical-client smoke | Change-aware selection, no licensed desktop, no full CITE, no cloud matrix |
| Nightly | Continuously prove broad interoperability and find drift early | Full server shards, containerized canonical clients, browser suites, SDK matrices, PyQGIS/GDAL, STAC validators, cloud-native validators, scheduled conformance suites | Parallel scheduled lanes, pinned tool caches, bounded fixtures, cloud targets on separate cadence |
| Release | Certify one immutable candidate | Exact-image CITE, full canonical-client matrix, licensed ArcGIS clients, required desktop/BI evidence, artifact consumption, upgrade, security, provenance, and evidence publication | Reuse only SHA/digest-equivalent fresh results; expensive work runs once per candidate |

PR CI remains deliberately small. Adding a release requirement does not imply adding it to the PR gate.

### Federated certification manifest

`honua-release` owns a generated `public-surface-certification.v1.json` that joins, rather than replaces, the
authoritative component evidence:

- `honua-server` feature catalog, capability matrix, public-interface proof ledger, CITE status, and client
  certification matrix.
- `honua-esri-compat` operation matrices and licensed evidence.
- Official JavaScript, Python, and .NET SDK compatibility results.
- Cloud-native format validator and client evidence.
- `platform-manifest.yaml`, `compatibility-matrix.yaml`, candidate image digests, and artifact versions.

Each row contains at least:

```text
surface_id
capability_key
maturity
protocol
protocol_version
operation_or_feature_id
addressable_by_client
canonical_clients
standards_suites
required_proof_classes
execution_lane
gate_level
fixture_id
client_version
server_sha
image_digest
status
skip_reason
evidence_uri
evidence_timestamp
freshness
owner
tracking_issue
```

Generated documentation and certification percentages use this manifest as the denominator. An evidence file
that is not joined to a supported surface is visible as orphaned evidence; a supported surface without evidence
is visible as a gap.

### Gate integrity prerequisites

The following work precedes expansion because a broken or misleading gate cannot support certification:

| Priority | Work item | Exit condition |
|---|---|---|
| P0 | Pin the cloud-native validator toolchain | No floating `@latest`; versions and checksums recorded |
| P0 | Repair the cloud-native nightly | Three consecutive scheduled green runs with uploaded evidence |
| P0 | Correct the FlatGeobuf exemption | Closed issue exemptions removed; live GDAL read-back is hard-gated |
| P0 | Wire the Cesium suite into CI | All declared Cesium client/protocol pairs execute nightly |
| P0 | Fix browser fixture schema drift | Browser stack seeds cleanly against the current schema |
| P0 | Enforce the global expected-lane set | Missing separate or cross-repo lanes fail aggregation |
| P0 | Enforce skip policy | A required operation changing to `skip` fails the owning gate |
| P0 | Enforce release binding and freshness | No `unknown`, stale, or different-image evidence can certify an RC |
| P0 | Correct summaries | "All passed" cannot be emitted when a required lane did not run |

### Protocol and client plan

#### GeoServices REST and Esri clients

| Surface | Required canonical clients | Completion requirement |
|---|---|---|
| FeatureServer | ArcGIS Pro/ArcPy, ArcGIS API for Python, ArcGIS Maps SDK for .NET or JS, Esri Leaflet | All supported client-addressable query, edit, attachment, sync, schema, and rendering operations pass |
| MapServer | ArcGIS Pro, ArcGIS API for Python, ArcGIS Maps SDK, Esri Leaflet | Discovery, export, identify, query, legend, style, and refresh pass |
| ImageServer | ArcGIS Pro/ArcPy, ArcGIS API for Python `ImageryLayer`, Rasterio for raster output | Discovery, export, identify, samples, multidimensional, and supported raster-function paths pass |
| GeometryServer | ArcGIS API for Python and ArcPy | Projection, transformation, buffer, simplify, relation, and error behavior pass |
| GPServer | ArcPy and ArcGIS API for Python | Synchronous, asynchronous, upload, polling, result, cancellation, and failure lifecycles pass |
| GeocodeServer | ArcPy and ArcGIS API for Python | Forward, reverse, suggest, and batch operations execute rather than skip |
| VectorTileServer | ArcGIS clients and MapLibre | Metadata, style, tile retrieval, and rendering pass |
| SceneServer/I3S | ArcGIS Pro or Maps SDK | A real scene loads, traverses content, and renders |
| VersionManagementServer | ArcGIS Pro | Create, start, read, reconcile, post, and delete lifecycle passes |
| Portal/Sharing | ArcGIS API for Python and ArcGIS Pro | Anonymous and authenticated discovery, item resolution, tokens, and access projection pass |
| NAServer | ArcPy network-analysis client | Implement real advertised routing behavior or mark the surface unsupported |
| Mobile/Field Maps | Licensed Field Maps run | Authentication, discovery, offline package, edits, and synchronization pass |

The Esri matrix is complete when 100% of supported, client-addressable operation cells pass. `not-applicable`
is allowed only when the client cannot issue the operation; required operations may not certify as `skip`.

#### OGC protocols

| Surface | Standards proof | Canonical clients |
|---|---|---|
| OGC API Features | Official CITE profile | GDAL/OGR, PyQGIS, OpenLayers |
| OGC API Tiles | Official CITE profile | OpenLayers, Cesium, MapLibre |
| OGC API Maps | Declared-class integration suite until an ETS exists | OpenLayers, Cesium, QGIS |
| OGC API Coverages | Declared-class integration suite until an ETS exists | GDAL plus Rasterio/xarray where applicable |
| OGC API Records | Declared-class integration suite | A selected maintained OGC Records client |
| OGC API Processes | Declared-class integration suite | A selected maintained Processes client plus a Honua SDK |
| OGC API Styles | Claimed-class integration suite | MapLibre style parser and renderer |
| OGC API EDR | Declared-class integration suite | A selected maintained EDR client |
| SensorThings 1.1 | Protocol contract suite | A selected maintained SensorThings client |
| WFS 1.0, 1.1, and 2.0 | Official CITE profiles | GDAL, PyQGIS, OpenLayers |
| WMS 1.1.1 and 1.3 | Official CITE profiles | GDAL/QGIS, OpenLayers, Cesium |
| WMTS 1.0 | Official CITE profile | GDAL/QGIS, OpenLayers, Cesium |
| WCS 2.0.1 | Official CITE profile | GDAL and a selected WCS client |
| WPS 2.0.2 | Official CITE profile when qualifying | A selected WPS client exercising sync and async lifecycles |
| KML, GML, and GeoPackage | Official applicable CITE profiles | GDAL/OGR and QGIS read-back |

All public CITE profiles require 100% reported assertions passing with zero failures, skips, or indeterminate
results. Protocols without an official ETS require a documented substitute and must not be described as CITE
certified.

#### Cloud-native formats

Cloud-native certification is bidirectional. Producer validation alone does not prove that Honua can consume a
registered source, and an internal parser test alone does not prove that ecosystem clients can consume Honua's
output.

| Format | Canonical tools and clients | Required end-to-end evidence |
|---|---|---|
| COG | `rio-cogeo`, Rasterio, GDAL; scheduled R `stars` and `terra` compatibility | Validate fixture, register cloud source, assert range reads, compare bounded pixels through ImageServer/WCS/Coverages |
| HDF5 | `h5stat`, `h5dump`, h5py, fsspec/ROS3 | Inspect file layout, register source, read a bounded slice, convert to Zarr, and compare values |
| NetCDF4 | `ncdump`, `netCDF4`, xarray, GDAL multidimensional tools | Inspect metadata, register/refresh, convert to Zarr, and compare axes, attributes, and values |
| Zarr | Python `zarr`, xarray, fsspec | Open v2/v3 stores, read spatial/temporal/vertical subsets, and compare API output |
| Kerchunk | Kerchunk, fsspec reference filesystem, xarray | Register a safe reference, open a bounded virtual slice, and compare source and served values |
| COPC | PDAL CLI and Python `readers.copc` | Register/import, perform remote range and spatial reads, and compare CRS, dimensions, bounds, and counts |
| GeoParquet | `gpq`, GeoPandas, PyArrow, fsspec, GDAL | Validate metadata, decode live output, verify CRS/geometry/attributes, and exercise import-to-query round trip |
| GeoArrow | PyArrow/GeoArrow and Apache Arrow JS | Decode the live stream and verify schema, geometry, CRS, null, and batch behavior |
| FlatGeobuf | GDAL/Pyogrio/GeoPandas and JS `flatgeobuf.deserialize(url, bbox)` | Validate live output, decode all features, and prove spatial HTTP range access |
| PMTiles | `pmtiles verify`, Python PMTiles reader, MVT decoder, MapLibre | Fetch through the public HEAD/range endpoint, decode a real tile, and render it |
| 3D Tiles | 3D Tiles/glTF validators and Cesium runtime | Validate the artifact, load it from the public endpoint, traverse content, and render |
| STAC assets | PySTAC-Client followed by Rasterio/xarray/fsspec | Discover an asset and consume the referenced COG, Zarr, or virtual dataset |

Each cloud-native lane covers source validation, storage/range behavior, canonical-library consumption, Honua
registration or production, public serving, cross-format fidelity, negative codec/corruption cases, and evidence
capture. Unsupported producer or consumer roles remain explicit rather than disappearing as carve-outs.

Kerchunk and COPC are implementation epics, not test-only work. They do not block a release until promoted to a
supported maturity state, but their implementation cannot be called complete until the canonical client lanes
above pass.

#### STAC and OData

STAC retains PySTAC, PySTAC-Client, and `stac-api-validator` coverage and adds asset consumption, private/signed
asset access, media types, roles, checksums, projection/raster/data-cube extensions, and pagination validation.

OData requires Microsoft.OData.Client on every release plus Power BI and Excel Power Query evidence for release
candidates. Metadata, filtering, paging, expansion, aggregation, type/null fidelity, errors, auth, and refresh
behavior are included. Desktop BI automation may remain release-only when reliable unattended execution is not
available.

#### Official SDKs, gRPC, MCP, and identity

| Surface | Required clients and proof |
|---|---|
| Admin/control plane | Honua JavaScript, Python, and .NET SDKs plus CLI; create/read/update/operate/poll/consume/delete lifecycles |
| gRPC | Generated .NET client plus one independent generated client; unary, streaming, deadline, cancellation, auth, invalid-message, and version-skew cases |
| MCP | Official MCP SDK; discovery, resources, auth, execution, errors, cancellation, timeout, and large-result cases |
| SCIM 2.0 | Microsoft Entra or Okta-compatible client harness plus protocol contract tests |
| SAML 2.0 | Standard test IdP, signed assertion, expiry, audience, replay, and logout behavior |
| OIDC/OAuth | Conformance harness plus commonly deployed library clients |
| mTLS | OpenSSL/curl plus an SDK using client-certificate authentication |

The deterministic MCP SDK lane gates releases. Live-LLM smoke remains advisory because model behavior is
nondeterministic.

### Release certification policy

A platform candidate is Honua-certified only when:

- Every supported surface appears in the federated manifest.
- Every surface has its required proof or an approved `not-applicable` rationale.
- Every mandatory canonical-client pair produced evidence.
- All claimed CITE profiles are fully green.
- All required cloud-native producer and consumer lanes pass.
- Licensed-client evidence is fresh for the candidate.
- No required operation is skipped.
- No evidence is stale or `unknown`.
- Accepted defects are explicitly linked from the release evidence.
- Every evidence envelope references the candidate's exact component SHAs and artifact digests.
- Evidence is immutable and publicly linkable where licensing permits.

### Execution roadmap

| Wave | Scope | Exit condition |
|---|---|---|
| 0: Trustworthy signals | Federated manifest, validator pinning, CNG repair, Cesium wiring, FlatGeobuf gate, skip/freshness enforcement | Three consecutive complete green nightlies; no orphaned required lane |
| 1: Existing supported surfaces | Esri skip burn-down, COG, HDF5/NetCDF, Zarr, PMTiles, 3D Tiles, STAC assets, GeoArrow, WPS | Every currently supported surface has its required canonical-client or validator proof |
| 2: Platform clients | SDK operation matrices, generated gRPC clients, MCP SDK, identity, Portal, and mobile evidence | Cross-repo compatibility matrix is executable for the supported version window |
| 3: New capabilities | Kerchunk and COPC implementation plus canonical-client certification | Capabilities move from roadmap to supported only after release-tier proof passes |
| 4: External certification | Exact-RC evidence publication and formal standards/vendor submissions where desired | External claims link to accepted third-party certification artifacts |

### Program metrics

| Metric | Target |
|---|---:|
| Implemented endpoints with route proof | 100% |
| Advertised logical operations with operation proof | 100% |
| Supported public surfaces with assigned canonical-client proof | 100% |
| Supported client-addressable operations passing | 100% |
| Required operations skipped | 0 |
| Public CITE assertions passing | 100% |
| Required cloud-native lanes passing | 100% |
| Release evidence bound to the exact candidate | 100% |
| Evidence marked stale or unknown | 0 |
| Broken or orphaned scheduled lanes | 0 |
| Accepted defects without linked evidence | 0 |

### Backlog ownership

`honua-release` owns the umbrella program, aggregate gate, release binding, and published certification bundle.
`honua-evidence` owns evidence ingestion and the generated capability/evidence join. Component repositories own
their implementations, fast tests, and emitted evidence. `honua-esri-compat` owns licensed Esri execution.
Official SDK repositories own their SDK-specific operation matrices. Cross-repository issues are linked to an
umbrella issue in `honua-release`; release-blocking children carry the release gate label appropriate to the
target train.
