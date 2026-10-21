"""Real isolated PostGIS setup/observer controls; no server or candidate credit."""
import hashlib
import json
import os
import re
import subprocess
import unittest

import mcp_ops_events_setup as setup
import mcp_ops_events_state as state
from mcp_render_validation_controls import runtime


class LiveOperateSqlControls(unittest.TestCase):
    def setUp(self):
        self.container = os.environ.get("CONTROL_POSTGIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", self.container):
            raise ValueError("Ops SQL controls require workflow-owned disposable PostGIS")
        # A minimal explicit SQL contract, independent of application migrations.
        # This verifies the real queries and transactions; it does not certify
        # a product migration, server image, authentication path, or engine.
        self.query("""
CREATE EXTENSION IF NOT EXISTS postgis;
DROP SCHEMA IF EXISTS honua CASCADE;
CREATE SCHEMA honua;
CREATE TABLE honua.alert_zones(zone_id bigserial PRIMARY KEY, service_id text, zone_name text,
    geometry geometry(MultiPolygon,4326), metadata jsonb, is_active boolean);
CREATE TABLE honua.alert_rules(rule_id bigserial PRIMARY KEY, service_id text, layer_id integer,
    zone_id bigint REFERENCES honua.alert_zones(zone_id), rule_name text, trigger_type smallint,
    conditions jsonb, cooldown_seconds integer, severity text, edition_required integer, channels text[], is_active boolean);
CREATE TABLE honua.alert_events(event_id bigserial PRIMARY KEY, dedupe_key text UNIQUE,
    rule_id bigint REFERENCES honua.alert_rules(rule_id), zone_id bigint REFERENCES honua.alert_zones(zone_id),
    service_id text, layer_id integer, objectid bigint, trigger_type smallint, generation bigint,
    severity text, occurred_at timestamptz, payload jsonb, incident_status smallint, incident_duration_ms bigint);
CREATE TABLE honua.alert_event_lifecycle(event_id bigint PRIMARY KEY REFERENCES honua.alert_events(event_id),
    lifecycle_status smallint, acknowledged_at timestamptz, acknowledged_by text, suppressed_until timestamptz,
    resolved_at timestamptz, resolved_by text, note text, updated_at timestamptz);
""")

    def query(self, sql, _password=None):
        result = subprocess.run(["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-A", "-t", "-1",
            "-v", "ON_ERROR_STOP=1", "-U", "honua", "-d", "honua_test"], input=sql,
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("owned Ops SQL control refused")
        return result.stdout.strip()

    def tearDown(self):
        self.query("DROP SCHEMA IF EXISTS honua CASCADE;")

    def provision(self):
        return setup.provision(password="controlled-private-sql-password", candidate_source=runtime()["candidateSourceSha"],
            candidate_image=runtime()["candidateImage"], sql=self.query, inspect=lambda *_: runtime())

    def test_actual_setup_retains_original_allocations_causal_rows_and_disabled_engine(self):
        value = self.provision()
        original = json.loads(self.query(state.STORE_SQL))
        selected = state.fixture_rows(original, value["seed"])
        self.assertEqual(len(selected), 2)
        self.assertEqual(value["phase"], "complete")
        self.assertEqual(value["setupResponseSha256"], "sha256:" + hashlib.sha256(value["setupResponseText"].encode()).hexdigest())
        self.assertEqual(json.loads(value["setupResponseText"]), value["seed"])
        self.assertEqual(original["databaseIdentity"]["database"], "honua_test")
        self.assertEqual(original["databaseIdentity"]["user"], "honua")
        self.assertGreater(value["seed"]["alerts"]["openAlertEventId"], 0)
        self.assertEqual(self.query("SELECT count(*) FROM honua.alert_zones WHERE is_active;"), "0")
        self.assertEqual(self.query("SELECT count(*) FROM honua.alert_rules WHERE is_active OR cardinality(channels)>0;"), "0")
        self.assertTrue(all(value["seed"][field] is False for field in ("evaluationExercised", "deliveryExercised", "lifecycleApiExercised")))
        self.assertNotIn("controlled-private-sql-password", json.dumps(value))

    def test_actual_reseed_refusal_preserves_original_rows_and_clock(self):
        value = self.provision()
        before = json.loads(self.query(state.STORE_SQL))
        with self.assertRaises(ValueError):
            self.provision()
        after = json.loads(self.query(state.STORE_SQL))
        self.assertEqual(before["alerts"], after["alerts"])
        self.assertEqual(before["windowEventIds"], after["windowEventIds"])
        state.fixture_rows(after, value["seed"])

    def test_actual_lifecycle_change_is_observed_and_cannot_reuse_setup_authority(self):
        value = self.provision()
        self.query("UPDATE honua.alert_event_lifecycle SET resolved_by='changed-control-actor';")
        observed = json.loads(self.query(state.STORE_SQL))
        with self.assertRaises(ValueError):
            state.fixture_rows(observed, value["seed"])

    def test_actual_ambient_event_in_original_window_is_not_silently_filtered(self):
        value = self.provision()
        self.query("""
INSERT INTO honua.alert_events(dedupe_key,rule_id,zone_id,service_id,layer_id,objectid,trigger_type,
    generation,severity,occurred_at,payload,incident_status,incident_duration_ms)
SELECT 'explicit-ambient-control',rule_id,zone_id,'other-service',layer_id,objectid,trigger_type,
    generation+100,severity,occurred_at+interval '1 second',payload,incident_status,incident_duration_ms
FROM honua.alert_events WHERE dedupe_key='canonical-mcp-ops-v1:alert-resolved';
""")
        observed = json.loads(self.query(state.STORE_SQL))
        self.assertEqual(len(observed["alerts"]), 2)
        self.assertEqual(len(observed["windowEventIds"]), 3)
        with self.assertRaises(ValueError):
            state.fixture_rows(observed, value["seed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
