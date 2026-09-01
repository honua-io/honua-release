\set ON_ERROR_STOP on

CREATE SCHEMA tenant_alpha;
CREATE SCHEMA tenant_beta;
CREATE TABLE tenant_alpha.customer_assets (asset_id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE tenant_beta.customer_assets (asset_id bigint PRIMARY KEY, name text NOT NULL);
INSERT INTO tenant_alpha.customer_assets VALUES (23401, 'alpha-private-asset');
INSERT INTO tenant_beta.customer_assets VALUES (23402, 'beta-private-asset');

INSERT INTO honua.services(service_name, description, metadata)
VALUES ('dr_parcels', 'DR drill customer dataset', '{"tenant":"alpha","drFixture":true}');
INSERT INTO honua.layers(layer_id, layer_name, description, table_schema, table_name,
                         geometry_type, srid, metadata)
VALUES (23401, 'Restored parcels', 'DR drill layer', 'public', 'features',
        'Point', 4326, '{"tenant":"alpha","drFixture":true}');
INSERT INTO honua.service_layers(service_name, layer_id, layer_order)
VALUES ('dr_parcels', 23401, 0);
INSERT INTO honua.layer_fields(layer_id, field_name, field_type, field_order, nullable)
VALUES (23401, 'name', 'esriFieldTypeString', 0, false);
INSERT INTO public.features(objectid, layer_id, geometry, attributes)
VALUES (2340101, 23401, ST_SetSRID(ST_Point(-157.8583, 21.3069), 4326),
        '{"name":"Honolulu restored parcel","tenant":"alpha"}');

INSERT INTO honua.operate_fixture_execution_jobs
  (operation_id, fixture_profile, record_json, status, kind, backend, requested_by,
   correlation_id, resource_refs, created_at, updated_at)
VALUES
  ('dr-job-234', 'dr-drill', '{"work":"rebuild-index","tenant":"alpha"}', 'queued',
   'maintenance', 'postgres', 'dr-operator', 'dr-234', ARRAY['layer:23401'], now(), now());
INSERT INTO honua.operate_fixture_execution_logs
  (operation_id, fixture_profile, timestamp, level, payload_json)
VALUES ('dr-job-234', 'dr-drill', now(), 'information', '{"message":"queued before backup"}');
INSERT INTO honua.alert_rules
  (rule_id, service_id, layer_id, rule_name, trigger_type, conditions, severity, channels)
VALUES (23401, 'dr_parcels', 23401, 'DR parcel threshold', 1,
        '{"field":"name","operator":"changed"}', 'warning', ARRAY['webhook']);
INSERT INTO honua.alert_events
  (event_id, dedupe_key, rule_id, service_id, layer_id, objectid, trigger_type,
   generation, severity, payload, source)
VALUES (23401, 'dr-alert-234', 23401, 'dr_parcels', 23401, 2340101, 1, 1,
        'warning', '{"tenant":"alpha","state":"open"}', 'dr-drill');
INSERT INTO honua.audit_log
  (timestamp, event_type, actor, actor_type, resource_type, resource_id, action,
   outcome, correlation_id, details)
VALUES (now(), 'DataExport', 'dr-operator', 'UserId', 'Layer', '23401', 'seed',
        'Success', 'dr-234', '{"tenant":"alpha"}');
INSERT INTO honua.feature_change_outbox
  (outbox_id, service_id, layer_id, object_id, operation, protocol, request_id,
   event_id, event_payload, status)
VALUES ('23400000-0000-0000-0000-000000000001', 'dr_parcels', 23401, 2340101,
        'create', 'dr-drill', 'dr-request-234', 'dr-event-234',
        '{"tenant":"alpha","name":"Honolulu restored parcel"}', 'pending');
INSERT INTO honua.fieldcollection_sync_cursors(client_id, last_sync_generation)
VALUES ('dr-client-alpha', 42);

SELECT setval('honua.layers_layer_id_seq', GREATEST(23401, (SELECT max(layer_id) FROM honua.layers)));
