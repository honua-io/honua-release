"""Installed credential custody controls; no deployed runtime qualification."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import native_bootstrap as bootstrap
from agent_e2e_budget import update
from agent_e2e_interactive_budget import funding_binding
from agent_e2e_pack import PackError, digest_bytes
import test_native_bootstrap as legacy


class InstalledRuntimeCustody(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.CredentialCustody()
        self.fixture.setUp()
        self.config = self.fixture.config
        self.durable = self.fixture.durable
        self.config['expiresAt'] = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp())+3600,
                                                        timezone.utc).isoformat()
        self.config['installedRuntime'] = {'runtimeId': 'runtime-original', 'mutationCeilings': {
            'createMap': True, 'createDashboard': False, 'compose': True,
            'proposePublication': True, 'proposeRestore': False,
            'datasetImportScopes': self.config['datasetImportScopes'].copy(),
            'managedProcessIds': self.config['managedProcessIds'].copy()}}
        self.config['secretArns']['control'] = self.config['secretArns']['agent'].replace('/agent-', '/control-')
        original = copy.deepcopy(self.durable.state['ledger'])
        artifact = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'ledger': original,
            'source': {'repository': 'honua-io/honua-release', 'workflow': '.github/workflows/agent-e2e-campaign.yml',
                       'sourceSha': 'a'*40, 'runId': 123, 'runAttempt': 1}}
        imported = {'artifactDigest': digest_bytes(json.dumps(artifact).encode()), 'artifact': artifact}
        self.config['bootstrapArtifactDigest'] = imported['artifactDigest']
        original_configuration = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'],
            'ledger': copy.deepcopy(original), 'history': {'path': 'original-history.json'},
            'expiresAt': self.config['expiresAt'], 'liveLiabilityReferences': [{'path': 'original-liability.json'}]}
        raw = json.dumps(original_configuration).encode()
        binding, _ = funding_binding(imported, source_reader=lambda sha, path: raw)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)/'ledger.json'
        path.write_text(json.dumps(original))
        update(path, 'amend-foss4g', candidate_key=self.config['candidateKey'])
        update(path, 'enable-interactive-accounting', candidate_key=self.config['candidateKey'], binding=binding)
        self.durable.state = {'ledger': json.loads(path.read_text()), 'bootstrapImport': imported,
                             'interactiveFunding': {'binding': binding, 'configuration': raw.decode()}}

    def test_control_and_task_credentials_have_distinct_identity_authority_and_private_custody(self):
        receipt = self.fixture.provision()
        self.assertEqual(set(receipt['keys']), {'setup', 'observer', 'agent', 'control'})
        self.assertEqual(len({record['apiKeyId'] for record in receipt['keys'].values()}), 4)
        control = receipt['keys']['control']
        self.assertEqual(control['permissions'], ['admin:read', 'agent:runtime-control:runtime-original'])
        self.assertEqual(control['secretArn'], self.config['secretArns']['control'])
        self.assertEqual(control['effectivePermissions']['id'], control['apiKeyId'])
        agent = receipt['keys']['agent']['permissions']
        self.assertIn('agent:runtime-task:runtime-original', agent)
        self.assertNotIn('agent:runtime-task:runtime-original', control['permissions'])
        self.assertIn('agent:studio-create-map-runtime:runtime-original', agent)
        self.assertIn('agent:studio-compose-runtime:runtime-original', agent)
        self.assertIn('agent:studio-propose-publication-runtime:runtime-original', agent)
        self.assertNotIn('agent:studio-create-dashboard-runtime:runtime-original', agent)
        self.assertNotIn('agent:studio-restore-runtime:runtime-original', agent)
        self.assertNotIn('agent:runtime-control:runtime-original', agent)
        self.assertNotIn('admin:write', agent)
        self.assertIn('agent:studio-compose:'+self.config['itemId'], agent)
        self.assertEqual(receipt['runtimeInstallation'], {'runtimeId': 'runtime-original',
            'state': 'registration-pending', 'requiresNativeRuntimeRegistration': True})
        self.assertFalse(receipt['qualification'])
        self.assertEqual(self.fixture.secrets.put_secret_value.call_count, 4)
        for row in self.durable.state['scenarioOperations'].values():
            if 'expiresAt' in row['value']:
                self.assertEqual(row['value']['expiresAt'], self.config['expiresAt'])
        self.assertNotIn('private-key-', json.dumps(self.durable.state))
        self.assertNotIn('private-root', json.dumps(self.durable.state))

    def test_restore_requires_its_own_explicit_runtime_ceiling(self):
        self.config['installedRuntime']['mutationCeilings']['proposeRestore'] = True
        receipt = self.fixture.provision()
        self.assertIn('agent:studio-restore-runtime:runtime-original', receipt['keys']['agent']['permissions'])
        self.assertNotIn('agent:studio-restore:'+self.config['itemId'], receipt['keys']['agent']['permissions'])
        self.assertEqual(receipt['keys']['control']['permissions'], ['admin:read', 'agent:runtime-control:runtime-original'])

    def test_import_only_runtime_keeps_its_required_non_authorizing_task_marker(self):
        self.config['installedRuntime']['mutationCeilings'].update({name: False for name in bootstrap.RUNTIME_STUDIO_PERMISSIONS})
        receipt = self.fixture.provision()
        task = receipt['keys']['agent']['permissions']
        self.assertEqual([permission for permission in task if '-runtime:' in permission], [])
        self.assertEqual([permission for permission in task if permission.startswith('agent:runtime-task:')],
                         ['agent:runtime-task:runtime-original'])
        self.assertIn('agent:dataset-import:'+self.config['datasetImportScopes'][0], task)
        self.assertEqual(receipt['runtimeInstallation']['state'], 'registration-pending')
        self.assertFalse(receipt['qualification'])

    def test_partial_installation_or_reused_control_secret_refuses_before_issuance(self):
        for change in ('missing-runtime', 'missing-control', 'shared-secret', 'swapped-secret'):
            config = copy.deepcopy(self.config)
            if change == 'missing-runtime': config.pop('installedRuntime')
            elif change == 'missing-control': config['secretArns'].pop('control')
            elif change == 'shared-secret': config['secretArns']['control'] = config['secretArns']['agent']
            else:
                config['secretArns']['control'], config['secretArns']['observer'] = config['secretArns']['observer'], config['secretArns']['control']
            with self.subTest(change=change), self.assertRaises(PackError):
                bootstrap.validate(config)
        self.fixture.secrets.put_secret_value.assert_not_called()

    def test_ambiguous_runtime_or_implicit_mutation_ceilings_are_refused(self):
        changes = [None, {'runtimeId': 'foreign/runtime', 'mutationCeilings': {}},
                   {'runtimeId': 'runtime-original', 'mutationCeilings': {}}]
        for name, value in [('compose', 1), ('proposeRestore', 'true'), ('datasetImportScopes', ['*']),
                            ('managedProcessIds', []), ('approvePublication', True)]:
            runtime = copy.deepcopy(self.config['installedRuntime'])
            runtime['mutationCeilings'][name] = value
            changes.append(runtime)
        for runtime in changes:
            with self.subTest(runtime=runtime), self.assertRaises(PackError):
                bootstrap.validate(self.config | {'installedRuntime': runtime})

    def test_credential_expiry_cannot_extend_or_replace_original_funding(self):
        expiry = self.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch']
        for offset in (-1, 1):
            config = self.config | {'expiresAt': datetime.fromtimestamp(expiry+offset, timezone.utc).isoformat()}
            with self.subTest(offset=offset), self.assertRaisesRegex(PackError, 'original funding expiry'):
                bootstrap.authorize(config, self.durable)
        self.assertNotIn('scenarioOperations', self.durable.state)

    def test_forged_or_missing_retained_funding_refuses_before_native_effects(self):
        original = copy.deepcopy(self.durable.state)
        for change in ('missing-proof', 'changed-source', 'missing-accounting'):
            self.durable.state = copy.deepcopy(original)
            if change == 'missing-proof': self.durable.state.pop('interactiveFunding')
            elif change == 'changed-source': self.durable.state['interactiveFunding']['configuration'] = '{}'
            else:
                self.durable.state.pop('interactiveFunding')
                self.durable.state['ledger'].pop('interactiveAccounting')
            with self.subTest(change=change), self.assertRaises((PackError, ValueError)):
                self.fixture.provision()
            self.assertNotIn('scenarioOperations', self.durable.state)
        self.fixture.secrets.put_secret_value.assert_not_called()

    def test_foreign_effective_key_identity_cannot_complete_installed_issuance(self):
        original = self.fixture.http
        def foreign(*args, **kwargs):
            answer = original(*args, **kwargs)
            if args[2] == 'GET' and args[3].endswith('/effective-permissions'):
                answer['id'] = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
            return answer
        self.fixture.http = foreign
        with self.assertRaisesRegex(PackError, 'another key'):
            self.fixture.provision()
        self.assertFalse(any(key.endswith('/completed') for key in self.durable.state['scenarioOperations']))
        with self.assertRaisesRegex(PackError, 'already started'):
            self.fixture.provision()

    def test_lost_control_key_response_preserves_intent_and_never_issues_a_replacement(self):
        original = self.fixture.http
        def lost(config, root_key, method, path, body=None):
            if method == 'POST' and body['name'].endswith('-control'):
                raise TimeoutError('lost control key acknowledgement')
            return original(config, root_key, method, path, body)
        self.fixture.http = MagicMock(side_effect=lost)
        with self.assertRaises(TimeoutError): self.fixture.provision()
        retained = self.durable.state['scenarioOperations']
        self.assertTrue(any(key.endswith('/key-control-intent') for key in retained))
        self.assertFalse(any(key.endswith('/key-control') for key in retained))
        self.assertFalse(any(key.endswith('/completed') for key in retained))
        calls = self.fixture.http.call_count
        with self.assertRaisesRegex(PackError, 'already started'): self.fixture.provision()
        self.assertEqual(self.fixture.http.call_count, calls)
        self.assertEqual(self.fixture.secrets.put_secret_value.call_count, 3)
