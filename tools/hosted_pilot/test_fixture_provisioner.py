"""Credential and authenticated state refusals for the private SQL provisioner."""
import unittest
import fixture_provisioner as provisioner
from agent_e2e_pack import PackError


class MasterBinding(unittest.TestCase):
    def test_private_module_connection_forces_verified_tls_and_exact_app_database(self):
        config = {'databaseHost': 'isolated.rds.amazonaws.com', 'database': 'honua_agent_bound', 'appRole': 'honua_agent_app'}
        raw = 'Host=isolated.rds.amazonaws.com;Port=5432;Database=honua_agent_bound;Username=honua_agent_app;Password='+'a'*48+';SSL Mode=Require'
        settings = provisioner.master_settings(raw, config)
        self.assertEqual(settings['sslmode'], 'verify-full')
        for replacement in [raw.replace('honua_agent_bound', 'shared'), raw+';Host=elsewhere', raw.replace('Port=5432', 'Port=1')]:
            with self.assertRaises(PackError):
                provisioner.master_settings(replacement, config)
