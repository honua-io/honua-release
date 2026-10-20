"""Live isolated query mapping only; controlled DDL is not product lifecycle proof."""
import os
import re
import subprocess
import unittest
import mcp_deployment_identity_role as role

IDENTIFIER="11111111-1111-4111-8111-111111111111"
MARKER="explicit-offline-sql-query-mapping"

class LiveRoleSqlControls(unittest.TestCase):
    def query(self,sql):
        result=subprocess.run(["docker","exec","-i",self.container,"psql","-X","-q","-A","-t","-v","ON_ERROR_STOP=1","-U","honua","-d","honua_test"],
            input=sql,check=True,capture_output=True,text=True,timeout=20)
        return result.stdout.strip()
    def setUp(self):
        self.container=os.environ.get("CONTROL_POSTGIS_CONTAINER","")
        if os.environ.get("GITHUB_ACTIONS")!="true" or not re.fullmatch(r"[a-f0-9]{64}",self.container):
            raise ValueError("role SQL controls require workflow-owned isolated PostGIS")
        self.query("""CREATE SCHEMA IF NOT EXISTS honua;
CREATE TABLE honua.rbac_roles(role_id uuid,name text,description text,is_built_in boolean,created_at timestamptz,updated_at timestamptz,deleted_at timestamptz);
CREATE TABLE honua.rbac_role_permissions(role_id uuid,service text,layer text,operation text);
INSERT INTO honua.rbac_roles VALUES ('11111111-1111-4111-8111-111111111111','scoped-api-key','explicit-offline-sql-query-mapping',false,now(),now(),null);
INSERT INTO honua.rbac_role_permissions VALUES ('11111111-1111-4111-8111-111111111111','Workspace','*','Read'),('11111111-1111-4111-8111-111111111111','Deployment','*','Read');""")
    def tearDown(self):self.query("DROP TABLE honua.rbac_role_permissions,honua.rbac_roles;")
    def observe(self,suffix=""):
        return role.observe_role(IDENTIFIER,"unused-controlled-adapter",query=lambda sql,password:self.query("BEGIN;"+sql+suffix+"COMMIT;"))
    def test_actual_active_record_grants_and_tombstone_mapping(self):
        before=self.observe();role.owned_record(before,IDENTIFIER,MARKER,role.GRANTS)
        self.assertEqual(before["value"]["permissions"],role.GRANTS)
        self.query("UPDATE honua.rbac_roles SET deleted_at=now();DELETE FROM honua.rbac_role_permissions;")
        after=self.observe();role.owned_record(after,IDENTIFIER,MARKER,[],deleted=True)
        with self.assertRaises(ValueError):role.owned_record(after,IDENTIFIER,MARKER,role.GRANTS)
    def test_actual_foreign_ownership_refusal_and_transaction_readonly_enforcement(self):
        self.query("UPDATE honua.rbac_roles SET description='foreign-role';")
        with self.assertRaises(ValueError):role.owned_record(self.observe(),IDENTIFIER,MARKER,role.GRANTS)
        with self.assertRaises(subprocess.CalledProcessError):self.observe("UPDATE honua.rbac_roles SET description='must-not-mutate';")
        self.assertEqual(self.observe()["value"]["role"]["description"],"foreign-role")

if __name__=="__main__":unittest.main()
