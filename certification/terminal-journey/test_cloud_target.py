"""Cloud cell targets, cloud principal minting and the cloud receipt kinds (honua-release#377, #507).

No cell, Docker or network: the admin API is an authored loopback peer, so the real Transport,
its credential policy and its last-exchange trace are exercised. Run: python test_cloud_target.py -v
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.append(str(ROOT / "e2e"))

import cloud_target  # noqa: E402
import discovery  # noqa: E402
import executor  # noqa: E402
import local_fixture  # noqa: E402
import run as driver  # noqa: E402
import stages  # noqa: E402
from transport import ExecutionError  # noqa: E402

TEMPLATE = json.loads((HERE / "targets" / "local-docker.json").read_text())
SECRETS = {
    "HONUA_CLOUD_JOURNEY_ADMIN": "Honua-Gate-Aa1!cell-bootstrap-secret",
    "HONUA_JOURNEY_DATASOURCE_HOST": "cell-db.abcdefgh.us-east-1.rds.amazonaws.com",
    "HONUA_JOURNEY_DATASOURCE_PORT": "5432",
    "HONUA_JOURNEY_DATASOURCE_DATABASE": "honua_cell",
    "HONUA_JOURNEY_DATASOURCE_USERNAME": "honua_cell_admin",
    "HONUA_JOURNEY_DATASOURCE_PASSWORD": "rds-password-value",
}


def cloud(kind="aws-serverless", endpoint="https://abc123.lambda-url.us-east-1.on.aws"):
    return cloud_target.build(copy.deepcopy(TEMPLATE), cell=f"{kind}/redis-off", endpoint=endpoint)


class CloudTargetDocumentTests(unittest.TestCase):
    def test_each_cloud_kind_gets_its_own_target_and_topology(self):
        for kind, topology in (("aws-ecs", "ecs-alb-tasks"), ("aws-serverless", "lambda-function")):
            target = cloud(kind, "https://cell.demo.honua.io/")
            self.assertEqual(target["kind"], kind)
            self.assertEqual(target["replicaBaseUrl"], "https://cell.demo.honua.io")
            self.assertEqual(target["replicaTopology"]["id"], topology)
            self.assertEqual(target["principalSource"], "cell-admin-api")
            self.assertEqual(set(target["principals"]), {"operator", "proposer", "approver", "viewer"})
            self.assertNotIn("compose", target)
            self.assertEqual(local_fixture.replica_topology(target)["id"], topology)
        self.assertIsNone(cloud_target.kind_of("aws-eks/redis-off"))
        self.assertIsNone(cloud_target.kind_of("stub/redis-on"))
        with self.assertRaises(cloud_target.TargetError):
            cloud_target.build(copy.deepcopy(TEMPLATE), cell="aws-eks/redis-off", endpoint="https://x.example")

    def test_rds_datasource_requires_tls_and_is_named_by_reference_only(self):
        target = cloud()
        datasource = target["execution"]["datasource"]
        self.assertIs(datasource["sslRequired"], True)
        self.assertEqual(datasource["sslMode"], "Require")
        self.assertEqual({k: v for k, v in datasource.items() if k.endswith("Env")},
                         {f"{field}Env": env for field, env in cloud_target.DATASOURCE_ENV.items()})
        # The upload fixture, style, pixel and map oracles are the authored local ones, unchanged.
        authored = {k: v for k, v in TEMPLATE["execution"].items() if k != "datasource"}
        self.assertEqual({k: v for k, v in target["execution"].items() if k != "datasource"}, authored)

    def test_the_retained_target_document_holds_no_credential_host_or_login(self):
        with mock.patch.dict(os.environ, SECRETS):
            serialized = json.dumps(cloud())
        for value in SECRETS.values():
            if value != "5432":
                self.assertNotIn(value, serialized)

    def test_second_tenant_is_documented_unavailable_never_fabricated(self):
        target = cloud()
        self.assertNotIn("other-tenant", target["principals"])
        unavailable = target["unavailablePrincipals"]["other-tenant"]
        self.assertEqual(unavailable["blockedBy"], [cloud_target.OTHER_TENANT_BLOCKER])
        self.assertIn("none is fabricated", unavailable["reason"])

    def test_validation_fails_closed_on_leaks_and_weakened_fixtures(self):
        def broken(change):
            target = cloud()
            change(target)
            with self.assertRaises(cloud_target.TargetError):
                cloud_target.validate(target, template=TEMPLATE)

        broken(lambda t: t["execution"]["datasource"].update(password="literal"))
        broken(lambda t: t["execution"]["datasource"].update(host="db.internal"))
        broken(lambda t: t["execution"]["datasource"].update(sslMode="Disable"))
        broken(lambda t: t["execution"]["datasource"].update(sslRequired=False))
        broken(lambda t: t["execution"]["datasource"].update(passwordEnv="OTHER_SECRET"))
        broken(lambda t: t["principals"].update({"other-tenant": "HONUA_JOURNEY_OTHER_TENANT_TOKEN"}))
        broken(lambda t: t["principals"].update(operator="hk_literal-key-value"))
        broken(lambda t: t.update(unavailablePrincipals={}))
        broken(lambda t: t.update(kind="aws-ecs"))
        broken(lambda t: t.update(replicaTopology=cloud_target.TOPOLOGIES["aws-ecs"]))
        broken(lambda t: t.update(replicaBaseUrl="https://user:pass@cell.example"))
        broken(lambda t: t.update(adminPassword={"env": "HONUA_CLOUD_JOURNEY_ADMIN", "default": "fallback"}))
        broken(lambda t: t.update(compose=TEMPLATE["compose"]))
        broken(lambda t: t["execution"]["upload"].update(sha256="0" * 64))
        broken(lambda t: t["execution"].update(pixelRgba=[0, 0, 0, 255]))


class AdminApiPeer:
    """An authored loopback stand-in for the cell admin API's key and role routes."""

    def __init__(self, *, revoke_status=200):
        self.seen, self.keys, self.revoke_status = [], {}, revoke_status
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def do_GET(self):
                peer.seen.append(("GET", self.path, self.headers.get("X-API-Key")))
                if self.path == "/api/v1/admin/roles/":
                    return self._reply(200, {"data": []})
                key_id = self.path.split("/")[5]
                grants = peer.keys[key_id]["permissions"]
                return self._reply(200, {"data": {"permissions": grants, "status": "active", "canAuthenticate": True}})

            def do_PUT(self):
                body = self._body()
                peer.seen.append(("PUT", self.path, self.headers.get("X-API-Key")))
                return self._reply(200, {"data": body["permissions"]})

            def do_POST(self):
                body = self._body()
                peer.seen.append(("POST", self.path, self.headers.get("X-API-Key")))
                if self.path == "/api/v1/admin/roles/":
                    return self._reply(201, {"data": {"roleId": "role-1"}})
                if self.path == "/api/v1/admin/api-keys/":
                    key_id = f"0000000{len(peer.keys)}-aaaa-bbbb-cccc-dddddddddddd"
                    peer.keys[key_id] = {"permissions": body["permissions"], "name": body["name"]}
                    return self._reply(201, {"data": {"apiKey": {"id": key_id}, "key": f"hk_minted_{len(peer.keys)}"}})
                key_id = self.path.split("/")[5]
                if peer.revoke_status != 200:
                    return self._reply(peer.revoke_status, {"error": "unavailable"})
                return self._reply(200, {"data": {"id": key_id, "status": "revoked"}})

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class CloudPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="cloud-principals-"))
        self.addCleanup(shutil.rmtree, self.workdir, True)
        patcher = mock.patch.dict(os.environ, {"HONUA_CLOUD_JOURNEY_ADMIN": SECRETS["HONUA_CLOUD_JOURNEY_ADMIN"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cloud_cell_mints_the_local_fixture_grants_through_its_admin_api_and_revokes_them(self):
        target = cloud()
        with AdminApiPeer() as peer:
            discovery.reset_trace()
            keys = local_fixture.credentials(target, self.workdir, peer.url, mint=True)
            # Same grants and Studio author role as local Docker; every call uses the cell bootstrap.
            self.assertEqual(sorted(k["permissions"] for k in peer.keys.values()),
                             sorted(local_fixture.GRANTS.values()))
            self.assertIn(("PUT", "/api/v1/admin/roles/role-1/permissions", SECRETS["HONUA_CLOUD_JOURNEY_ADMIN"]),
                          peer.seen)
            self.assertTrue(all(key == SECRETS["HONUA_CLOUD_JOURNEY_ADMIN"] for _, _, key in peer.seen))
            self.assertEqual(set(keys), {"operator", "proposer", "approver", "viewer"})
            self.assertNotIn("other-tenant", keys)
            self.assertEqual(len(set(keys.values())), 4)
            private = self.workdir / "private-principals.json"
            if os.name == "posix":
                self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("signingKey", json.loads(private.read_text()))
            # The last-exchange diagnostics never carry minted key material.
            self.assertNotIn("hk_minted", " ".join(discovery.describe_trace()))
            revoked, failed = local_fixture.revoke(target, self.workdir, peer.url)
            self.assertEqual((sorted(revoked), failed), (sorted(local_fixture.GRANTS), []))
            self.assertEqual(sorted(path for method, path, _ in peer.seen if path.endswith("/revoke")),
                             sorted(f"/api/v1/admin/api-keys/{key_id}/revoke" for key_id in peer.keys))
        local_fixture.cleanup(self.workdir)
        self.assertFalse((self.workdir / "private-principals.json").exists())

    def test_revocation_is_best_effort_and_names_what_it_could_not_revoke(self):
        target = cloud()
        with AdminApiPeer(revoke_status=503) as peer:
            local_fixture.credentials(target, self.workdir, peer.url, mint=True)
            revoked, failed = local_fixture.revoke(target, self.workdir, peer.url)
        self.assertEqual((revoked, sorted(failed)), ([], sorted(local_fixture.GRANTS)))

    def test_local_docker_is_never_revoked_through_the_cloud_path_and_unminted_cloud_resolves_refs(self):
        self.assertEqual(local_fixture.revoke(TEMPLATE, self.workdir, "http://127.0.0.1:1"), ([], []))
        keys = local_fixture.credentials(cloud(), self.workdir, "https://cell.invalid", mint=False)
        self.assertEqual(keys, {name: "" for name in local_fixture.GRANTS})
        self.assertFalse((self.workdir / "private-principals.json").exists())

    def test_a_key_id_that_could_escape_the_revoke_route_is_refused(self):
        transport = mock.Mock()
        transport.get_json.return_value = {"data": []}
        transport.http.side_effect = [
            (json.dumps({"data": {"roleId": "role-1"}}).encode(), 201),
            (json.dumps({"data": local_fixture.AUTHOR_GRANTS}).encode(), 200),
            (json.dumps({"data": {"apiKey": {"id": "../../roles/x"}, "key": "hk"}}).encode(), 201)]
        with mock.patch.object(local_fixture, "Transport", return_value=transport):
            with self.assertRaises(ExecutionError):
                local_fixture.credentials(cloud(), self.workdir, "https://cell.invalid", mint=True)


def engine(target, transport):
    observation = stages.Observation(setup_view_present=True, setup_discovery={"tools": [], "metadata": {}})
    return executor.JourneyExecutor({"workspaceId": "workspace"}, target, observation, transport)


class CloudExecutionTests(unittest.TestCase):
    def test_tenant_isolation_is_blocked_with_the_precise_reason_while_rbac_denial_runs(self):
        transport = mock.Mock(credentials={"viewer": "hk_viewer", "operator": "hk_operator"})
        transport.http.return_value = (b"{}", 403)
        run = engine(cloud(), transport)
        run.resources.update(itemId="item", versionId="version", contentHash="hash", draftId="draft")
        checks = run.verify_authority()
        tenant, rbac = checks["tenantIsolation"], checks["rbacDenial"]
        self.assertEqual(tenant.status, "blocked")
        self.assertEqual(tenant.blocked_by, [cloud_target.OTHER_TENANT_BLOCKER])
        self.assertEqual(tenant.detail, cloud_target.OTHER_TENANT_REASON)
        self.assertEqual(rbac.status, "pass")
        transport.http.assert_called_once_with("GET", "/api/v1/admin/api-keys/", principal="viewer", expected=(403,))
        # Without a documented reason the old, generic blocker remains.
        generic = engine({}, mock.Mock(credentials={}))
        generic.resources.update(itemId="item", versionId="version", contentHash="hash")
        self.assertEqual(generic.verify_authority()["tenantIsolation"].blocked_by, [stages.JOURNEY_DRIVER])

    def test_cloud_replica_reread_goes_through_the_cell_endpoint_and_names_its_topology(self):
        for kind in ("aws-ecs", "aws-serverless"):
            target = cloud(kind, "https://cell.demo.honua.io")
            transport = mock.Mock(base_url="https://cell.demo.honua.io", proxy=None, honua=None,
                                  workdir=Path("/tmp"), credentials={"proposer": "hk"})
            run = engine(target, transport)
            run.fixture = {"mapBody": {}}
            run.resources.update(itemId="item", versionId="version", contentHash="hash")
            document = {"itemId": "item", "versionId": "version", "contentHash": "hash",
                        "envelope": {"family": "map", "body": {}}}
            transport.get_json.return_value = document
            with mock.patch.object(executor, "Transport") as replica:
                replica.return_value.get_json.return_value = document
                run.check_map()
            replica.assert_called_once()
            self.assertEqual(replica.call_args.args[0], "https://cell.demo.honua.io")
            row = run.evidence["checks"]["6"]["replica-map"]
            self.assertEqual(row["status"], "pass")
            self.assertIn(cloud_target.TOPOLOGIES[kind]["id"], row["invocation"])
            self.assertEqual(run.evidence["proofs"]["replica-map"]["replicaTopology"],
                             cloud_target.TOPOLOGIES[kind]["id"])

    def test_cloud_datasource_resolves_references_in_memory_and_blocks_when_missing(self):
        run = engine(cloud(), mock.Mock(credentials={}))
        with mock.patch.dict(os.environ, SECRETS):
            arguments = run.datasource_arguments()
        self.assertEqual(arguments, {
            "name": "journey_source", "host": SECRETS["HONUA_JOURNEY_DATASOURCE_HOST"], "port": 5432,
            "databaseName": "honua_cell", "username": "honua_cell_admin",
            "password": "rds-password-value", "sslRequired": True, "sslMode": "Require"})
        missing = {k: v for k, v in SECRETS.items() if k != "HONUA_JOURNEY_DATASOURCE_HOST"}
        with mock.patch.dict(os.environ, missing, clear=True):
            with self.assertRaises(ExecutionError) as raised:
                run.datasource_arguments()
        self.assertTrue(raised.exception.blocked)
        self.assertIn("host", raised.exception.reason)
        # The local target keeps its literal compose coordinates and password reference.
        local = engine(TEMPLATE, mock.Mock(credentials={}))
        with mock.patch.dict(os.environ, {"HONUA_JOURNEY_DATASOURCE_PASSWORD": "honua"}):
            self.assertEqual(local.datasource_arguments()["host"], "db")


class CloudReceiptSchemaTests(unittest.TestCase):
    schema = HERE / "receipt.schema.json"

    def receipt(self, kind, stage_results=None):
        target = cloud(kind)
        manifest = __import__("yaml").safe_load((ROOT / "platform-manifest.yaml").read_text())
        journey = json.loads((HERE / "journey.v1.json").read_text())
        workspace = driver.pins.ClientWorkspace(status="blocked", root=None, reason="offline")
        return driver.build_receipt(
            manifest=manifest, journey=journey,
            roster=driver.roster_verdict(json.loads((HERE / "control-plane-roster.v1.json").read_text()), None, None),
            evidence_uri="urn:test:cloud#honua-run=1/1", mode="live", target=target, target_path=None,
            target_base_url=target["replicaBaseUrl"], workspace=workspace,
            stage_results=stage_results or [stages.StageResult(n, s["id"], s["command"], "blocked",
                                                               blocked_by=[stages.JOURNEY_DRIVER])
                                            for n, s in enumerate(journey["stages"], 1)],
            notices=["cloud"])

    def test_cloud_kinds_and_live_sources_are_admitted(self):
        for kind in ("aws-ecs", "aws-serverless"):
            receipt = self.receipt(kind)
            driver.validate_receipt(receipt, self.schema)
            self.assertEqual(receipt["target"]["kind"], kind)
            self.assertEqual({row["evidence"]["source"] for row in receipt["stages"]}, {"live-" + kind})

    def test_a_passing_serverless_stage_qualifies_on_live_serverless_evidence_only(self):
        import jsonschema
        receipt = self.receipt("aws-serverless")
        row = receipt["stages"][0]
        row.update(status="pass", blockedBy=[], checks=[{"id": "1.ready", "kind": "http", "invocation": "GET",
                                                          "status": "pass", "detail": "ok"}])
        row["evidence"].update(freshness="verified-current", completeness="complete")
        driver.validate_receipt(receipt, self.schema)
        row["evidence"]["source"] = "harness-build"
        with self.assertRaises(jsonschema.ValidationError):
            driver.validate_receipt(receipt, self.schema)
        receipt["target"]["kind"] = "aws-lambda"
        with self.assertRaises(jsonschema.ValidationError):
            driver.validate_receipt(receipt, self.schema)

    def test_committed_receipts_still_validate(self):
        committed = sorted((HERE / "fixtures").glob("*.local-docker.json")) + sorted(
            (ROOT / "artifacts").glob("terminal-journey-*.json"))
        self.assertTrue(committed)
        schema = json.loads(self.schema.read_text())
        import jsonschema
        for path in committed:
            document = json.loads(path.read_text())
            if document.get("receiptSchema") == "terminal-journey-receipt-v1":
                jsonschema.validate(document, schema)


class CloudJourneyHandoffTests(unittest.TestCase):
    def setUp(self):
        import cloud_journey
        self.cj = cloud_journey

    def test_handoff_carries_the_seed_connection_only_when_there_is_one(self):
        datasource = {"host": "db.example", "port": 5432, "databaseName": "honua", "username": "u", "password": "p"}
        self.assertEqual(self.cj.pack_handoff("key", None), "key")
        self.assertEqual(self.cj.unpack_handoff("key"), ("key", None))
        packed = self.cj.pack_handoff("key", {**datasource, "provider": "PostGIS", "sslMode": "Require"})
        self.assertEqual(self.cj.unpack_handoff(packed), ("key", datasource))
        for bad in ('{"schema":"other"}', '{"schema":"honua-cloud-journey-handoff-v1","appKey":"k"}',
                    json.dumps({"schema": self.cj.HANDOFF_SCHEMA, "appKey": "k",
                                "datasource": {**datasource, "port": "5432"}})):
            with self.assertRaises(ValueError):
                self.cj.unpack_handoff(bad)

    def test_cell_datasource_is_exposed_for_one_phase_and_survives_the_candidate_sandbox(self):
        datasource = {"host": "db.example", "port": 5432, "databaseName": "honua", "username": "u", "password": "p"}
        with mock.patch.dict(os.environ, {}, clear=False):
            for env in cloud_target.DATASOURCE_ENV.values():
                os.environ.pop(env, None)
            with self.cj.cell_datasource(datasource):
                with driver.pins.candidate_sandbox():
                    self.assertEqual(os.environ["HONUA_JOURNEY_DATASOURCE_HOST"], "db.example")
                    self.assertEqual(os.environ["HONUA_JOURNEY_DATASOURCE_PORT"], "5432")
                    self.assertEqual(os.environ["HONUA_JOURNEY_DATASOURCE_PASSWORD"], "p")
            self.assertFalse(any(env in os.environ for env in cloud_target.DATASOURCE_ENV.values()))
            with self.cj.cell_datasource(None):
                self.assertNotIn("HONUA_JOURNEY_DATASOURCE_HOST", os.environ)

    def test_serverless_attempt_runs_live_on_its_own_target_without_the_unsupported_notice(self):
        cj = self.cj
        seen = {}
        workspace = driver.pins.ClientWorkspace(status="blocked", root=None, reason="offline")

        def live(target, pinned, contract, workdir, base_url, keep):
            seen.update(target=target, base_url=base_url, env=dict(os.environ))
            return workspace, [stages.StageResult(n, s["id"], s["command"], "blocked", blocked_by=["x"])
                               for n, s in enumerate(contract["stages"], 1)], [], None

        datasource = {"host": "cell-db.example", "port": 5432, "databaseName": "honua", "username": "dbadmin",
                      "password": "rds-secret-value"}
        env = {"GITHUB_RUN_ID": "cloudtarget-offline", "GITHUB_RUN_ATTEMPT": "1"}
        run_module, _ = cj.drivers()
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(run_module, "run_live", live), \
                    mock.patch.object(cj, "server_identity", lambda endpoint: None), \
                    cj.cell_datasource(datasource):
                record = cj.attempt("aws-serverless/redis-on", 1, "https://fn.lambda-url.us-east-1.on.aws",
                                    "admin-secret-value", None)
            receipt_path = ROOT / "e2e" / record["receipt"]
            receipt = json.loads(receipt_path.read_text())
            target_text = (receipt_path.parent / "target-1.json").read_text()
            self.assertEqual(seen["target"]["kind"], "aws-serverless")
            self.assertEqual(seen["env"]["HONUA_JOURNEY_DATASOURCE_PASSWORD"], "rds-secret-value")
            self.assertEqual(receipt["mode"], "live")
            self.assertEqual(receipt["target"]["kind"], "aws-serverless")
            self.assertEqual({row["evidence"]["source"] for row in receipt["stages"]}, {"live-aws-serverless"})
            self.assertFalse(any(n.startswith(cj.UNSUPPORTED_KIND_NOTICE) for n in receipt["notices"]))
            for secret in ("admin-secret-value", "rds-secret-value", "cell-db.example", "dbadmin"):
                self.assertNotIn(secret, target_text)
                self.assertNotIn(secret, receipt_path.read_text())
        finally:
            shutil.rmtree(cj.EVIDENCE / "cloudtarget-offline", ignore_errors=True)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class SealedHandoffTests(unittest.TestCase):
    def test_a_handoff_larger_than_one_rsa_block_round_trips_sealed(self):
        import run_cloud
        store = {}

        def fake(argv, **kwargs):
            if argv[:2] != ["aws", "secretsmanager"]:
                return subprocess.run(argv, **kwargs)
            name = argv[argv.index("--name" if "--name" in argv else "--secret-id") + 1]
            if argv[2] == "create-secret":
                store[name] = kwargs["input"]
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            return subprocess.CompletedProcess(argv, 0, store[name] + "\n", "")

        value = run_cloud.cloud_journey.pack_handoff("Honua-Gate-Aa1!" + "k" * 43, {
            "host": "honuacell-db." + "x" * 40 + ".us-east-1.rds.amazonaws.com", "port": 5432,
            "databaseName": "honua", "username": "honua_admin", "password": "p" * 64})
        self.assertGreater(len(value.encode()), run_cloud.SEAL_BLOCK_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for bits in ("2048", "3072"):
                subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{bits}",
                                "-out", str(root / "private.pem")], check=True, capture_output=True)
                subprocess.run(["openssl", "pkey", "-in", str(root / "private.pem"), "-pubout",
                                "-out", str(root / "public.pem")], check=True, capture_output=True)
                store.clear()
                run_cloud.store_secret("cell-app-key", value, "fixture", run=fake)
                run_cloud.seal_key("cell-app-key", root / "public.pem", root / "key.sealed", run=fake)
                sealed = (root / "key.sealed").read_bytes()
                self.assertNotIn(b"rds.amazonaws.com", sealed)
                self.assertEqual(len(sealed) % (int(bits) // 8), 0)
                self.assertEqual(run_cloud.open_key(root / "key.sealed", root / "private.pem"), value)
                (root / "key.sealed").write_bytes(sealed[:-1])
                with self.assertRaises(ValueError):
                    run_cloud.open_key(root / "key.sealed", root / "private.pem")


if __name__ == "__main__":
    unittest.main()
