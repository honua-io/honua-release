\set ON_ERROR_STOP on
SELECT jsonb_build_object(
  'schemaVersion', (SELECT max(scriptname) FROM public.schema_versions),
  'rows', jsonb_build_object(
    'tenants', (SELECT count(*) FROM pg_namespace WHERE nspname IN ('tenant_alpha','tenant_beta')),
    'layers', (SELECT count(*) FROM honua.layers WHERE layer_id=23401),
    'features', (SELECT count(*) FROM public.features WHERE layer_id=23401),
    'jobs', (SELECT count(*) FROM honua.operate_fixture_execution_jobs WHERE operation_id='dr-job-234'),
    'jobLogs', (SELECT count(*) FROM honua.operate_fixture_execution_logs WHERE operation_id='dr-job-234'),
    'alerts', (SELECT count(*) FROM honua.alert_events WHERE dedupe_key='dr-alert-234'),
    'audit', (SELECT count(*) FROM honua.audit_log WHERE correlation_id='dr-234'),
    'outbox', (SELECT count(*) FROM honua.feature_change_outbox WHERE event_id='dr-event-234'),
    'cursors', (SELECT count(*) FROM honua.fieldcollection_sync_cursors WHERE client_id='dr-client-alpha')
  ),
  'contentChecksums', jsonb_build_object(
    'service', (SELECT md5(row_to_json(x)::text) FROM (SELECT service_name,description,metadata FROM honua.services WHERE service_name='dr_parcels') x),
    'layer', (SELECT md5(row_to_json(x)::text) FROM (SELECT layer_id,layer_name,table_schema,table_name,geometry_type,srid,metadata FROM honua.layers WHERE layer_id=23401) x),
    'feature', (SELECT md5(row_to_json(x)::text) FROM (SELECT objectid,layer_id,ST_AsEWKT(geometry) geometry,attributes FROM public.features WHERE objectid=2340101) x),
    'job', (SELECT md5(row_to_json(x)::text) FROM (SELECT operation_id,status,kind,backend,record_json FROM honua.operate_fixture_execution_jobs WHERE operation_id='dr-job-234') x),
    'alert', (SELECT md5(row_to_json(x)::text) FROM (SELECT dedupe_key,severity,payload FROM honua.alert_events WHERE dedupe_key='dr-alert-234') x),
    'outbox', (SELECT md5(row_to_json(x)::text) FROM (SELECT event_id,status,event_payload FROM honua.feature_change_outbox WHERE event_id='dr-event-234') x),
    'cursor', (SELECT md5(row_to_json(x)::text) FROM (SELECT client_id,last_sync_generation FROM honua.fieldcollection_sync_cursors WHERE client_id='dr-client-alpha') x)
  ),
  'tenantIsolation', jsonb_build_object(
    'alphaSees', (SELECT jsonb_agg(name ORDER BY asset_id) FROM tenant_alpha.customer_assets),
    'betaSees', (SELECT jsonb_agg(name ORDER BY asset_id) FROM tenant_beta.customer_assets),
    'crossTenantLeakCount', (SELECT count(*) FROM tenant_alpha.customer_assets a JOIN tenant_beta.customer_assets b USING (asset_id))
  )
)::text;
