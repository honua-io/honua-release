# Issue #343 retry: no managed-store collection accepts OGC create edits at the pin

Diagnostic evidence, **not a DR receipt**. It answers the retry question: can the manifest-pinned
server create a managed-store collection, with Create declared, through a supported product
surface, so the drill's transactional-outbox/redis leg can insert a sentinel and read it back?

- Release candidate: PR #342 head `98fa9e702ec6ce37c34fc9c79c4c0d3f3d12d80f`, manifest SHA-256
  `93128f683d7cc09f83e4d3b64fc2273edc84170abfc7693bad1315e391257619`, dbSchema `116`.
- Server source: `9f2f16a5b9d19becf052f8b2635cf7c2ce109fdd`.
- Image: `ghcr.io/honua-io/honua-server@sha256:a5d962958ec8a6890ecd0f5f34f1da9c08a9d464da0418bdfbbc381c754d30fc`.

## Source findings at 9f2f16a

1. Every product publish path funnels into `PostgreSqlLayerPublishingService.PublishLayerAsync`: the
   admin endpoint (`LayerPublishingEndpoints.cs:197`), the operations/MCP executor
   (`ServicePublishExecutor.cs:104`), GP dataset import (`ImportDatasetJobExecutor.cs:258`), GeoServer
   and GeoServices migration (`GeoServerImportService.Apply.cs:363`,
   `GeoservicesLayerPublicationService.cs:111`) and feature copy (`PostgresFeatureLayerCopyService.cs:117`).
   The only non-test `new MetadataV2Publication` constructions are in that service.
2. That service declares `_defaultCapabilities = ["Query", "Extract"]`
   (`PostgreSqlLayerPublishingService.cs:48`) and writes it onto every publication
   (`PostgreSqlLayerPublishingService.MetadataV2Graph.cs:3356`). Neither `PublishLayerRequest`
   (`Features/Admin/Models/LayerPublishingModels.cs`) nor the internal `LayerPublishRequest` carries a
   capability member. The published admin OpenAPI (`docs/developer/api-specs/admin-api.json:22929`)
   lists exactly: attribution, description, enabled, fields, geometryColumn, geometryType, layerName,
   license, licenseUrl, primaryKey, publisher, schema, serviceName, sourceUrl, srid, table.
3. No admin route updates a publication's capabilities after publish. The `/admin/connections/{id}/layers`
   group maps list, publish, enabled toggles, extent/feature refresh and table validation. The
   `/admin/metadata/layers` groups cover popup, drawing info, relationships, fields, filter, style and
   validation. Metadata release packages promote existing semantic ids between environments
   (`CreateMetadataReleasePackageRequest`) and do not author publications.
4. The OGC write surface refuses a publication whose declared set omits Create with 405
   (`LayerValidationHelpers.cs:977`, `MetadataV2EditCapabilities.SupportsDeclared`). The storage guard only
   admits managed writes for a non-source-backed mapping or the shared `features` table
   (`FeatureStorageMapping.cs:91-93`), else 501 (`LayerValidationHelpers.cs:1051-1062`).
5. The shared `features` table is the only managed store a published layer could bind to
   (`MetadataV2Graph.cs:3265-3290`). The publish validator refuses it before capabilities are
   even considered (runtime evidence below).

## Runtime probe on the pinned image

`probe.py` boots the drill's own topology through `full_platform.Stack` (compose file and
`image@digest` from the manifest), records every request/response in `probe-log.json`, and removes the
stack and its volumes afterwards.

- The managed feature table on this image is `public.features`.
- `POST /api/v1/admin/connections/{id}/tables/validate` `{"schema":"public","table":"features"}` -> 200
  with `isValid:false`: `source-srid` error (actual `0`), `target-srid` error, `feature-count` error
  ("Source table is empty.").
- `POST /api/v1/admin/connections/{id}/layers` `{"schema":"public","table":"features","layerName":"dr-managed",
  "serviceName":"dr-managed","geometryType":"Point","srid":4326,"enabled":true,
  "capabilities":["Query","Create","Update","Delete"]}` -> **400**
  `Table validation failed (source-srid): Source table does not report a valid SRID.` The request `srid`
  does not satisfy the source-SRID check (`PostgreSqlLayerPublishingService.Validation.cs:179`), and the
  `capabilities` member is not part of the contract.

The previous attempt's evidence (`../README.md`) covers the import-backed publication: 405 as published,
501 with Create injected. Together, the pinned server offers no supported way to create a collection
that accepts an OGC API Features create edit. That is a server defect, filed in honua-server. The DR
criterion stays open and nothing was released.
