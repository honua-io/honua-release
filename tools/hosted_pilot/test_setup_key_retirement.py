"""Original setup authority is revoked once, independently read, then denied."""
import copy
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import unittest
from unittest.mock import MagicMock, patch
import setup_key_retirement as retirement
import setup_key_retirement_dispatch as dispatcher
from controller_archive import canonical, digest
from agent_e2e_pack import canonical_digest


class SetupRetirement(unittest.TestCase):
    def setUp(self):
        candidate, generation = 'sha256:'+'a'*64, 'sha256:'+'b'*64
        self.root, self.setup = 'private-root-'+'r'*64, 'private-setup-'+'s'*64
        operation = {'operationId': 'original-bootstrap', 'liabilityBasisDigest': 'sha256:'+'d'*64,
                     'reservationIds': {'infrastructure': 'original-infra', 'cleanup': 'original-cleanup'}}
        key_id = '11111111-1111-1111-1111-111111111111'
        agent_id, item_id = '22222222-2222-2222-2222-222222222222', '33333333-3333-3333-3333-333333333333'
        setup = {'apiKeyId': key_id, 'secretArn': 'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-unit-agent/native-agent/setup-ABC123',
                 'versionId': 'e'*64, 'permissions': ['admin:write']}
        issued = {'setup': setup, 'agent': {'apiKeyId': agent_id, 'permissions': ['agent:workspace', 'agent:studio-compose:'+item_id]}}
        original_http = {'original-fixture-response': {'httpStatus': 200, 'candidateKey': candidate, 'body': {'itemId': item_id}}}
        fixture = {'schema': 'honua.agent-e2e.studio-bootstrap/v1', 'candidateKey': candidate,
                   'fixture': {'ownerPrincipalId': agent_id, 'itemId': item_id}, 'nativeOriginals': original_http}
        self.config = {'schemaVersion': 1, 'candidateKey': candidate, 'generationDigest': generation,
            'bootstrapArtifactDigest': 'sha256:'+'f'*64, 'state': {'region': 'us-east-1', 'bucket': 'original-store', 'table': 'original-head'},
            'operation': operation, 'region': 'us-east-1', 'accountId': '585192672263', 'endpoint': 'https://agent.pilot.demo.honua.io',
            'nativeBootstrapOperationId': 'original-bootstrap', 'setupKey': {key: setup[key] for key in ('apiKeyId', 'secretArn', 'versionId')},
            'fixtureReference': {'key': 'sha256:'+'c'*64, 'sha256': canonical_digest(fixture)},
            'preparationOutcome': {'bucket': 'original-private-custody', 'key': 'controller/outcomes/'+'a'*64+'/original.json', 'versionId': 'original-version', 'sha256': 'sha256:'+'9'*64},
            'bucket': 'original-private-custody', 'receiptKey': 'native/setup-key-retirement/'+'a'*64+'/'+key_id+'/retire.json',
            'clusterArn': 'arn:aws:ecs:us-east-1:585192672263:cluster/honua-pilot-unit-agent',
            'taskDefinitionFamily': 'arn:aws:ecs:us-east-1:585192672263:task-definition/honua-pilot-unit-agent-setup-key-retirement',
            'imageUri': '585192672263.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-trusted-controller@sha256:'+'8'*64,
            'rootSecretArn': 'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-unit-agent-admin-password-ABC123',
            'taskRoleArn': 'arn:aws:iam::585192672263:role/honua-pilot-unit-agent-setup-retire',
            'executionRoleArn': 'arn:aws:iam::585192672263:role/honua-pilot-unit-agent-retire-execution',
            'mode': 'retire', 'originalClaim': None, 'claimTable': 'honua-pilot-unit-agent-controller-dispatch-claims'}
        prefix = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': candidate, 'operationId': 'original-bootstrap'})
        originals = {**original_http, **{prefix+'/key-'+kind: {'producer': 'private-native-bootstrap', 'candidateKey': candidate, 'value': record}
                                       for kind, record in issued.items()}}
        self.durable = MagicMock(); self.durable.store.generation = {'digest': generation}
        self.durable.state = {'bootstrapImport': {'artifactDigest': self.config['bootstrapArtifactDigest']},
            'ledger': {'schemaVersion': 2, 'candidateKey': candidate, 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [],
                'operations': [{'id': reservation, 'operationId': operation['operationId'], 'purpose': 'bootstrap', 'kind': kind,
                    'amountUsd': '5', 'basisDigest': operation['liabilityBasisDigest'], 'state': 'reserved', 'actualUsd': None}
                    for kind, reservation in operation['reservationIds'].items()]}, 'scenarioOperations': originals,
            'studioBootstrapFixtures': {self.config['fixtureReference']['key']: fixture}}
        self.native = {'taskArn': 'arn:aws:ecs:us-east-1:585192672263:task/honua-pilot-unit-agent/'+'8'*32,
                       'taskDefinitionArn': self.config['taskDefinitionFamily']+':3', 'imageUri': self.config['imageUri']}
        self.preparation = {'taskArn': 'original-fixture-task', 'lastStatus': 'STOPPED'}
        self.status = 'active'; self.calls = []; self.lost_ack = False; self.denied_status = 401
    def http(self, config, credential, method, path):
        self.calls.append((credential, method, path))
        self.assertTrue(path.startswith('/api/v1/admin/api-keys/'+self.config['setupKey']['apiKeyId']+'/'))
        if method == 'POST':
            self.assertEqual(credential, self.root); self.assertTrue(path.endswith('/revoke'))
            self.status = 'revoked'
            if self.lost_ack: raise TimeoutError('acknowledgement lost')
        elif credential == self.setup and self.status == 'revoked':
            return {'httpStatus': self.denied_status, 'body': {'success': False, 'error': 'Authentication required'}}
        return {'httpStatus': 200, 'body': {'success': True, 'data': {'id': self.config['setupKey']['apiKeyId'],
            'status': self.status, 'permissions': ['admin:write'], 'canAuthenticate': self.status == 'active'}}}
    def retire(self, original=None):
        return retirement.retire(self.config, self.durable, root_credential=self.root, setup_credential=self.setup,
                                  preparation=self.preparation, native=self.native, http=self.http, original=original)

    def definition(self, config=None):
        config = config or self.config
        return {'taskDefinitionArn': self.native['taskDefinitionArn'], 'taskRoleArn': config['taskRoleArn'],
            'executionRoleArn': config['executionRoleArn'], 'networkMode': 'awsvpc',
            'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
            'containerDefinitions': [{'name': 'setup-key-retirement', 'image': config['imageUri'], 'command': dispatcher.COMMAND,
                'user': '10001:10001', 'readonlyRootFilesystem': True,
                'mountPoints': [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}],
                'environment': [{'name': 'PILOT_SETUP_RETIREMENT_CONFIG', 'value': canonical(config).decode()}],
                'secrets': [{'name': 'PILOT_SETUP_RETIRE_ROOT_KEY', 'valueFrom': config['rootSecretArn']},
                    {'name': 'PILOT_SETUP_RETIRE_KEY', 'valueFrom': config['setupKey']['secretArn']+':::'+config['setupKey']['versionId']}]}]}

    def recovery(self, *, exit_code=1):
        original_config = copy.deepcopy(self.config)
        task = {'taskArn': self.native['taskArn'], 'taskDefinitionArn': self.native['taskDefinitionArn'], 'lastStatus': 'STOPPED',
                'containers': [{'image': self.config['imageUri'], 'exitCode': exit_code}], 'enableExecuteCommand': False}
        terminal = {'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn'],
                    'lastStatus': 'STOPPED', 'exitCode': exit_code, 'imageUri': self.config['imageUri']}
        claim = {'table': self.config['claimTable'], 'key': 'dispatch:'+'4'*64, 'bindingDigest': 'sha256:'+'5'*64,
                 'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn']}
        row = {'bindingDigest': {'S': claim['bindingDigest']}, 'taskArn': {'S': task['taskArn']},
               'terminalDigest': {'S': digest(canonical(terminal))}, 'terminalObservation': {'S': canonical(terminal).decode()}}
        self.config.update(mode='observe', originalClaim=claim)
        self.config['receiptKey'] = self.config['receiptKey'].replace('/retire.json', '/observe-'+'4'*64+'.json')
        self.native = self.native | {'taskArn': task['taskArn'][:-32]+'9'*32}
        ecs, db = MagicMock(), MagicMock()
        ecs.describe_task_definition.return_value = {'taskDefinition': self.definition(original_config)}
        ecs.describe_tasks.return_value = {'tasks': [task]}
        db.get_item.return_value = {'Item': row}
        return ecs, db
    def test_original_identity_positive_revoke_independent_read_and_old_key_401(self):
        prior = copy.deepcopy(self.durable.state['ledger'])
        receipt = self.retire()
        self.assertEqual([method for _,method,_ in self.calls], ['GET', 'POST', 'GET', 'GET'])
        self.assertEqual(receipt['revokedKeyId'], self.config['setupKey']['apiKeyId'])
        self.assertEqual(receipt['postRevocationProbeStatus'], 401)
        self.assertEqual(self.durable.state['ledger'], prior)
        self.assertNotIn(self.root, json.dumps(self.durable.state)); self.assertNotIn(self.setup, json.dumps(self.durable.state))
        self.assertFalse(receipt['qualification'])
        with self.assertRaisesRegex(ValueError, 'already started'): self.retire()
        self.assertEqual(len(self.calls), 4)
    def test_lost_revoke_ack_is_reconciled_by_actual_reads_without_second_post(self):
        self.lost_ack = True
        receipt = self.retire()
        self.assertEqual(sum(method == 'POST' for _,method,_ in self.calls), 1)
        self.assertEqual(receipt['postRevocationProbeStatus'], 401)
        self.assertTrue(any(key.endswith('/revoke-acknowledgement') for key in self.durable.state['scenarioOperations']))
    def test_scope_403_never_counts_as_revocation_proof(self):
        self.denied_status = 403
        with self.assertRaisesRegex(ValueError, '403 is not revocation proof'): self.retire()
        self.assertFalse(any(key.endswith('/completed') for key in self.durable.state['scenarioOperations']))
    def test_changed_original_version_or_fixture_cannot_revoke_any_key(self):
        original = copy.deepcopy(self.config)
        for edit in ('version', 'fixture'):
            self.config = copy.deepcopy(original)
            if edit == 'version': self.config['setupKey']['versionId'] = '9'*64
            else: self.config['fixtureReference']['sha256'] = 'sha256:'+'7'*64
            with self.subTest(edit=edit), self.assertRaisesRegex(ValueError, 'original separate setup authority|actual native saved fixture'):
                self.retire()
        self.assertEqual(self.calls, [])
    def test_settled_cleanup_and_unstopped_original_task_refuse_before_http(self):
        self.durable.state['ledger']['operations'][1].update(state='settled', actualUsd='1')
        with self.assertRaisesRegex(ValueError, 'original liability'): self.retire()
        self.durable.state['ledger']['operations'][1].update(state='reserved', actualUsd=None)
        self.preparation['lastStatus'] = 'RUNNING'
        with self.assertRaisesRegex(ValueError, 'fresh private task'): self.retire()
        self.assertEqual(self.calls, [])

    def test_recovery_after_revocation_and_after_completion_only_observes(self):
        for cut in ('independent-read', 'old-key-probe', 'completed-receipt-ack'):
            with self.subTest(cut=cut):
                self.setUp()
                if cut != 'completed-receipt-ack':
                    original_http = self.http
                    def interrupted(config, credential, method, path):
                        if method == 'GET' and (credential == self.root if cut == 'independent-read' else credential == self.setup and self.status == 'revoked'):
                            raise TimeoutError('read acknowledgement lost')
                        return original_http(config, credential, method, path)
                    self.http = interrupted
                    with self.assertRaises(TimeoutError): self.retire()
                    self.http = original_http
                else:
                    self.retire()  # S3 put acknowledgement is outside retire(); all evidence remains.
                originals = copy.deepcopy(self.durable.state['scenarioOperations'])
                ledger = copy.deepcopy(self.durable.state['ledger'])
                ecs, db = self.recovery()
                proof = retirement.observe_original_retirement(self.config, self.durable, ecs=ecs, db=db)
                calls = len(self.calls)
                receipt = self.retire(proof)
                self.assertEqual([method for _,method,_ in self.calls[calls:]], ['GET', 'GET'])
                self.assertEqual(sum(method == 'POST' for _,method,_ in self.calls), 1)
                self.assertEqual(receipt['originalRetirement']['exitCode'], 1)
                self.assertEqual(receipt['postRevocationProbeStatus'], 401)
                for key, value in originals.items(): self.assertEqual(self.durable.state['scenarioOperations'][key], value)
                self.assertEqual(self.durable.state['ledger'], ledger)

    def test_recovery_requires_original_positive_control_and_intent(self):
        self.retire()
        ecs, db = self.recovery()
        proof = retirement.observe_original_retirement(self.config, self.durable, ecs=ecs, db=db)
        records = self.durable.state['scenarioOperations']
        key = next(key for key in records if key.endswith('/revoke-intent'))
        del records[key]
        calls = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'positive control or revoke intent'): self.retire(proof)
        self.assertEqual(len(self.calls), calls)

    def test_original_terminal_claim_task_and_configuration_are_independent_controls(self):
        for mismatch in ('claim', 'terminal-digest', 'live-task', 'credential-version'):
            with self.subTest(mismatch=mismatch):
                self.setUp(); self.retire(); ecs, db = self.recovery()
                if mismatch == 'claim': db.get_item.return_value['Item']['bindingDigest']['S'] = 'sha256:'+'0'*64
                elif mismatch == 'terminal-digest': db.get_item.return_value['Item']['terminalDigest']['S'] = 'sha256:'+'0'*64
                elif mismatch == 'live-task': ecs.describe_tasks.return_value['tasks'][0]['lastStatus'] = 'RUNNING'
                else: self.config['setupKey']['versionId'] = '0'*64
                with self.assertRaises(ValueError): retirement.observe_original_retirement(self.config, self.durable, ecs=ecs, db=db)

    def test_expired_ecs_metadata_needs_retained_actual_terminal_proof(self):
        self.retire(); ecs, db = self.recovery()
        ecs.describe_tasks.return_value = {'tasks': [], 'failures': [{'reason': 'MISSING'}]}
        self.assertEqual(retirement.observe_original_retirement(self.config, self.durable, ecs=ecs, db=db)['lastStatus'], 'STOPPED')
        del db.get_item.return_value['Item']['terminalObservation']
        with self.assertRaisesRegex(ValueError, 'terminal task'):
            retirement.observe_original_retirement(self.config, self.durable, ecs=ecs, db=db)

    def test_native_http_accepts_actual_empty_and_non_json_401_without_disclosing_body(self):
        with self.assertRaisesRegex(ValueError, 'may only observe or revoke'):
            retirement.native_http(self.config | {'mode': 'observe'}, self.root, 'POST',
                '/api/v1/admin/api-keys/'+self.config['setupKey']['apiKeyId']+'/revoke')
        for body in (b'', b'Unauthorized', b'{"error":"unauthorized"}'):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(handler):
                    handler.send_response(401); handler.end_headers(); handler.wfile.write(body)
                def log_message(*args): pass
            with HTTPServer(('127.0.0.1', 0), Handler) as server:
                thread = threading.Thread(target=server.handle_request); thread.start()
                config = self.config | {'endpoint': 'http://127.0.0.1:'+str(server.server_port)}
                result = retirement.native_http(config, self.setup, 'GET', '/api/v1/admin/api-keys/'+config['setupKey']['apiKeyId']+'/effective-permissions')
                thread.join(5)
            self.assertEqual(result['httpStatus'], 401)
            self.assertEqual(result['bodyBytes'], len(body))
            self.assertEqual(result['bodySha256'], digest(body))
            self.assertEqual(result['bodyFormat'], 'empty' if not body else 'json' if body.startswith(b'{') else 'non-json')

    def handoff(self):
        return {'schemaVersion': 1, **{key: self.config[key] for key in ('region', 'accountId', 'clusterArn', 'taskRoleArn', 'executionRoleArn', 'imageUri')},
            'taskDefinitionArn': self.native['taskDefinitionArn'], 'subnetIds': ['private-subnet'], 'securityGroupIds': ['controller-sg'],
            'configuration': self.config, 'configurationSha256': digest(canonical(self.config)),
            'dispatchBudget': {'candidateKey': self.config['candidateKey'], 'state': self.config['state'],
                'generationDigest': self.config['generationDigest'], 'claimTable': self.config['claimTable']}}

    def test_dispatcher_retains_stopped_failure_before_receipt_and_never_relaunches_unknown_ack(self):
        handoff = self.handoff()
        ecs, s3, db, sts = (MagicMock() for _ in range(4))
        sts.get_caller_identity.return_value = {'Account': self.config['accountId']}
        ecs.describe_task_definition.return_value = {'taskDefinition': self.definition()}
        ecs.run_task.return_value = {'tasks': [{'taskArn': self.native['taskArn']}]}
        ecs.describe_tasks.return_value = {'tasks': [{'taskArn': self.native['taskArn'], 'taskDefinitionArn': self.native['taskDefinitionArn'],
            'lastStatus': 'STOPPED', 'enableExecuteCommand': False, 'containers': [{'image': self.config['imageUri'], 'exitCode': 1}]}]}
        services = {'ecs': ecs, 's3': s3, 'dynamodb': db, 'sts': sts}
        with patch.object(dispatcher, 'sdk', side_effect=lambda name, region: services[name]), patch.object(dispatcher, 'read_allowance'), patch.object(dispatcher, 'consume', return_value=True):
            with self.assertRaisesRegex(ValueError, 'retirement failed'): dispatcher.dispatch(handoff)
        terminal = json.loads(db.update_item.call_args.kwargs['ExpressionAttributeValues'][':observation']['S'])
        self.assertEqual(terminal['exitCode'], 1)
        self.assertEqual(terminal['taskDefinitionArn'], self.native['taskDefinitionArn'])
        s3.get_object.assert_not_called()
        ecs.run_task.reset_mock(); db.get_item.return_value = {'Item': {}}
        with patch.object(dispatcher, 'sdk', side_effect=lambda name, region: services[name]), patch.object(dispatcher, 'read_allowance'), patch.object(dispatcher, 'consume', return_value=False):
            with self.assertRaisesRegex(ValueError, 'acknowledgement unknown'): dispatcher.dispatch(handoff)
        ecs.run_task.assert_not_called()

    def test_lost_s3_receipt_ack_does_not_lose_independent_terminal_proof(self):
        handoff = self.handoff()
        ecs, s3, db, sts = (MagicMock() for _ in range(4))
        sts.get_caller_identity.return_value = {'Account': self.config['accountId']}
        ecs.describe_task_definition.return_value = {'taskDefinition': self.definition()}
        ecs.run_task.return_value = {'tasks': [{'taskArn': self.native['taskArn']}]}
        ecs.describe_tasks.return_value = {'tasks': [{'taskArn': self.native['taskArn'], 'taskDefinitionArn': self.native['taskDefinitionArn'],
            'lastStatus': 'STOPPED', 'enableExecuteCommand': False, 'containers': [{'image': self.config['imageUri'], 'exitCode': 0}]}]}
        s3.get_object.side_effect = TimeoutError('receipt acknowledgement lost')
        services = {'ecs': ecs, 's3': s3, 'dynamodb': db, 'sts': sts}
        with patch.object(dispatcher, 'sdk', side_effect=lambda name, region: services[name]), patch.object(dispatcher, 'read_allowance'), patch.object(dispatcher, 'consume', return_value=True):
            with self.assertRaises(TimeoutError): dispatcher.dispatch(handoff)
        terminal = json.loads(db.update_item.call_args.kwargs['ExpressionAttributeValues'][':observation']['S'])
        self.assertEqual(terminal['exitCode'], 0)
        self.assertEqual(terminal['lastStatus'], 'STOPPED')
