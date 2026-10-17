# Import-fidelity inputs and native owner handoff

The producer runs the frozen 142 supported requirements and retains ten explicit
not-applicable baseline checks. This document defines inputs to executable checks;
it does not waive a missing source capability, native runtime or candidate image.

## Authored source

Pass `--authored-source PATH` to `produce_import_fidelity.py`. The closed contract is
[import-fidelity-source.schema.json](import-fidelity-source.schema.json), schema
`honua.import-fidelity-source/v1`. Each role identifies a real HTTPS service URL,
layer ID and `expectedSnapshotDigest`. The digest uses SHA-256 of UTF-8 JSON with
sorted keys, compact separators and finite numbers, prefixed with `sha256:`. Its
input is the producer's normalized `read_snapshot` result: raw layer metadata,
count, sorted IDs, object-ID field and all features sorted by ID, with geometries
queried in 4326 and Z/M requested. The author captures and pins this expected
snapshot **before** running the candidate import. The producer reads the source
again, checks the authored digest, imports, measures the target, then checks that
the source did not change. A synthetic HTTP source cannot qualify these checks.

The same service may supply several roles if it actually contains every required
construct. Every role needs at least two records, and the richer roles enforce
these additional prerequisites:

| Role | Required source contents and execution |
| --- | --- |
| `points`, `lines`, `polygons` | Correct geometry type and actual non-null shapes; all values and geometry compared after import. |
| `tables` | Nonspatial table, imported without inventing geometry. |
| `domains-subtypes` | Actual domain and subtype definitions, preserved in target metadata. |
| `related-records` | Complete service selection, relationship application, all source/target records, relationship metadata and independent physical join edges. |
| `attachments` | At least two real attachments; source/target SDK downloads compare parent, name, MIME, length and SHA-256 of bytes. |
| `zm-curves` | Z, M and actual curve paths/rings in source reads; densified-only reads cannot qualify. |
| `nulls-int64-time` | Distinct null/empty values, an integer outside IEEE-754's exact 53-bit range and a date field. |
| `renderer-time-metadata` | Actual renderer and time metadata; also supplies the live metadata-loss control. |
| `filters-crs` | Non-4326 source, a nontrivial import WHERE predicate and a spatial query predicate; transformed geometry and selected rows compared. |
| `auth` | Authenticated source succeeds; the same source rejects an anonymous SDK read. |
| `pagination-limits` | More records than the source's advertised transfer cap; all IDs read in bounded batches. |
| `bounded-large-data` | 2,001–50,000 records, above the source cap; also exercises cancellation/retry. |

The points role must contain a populated nullable attribute for the committed
attribute-loss control. The time role needs a populated `startTimeField` for the
metadata-loss control. Those controls mutate only the owned disposable target,
prove the real SDK/oracle detects the fault, then restore through SDK operations
and verify fresh parity. They never put an intentionally failing verdict into a
passing scorecard.

The filters role additionally supplies `filter` in the published SDK query shape
(`where` and `spatialFilter`) and `expectedFilteredSnapshotDigest`. The producer
uses the attribute predicate in the import request and independently reads the
filtered source. The predicate must actually exclude records.

The relationships role supplies `expectedInventoryDigest`, the authored
`migrationManifest` from real migration discovery, `relatedLayers` identifying
every discovered resource and its expected snapshot digest, and `relationships`.
Each edge identifies `relationshipId`, `originLayerId`, `destinationLayerId`,
`keyField`, and `foreignKey`. The producer compares those to fresh declared source
relationships, requires both ends of every edge, invokes the SDK batch API with
`applyRelationships=true`, then measures actual target joins. Neither an omitted
relationship nor a succeeded batch with absent relationship evidence is a pass.

`IMPORT_FIDELITY_AUTH_SOURCE_URL` separately fixes the only credential destination.
`IMPORT_FIDELITY_SOURCE_TOKEN` is provided to the SDK runtime and disposable server
environment. Import requests contain only `env:IMPORT_FIDELITY_SOURCE_TOKEN` as a
secret reference. Source credentials never accompany other service URLs or redirects.
GitHub/native verification and cloud credentials are excluded from the SDK child
environment. No source token value belongs in a manifest, request, capture or receipt.

Both installed-client workflows accept `authored_source_manifest` as a file within
the reviewed checkout. Missing inputs keep their cells blocked. The workflows
install `gdal-bin`, retain actual decoded vector outputs and forward the producer
receipt to the existing installed-client consumer. A complete native handoff still
requires the native owner's orchestration; these workflows do not dispatch or
adopt its Windows lane.

## Native request and authenticated response

After all authored fixtures pass, the producer writes
`import-fidelity-observations/native-reference-request.json`, schema
`honua.import-fidelity-reference-request/v1`. It contains a fresh random challenge,
exact image/source and published-package identity, requirements revision, and each
fixture's source/target URL, layer ID and snapshot digests. `--reference-inbox PATH`
optionally waits up to `--reference-wait-seconds` (0–300) for the native owner to
provide a response. Its endpoint must be reachable from the native host; localhost
on an unrelated hosted worker is not a viable native execution destination.

The Windows owner retains execution and producer ownership. This lane consumes a
response only from protected `honua-io/honua-esri-compat` trunk using
`.github/workflows/esri-sdk-certification.yml`. That workflow must produce an
artifact attestation for the exact response bytes with SLSA provenance v1. The
consumer itself invokes `gh attestation verify` with fixed repository, signer
workflow, source ref and exact source digest. It also reads GitHub metadata to
check protected-trunk ancestry and the exact completed successful run/attempt.
A producer-authored `verified=true`, an unsigned JSON file, a pull-request run,
a different workflow, a failed run or an old challenge cannot qualify. Read access
to that private producer repository is an orchestrator prerequisite, never an SDK
credential. No token or key configuration is installed by this change.

The JSON response has schema `honua.import-fidelity-reference-response/v1`,
`requestDigest` (the canonical digest of the complete request), and `producer`
with exact `sourceSha`, integer `runId` and integer `runAttempt`. Its `fixtures`
map must contain exactly the request's roles. Each fixture has `identity` equal to
the corresponding request entry and `observations` keyed by `arcpy` and `pyqgis`.
Each native observation identifies `runtimeVersion`, actual executed `methods`,
`live=true`, and independently measured `source` and `target` objects containing:

- `count` and `rows`: exact record count and attribute dictionaries sorted by
  canonical JSON. Preserve integers, booleans, null and empty strings.
- `shapes`: `[objectId, {"present": true, "value": geometry}]` entries sorted by
  ID, preserving Z, M and curves. Native observation order must be normalized;
  only numeric geometry coordinates receive the `1e-8` tolerance.
- `schema`: declared fields sorted by case-insensitive name, plus present
  `geometryType`, `hasZ`, `hasM`, `typeIdField`, `types`, `drawingInfo`, `timeInfo`,
  `relationships` and `hasAttachments`. These are measured values, not pass flags.
- For the attachment role, `attachments`: objects with parent `objectId`, `name`,
  `contentType`, byte `size` and unprefixed lowercase `sha256`, sorted by canonical
  JSON. Download the bytes; copying source metadata is insufficient.
- For the related-records role, `relationships`: objects with relationship `name`
  and all actual `[originObjectId, destinationObjectId]` `pairs`, sorted by IDs.
  Query the actual related records independently; copying a declared relationship
  list is insufficient.

The consumer compares these raw measurements to its fresh published-SDK reads and
physical relationship edges. It never imports a native reported verdict. The
combined client cell requires every independent comparison to succeed.

Existing native workflows do not yet emit this response/attestation contract. An
absent native response is therefore blocked; this document does not establish
native qualification. SDK pin `1.11.0` also omits explicit `DataCommitted` from its
progress model and a `returnTrueCurves` query option. The producer refuses to infer
false commit state or treat densified shapes as true curves. A suitable published
pin and real input replay remain prerequisites. The fixed geometry/catalog mapping
on server trunk likewise needs its own immutable image and measured readback;
source commits and closed issues alone are not candidate proof.
