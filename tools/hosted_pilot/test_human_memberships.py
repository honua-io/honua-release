"""Local HTTP provisioning/recovery contracts; not actual identity qualification."""
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import socket
from threading import Thread
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse
import urllib.request

import human_memberships as memberships
import native_bootstrap
from agent_e2e_pack import canonical_digest, digest_bytes
from test_native_bootstrap import configuration as bootstrap_configuration


class HumanMembershipContracts(unittest.TestCase):
    def setUp(self):
        self.bootstrap = bootstrap_configuration()
        self.bootstrap.update(schemaVersion=2, targetId='target-'+'a'*32)
        self.bootstrap['datasetImportScopes'] = [self.bootstrap['targetId']+'/honua_data/owned_import']
        self.prefix = 'honua-pilot-unit'
        self.issuer = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ActualPool'
        self.native = {'taskArn': self.bootstrap['clusterArn'].replace(':cluster/', ':task/')+'/'+'1'*32,
            'taskDefinitionArn': self.bootstrap['taskDefinitionFamily']+':1', 'imageUri': self.bootstrap['imageUri'], 'imageSource': {'sourceSha': 'e'*40}}
        self.config = {'schemaVersion': 1, 'credentials': {kind: {'secretArn':
            'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-unit/'+kind+'-ABC123', 'versionId': char*32}
            for kind, char in [('scim', 'f'), ('root', 'e')]}}
        self.source = {'name_prefix': self.prefix, 'server_domain': 'agent.pilot.demo.honua.io',
            'candidate': {'server_image': 'registry/server@sha256:'+'1'*64, 'console_image': 'registry/console@sha256:'+'2'*64},
            'human_enrollment': {'pool_id': 'us-east-1_ActualPool', 'users': {
                'actual_author': {'role': 'authors', 'payload': 'opaque-ciphertext'},
                'actual_approver': {'role': 'approvers', 'payload': 'different-opaque-ciphertext'}}}}
        self.source_path = 'certification/hosted-pilot/deployments/actual-input.tfvars.json'
        self.source_raw = json.dumps(self.source).encode()
        self.review = {'schema': 'honua.hosted-pilot-saved-plan/v1', 'context': {'GITHUB_SHA': 'a'*40,
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REPOSITORY': memberships.REPOSITORY},
            'operation': 'apply', 'root_path': 'certification/hosted-pilot', 'config_path': self.source_path,
            'config_sha256': digest_bytes(self.source_raw)[7:], 'plan_sha256': 'b'*64, 's3_version_id': 'original-plan-version'}
        self.enrollment = {'schema': memberships.SCHEMA, 'accountId': '123456789012', 'candidateKey': self.bootstrap['candidateKey'],
            'producer': {'repository': memberships.REPOSITORY, 'workflow': memberships.WORKFLOW, 'sourceSha': 'a'*40,
                'runId': 123, 'runAttempt': 2, 'jobKey': 'apply', 'ref': 'refs/heads/trunk', 'event': 'workflow_dispatch'},
            'sourceConfiguration': {'path': self.source_path, 'sha256': digest_bytes(self.source_raw)},
            'plan': {'sha256': 'sha256:'+'b'*64, 'versionId': 'original-plan-version', 'attempt': 1,
                'reviewSha256': '', 'reviewArtifactId': 456, 'review': {}},
            'identity': {'region': 'us-east-1', 'poolId': 'us-east-1_ActualPool', 'issuer': self.issuer,
                'authorRole': self.prefix+'-authors', 'approverRole': self.prefix+'-approvers'},
            'deployment': {'clusterArn': self.bootstrap['clusterArn'],
                'serverServiceArn': self.bootstrap['clusterArn'].replace(':cluster/', ':service/')+'/server',
                'serverTaskDefinitionArn': 'arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-unit-server:1',
                'consoleServiceArn': 'original-console-service', 'consoleTaskDefinitionArn': 'original-console-task:1',
                'serverOrigin': self.bootstrap['endpoint'], 'serverImage': self.source['candidate']['server_image'],
                'consoleImage': self.source['candidate']['console_image']},
            'users': {'actual_author': {'subjectId': '11111111-1111-4111-8111-111111111111', 'role': 'authors', 'status': 'FORCE_CHANGE_PASSWORD'},
                'actual_approver': {'subjectId': '22222222-2222-4222-8222-222222222222', 'role': 'approvers', 'status': 'CONFIRMED'}},
            'qualification': False}
        self.objects = {}
        self.seal_objects()
        self.roles, self.users, self.requests = {}, {}, []
        self.lost = self.crash = None
        self.before_commit = False
        self.job_status = 'success'
        self.disclose = False
        self.rollout_on_write = False
        self.same_attempt = False
        self.bucket_key_enabled = False
        self.wrap_target = False
        self.ecs = MagicMock()
        deployment = self.enrollment['deployment']
        self.server_definition = {'taskDefinitionArn': deployment['serverTaskDefinitionArn'], 'containerDefinitions': [{
            'name': 'server', 'image': deployment['serverImage'], 'environment': [
                {'name': 'Scim__OidcIssuer', 'value': self.issuer}, {'name': 'Oidc__Authority', 'value': self.issuer},
                {'name': 'Oidc__AdminRoles__0', 'value': self.prefix+'-approvers'},
                {'name': 'Oidc__AdminRoles__1', 'value': self.prefix+'-approvers'}],
            'secrets': [{'name': 'Scim__BearerToken', 'valueFrom': memberships.immutable_secret(self.config['credentials']['scim'])}]}]}
        self.own_definition = {'taskDefinitionArn': self.native['taskDefinitionArn'], 'containerDefinitions': [{
            'name': 'native-bootstrap', 'image': self.native['imageUri'], 'environment': [], 'secrets': [
                {'name': name, 'valueFrom': memberships.immutable_secret(self.config['credentials'][kind])}
                for name, kind in ((memberships.SCIM_ENV, 'scim'), (memberships.ROOT_ENV, 'root'))]}]}
        self.ecs.describe_task_definition.side_effect = lambda **kw: {'taskDefinition': self.server_definition
            if kw['taskDefinition'] == deployment['serverTaskDefinitionArn'] else self.own_definition}
        self.ecs.describe_services.return_value = {'services': [{'serviceArn': deployment['serverServiceArn'], 'clusterArn': deployment['clusterArn'],
            'taskDefinition': deployment['serverTaskDefinitionArn'], 'status': 'ACTIVE', 'desiredCount': 1, 'runningCount': 1,
            'pendingCount': 0, 'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED', 'taskDefinition': deployment['serverTaskDefinitionArn']}]}]}
        self.ecs.describe_tasks.return_value = {'tasks': [{'taskArn': self.native['taskArn'], 'clusterArn': self.bootstrap['clusterArn'],
            'taskDefinitionArn': self.native['taskDefinitionArn'], 'lastStatus': 'RUNNING', 'enableExecuteCommand': False}]}
        self.durable = MagicMock(); self.durable.store.candidate = self.bootstrap['candidateKey']
        self.durable.state = {'scenarioOperations': {}}
        case = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def answer(self, value, status=200, *, root=True):
                raw = json.dumps({'success': True, 'data': value} if root else value).encode()
                self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(raw)
            def handle_native(self):
                scim = self.path.startswith('/scim/')
                expected = 'Bearer '+('s'*48) if scim else 'r'*48
                if self.headers.get('Authorization' if scim else 'X-API-Key') != expected:
                    self.send_error(401); return
                case.requests.append((self.command, self.path))
                fault = case.lost in (self.command, (self.command, self.path))
                if fault and case.before_commit:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                payload = None if self.command == 'GET' else json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                value, status = {}, 200
                if self.path == '/scim/v2/ServiceProviderConfig':
                    value = {'schemas': ['urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig'], 'filter': {'supported': True},
                        'authenticationSchemes': [{'type': 'oauthbearertoken'}]}
                    if case.disclose: value['unexpected'] = 's'*48
                elif self.path == '/api/v1/agent/target': value = {'targetId': case.bootstrap['targetId']}
                elif self.path == '/api/v1/admin/roles':
                    if self.command == 'POST':
                        role_id = '00000000-0000-4000-8000-'+str(len(case.roles)+1).zfill(12)
                        case.roles[role_id] = dict(payload, roleId=role_id, isBuiltIn=False, permissions=[]); status = 201
                        value = case.roles[role_id]
                    else: value = list(case.roles.values())
                elif '/admin/roles/' in self.path:
                    role = case.roles[self.path.split('/')[5]]
                    if self.command == 'PUT': role['permissions'] = payload['permissions']
                    value = role['permissions']
                elif self.path == '/scim/v2/Users':
                    user_id = 'managed-'+payload['userName']
                    case.users[user_id] = dict(payload, id=user_id, roles=[]); value = case.users[user_id]; status = 201
                elif self.path.startswith('/scim/v2/Users?'):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['filter'][0]
                    alias = query.split('"')[1]
                    selected = [u for u in case.users.values() if u['userName'] == alias]
                    value = {'totalResults': len(selected), 'startIndex': 1, 'resources': selected}
                elif '/admin/users/' in self.path:
                    user = case.users[self.path.split('/')[5]]
                    if self.command == 'PUT': user['roles'] = payload['roles']
                    if self.path.endswith('/effective-permissions'):
                        permissions = [r['permissions'] for r in case.roles.values() if r['name'] in user['roles']]
                        value = {'userId': user['id'], 'roles': user['roles'], 'permissions': sum(permissions, [])}
                    else: value = {'userId': user['id'], 'isActive': True, 'provisioningSource': 'scim', 'roles': user['roles']}
                else: self.send_error(404); return
                if fault:
                    case.lost = None
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                self.answer(value, status, root=not scim and (self.path != '/api/v1/agent/target' or case.wrap_target))
            do_GET = do_POST = do_PUT = handle_native
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def seal_objects(self):
        def put(key, value, version):
            raw = json.dumps(value).encode()
            reference = {'bucket': 'private-originals', 'key': key, 'versionId': version, 'sha256': digest_bytes(raw),
                'kmsArn': 'arn:aws:kms:us-east-1:123456789012:key/'+'1'*36}
            self.objects[key] = (reference, raw)
            return reference
        review_ref = put('hosted-pilot/plans/123/1/review.json', self.review, 'original-review-version')
        self.enrollment['plan'].update(review=review_ref, reviewSha256=review_ref['sha256'])
        self.config['enrollment'] = put('hosted-pilot/plans/123/1/human-identities-'+str(self.enrollment['producer']['runAttempt'])+'.json', self.enrollment, 'original-enrollment-version')

    def github(self, path):
        if '/contents/' in path:
            return {'encoding': 'base64', 'content': base64.b64encode(self.source_raw).decode()}
        attempt = int(path.split('/attempts/')[1].split('/')[0])
        if '/jobs?' in path:
            if self.same_attempt:
                return {'total_count': 2, 'jobs': [{'name': name, 'run_id': 123, 'head_sha': 'a'*40,
                    'status': 'completed', 'conclusion': 'success'} for name in ('plan', 'apply')]}
            return {'total_count': 1, 'jobs': [{'name': 'apply' if attempt == 2 else 'plan', 'run_id': 123,
                'head_sha': 'a'*40, 'status': 'completed', 'conclusion': self.job_status}]}
        return {'id': 123, 'run_attempt': attempt, 'head_sha': 'a'*40, 'head_branch': 'trunk', 'path': memberships.WORKFLOW,
            'event': 'workflow_dispatch', 'repository': {'full_name': memberships.REPOSITORY}, 'status': 'completed', 'conclusion': self.job_status}

    def run_helper(self):
        s3 = MagicMock()
        def get(**kw):
            ref, raw = self.objects[kw['Key']]
            return {'Body': io.BytesIO(raw), 'VersionId': ref['versionId'], 'ServerSideEncryption': 'aws:kms',
                'SSEKMSKeyId': ref['kmsArn'], 'BucketKeyEnabled': self.bucket_key_enabled}
        s3.get_object.side_effect = get
        case = self
        def http_factory(endpoint, *, fence):
            # Only this local test transport substitutes loopback; the production
            # constructor still rejects non-pilot/HTTP origins.
            value = memberships.NativeHttp.__new__(memberships.NativeHttp)
            value.endpoint = 'http://127.0.0.1:'+str(self.server.server_port)
            value.fence = fence; value.credentials = {'scim': 's'*48, 'root': 'r'*48}
            value.opener = urllib.request.build_opener(memberships.NoRedirect)
            original = value.request
            def request(kind, method, path, payload=None):
                result = original(kind, method, path, payload)
                if case.rollout_on_write and method == 'POST':
                    case.ecs.describe_services.return_value['services'][0]['deployments'][0]['rolloutState'] = 'IN_PROGRESS'
                if case.crash == method:
                    case.crash = None
                    raise SystemExit('controller interrupted after real HTTP commit')
                return result
            value.request = request
            return value
        return memberships.provision_human_memberships(configuration=self.config, bootstrap=self.bootstrap,
            durable=self.durable, native=self.native, guard=self.durable._require_live_lease,
            s3=s3, ecs=self.ecs, github=self.github, http_factory=http_factory)

    def use_campaign_source(self):
        application = {'candidate': copy.deepcopy(self.source['candidate']),
            'human_enrollment': copy.deepcopy(self.source['human_enrollment'])}
        application['candidate']['manifest_sha256'] = self.bootstrap['candidateKey'][7:]
        self.source = {'name_prefix': self.prefix+'-agent', 'candidate_digest': self.bootstrap['candidateKey'],
            'server_image': application['candidate']['server_image'], 'target_domain': 'agent.pilot.demo.honua.io',
            'application': application}
        self.source_path = 'certification/hosted-pilot/campaign/deployments/actual-campaign.tfvars.json'
        self.review['root_path'] = 'certification/hosted-pilot/campaign'
        self.reseal_source()

    def reseal_source(self):
        self.source_raw = json.dumps(self.source).encode()
        self.review.update(config_path=self.source_path, config_sha256=digest_bytes(self.source_raw)[7:])
        self.enrollment['sourceConfiguration'] = {'path': self.source_path, 'sha256': digest_bytes(self.source_raw)}
        self.seal_objects()

    def test_exact_actual_http_memberships_and_completed_reobservation_never_rewrite(self):
        result = self.run_helper()
        before = copy.deepcopy(self.durable.state['scenarioOperations'])
        effects = [row for row in self.requests if row[0] != 'GET']
        self.assertEqual(len(self.users), 2); self.assertEqual(len(self.roles), 2)
        self.assertFalse(result['qualification'])
        self.assertEqual(result['users']['actual_author']['userId'], 'managed-actual_author')
        self.assertEqual(result['roles']['approvers']['permissions'], [])
        self.assertEqual(self.run_helper(), result)
        self.assertEqual(effects, [row for row in self.requests if row[0] != 'GET'])
        self.assertTrue(all(self.durable.state['scenarioOperations'][k] == v for k, v in before.items()))
        self.assertNotIn('s'*48, json.dumps(self.durable.state)); self.assertNotIn('r'*48, json.dumps(self.durable.state))

    def test_lost_post_reply_reconciles_native_effect_without_second_post(self):
        self.lost = 'POST'
        self.assertFalse(self.run_helper()['qualification'])
        self.assertEqual(sum(row == ('POST', '/api/v1/admin/roles') for row in self.requests), 2)

    def test_lost_scim_user_reply_uses_observed_user_id_without_recreating_user(self):
        self.lost = ('POST', '/scim/v2/Users')
        result = self.run_helper()
        self.assertEqual(result['users']['actual_author']['userId'], 'managed-actual_author')
        self.assertEqual(sum(row == ('POST', '/scim/v2/Users') for row in self.requests), 2)

    def test_lost_permission_put_reply_reads_the_exact_grants_without_repeating_put(self):
        self.lost = 'PUT'
        result = self.run_helper()
        self.assertEqual(result['roles']['authors']['permissions'], [
            {'service': 'agent', 'operation': 'workspace', 'layer': '*'},
            {'service': 'agent', 'operation': 'dataset-import', 'layer': self.bootstrap['datasetImportScopes'][0]}])
        self.assertEqual(sum(row[0] == 'PUT' for row in self.requests), 3)

    def test_crash_after_native_commit_resumes_only_from_original_intent(self):
        self.crash = 'POST'
        with self.assertRaises(SystemExit): self.run_helper()
        original_intents = copy.deepcopy(self.durable.state['scenarioOperations'])
        self.run_helper()
        self.assertTrue(all(self.durable.state['scenarioOperations'][k] == v for k, v in original_intents.items()))
        self.assertEqual(sum(row == ('POST', '/api/v1/admin/roles') for row in self.requests), 2)

    def test_unknown_mutation_outcome_remains_blocked_and_never_replays(self):
        self.lost = 'POST'; self.before_commit = True
        with self.assertRaisesRegex(ValueError, 'outcome unresolved'): self.run_helper()
        self.lost = None
        with self.assertRaisesRegex(ValueError, 'outcome unresolved'): self.run_helper()
        self.assertEqual(sum(row[0] == 'POST' for row in self.requests), 1)

    def test_foreign_or_missing_source_proofs_refuse_before_native_mutation(self):
        for change in ('job', 'review-version', 'duplicate-subject', 'issuer', 'scim-version', 'author-admin'):
            with self.subTest(change=change):
                original = copy.deepcopy((self.enrollment, self.review, self.server_definition))
                if change == 'job': self.job_status = 'failure'
                if change == 'review-version': self.review['s3_version_id'] = 'different-plan'
                if change == 'duplicate-subject': self.enrollment['users']['actual_approver']['subjectId'] = self.enrollment['users']['actual_author']['subjectId']
                if change == 'issuer': self.server_definition['containerDefinitions'][0]['environment'][0]['value'] = 'https://other-issuer.invalid'
                if change == 'scim-version': self.server_definition['containerDefinitions'][0]['secrets'][0]['valueFrom'] += 'different'
                if change == 'author-admin': self.server_definition['containerDefinitions'][0]['environment'].append({'name': 'Oidc__AdminRoles__2', 'value': self.prefix+'-authors'})
                self.seal_objects()
                with self.assertRaises(ValueError): self.run_helper()
                self.assertFalse(any(row[0] != 'GET' for row in self.requests))
                self.enrollment, self.review, self.server_definition = original
                self.job_status = 'success'; self.durable.state = {'scenarioOperations': {}}
        self.seal_objects()

    def test_preexisting_role_cannot_be_adopted_by_matching_its_name(self):
        self.roles['00000000-0000-4000-8000-000000000001'] = {'name': self.prefix+'-authors', 'description': 'unrelated',
            'roleId': '00000000-0000-4000-8000-000000000001', 'isBuiltIn': False, 'permissions': []}
        with self.assertRaisesRegex(ValueError, 'preexisting or unknown'): self.run_helper()
        self.assertFalse(any(row[0] != 'GET' for row in self.requests))

    def test_private_credential_echo_is_refused_without_retaining_it(self):
        self.disclose = True
        with self.assertRaisesRegex(ValueError, 'credential material'): self.run_helper()
        self.assertNotIn('s'*48, json.dumps(self.durable.state))
        self.assertFalse(any(row[0] != 'GET' for row in self.requests))

    def test_later_authority_escalation_is_detected_without_resetting_or_rewriting(self):
        result = self.run_helper()
        effects = [row for row in self.requests if row[0] != 'GET']
        self.roles[result['roles']['authors']['roleId']]['permissions'].append(
            {'service': '*', 'operation': '*', 'layer': '*'})
        with self.assertRaisesRegex(ValueError, 'outcome unresolved'):
            self.run_helper()
        self.assertEqual(effects, [row for row in self.requests if row[0] != 'GET'])

    def test_later_subject_replacement_cannot_reuse_original_membership_receipt(self):
        self.run_helper()
        effects = [row for row in self.requests if row[0] != 'GET']
        self.users['managed-actual_author']['externalId'] = '33333333-3333-4333-8333-333333333333'
        with self.assertRaisesRegex(ValueError, 'outcome unresolved'):
            self.run_helper()
        self.assertEqual(effects, [row for row in self.requests if row[0] != 'GET'])

    def test_parent_scope_grammar_has_no_separate_human_grant_count_ceiling(self):
        self.bootstrap['targetId'] = 'observed_target-A'
        self.bootstrap['datasetImportScopes'] = ['observed_target-A/_original_schema/output_'+str(i) for i in range(17)]
        result = self.run_helper()
        imports = [grant['layer'] for grant in result['roles']['authors']['permissions'] if grant['operation'] == 'dataset-import']
        self.assertEqual(imports, self.bootstrap['datasetImportScopes'])

    def test_first_apply_and_plan_can_share_one_authenticated_attempt(self):
        self.same_attempt = True
        self.enrollment['producer']['runAttempt'] = 1
        self.seal_objects()
        self.assertFalse(self.run_helper()['qualification'])

    def test_server_rollout_after_actual_write_fences_all_further_authority_effects(self):
        self.rollout_on_write = True
        with self.assertRaisesRegex(ValueError, 'stable deployed revision'):
            self.run_helper()
        self.assertEqual([row for row in self.requests if row[0] != 'GET'], [('POST', '/api/v1/admin/roles')])
        self.assertFalse(self.users)
        self.assertTrue(any(key.endswith('/role-authors-intent') for key in self.durable.state['scenarioOperations']))
        self.assertFalse(any(key.endswith('/completed') for key in self.durable.state['scenarioOperations']))

    def test_unified_campaign_carrier_provisions_nested_application_against_actual_http(self):
        self.use_campaign_source()
        result = self.run_helper()
        self.assertEqual(result['roles']['authors']['name'], self.prefix+'-authors')
        self.assertEqual(result['roles']['approvers']['name'], self.prefix+'-approvers')
        self.assertEqual(len(self.users), 2)
        self.assertFalse(result['qualification'])

    def test_campaign_does_not_fall_back_to_flat_standalone_enrollment(self):
        self.use_campaign_source()
        self.source['human_enrollment'] = self.source['application'].pop('human_enrollment')
        self.reseal_source()
        with self.assertRaisesRegex(ValueError, 'inputs are missing'):
            self.run_helper()
        self.assertFalse(self.requests)

    def test_campaign_candidate_image_and_domain_must_all_join_the_exact_parent(self):
        for field in ('candidate_digest', 'server_image', 'target_domain'):
            with self.subTest(field=field):
                self.use_campaign_source() if 'application' not in self.source else None
                original = copy.deepcopy(self.source)
                self.source[field] = {'candidate_digest': 'sha256:'+'f'*64,
                    'server_image': 'registry/server@sha256:'+'f'*64, 'target_domain': 'other.pilot.demo.honua.io'}[field]
                self.reseal_source()
                with self.assertRaises(ValueError): self.run_helper()
                self.assertFalse(self.requests)
                self.source = original
        self.reseal_source()

    def test_bucket_key_encryption_context_cannot_replace_exact_object_custody(self):
        self.bucket_key_enabled = True
        with self.assertRaisesRegex(ValueError, 'bucket-key context'):
            self.run_helper()
        self.assertFalse(self.requests)

    def test_target_does_not_accept_an_admin_envelope_as_a_native_target_identity(self):
        self.wrap_target = True
        with self.assertRaisesRegex(ValueError, 'actual native deployment'):
            self.run_helper()
        self.assertFalse(any(method != 'GET' for method, _ in self.requests))
