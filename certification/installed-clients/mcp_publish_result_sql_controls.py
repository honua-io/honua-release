"""Real isolated PostGIS observer controls; never candidate/client qualification."""
import copy
import json
import os
import re
import subprocess
import unittest
from unittest.mock import patch
import mcp_publish_result_state as state
from mcp_publish_result_validation_controls import bootstrap, database
from mcp_ingest_publish_validation_controls import connection_control
from mcp_render_validation_controls import FIXTURE
from mcp_publish_result_validation import scenario


class SqlObserverControls(unittest.TestCase):
    def setUp(self):
        self.container = os.environ.get("CONTROL_POSTGIS_CONTAINER","")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}",self.container):
            raise ValueError("SQL controls require their workflow-owned disposable PostGIS service")
        self.authored = scenario(FIXTURE,bootstrap(),"abcdef123456","Official MCP TypeScript SDK")
        self.published = {"layerId":"741"}
        self.initial,self.completed = database(self.authored,self.published)
        table = self.authored["table"]
        self.query("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS honua CASCADE; CREATE SCHEMA honua; "
            "DROP TABLE IF EXISTS public.features; CREATE TABLE public.features(objectid bigint,layer_id integer,attributes jsonb,geometry geometry); "
            f'DROP TABLE IF EXISTS public."{table}"; CREATE TABLE public."{table}"(id bigint,geom geometry,attributes jsonb); '
            "CREATE TABLE honua.services(service_name text); CREATE TABLE honua.layers(layer_id integer); "
            "CREATE TABLE honua.service_layers(service_name text,layer_id integer); CREATE TABLE honua.layer_fields(layer_id integer,field_order integer,field_name text); "
            "CREATE TABLE honua.data_connections(connection_id uuid,encrypted_secret text); "
            "CREATE TABLE honua.metadata_v2_current(environment text,revision bigint); CREATE TABLE honua.metadata_v2_snapshots(environment text,revision bigint,document jsonb);")
        for index,row in enumerate(self.authored["expectedRows"],11):
            attrs = json.dumps(row["properties"]).replace("'","''"); x,y = row["geometry"]["coordinates"]
            self.query(f'INSERT INTO public."{table}" VALUES ({index},ST_SetSRID(ST_MakePoint({x},{y}),4326),\'{attrs}\'::jsonb);')
        self.query("INSERT INTO honua.data_connections VALUES ('22222222-2222-4222-8222-222222222222','controlled-encrypted-secret-never-retain');")

    def query(self,sql):
        result = subprocess.run(["docker","exec","-i",self.container,"psql","-X","-q","-A","-t","-v","ON_ERROR_STOP=1","-U","honua","-d","honua_test"],input=sql,
            capture_output=True,text=True,timeout=20)
        if result.returncode: raise ValueError("owned SQL control refused")
        return result.stdout.strip()

    def graph(self,environment,document):
        document = copy.deepcopy(document); document["environment"] = environment
        literal = json.dumps(document).replace("'","''")
        self.query(f"INSERT INTO honua.metadata_v2_current VALUES ('{environment}',{document['revision']}); INSERT INTO honua.metadata_v2_snapshots VALUES ('{environment}',{document['revision']},'{literal}'::jsonb);")

    def observe(self):
        with patch.object(state,"connection_observation",return_value=connection_control()),patch.object(state,"database_query",side_effect=lambda query,_:self.query(query)):
            return state.database_snapshot(self.authored,"controlled-unused-password")

    def test_upgrade_cleanup_releases_actual_owned_advisory_backend(self):
        # Actual Docker exec -> psql child and PostgreSQL advisory lock. This is
        # driver cleanup proof against an isolated control DB, not a candidate.
        import tempfile
        from pathlib import Path
        driver = Path(__file__).resolve().parents[2] / "e2e/harness/upgrade-chaos.sh"
        definitions = driver.read_text().split("\ntrap cleanup EXIT\n", 1)[0]
        commands = r'''
restore_baseline() { :; }
db_exec() {
  if [[ "$1" == *pg_terminate_backend* ]]; then
    printf '%s\n' "$1" > "$OUT/termination-query"
  fi
  command docker exec -i "$CONTROL_POSTGIS_CONTAINER" psql -X -q -A -t -v ON_ERROR_STOP=1 -U honua -d honua_test -c "$1"
}
sleep() {
  local attempt
  for attempt in $(seq 1 100); do
    if [ "$(db_exec "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$holder_name' AND wait_event = 'PgSleep';")" = 1 ]; then
      printf acquired > "$OUT/advisory-acquired"
      return 0
    fi
    command sleep 0.05
  done
  return 1
}
compose() { printf '%s\n' "$LOCK_HOLDER_PID" > "$OUT/wrapper-pid"; return 1; }
docker() { [ "$1" != rm ] || printf '%s\n' "$*" >> "$OUT/removed"; return 0; }
if run_concurrent_start; then exit 9; fi
[ -f "$OUT/advisory-acquired" ] || exit 8
if kill -0 "$(cat "$OUT/wrapper-pid")" 2>/dev/null; then exit 7; fi
printf advisory-holder-completed
'''
        with tempfile.TemporaryDirectory(prefix="upgrade-owned-backend-") as directory:
            result = subprocess.run(["bash", "-c", "source /dev/stdin"], input=definitions+"\n"+commands,
                capture_output=True, text=True, timeout=20,
                env={**os.environ,"E2E_OUT":directory,"MIGRATION_LOCK_KEY":"8044282257919950151"})
            self.assertEqual(result.returncode,0,"owned PostgreSQL cleanup control refused")
            self.assertEqual(result.stdout,"advisory-holder-completed")
            query = (Path(directory)/"termination-query").read_text()
            name = re.search(r"application_name = '(honua-chaos-[0-9a-f]{32})'",query).group(1)
            self.assertEqual(self.query(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{name}';"),"0")
            self.assertEqual(self.query("SELECT pg_try_advisory_lock(8044282257919950151);"),"t")
            self.assertEqual(len((Path(directory)/"removed").read_text().splitlines()),2)

    def test_actual_sql_preserves_identical_mapping_multiplicity_across_environments(self):
        document = self.completed["graphs"][0]["document"]
        self.graph("production",document); self.graph("staging",document)
        value = self.observe()
        self.assertEqual(len(value["mappings"]),2)
        self.assertEqual({row["environment"] for row in value["mappings"]},{"production","staging"})
        self.assertEqual(value["mappings"][0]["publication"],value["mappings"][1]["publication"])
        self.assertNotIn("controlled-encrypted-secret-never-retain",json.dumps(value))

    def test_actual_nongeometry_jsonb_projection_and_separate_geometry_distinct_ids(self):
        table = self.authored["table"]; self.graph("production",self.completed["graphs"][0]["document"])
        self.query(f'INSERT INTO public.features SELECT id+870,741,jsonb_build_object(\'id\',id,\'attributes\',attributes),geom FROM public."{table}";')
        value = self.observe(); self.assertEqual(len(value["materialized"]),2)
        by_source = {row["id"]:row for row in value["rows"]}
        for row in value["materialized"]:
            source = by_source[row["attributes"]["id"]]
            self.assertNotEqual(row["id"],source["id"])
            self.assertEqual(row["geometry"],source["geometry"])
            self.assertEqual(set(row["attributes"]),{"id","attributes"})
            self.assertEqual(row["srid"],4326)
            self.assertEqual(row["attributes"]["attributes"],source["properties"])


if __name__ == "__main__":
    unittest.main()
