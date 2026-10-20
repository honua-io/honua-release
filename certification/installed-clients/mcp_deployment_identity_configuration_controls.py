"""Prepared explicit configuration controls; no actual container qualification."""
import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import mcp_deployment_identity_configuration as subject

RUNTIME = {"running": True, "containerId": "a"*64, "imageId": "sha256:"+"b"*64, "candidateSourceSha": "c"*40}

class ConfigurationControls(unittest.TestCase):
    def setUp(self):
        self.configuration = subject.declaration("mcp-deployment-"+"d"*12)

    def observe(self, environment=None, inspect=None):
        entries = [key+"="+value for key,value in (environment or subject.ENV).items()]
        # A real candidate has unrelated private environment. It must never leak.
        entries.append("HONUA_ADMIN_PASSWORD=controlled-private-not-for-receipt")
        with patch.object(subject.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(entries))) as run:
            result = subject.observe_configuration(self.configuration, inspect or (lambda: copy.deepcopy(RUNTIME)))
            self.assertEqual(run.call_args.args[0], ["docker", "inspect", "--format", "{{json .Config.Env}}", RUNTIME["containerId"]])
            self.assertNotIn("controlled-private-not-for-receipt", json.dumps(result))
            return result

    def test_exact_owned_config_readback_preserves_admin_bypass_and_nonbypass_role(self):
        observed = self.observe()
        checked = subject.verify_configuration(self.configuration, observed, RUNTIME)
        self.assertEqual(checked["foreignRole"], "scoped-api-key")
        self.assertNotIn("scoped-api-key", subject.ENV.values())
        self.assertEqual(observed["environment"], subject.ENV)

    def test_missing_changed_alias_and_unreviewed_authorization_keys_refuse(self):
        for mutate in (lambda env: env.pop("MultiTenancy__Enabled"),
                       lambda env: env.update({"MultiTenancy__Enabled": "false"}),
                       lambda env: env.update({"multitenancy__Enabled": "true"}),
                       lambda env: env.update({"Studio__EndUserAuthorization__DefaultRole": "admin"})):
            environment=dict(subject.ENV); mutate(environment)
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.observe(environment)

    def test_runtime_change_during_observation_refuses(self):
        changed=copy.deepcopy(RUNTIME); changed["containerId"]="e"*64
        with self.assertRaises(ValueError):
            self.observe(inspect=iter([RUNTIME, changed]).__next__)

    def test_original_configuration_observation_rejudged_against_exact_runtime_and_declaration(self):
        observed=self.observe()
        for field,value in (("containerId","e"*64), ("imageId","sha256:"+"f"*64),
                            ("declarationSha256","sha256:"+"0"*64), ("environment",{})):
            altered=copy.deepcopy(observed); altered[field]=value
            with self.subTest(field=field), self.assertRaises(ValueError):
                subject.verify_configuration(self.configuration, altered, RUNTIME)
        altered=copy.deepcopy(self.configuration); altered["foreignRole"]="admin"
        with self.assertRaises(ValueError): subject.verify_configuration(altered, observed, RUNTIME)

if __name__ == "__main__": unittest.main()
