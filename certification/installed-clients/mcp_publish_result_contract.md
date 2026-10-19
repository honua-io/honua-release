# Worker result publication source contract

This is an authored producer contract, not an executed candidate receipt. The
immutable source audited here is honua-server
`ce5bcccecdb64dcff4094a0982d95eea875bad7f`; the receipt media correction is being
validated separately in server PR 6394 (`610d260b05`). Historical selected image
`3ecd214c1ce20e96ad4911b6f8447055dee9089d` predates these producer prerequisites.

| Surface | Authored request / independently required observation |
| --- | --- |
| Console workflow authoring | Package graph has one `process:sink.external-postgis`, literal two-point GeoJSON, registered connection GUID, owned public table, 4326 and explicit batch ID. Create version 1, validate and publish an Active Schedule whose scheduler is disabled; read the actual publication inventory before manual run. |
| Manual workflow | Actual `wfp-...` publication and `wf-...` run, terminal workflow status 2, manual trigger 0, one completed step 3, actual worker `gp-...` job. Definition remains dormant and unchanged; exactly one new run/job/result with every prior raw record hash unchanged. |
| Worker spec and committed receipt | Immutable Geoprocess spec 0 has exact canonical sink inputs; exactly one artifact reference equals its committed-effect reference. JSON receipt says committed=true, featuresWritten=2, featuresRejected=0 and exact table/schema/batch/process. No fabricated artifact or deployment ID. |
| Result package | Completed enum 5; one FeatureLayer enum 1, actual job-derived artifact ID, JSON data URI and `application/json`, exact credential-free connectionId/schema/table metadata. Conflicting receipt media is refused. |
| Source SQL | Sink table uses id/geom/attributes. Observer explicitly projects attributes AS properties. Full authored points, gid/name/batch marker and 4326 remain exact. |
| `honua_publish_result` | Arguments sourceId/actual artifactId/serviceName/layerName. Only Completed service.publish with actual operation handle, sourceJobId, artifactId, serviceId/serviceUri, physical layerId and metadataRevision is positive. Other outcomes retain diagnostics and fail this positive. |
| Persisted publication | One new catalog service/layer/relation, fields id/attributes/geom in 1-based order, one new canonical service/resource/binding and two related protocol publications. Source-backed binding retains actual registered GUID and owned locator. Revision increases; no unrelated graph/catalog/source data may change. |
| Canonical snapshot copy | Publisher materializes a copy into public.features. Its object IDs differ independently from source PKs; complete id/attributes JSON and separate primary geometry must equal authored data. Prior canonical rows remain hashed and unchanged. |
| Fresh resource and query | Read the exact returned URI; canonical graph service ID, public layerIds/servingLayerIds and physical binding independently agree. Query uses the resource-returned public index and explicit id/attributes fields. Both typed and GeoJSON representations preserve actual source IDs, full nested JSON attributes and geometry. |
| Refusals | Actual unknown job/artifact tool errors require not_found; fresh anonymous tools/call requires typed unauthenticated tool result, not Inspector-local discovery refusal. Full SQL/graph/revision and all durable record hashes remain unchanged. |
| Restart | Original shared bootstrap alone may predate earlier owned restarts. Current client runtimeBefore equals setup before/after and immediate prepublication inspection exactly; explicit same-container restart advances StartedAt and preserves source/image/digest/container identity. Fresh resource/query/results and full persisted census remain exact. |

Source implementation ownership: Console workflow package endpoints and
WorkflowPackagePublisher/WorkflowOrchestrationEngine create actual work;
PostgisSinkProcess and GeoprocessingResultPackageFactory provide the actual
committed result; PublishResultTool selects the real result and delegates to
service.publish. PostgreSqlLayerPublishingService and its MetadataV2Graph,
LayerPersistence and SqlHelpers partials define catalog/copy/projection behavior.
PostgreSqlTableDiscoveryService excludes geometry columns from selected attributes;
BuildLayerFields appends geometry last, so no synthetic geom attribute is expected;
QueryFeaturesTool and PostgresStorageMappedFeatureReader define source-backed
readback. RedisWorkflowRunStore, RedisWorkflowDefinitionStore,
RedisOperationProposalStore and RedisOperationInstanceStore define observed key
namespaces and types.

Known boundary: the public MCP outFields description says to omit for all
fields. The former shared ingest producer's wildcard request needs separate
source/runtime disposition; server wildcard/unknown-field handling is being
owned independently. This producer's explicit projection does not qualify it.
A strict connection-preservation check may also expose replacement of nondefault
connection Options/SecretRef, since current publisher preserves only dependency
Metadata before replacing that connection object. No failure is waived here.
