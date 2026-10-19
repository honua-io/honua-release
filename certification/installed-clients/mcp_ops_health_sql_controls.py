"""Actual isolated PostGIS observer query controls, not candidate qualification."""
import json
import os
import re
import subprocess
import unittest
import mcp_ops_health_state as state
from mcp_render_validation_controls import FIXTURE


class LivePostgisHealthControls(unittest.TestCase):
    def setUp(self):
        self.container = os.environ.get("CONTROL_POSTGIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", self.container):
            raise ValueError("health SQL controls require workflow-owned isolated PostGIS")
        self.query("CREATE EXTENSION IF NOT EXISTS postgis; CREATE SCHEMA IF NOT EXISTS honua_data; "
                   "DROP TABLE IF EXISTS honua_data.sdkreg_area; CREATE TABLE honua_data.sdkreg_area(gid integer,name text,geom geometry);")
        for row in json.loads(FIXTURE)["area"]["features"]:
            x1,y1,x2,y2 = row["envelope"]; name = row["name"].replace("'", "''")
            self.query(f"INSERT INTO honua_data.sdkreg_area VALUES ({row['gid']},'{name}',ST_MakeEnvelope({x1},{y1},{x2},{y2},4326));")

    def query(self, sql):
        result = subprocess.run(["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-A", "-t",
                                 "-v", "ON_ERROR_STOP=1", "-U", "honua", "-d", "honua_test"],
                                input=sql, check=True, capture_output=True, text=True, timeout=20)
        return result.stdout.strip()

    def tearDown(self):
        self.query("DROP TABLE IF EXISTS honua_data.sdkreg_area;")

    def test_actual_database_cluster_postgis_and_authored_geometry_readback(self):
        identity = json.loads(self.query(state.IDENTITY_SQL)); observed = json.loads(self.query(state.DATABASE_SQL))
        self.assertRegex(identity["clusterId"], r"^[0-9]+$")
        self.assertEqual((identity["database"], identity["user"]), ("honua_test", "honua"))
        self.assertEqual(observed["probe"], 1); self.assertTrue(observed["postgis"])
        expected = [dict(gid=row["gid"], name=row["name"], envelope=row["envelope"], srid=4326, geometryType="ST_Polygon")
                    for row in sorted(json.loads(FIXTURE)["area"]["features"], key=lambda row: row["gid"])]
        self.assertEqual(observed["area"], expected)

    def test_actual_data_change_is_observed_and_missing_fixture_fails(self):
        before = json.loads(self.query(state.DATABASE_SQL))
        self.query("UPDATE honua_data.sdkreg_area SET name='changed-owned-marker';")
        self.assertNotEqual(before, json.loads(self.query(state.DATABASE_SQL)))
        self.query("DROP TABLE honua_data.sdkreg_area;")
        with self.assertRaises(subprocess.CalledProcessError): self.query(state.DATABASE_SQL)


if __name__ == "__main__":
    unittest.main()
