"""Real HTTP adapter/durable-intent controls; no installed runtime qualification."""
import copy
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_setup_http import SetupHttp
import native_runtime_registration as registration


class Response:
    def __init__(self, status, value):
        self.code, self.raw = status, json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read1(self, limit):
        result, self.raw = self.raw[:limit], self.raw[limit:]
        return result


class Durable:
    def __init__(self, candidate):
        self.store = SimpleNamespace(candidate=candidate)
        self.state = {'scenarioOperations': {}}
        self.saved = {}
        self.fail_intent_persist = False
    def _require_live_lease(self): pass
    def _persist(self):
        rows = self.state['scenarioOperations']
        for key, value in self.saved.items():
            if rows.get(key) != value: raise AssertionError('original evidence changed')
        if self.fail_intent_persist and any(key.endswith('/http/intent/register') for key in rows):
            raise RuntimeError('lost original mutation-intent persist acknowledgement')
        self.saved = copy.deepcopy(rows)


class NativeRegistrationProtocol(unittest.TestCase):
    def setUp(self):
        self.candidate = 'sha256:'+'a'*64
        self.durable = Durable(self.candidate)
        self.request = {'schemaVersion': 1, 'runtimeId': 'runtime-original', 'candidateKey': self.candidate,
            'targetId': 'native-original', 'configurationDigest': 'sha256:'+'b'*64,
            'originalFundingExpiresEpoch': int(datetime.now(timezone.utc).timestamp())+3600,
            'controlApiKeyId': '11111111-1111-1111-1111-111111111111',
            'taskApiKeyId': '22222222-2222-2222-2222-222222222222',
            'mutationCeilings': {'createMap': True, 'createDashboard': False, 'compose': True,
                'proposePublication': False, 'proposeRestore': False, 'datasetImportScopes': [], 'managedProcessIds': []},
            'slots': [{'slotId': 'slot-original', 'taskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/original:1',
                'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-unit-agent-cluster',
                'taskRoleArn': 'arn:aws:iam::123456789012:role/original',
                'image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/original@sha256:'+'d'*64,
                'mcpSecretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:original-ABC123',
                'mcpSecretVersionId': 'e'*64}]}
        self.keys, self.references = {}, {}
        for kind in ('control', 'task'):
            permissions = ['admin:read', 'agent:runtime-control:runtime-original'] if kind == 'control' else [
                'admin:read', 'agent:workspace', 'agent:runtime-task:runtime-original',
                'agent:studio-create-map-runtime:runtime-original', 'agent:studio-compose-runtime:runtime-original']
            self.keys[kind] = {'apiKeyId': self.request[kind+'ApiKeyId'],
                'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:original-'+kind+'-ABC123',
                'versionId': ('c' if kind == 'control' else 'd')*64, 'permissions': permissions,
                'effectivePermissions': {'id': self.request[kind+'ApiKeyId'], 'permissions': permissions, 'canAuthenticate': True}}
            row = {'producer': 'private-native-bootstrap', 'candidateKey': self.candidate, 'value': copy.deepcopy(self.keys[kind])}
            key = 'original-bootstrap/key-'+('agent' if kind == 'task' else kind)
            self.durable.state['scenarioOperations'][key] = row
            self.references[kind] = {'key': key, 'sha256': canonical_digest(row)}
        self.principals = {kind: {'actorId': 'native-opaque-'+kind, 'studioOwnerId': 'observed-owner-'+kind,
            'tenantId': 'original-tenant', 'authenticationKind': 'api-key', 'isHumanSession': False}
            for kind in ('setup', 'control', 'task')}
        self.secrets = {kind: 'private-'+kind+'-credential-'+'x'*48 for kind in ('setup', 'control', 'task')}
        self.env = patch.dict(os.environ, {registration.CREDENTIAL_ENVS[kind]: value for kind, value in self.secrets.items()})
        self.env.start(); self.addCleanup(self.env.stop)
        self.calls, self.original = [], None
        self.lose_post = self.expire_after_post = self.expired = False
        self.bad_effective = self.bad_registration = None
        self.hide_original = False
        self.provenance = {'source': {'path': 'certification/agent-e2e/runtime.json', 'sha256': 'sha256:'+'f'*64},
                           'verifiedWorkerReceipts': [{'sha256': 'sha256:'+'9'*64}]}

    def guard(self):
        self.durable._require_live_lease()
        if self.expired: raise PackError('original funding expired')

    def native_registration(self):
        value = {name: copy.deepcopy(value) for name, value in self.request.items()
                 if name not in ('controlApiKeyId', 'taskApiKeyId')}
        value.update(tenantId='original-tenant', registeredAt=datetime.now(timezone.utc).isoformat(),
                     registrationDigest='sha256:'+'8'*64)
        for kind in ('control', 'task'):
            value[kind+'Principal'] = {'apiKeyId': self.keys[kind]['apiKeyId'],
                'actorId': self.principals[kind]['actorId'], 'studioOwnerId': self.principals[kind]['studioOwnerId'],
                'credentialVersion': 'opaque-native-key-store-version-'+kind,
                'permissionsDigest': registration.permission_digest(self.keys[kind]['permissions'])}
        if self.bad_registration: self.bad_registration(value)
        return value

    def open(self, request, timeout):
        credential = request.get_header('X-api-key')
        kind = next(kind for kind, value in self.secrets.items() if credential == value)
        path = request.full_url.removeprefix('https://agent.pilot.demo.honua.io')
        self.calls.append((kind, request.method, path))
        if path == '/api/v1/agent/target':
            return Response(200, {'targetId': self.request['targetId'], 'principal': self.principals[kind]})
        if path.endswith('/effective-permissions'):
            value = {'id': self.keys[kind]['apiKeyId'], 'status': 'active', 'canAuthenticate': True,
                     'permissions': self.keys[kind]['permissions']}
            if self.bad_effective: self.bad_effective(kind, value)
            return Response(200, {'success': True, 'data': value})
        self.assertEqual(path, '/api/v1/agent/runtimes/runtime-original/registration')
        if request.method == 'POST':
            self.assertEqual(kind, 'setup')
            self.assertEqual(json.loads(request.data), self.request)
            self.assertIsNone(self.original, 'protocol repeated a registration mutation')
            self.assertTrue(any(key.endswith('/http/intent/register') for key in self.durable.saved),
                            'mutation ran before durable original intent')
            self.original = self.native_registration()
            if self.expire_after_post: self.expired = True
            if self.lose_post:
                self.lose_post = False
                raise ConnectionError('lost mutation acknowledgement')
            return Response(201, self.original)
        self.assertEqual((kind, request.method), ('control', 'GET'))
        return Response(404, {'error': 'missing'}) if self.hide_original or self.original is None else Response(200, self.original)

    def factory(self, *args, **kwargs):
        client = SetupHttp(*args, **kwargs)
        client.opener = self
        return client

    def provision(self):
        return registration.provision(self.durable, endpoint='https://agent.pilot.demo.honua.io', request=self.request,
            key_references=self.references, provenance=self.provenance, guard=self.guard, http_factory=self.factory)

    def posts(self): return [call for call in self.calls if call[1] == 'POST']
    def completed(self): return [key for key in self.durable.state['scenarioOperations'] if key.endswith('/completed')]

    def test_original_native_registration_joins_actual_opaque_principals_and_keeps_credentials_private(self):
        result = self.provision()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result['registration'], self.original)
        self.assertFalse(result['activation']); self.assertFalse(result['qualification'])
        self.assertEqual(result['registration']['taskPrincipal']['studioOwnerId'], 'observed-owner-task')
        rows = self.durable.state['scenarioOperations']
        for reference in result['nativeOriginals'].values():
            self.assertEqual(canonical_digest(rows[reference['key']]), reference['sha256'])
        for secret in self.secrets.values(): self.assertNotIn(secret, json.dumps(rows))

    def test_lost_post_acknowledgement_recovers_original_with_control_get_without_repeating_post(self):
        self.lose_post = True
        with self.assertRaisesRegex(PackError, 'reconcile without replay'): self.provision()
        self.assertEqual(len(self.posts()), 1); self.assertFalse(self.completed())
        original = copy.deepcopy(self.original)
        result = self.provision()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result['registration'], original)

    def test_missing_original_after_lost_acknowledgement_remains_unresolved_without_reissue(self):
        self.lose_post = True
        with self.assertRaises(PackError): self.provision()
        self.hide_original = True
        with self.assertRaisesRegex(PackError, 'without replay'): self.provision()
        self.assertEqual(len(self.posts()), 1); self.assertFalse(self.completed())

    def test_mutation_cannot_run_without_durable_intent_acknowledgement(self):
        self.durable.fail_intent_persist = True
        with self.assertRaisesRegex(RuntimeError, 'intent persist'): self.provision()
        self.assertFalse(self.posts()); self.assertFalse(self.completed())
        self.durable.fail_intent_persist = False
        with self.assertRaisesRegex(PackError, 'without replay'): self.provision()
        self.assertFalse(self.posts())

    def test_expiry_during_post_keeps_original_response_but_cannot_complete_or_continue(self):
        self.expire_after_post = True
        with self.assertRaisesRegex(PackError, 'funding expired'): self.provision()
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(any(key.endswith('/http/response/register') for key in self.durable.saved))
        self.assertFalse(self.completed())
        before = len(self.calls)
        with self.assertRaisesRegex(PackError, 'funding expired'): self.provision()
        self.assertEqual(len(self.calls), before)

    def test_changed_original_key_refuses_before_native_http(self):
        self.durable.state['scenarioOperations'][self.references['task']['key']]['value']['permissions'].append('admin:write')
        with self.assertRaisesRegex(PackError, 'custody'): self.provision()
        self.assertFalse(self.calls)

    def test_changed_declaration_after_lost_acknowledgement_cannot_replace_original(self):
        self.lose_post = True
        with self.assertRaises(PackError): self.provision()
        before = len(self.calls)
        self.request['configurationDigest'] = 'sha256:'+'7'*64
        with self.assertRaisesRegex(PackError, 'different immutable'): self.provision()
        self.assertEqual(len(self.calls), before); self.assertEqual(len(self.posts()), 1)

    def test_wrong_effective_key_or_inactive_credential_refuses_before_registration(self):
        for update in ({'id': 'foreign'}, {'status': 'revoked'}, {'canAuthenticate': False}, {'permissions': ['admin:write']}):
            with self.subTest(update=update):
                self.bad_effective = lambda kind, value: value.update(update) if kind == 'task' else None
                with self.assertRaises(PackError): self.provision()
        self.assertFalse(self.posts()); self.assertFalse(self.completed())

    def test_native_registration_actor_mismatch_cannot_become_completed_evidence(self):
        self.bad_registration = lambda value: value['taskPrincipal'].update(actorId='different-native-actor')
        with self.assertRaisesRegex(PackError, 'current principal'): self.provision()
        self.assertEqual(len(self.posts()), 1); self.assertFalse(self.completed())
        with self.assertRaisesRegex(PackError, 'current principal'): self.provision()
        self.assertEqual(len(self.posts()), 1)

    def test_native_registration_cannot_substitute_another_cluster(self):
        self.bad_registration = lambda value: value['slots'][0].update(
            clusterArn='arn:aws:ecs:us-east-1:123456789012:cluster/different')
        with self.assertRaisesRegex(PackError, 'reviewed original installation'): self.provision()
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(self.completed())

    def test_native_response_disclosing_any_private_key_is_rejected(self):
        self.bad_registration = lambda value: value.update(registrationDigest=self.secrets['task'])
        with self.assertRaisesRegex(PackError, 'disclosed credential'): self.provision()
        self.assertFalse(self.completed())

    def test_completed_recovery_appends_fresh_reads_and_preserves_first_original_receipt(self):
        result = self.provision()
        snapshot = copy.deepcopy(self.durable.saved)
        self.assertEqual(self.provision(), result)
        self.assertEqual(len(self.posts()), 1)
        self.assertGreater(len(self.durable.saved), len(snapshot))
        self.assertTrue(all(self.durable.saved[key] == value for key, value in snapshot.items()))

    def test_separate_setup_tenant_is_rejected_before_mutation(self):
        self.principals['setup']['tenantId'] = 'foreign-tenant'
        with self.assertRaisesRegex(PackError, 'original tenant'): self.provision()
        self.assertFalse(self.posts())

    def test_permission_digest_matches_native_ascii_sorting_without_trailing_newline(self):
        import hashlib
        self.assertEqual(registration.permission_digest(['z', 'a']), 'sha256:'+hashlib.sha256(b'a\nz').hexdigest())
        for permissions in ([], ['a', 'a'], ['a\nb'], ['space label'], ['nonascii-\u00e9']):
            with self.subTest(permissions=permissions), self.assertRaises(PackError):
                registration.permission_digest(permissions)

    def test_reviewed_parent_uses_loader_request_original_keys_and_worker_provenance(self):
        reference = {'path': 'certification/agent-e2e/runtime.json', 'sha256': 'sha256:'+'f'*64}
        verified = {'schema': 'honua.verified-installed-console-declaration/v1',
            **{name: self.request[name] for name in ('candidateKey', 'runtimeId', 'targetId',
                'configurationDigest', 'originalFundingExpiresEpoch', 'mutationCeilings')},
            'endpoint': 'https://agent.pilot.demo.honua.io', 'registrationRequest': self.request,
            'keys': {kind: {'reference': self.references[kind], 'issued': self.keys[kind]} for kind in self.keys},
            'nativeBootstrap': {'key': 'original-bootstrap/completed', 'sha256': 'sha256:'+'6'*64},
            'fundingBinding': {'originalExpiresEpoch': self.request['originalFundingExpiresEpoch']},
            'operation': {'operationId': 'original-reserved-operation'},
            'workers': [{'proof': {'source': 'actual-checked-workflow'}}], 'evidence': {'sourceSha': '5'*40}}
        def load(selected, **kwargs):
            self.assertEqual(selected, reference)
            self.assertIs(kwargs['durable'], self.durable)
            kwargs['guard']()
            return copy.deepcopy(verified)
        result = registration.provision_reviewed(self.durable, reference, s3=object(), ecs=object(), states=object(),
            guard=self.guard, loader=load, http_factory=self.factory)
        self.assertEqual(result['declarationDigest'], self.request['configurationDigest'])
        self.assertEqual(result['provenance']['workerProofs'], [{'source': 'actual-checked-workflow'}])
        self.assertFalse(result['activation']); self.assertFalse(result['qualification'])


if __name__ == '__main__': unittest.main()
