"""Endpoint controls exercise deployment binding and authority boundaries, never qualify a client."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("installed_endpoint_test", HERE / "endpoint.py")
endpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(endpoint)
run = endpoint.runner()


def inputs():
    path = run.ROOT / "platform-manifest.yaml"
    manifest, matrix = run.load_inputs(path, run.DEFAULT_MATRIX)
    binding = {"endpoint": "https://candidate.example", "cell": "aws-ecs/redis-on", "runId": "123", "runAttempt": "1",
        "candidateDigest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "server": {"sourceSha": manifest["components"]["honua-server"]["sha"], "image": run.server_image_ref(manifest)},
        "fixtureRevision": matrix["fixtureRevision"],
        "matrixDigest": hashlib.sha256(json.dumps(matrix, sort_keys=True).encode()).hexdigest(),
        "suiteFixtureDigest": hashlib.sha256((HERE / "fixture.v1.json").read_bytes()).hexdigest(),
        "interopFixtureDigest": hashlib.sha256((HERE / "interop/fixture.v1.json").read_bytes()).hexdigest(),
        "preparedAt": datetime.now(timezone.utc).isoformat()}
    context = {"schema": endpoint.SCHEMA, "binding": binding,
        "database": {"host": "private-db", "port": 5432, "databaseName": "honua", "username": "fixture",
                     "provider": "PostGIS", "sslRequired": True, "sslMode": "Require"},
        "dbPassword": "database-secret", "bearer": "bearer-secret", "published": {
            "sites": {"service": "sdkreg-sites", "layerId": 1}, "area": {"service": "sdkreg-area", "layerId": 2},
            "edits": {"python": {"service": "sdkreg-edits-python", "layerId": 3}}},
        "principals": {"proposer": {"key": "proposer-secret", "id": "proposer-id"}},
        "interopPrincipals": {"approver": {"key": "approver-secret", "id": "approver-id"}},
        "interopContext": {"connectionId": "fixture-connection", "tag": "fixture"}}
    return manifest, matrix, context, path


class EndpointTests(unittest.TestCase):
    def setUp(self):
        # Direct runner calls below must use the same sibling-import boundary as
        # the production prepare/run_cloud entrypoints, including lazy imports.
        scope = endpoint.module_scope()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_endpoint_imports_preserve_terminal_oracles_and_judge(self):
        original = {name: types.ModuleType("terminal_" + name) for name in ("oracles", "judge")}
        before = list(sys.path)
        with mock.patch.dict(sys.modules, original):
            with endpoint.module_scope():
                self.assertEqual(HERE / "oracles.py", Path(sys.modules["oracles"].__file__))
                self.assertIs(sys.modules["judge"].oracles, sys.modules["oracles"])
                self.assertIs(run._regression().oracles, sys.modules["oracles"])
            self.assertIs(original["oracles"], sys.modules["oracles"])
            self.assertIs(original["judge"], sys.modules["judge"])
        self.assertEqual(before, sys.path)

    def test_exact_context_accepts_both_full_workflow_topologies(self):
        manifest, matrix, context, path = inputs()
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
            for cell in ("aws-ecs/redis-on", "aws-serverless/redis-on"):
                context["binding"]["cell"] = cell
                context["binding"]["server"]["image"] = endpoint.cloud_policy().candidate_image(cell, manifest)
                endpoint.validate_context(context, manifest, matrix, path)

    def test_generic_lambda_and_raw_mirror_images_cannot_substitute_for_each_other(self):
        manifest, matrix, context, path = inputs()
        policy = endpoint.cloud_policy()
        generic = policy.candidate_image("aws-ecs/redis-on", manifest)
        specific = policy.candidate_image("aws-serverless/redis-on", manifest)
        self.assertNotEqual(generic, specific)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
            for cell, wrong in (("aws-serverless/redis-on", generic), ("aws-ecs/redis-on", specific),
                               ("aws-serverless/redis-on", "123456789012.dkr.ecr.us-east-1.amazonaws.com/mirror@" + manifest["components"]["honua-server"]["awsLambdaEcrDigest"])):
                with self.subTest(cell=cell, image=wrong):
                    context["binding"]["cell"] = cell
                    context["binding"]["server"]["image"] = wrong
                    with self.assertRaisesRegex(ValueError, "contract differs"):
                        endpoint.validate_context(context, manifest, matrix, path)

    def test_lambda_mirror_readback_requires_observed_ecr_digest_before_canonical_identity(self):
        manifest, matrix, context, path = inputs()
        target = mock.Mock(_workdir=Path("/isolated-iac"))
        target._tf.return_value.stdout = "owned-function"
        server = manifest["components"]["honua-server"]
        context["binding"]["cell"] = "aws-serverless/redis-on"
        for digest, passes in ((server["awsLambdaEcrDigest"], True), ("sha256:" + "0" * 64, False)):
            observed = {"Code": {"RepositoryType": "ECR", "ResolvedImageUri": "123.dkr.ecr.us-east-1.amazonaws.com/mirror@" + digest},
                        "Configuration": {"PackageType": "Image"}}
            readback = mock.Mock(stdout=json.dumps(observed))
            image = endpoint.cloud_policy().observed_lambda_image(target, manifest, run=mock.Mock(return_value=readback))
            context["binding"]["server"]["image"] = image
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
                if passes:
                    endpoint.validate_context(context, manifest, matrix, path)
                    receipt = run.execute(manifest, {**matrix, "cells": []}, "urn:unit-control", endpoint_context=context)
                    self.assertEqual(image, receipt["server"]["image"])
                    self.assertEqual("fail", receipt["status"])
                else:
                    self.assertIsNone(image)
                    with self.assertRaisesRegex(ValueError, "contract differs"):
                        endpoint.validate_context(context, manifest, matrix, path)

    def test_approved_alb_http_is_recognized_but_clients_wait_for_https(self):
        manifest, matrix, context, path = inputs()
        context["binding"]["endpoint"] = "http://owned-cell.us-east-1.elb.amazonaws.com"
        state = {name: context["binding"][name] for name in ("endpoint", "cell", "runId", "runAttempt", "candidateDigest")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True), \
             mock.patch.object(run, "execute") as execute:
            endpoint.validate_context(context, manifest, matrix, path)
            with self.assertRaisesRegex(endpoint.PrerequisiteUnavailable, "needs HTTPS"):
                endpoint.run_cloud(state, context, "app", Path(tmp) / "receipt.json")
            execute.assert_not_called()
            self.assertFalse((Path(tmp) / "receipt.json").exists())
        for url in ("http://other.example", "http://owned-cell.elb.amazonaws.com.evil.example", "http://.elb.amazonaws.com"):
            context["binding"]["endpoint"] = url
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
                with self.assertRaisesRegex(ValueError, "contract differs"):
                    endpoint.validate_context(context, manifest, matrix, path)

    def test_wrong_artifact_deployment_fixture_and_stale_context_fail(self):
        manifest, matrix, original, path = inputs()
        mutations = [
            ("server", {"sourceSha": "f" * 40, "image": run.server_image_ref(manifest)}),
            ("server", {**original["binding"]["server"], "image": "unobserved"}),
            ("candidateDigest", "0" * 64), ("runId", "another-run"), ("runAttempt", "2"),
            ("fixtureRevision", "other-fixture"), ("suiteFixtureDigest", "0" * 64),
            ("matrixDigest", "0" * 64),
            ("interopFixtureDigest", "0" * 64), ("endpoint", "https://user:secret@candidate.example"),
            ("endpoint", "http://candidate.example"), ("cell", "local-docker"),
            ("preparedAt", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()),
            ("unexpectedSecret", "must-not-enter-receipt")]
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
            for name, value in mutations:
                with self.subTest(name=name, value=value):
                    context = copy.deepcopy(original)
                    context["binding"][name] = value
                    with self.assertRaisesRegex(ValueError, "contract differs"):
                        endpoint.validate_context(context, manifest, matrix, path)

    def test_live_source_is_independent_of_supplied_context(self):
        manifest, _, context, _ = inputs()
        for revision, expected in ((manifest["components"]["honua-server"]["sha"], True), ("f" * 40, False)):
            response = io.BytesIO(json.dumps({"server": {"deploymentRevision": revision,
                "deploymentRevisionSource": "commit-sha"}}).encode())
            with mock.patch.object(endpoint.urllib.request, "urlopen", return_value=response):
                if expected:
                    endpoint.verify_live_identity(context, manifest)
                else:
                    with self.assertRaisesRegex(ValueError, "independently"):
                        endpoint.verify_live_identity(context, manifest)

    def test_cloud_authority_refused_before_entering_client_scope(self):
        context = inputs()[2]
        for name in endpoint.CLOUD_CREDENTIALS:
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "cloud-secret"}, clear=True):
                with self.assertRaisesRegex(ValueError, "separate job"):
                    with endpoint.client_environment(context, "application-secret"):
                        self.fail("cloud authority reached client scope")

    def test_install_environment_drops_inherited_tokens_and_restores_parent(self):
        context = inputs()[2]
        original = {"PATH": "/bin", "HOME": "/tmp/home", "GITHUB_TOKEN": "repository-secret",
                    "ARBITRARY_SECRET": "sensitive", "SDKREG_SIGNING_KEY": "issuer-private-key"}
        with mock.patch.dict(os.environ, original, clear=True):
            with endpoint.client_environment(context, "application-secret"):
                self.assertEqual({"PATH", "HOME", "DOTNET_CLI_HOME", "HONUA_SERVER_URL", "E2E_API_KEY"}, set(os.environ))
                self.assertNotEqual(original["HOME"], os.environ["HOME"])
                self.assertTrue(Path(os.environ["HOME"]).is_dir())
                self.assertEqual("https://candidate.example", os.environ["HONUA_SERVER_URL"])
                self.assertNotIn("SDKREG_SIGNING_KEY", os.environ)
            self.assertEqual(original, dict(os.environ))

    def test_private_context_and_secret_redaction(self):
        context = inputs()[2]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text(json.dumps(context))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "only by its owner"):
                endpoint.read_context(path)
            path.chmod(0o600)
            self.assertEqual(context, endpoint.read_context(path))
        receipt = {"detail": "application-secret database-secret bearer-secret proposer-secret approver-secret"}
        self.assertEqual({"detail": " ".join(["[redacted]"] * 5)},
                         endpoint.redact_receipt(receipt, context, "application-secret"))

    def test_preparation_seeds_authored_sql_without_running_installers(self):
        manifest, _, context, path = inputs()
        rx = run._regression()
        target = mock.Mock()
        target.seed_database.return_value = {**context["database"], "password": context["dbPassword"]}
        api = mock.Mock()
        api.request.return_value = {"connectionId": "connection"}
        state = {**context["binding"], "runningImage": run.server_image_ref(manifest),
                 "observedServer": {"revision": manifest["components"]["honua-server"]["sha"]}}
        with mock.patch.object(rx, "AdminApi", return_value=api), \
             mock.patch.object(rx, "publish_harness", return_value=context["published"]) as publish, \
             mock.patch.object(rx, "mint_principals", return_value=context["principals"]), \
             mock.patch.object(run, "_run", side_effect=AssertionError("installer in trusted phase")):
            prepared = endpoint.prepare(target, state, manifest, path, admin_key="app", signing_key="f" * 64)
        sql = target.seed_database.call_args.args[0]
        for table in ("sdkreg_sites", "sdkreg_area", "sdkreg_edits_dotnet", "sdkreg_lifecycle_python", "sdkreg_proposal_clijs"):
            self.assertIn(table, sql)
        self.assertIn(run._interop().fixture["handoff"]["tablePrefix"], sql)
        self.assertTrue(publish.call_args.args[1]["lifecycle"]["database"]["sslRequired"])
        claims = json.loads(__import__("base64").urlsafe_b64decode(prepared["bearer"].split(".")[1] + "=="))
        self.assertEqual("https://terminal-journey.invalid", claims["iss"])
        self.assertEqual("terminal-journey", claims["aud"])

    def test_cloud_adapter_checks_identity_before_any_installer_and_keeps_full_matrix(self):
        manifest, matrix, context, _ = inputs()
        state = {name: context["binding"][name] for name in ("endpoint", "cell", "runId", "runAttempt", "candidateDigest")}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True):
            with mock.patch.object(endpoint, "verify_live_identity", side_effect=ValueError("wrong candidate")), \
                 mock.patch.object(run, "execute") as execute:
                with self.assertRaisesRegex(ValueError, "wrong candidate"):
                    endpoint.run_cloud(state, context, "app", Path(tmp) / "receipt.json")
                execute.assert_not_called()
            receipt = {"status": "blocked", "results": [{"cell": cell["id"], "status": cell["status"]} for cell in matrix["cells"]]}
            with mock.patch.object(endpoint, "verify_live_identity") as identity, \
                 mock.patch.object(run, "execute", return_value=receipt) as execute, \
                 mock.patch.object(run, "verify_receipt", return_value=[]):
                actual = endpoint.run_cloud(state, context, "app", Path(tmp) / "receipt.json")
                self.assertEqual(41, len(execute.call_args.args[1]["cells"]))
                self.assertEqual("blocked", actual["status"])
                self.assertEqual(2, identity.call_count)
                self.assertEqual(context, execute.call_args.kwargs["endpoint_context"])

    def test_endpoint_suite_reuses_prepared_fixture_and_tls_without_docker_or_signer(self):
        manifest, matrix, context, _ = inputs()
        matrix["cells"] = [cell for cell in matrix["cells"] if cell["driver"] == "pypi-sdk"]
        rx = run._regression()
        self.assertEqual(HERE / "oracles.py", Path(rx.oracles.__file__))
        seen = []

        def install(driver, pins, work):
            work.mkdir()
            return True, "installed", ["installed-client"], {}

        def driver(command, work, env):
            seen.append((json.loads((work / "plan.json").read_text()), env))
            return 0, "", ""

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"HONUA_SERVER_URL": context["binding"]["endpoint"], "E2E_API_KEY": "app"}, clear=True), \
             mock.patch.object(run, "install_suite_client", side_effect=install), \
             mock.patch.object(rx, "seed", side_effect=AssertionError("Docker seed on endpoint")), \
             mock.patch.object(rx, "publish_harness", side_effect=AssertionError("publication repeated")), \
             mock.patch.object(rx, "mint_bearer", side_effect=AssertionError("signer in client job")), \
             mock.patch.object(rx, "run_driver", side_effect=driver), \
             mock.patch.object(rx, "evaluate_cell", return_value=("fail", "intentionally absent observation", [])):
            results = run.run_suite(manifest, matrix, Path(tmp), context)
        self.assertEqual(len(matrix["cells"]), len(results))
        plan, env = seen[0]
        self.assertEqual(context["binding"]["endpoint"], plan["baseUrl"])
        self.assertEqual(context["database"], plan["lifecycle"]["database"])
        self.assertTrue(plan["lifecycle"]["database"]["sslRequired"])
        self.assertEqual(context["bearer"], env["SDKREG_BEARER"])
        self.assertEqual(context["dbPassword"], env["SDKREG_DB_PASSWORD"])


if __name__ == "__main__":
    unittest.main()
