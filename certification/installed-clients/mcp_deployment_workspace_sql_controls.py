"""Actual isolated SQL query mapping; controlled DDL is not a migration/lifecycle proof."""
import json
import os
import re
import subprocess
import unittest
import mcp_deployment_workspace_state as state


class LiveDeploymentWorkspaceSqlControls(unittest.TestCase):
    def query(self, sql):
        result = subprocess.run(["docker", "exec", "-i", self.container, "psql", "-X", "-q", "-A", "-t",
                                 "-v", "ON_ERROR_STOP=1", "-U", "honua", "-d", "honua_test"],
                                input=sql, check=True, capture_output=True, text=True, timeout=20)
        return result.stdout.strip()

    def setUp(self):
        self.container = os.environ.get("CONTROL_POSTGIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", self.container):
            raise ValueError("lifecycle SQL controls require workflow-owned isolated PostGIS")
        self.query("""CREATE SCHEMA IF NOT EXISTS honua;
CREATE TABLE honua.promotion_deployments(deployment_id text,source_kind text,source_id text,target_id text,status text,document jsonb,created_at timestamptz,updated_at timestamptz);
CREATE TABLE honua.studio_content_items(item_id uuid,family text,package_key text,owner_id text,tenant_id text,current_version_id uuid,published_version_id uuid);
CREATE TABLE honua.studio_content_versions(item_id uuid,version_id uuid,content_hash text,content_hash_input bytea,envelope jsonb,owner_id text,tenant_id text);
CREATE TABLE honua.studio_publication_requests(request_id uuid,item_id uuid,version_id uuid,intent jsonb,status text,deployment_id text,governs_studio_routes boolean);
CREATE TABLE honua.gp_workspaces(workspace_id text,kind integer,label text,owner_id text,scope_id text,state integer,uri text,created_at timestamptz,expires_at timestamptz);
CREATE TABLE honua.gp_workspace_artifacts(artifact_id text,workspace_id text,kind integer,label text,label_key text,state integer,uri text,content_type text,size_bytes bigint,created_at timestamptz,metadata jsonb);
""")

    def tearDown(self):
        self.query("DROP TABLE honua.promotion_deployments,honua.studio_content_items,honua.studio_content_versions,honua.studio_publication_requests,honua.gp_workspace_artifacts,honua.gp_workspaces;")

    def observe(self, suffix=""):
        return state.observe("owned-target", "dep-control", "workspace-control", password="unused-controlled-adapter",
                             sql=lambda sql, password: self.query("BEGIN;" + sql + suffix + "COMMIT;"),
                             redis=lambda arguments: [b"0", []])

    def provision(self):
        self.query("""INSERT INTO honua.promotion_deployments VALUES ('dep-control','StudioPublishedVersion','22222222-2222-4222-8222-222222222222','studio-route:/maps/control','Active','{"source":{"studioItemId":"11111111-1111-4111-8111-111111111111","studioVersionId":"22222222-2222-4222-8222-222222222222","studioPublicationRequestId":"33333333-3333-4333-8333-333333333333"}}',now(),now());
INSERT INTO honua.studio_content_items VALUES ('11111111-1111-4111-8111-111111111111','map','control','owned','tenant','22222222-2222-4222-8222-222222222222','22222222-2222-4222-8222-222222222222');
INSERT INTO honua.studio_content_versions VALUES ('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','controlled-hash',decode('000102ff','hex'),'{}','owned','tenant');
INSERT INTO honua.studio_publication_requests VALUES ('33333333-3333-4333-8333-333333333333','11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','{"route":"/maps/control","visibility":"public"}','accepted','dep-control',true);
INSERT INTO honua.gp_workspaces VALUES ('workspace-control',0,'control','owned','tenant',0,null,now(),null);
INSERT INTO honua.gp_workspace_artifacts VALUES ('artifact-control','workspace-control',0,'outputScalar','OUTPUTSCALAR',1,'controlled-uri',null,14,now(),'{}');
""")

    def test_actual_join_bytes_identity_and_missing_rows_are_observed(self):
        absent = self.observe()["stores"]
        self.assertIsNone(absent["deployment"]); self.assertIsNone(absent["workspace"])
        self.assertEqual(absent["artifacts"], [])
        self.provision()
        value = self.observe()["stores"]
        self.assertRegex(value["databaseIdentity"]["clusterId"], r"^[0-9]+$")
        self.assertEqual(value["databaseIdentity"]["database"], "honua_test")
        self.assertEqual(value["studioVersion"]["contentHashInputBase64"], "AAEC/w==")
        self.assertEqual(value["studioItem"]["ownerId"], "owned")
        self.assertTrue(value["studioPublication"]["governsStudioRoutes"])
        self.assertEqual(value["workspace"]["scope_id"], "tenant")
        self.assertEqual(value["artifacts"][0]["artifact_id"], "artifact-control")
        self.query("UPDATE honua.studio_content_versions SET content_hash='changed'; DELETE FROM honua.gp_workspaces;")
        changed = self.observe()["stores"]
        self.assertEqual(changed["studioVersion"]["contentHash"], "changed")
        self.assertIsNone(changed["workspace"])

    def test_actual_transaction_refuses_writes_and_ambiguous_publication_join(self):
        self.provision()
        with self.assertRaises(subprocess.CalledProcessError):
            self.observe("UPDATE honua.gp_workspaces SET owner_id='forbidden';")
        self.assertEqual(self.query("SELECT owner_id FROM honua.gp_workspaces;"), "owned")
        self.query("INSERT INTO honua.studio_publication_requests SELECT * FROM honua.studio_publication_requests;")
        with self.assertRaises(subprocess.CalledProcessError): self.observe()


if __name__ == "__main__":
    unittest.main()
