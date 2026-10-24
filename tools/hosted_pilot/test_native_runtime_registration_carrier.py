"""Registrar identity and original funding controls; no AWS/native qualification."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agent_e2e_budget import update
from agent_e2e_interactive_budget import funding_binding
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
import native_runtime_registration_carrier as carrier
from registration_allowance import admission_key


class RegistrarCarrier(unittest.TestCase):
    def setUp(self):
        candidate = 'sha256:'+'a'*64
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path = self.root/'ledger.json'
        update(path, 'init', candidate_key=candidate)
        for kind in ('infrastructure', 'cleanup'):
            update(path, 'reserve-operation', candidate_key=candidate, reservation_id='original-'+kind,
                   operation_id='original-bootstrap', purpose='bootstrap', kind=kind, amount='5', basis_digest='sha256:'+'b'*64)
        original = json.loads(path.read_text())
        artifact = {'schemaVersion': 1, 'candidateKey': candidate, 'ledger': original,
            'source': {'repository': 'honua-io/honua-release', 'workflow': '.github/workflows/agent-e2e-campaign.yml',
                       'sourceSha': 'a'*40, 'runId': 123, 'runAttempt': 1}}
        imported = {'artifactDigest': digest_bytes(json.dumps(artifact).encode()), 'artifact': artifact}
        self.expiry = int(datetime.now(timezone.utc).timestamp())+3600
        history_path='certification/hosted-pilot/history/synthetic-carrier.json'
        history={'schema':'honua.hosted-pilot-budget-history/v1','coverage':{'start':datetime.fromtimestamp(self.expiry-7200,timezone.utc).isoformat()}}
        history_raw=json.dumps(history).encode();target=self.root/history_path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(history_raw)
        history_reference={'path':history_path,'sha256':digest_bytes(history_raw)}
        raw = json.dumps({'schemaVersion': 1, 'candidateKey': candidate, 'ledger': original,
            'history': history_reference, 'expiresAt': datetime.fromtimestamp(self.expiry, timezone.utc).isoformat(),
            'liveLiabilityReferences': [{'path': 'original-liability.json'}]}).encode()
        binding, _ = funding_binding(imported, source_reader=lambda sha, name: raw)
        update(path, 'amend-foss4g', candidate_key=candidate)
        update(path, 'enable-interactive-accounting', candidate_key=candidate, binding=binding)
        for kind in ('infrastructure', 'cleanup'):
            update(path, 'reserve-operation', candidate_key=candidate, reservation_id='shared-'+kind,
                   operation_id='original-network', purpose='shared-environment', kind=kind, amount='5', basis_digest='sha256:'+'e'*64)
        self.config = {'schemaVersion': 1, 'region': 'us-east-1', 'accountId': '123456789012', 'candidateKey': candidate,
            'state': {'region': 'us-east-1', 'bucket': 'honua-agent-'+candidate[7:31]+'-123456789012',
                      'table': 'honua-agent-'+candidate[7:31]+'-state'}, 'generationDigest': 'sha256:'+'c'*64,
            'bootstrapArtifactDigest': imported['artifactDigest'],
            'declaration': {'path': 'certification/agent-e2e/installed-runtimes/runtime-original.json', 'sha256': 'sha256:'+'d'*64},
            'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-unit-agent-cluster',
            'taskDefinitionFamily': 'arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-unit-agent-runtime-registration',
            'imageUri': '123456789012.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-trusted-controller@sha256:'+'e'*64,
            'credentials': {kind: {'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-unit/'+kind+'-ABC123',
                                  'versionId': ('f' if kind == 'registrar' else '1' if kind == 'control' else '2')*64}
                            for kind in carrier.SECRET_ENVS},
            'maximumUsd': {'infrastructure': '5', 'cleanup': '5'}, 'maxExecutionSeconds': 600}
        # Synthetic original wire documents exercise the real source, quote,
        # funding and retained admission validators; they are not cloud evidence.
        pricing = []
        for kind, usage, unit, price in (
                ('fargate-vcpu', 'USE1-Fargate-vCPU-Hours:perCPU', 'hours', '0.04048'),
                ('fargate-memory', 'USE1-Fargate-GB-Hours', 'hours', '0.004445'),
                ('nat-transfer', 'NatGateway-Bytes', 'GB', '0.045'),
                ('cloudwatch-ingestion', 'USE1-DataProcessing-Bytes', 'GB', '0.50'),
                ('standard-state-transitions', 'USE1-StateTransition', 'StateTransitions', '0.000025'),
                ('dynamodb-read', 'ReadRequestUnits', 'ReadRequestUnits', '0.000000125'),
                ('dynamodb-write', 'WriteRequestUnits', 'WriteRequestUnits', '0.000000625'),
                ('nat-hours','NatGateway-Hours','Hrs','0.045'),
                ('cloudwatch-retention','USE1-TimedStorage-ByteHrs','GB-Mo','0.03'),
                ('scheduler-invocations','USE1-ScheduledInvocation','Invocations','0.000001')):
            original = {'PriceList': [json.dumps({'product': {'attributes': {'regionCode': 'us-east-1', 'usagetype': usage}},
                'terms': {'OnDemand': {'offer': {'priceDimensions': {'rate': {'unit': unit, 'beginRange': '0',
                    'endRange': 'Inf', 'pricePerUnit': {'USD': price}}}}}}})]}
            if kind=='scheduler-invocations':
                value=json.loads(original['PriceList'][0]);value['terms']['OnDemand']['offer']['priceDimensions']={
                    'free':{'unit':'Invocations','beginRange':'0','endRange':'14000000','pricePerUnit':{'USD':'0'}},
                    'paid':{'unit':'Invocations','beginRange':'14000000','endRange':'Inf','pricePerUnit':{'USD':'0.000001'}}}
                original['PriceList']=[json.dumps(value)]
            pricing.append({'kind': kind, 'reference': self.put_source(kind, original)})
        quote = {'schema': 'honua.registrar-liability-quote/v1', 'candidateKey': candidate, 'runtimeId': 'runtime-original',
            'region': 'us-east-1', 'cpu': 256, 'memory': 1024, 'maxExecutionSeconds': 600, 'image': self.config['imageUri'],
            'maximumUsd': self.config['maximumUsd'], 'pricingOriginals': pricing,
            'bounds': {'maximumImagePullBytes': 2*1024**3, 'maximumRequestResponseBytes': 64*1024**2,
                'maximumLogBytes': 1024**2, 'maximumStateTransitions':1200, 'maximumDynamoReadRequestUnits':512,
                'maximumDynamoWriteRequestUnits':256, 'maximumTaskStarts': 1, 'maximumCleanupTaskStarts': 1, 'cleanupMaxExecutionSeconds': 600},
            'imageOriginal': self.put_source('image', {'schema': 'honua.registrar-ecr-image-original/v1', 'region': 'us-east-1',
                'accountId': self.config['accountId'], 'repository': 'honua-pilot-unit-trusted-controller', 'imageDigest': 'sha256:'+'e'*64,
                'response': {'imageDetails': [{'registryId': self.config['accountId'], 'repositoryName': 'honua-pilot-unit-trusted-controller',
                    'imageDigest': 'sha256:'+'e'*64, 'imageSizeInBytes': 1024**3}]}}),
            'sharedInfrastructureOriginal': self.put_source('shared', {'schema': 'honua.registrar-shared-infrastructure-binding/v2',
                'historyOriginal':history_reference,'originalFundingExpiresEpoch':self.expiry,'plannedRetirementAtEpoch':self.expiry-900,
                'candidateKey': candidate, 'operationId': 'original-network', 'basisDigest': 'sha256:'+'e'*64,
                'reservationIds': {kind: 'shared-'+kind for kind in ('infrastructure', 'cleanup')}})}
        self.config['liabilityQuote'] = self.put_source('quote', quote)
        self.config['operation'] = {'operationId': 'runtime-registration-runtime-original',
            'liabilityBasisDigest': canonical_digest(self.config),
            'reservationIds': {kind: 'registrar-'+kind for kind in ('infrastructure', 'cleanup')}}
        for kind, reservation in self.config['operation']['reservationIds'].items():
            update(path, 'reserve-operation', candidate_key=candidate, reservation_id=reservation,
                   operation_id=self.config['operation']['operationId'], purpose='bootstrap', kind=kind,
                   amount='5', basis_digest=self.config['operation']['liabilityBasisDigest'])
        self.durable = MagicMock()
        self.durable.store = SimpleNamespace(candidate=candidate, generation={'digest': self.config['generationDigest']},
            bucket=self.config['state']['bucket'], table=self.config['state']['table'])
        self.durable.state = {'ledger': json.loads(path.read_text()), 'bootstrapImport': imported,
            'interactiveFunding': {'binding': binding, 'configuration': raw.decode()}, 'scenarioOperations': {}}
        self.admitted = self.expiry-3600
        self.durable.state['runtimeRegistrationAdmissions'] = {self.config['operation']['operationId']: {
            'schema': 'honua.runtime-registration-admission/v1', 'candidateKey': candidate, 'runtimeId': 'runtime-original',
            'operationId': self.config['operation']['operationId'], 'configurationDigest': canonical_digest(self.config),
            'basisDigest': self.config['operation']['liabilityBasisDigest'], 'quoteDigest': self.config['liabilityQuote']['sha256'],
            'admittedEpoch': self.admitted, 'deadlineEpoch': self.admitted+600, 'originalFundingExpiresEpoch': self.expiry,
            'sourceHead': {'generationDigest': self.config['generationDigest'], 'stateVersion': 7,
                'snapshotDigest': 'sha256:'+'1'*64, 'snapshotVersion': 'original-snapshot'}}}
        self.durable.state['scenarioOperations'][admission_key(self.config)] = {
            'producer': 'private-runtime-registration-admission', 'candidateKey': candidate,
            'value': copy.deepcopy(self.durable.state['runtimeRegistrationAdmissions'][self.config['operation']['operationId']])}
        self.task_arn = 'arn:aws:ecs:us-east-1:123456789012:task/honua-pilot-unit-agent-cluster/'+'3'*32
        self.task = {'taskArn': self.task_arn, 'clusterArn': self.config['clusterArn'], 'lastStatus': 'RUNNING',
            'desiredStatus': 'RUNNING', 'launchType': 'FARGATE', 'enableExecuteCommand': False,
            'taskDefinitionArn': self.config['taskDefinitionFamily']+':7',
            'containers': [{'name': 'runtime-registration', 'image': self.config['imageUri'], 'imageDigest': 'sha256:'+'e'*64}],
            'overrides': {'containerOverrides': [{'name': 'runtime-registration'}]}}
        self.container = {'name': 'runtime-registration', 'image': self.config['imageUri'], 'command': carrier.COMMAND.copy(),
            'user': '10001:10001', 'readonlyRootFilesystem': True,
            'mountPoints': [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}],
            'environment': [{'name': carrier.CONFIG_ENV, 'value': json.dumps(self.config)}],
            'secrets': [{'name': carrier.SECRET_ENVS[kind], 'valueFrom': ref['secretArn']+':::'+ref['versionId']}
                        for kind, ref in self.config['credentials'].items()]}
        self.definition = {'taskDefinitionArn': self.task['taskDefinitionArn'], 'status': 'ACTIVE',
            'taskRoleArn': 'original-bootstrap-role', 'executionRoleArn': 'original-bootstrap-execution-role',
            'cpu': '256', 'memory': '1024',
            'networkMode': 'awsvpc', 'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
            'containerDefinitions': [self.container], 'registeredAt': datetime.now(timezone.utc)}
        self.ecs = MagicMock()
        self.ecs.describe_tasks.side_effect = lambda **kw: {'tasks': [self.task]}
        self.ecs.describe_task_definition.side_effect = lambda **kw: {'taskDefinition': self.definition}
        self.image_verifier = MagicMock(return_value={'binding': 'native-exact-digest', 'image': self.config['imageUri']})

    def put_source(self, name, value):
        path = 'certification/hosted-pilot/quotes/'+name+'.json'
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        target.write_bytes(raw)
        return {'path': path, 'sha256': digest_bytes(raw)}

    def source(self, path, sha):
        return self.root/path

    def observe(self):
        return carrier.verify_task(self.config, self.task_arn, ecs=self.ecs, image_verifier=self.image_verifier)

    def test_exact_configuration_keeps_original_state_and_distinct_operation(self):
        self.assertEqual(carrier.validate(self.config), 'runtime-original')
        self.assertNotIn('expiresAt', self.config)
        self.assertEqual(self.config['maximumUsd'], {'infrastructure': '5', 'cleanup': '5'})
        self.assertEqual(self.config['maxExecutionSeconds'], 600)

    def test_cluster_matches_actual_ecs_module_name(self):
        self.assertEqual(self.config['clusterArn'].rsplit('/', 1)[1], 'honua-pilot-unit-agent-cluster')
        self.assertEqual(carrier.validate(self.config), 'runtime-original')
        for name in ('honua-pilot-unit-agent', 'honua-pilot-other-agent-cluster'):
            with self.subTest(name=name):
                bad = copy.deepcopy(self.config)
                bad['clusterArn'] = bad['clusterArn'].rsplit('/', 1)[0]+'/'+name
                bad['operation']['liabilityBasisDigest'] = canonical_digest({key: value for key, value in bad.items() if key != 'operation'})
                with self.assertRaisesRegex(PackError, 'isolated original cluster'):
                    carrier.validate(bad)

    def test_new_expiry_allowance_or_foreign_namespace_cannot_be_supplied(self):
        for field, value in [('expiresAt', self.expiry+3600), ('maximumUsd', '5000'), ('state', {'region': 'us-east-1', 'table': 'new', 'bucket': 'new'})]:
            with self.subTest(field=field):
                bad = copy.deepcopy(self.config); bad[field] = value
                with self.assertRaises(PackError): carrier.validate(bad)

    def test_reused_operation_changed_basis_and_floating_credentials_refuse(self):
        for kind in ('operation', 'basis', 'version', 'shared-secret', 'image'):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(self.config)
                if kind == 'operation': bad['operation']['operationId'] = 'original-bootstrap'
                elif kind == 'basis': bad['operation']['liabilityBasisDigest'] = 'sha256:'+'0'*64
                elif kind == 'version': bad['credentials']['control']['versionId'] = 'AWSCURRENT'
                elif kind == 'shared-secret': bad['credentials']['task'] = copy.deepcopy(bad['credentials']['control'])
                else: bad['imageUri'] = bad['imageUri'].split('@')[0]+':latest'
                with self.assertRaises(PackError): carrier.validate(bad)

    def test_actual_task_and_datetime_definition_bind_without_private_values(self):
        value = self.observe()
        self.assertEqual(value['taskArn'], self.task_arn)
        self.assertRegex(value['definitionDigest'], r'^sha256:[a-f0-9]{64}$')
        self.image_verifier.assert_called_once_with(self.config['imageUri'], 'sha256:'+'e'*64)
        self.assertNotIn('SecretString', json.dumps(value))

    def test_task_resources_cannot_exceed_original_quoted_bounds(self):
        for field, value in [('cpu', '512'), ('memory', '2048'), ('ephemeralStorage', {'sizeInGiB': 21})]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.definition)
                self.definition[field] = value
                with self.assertRaises(PackError): self.observe()
                self.definition.clear(); self.definition.update(original)
        self.image_verifier.assert_not_called()

    def test_command_environment_and_role_overrides_refuse(self):
        for override in ({'containerOverrides': [{'name': 'runtime-registration', 'command': ['sh']}]},
                         {'containerOverrides': [{'name': 'runtime-registration', 'environment': [{'name': 'PYTHONPATH', 'value': '/work'}]}]},
                         {'taskRoleArn': 'different-role'}):
            with self.subTest(override=override):
                self.task['overrides'] = override
                with self.assertRaises(PackError): self.observe()
        self.image_verifier.assert_not_called()

    def test_extra_or_changed_secret_injection_refuses(self):
        original = copy.deepcopy(self.container['secrets'])
        for changed in (original+[{'name': 'SQL_PASSWORD', 'valueFrom': 'other'}],
                        [dict(row, valueFrom=row['valueFrom']+'changed') for row in original], original[:2]):
            self.container['secrets'] = changed
            with self.assertRaises(PackError): self.observe()

    def test_container_mutability_or_alternate_entry_refuses(self):
        for field, value in [('user', '0'), ('readonlyRootFilesystem', False), ('entryPoint', ['sh']),
                             ('environmentFiles', [{'value': 'arbitrary', 'type': 's3'}]), ('mountPoints', [])]:
            with self.subTest(field=field):
                old = copy.deepcopy(self.container)
                self.container[field] = value
                with self.assertRaises(PackError): self.observe()
                self.container.clear(); self.container.update(old)

    def test_stopped_replaced_or_exec_enabled_task_refuses(self):
        for field, value in [('lastStatus', 'STOPPED'), ('desiredStatus', 'STOPPED'), ('taskArn', 'other'),
                             ('taskDefinitionArn', self.config['taskDefinitionFamily']+'-other:7'), ('enableExecuteCommand', True)]:
            old = self.task[field]; self.task[field] = value
            with self.assertRaises(PackError): self.observe()
            self.task[field] = old

    def test_original_live_funding_is_read_without_renewal_or_settlement(self):
        before = copy.deepcopy(self.durable.state)
        self.assertEqual(carrier.authorize(self.config, self.durable, source_reader=self.source), self.expiry)
        self.assertEqual(before, self.durable.state)
        self.durable._persist.assert_not_called()

    def test_expiry_refuses_without_replacing_original_hold(self):
        before = copy.deepcopy(self.durable.state)
        with patch.object(carrier.time, 'time', return_value=self.expiry):
            with self.assertRaisesRegex(PackError, 'expired'): carrier.authorize(self.config, self.durable, source_reader=self.source)
        self.assertEqual(before, self.durable.state)

    def test_original_admission_deadline_cannot_restart_with_a_live_funding_window(self):
        before = copy.deepcopy(self.durable.state)
        with patch.object(carrier.time, 'time', return_value=self.admitted+600):
            with self.assertRaisesRegex(PackError, 'expired'):
                carrier.authorize(self.config, self.durable, source_reader=self.source)
        self.assertEqual(before, self.durable.state)
        self.durable._persist.assert_not_called()
        self.durable.update.assert_not_called()

    def test_changed_generation_or_original_import_refuses(self):
        for field in ('generationDigest', 'bootstrapArtifactDigest'):
            bad = copy.deepcopy(self.config); bad[field] = 'sha256:'+'9'*64
            with self.assertRaises(PackError): carrier.authorize(bad, self.durable, source_reader=self.source)

    def test_missing_original_funding_or_cleanup_refuses(self):
        original = copy.deepcopy(self.durable.state)
        self.durable.state['ledger'].pop('interactiveAccounting')
        with self.assertRaises(PackError): carrier.authorize(self.config, self.durable, source_reader=self.source)
        self.durable.state = copy.deepcopy(original)
        self.durable.state['ledger']['operations'] = [row for row in original['ledger']['operations'] if row['id'] != 'registrar-cleanup']
        with self.assertRaises(PackError): carrier.authorize(self.config, self.durable, source_reader=self.source)

    def binding(self):
        original = {'producer': 'private-native-bootstrap', 'candidateKey': self.config['candidateKey'],
                    'value': {'native': {'taskDefinitionArn': 'original-bootstrap-definition:4'}}}
        self.durable.state['scenarioOperations']['bootstrap/completed'] = original
        verified = {'candidateKey': self.config['candidateKey'], 'declarationReference': self.config['declaration'],
            'originalFundingExpiresEpoch': self.expiry, 'keys': {kind: {'issued': self.config['credentials'][kind]}
            for kind in ('control', 'task')}, 'nativeBootstrap': {'key': 'bootstrap/completed', 'sha256': canonical_digest(original)}}
        native = self.observe()
        bootstrap = copy.deepcopy(self.definition)
        bootstrap['taskDefinitionArn'] = 'original-bootstrap-definition:4'
        bootstrap['containerDefinitions'][0]['name'] = 'native-bootstrap'
        bootstrap['containerDefinitions'][0]['command'] = ['python', 'tools/hosted_pilot/native_bootstrap.py']
        self.ecs.describe_task_definition.side_effect = lambda **kw: {'taskDefinition': bootstrap}
        return verified, native, bootstrap

    def test_privileged_root_is_bound_to_original_bootstrap_injection(self):
        verified, native, _ = self.binding()
        carrier.bind_original_credentials(self.config, verified, self.durable, native, ecs=self.ecs, source_reader=self.source)

    def test_issued_setup_key_or_different_role_cannot_replace_original_root(self):
        verified, native, bootstrap = self.binding()
        original = copy.deepcopy(bootstrap)
        for kind in ('root-version', 'role'):
            if kind == 'role': bootstrap['taskRoleArn'] = 'ordinary-controller-role'
            else: bootstrap['containerDefinitions'][0]['secrets'][0]['valueFrom'] = 'issued-setup-key'
            with self.assertRaises(PackError): carrier.bind_original_credentials(self.config, verified, self.durable, native, ecs=self.ecs, source_reader=self.source)
            bootstrap.clear(); bootstrap.update(copy.deepcopy(original))

    def test_fixed_entry_rejects_arguments_and_root_before_clients(self):
        for uid, arguments in [(0, []), (10001, ['--configuration', 'caller.json'])]:
            with patch.object(os, 'getuid', return_value=uid), patch.object(carrier.sys, 'argv', ['registrar']+arguments):
                with self.assertRaisesRegex(PackError, 'fixed reviewed nonroot'): carrier.main()

    def test_metadata_rejects_arbitrary_origins_without_network(self):
        for uri in ('https://example.com', 'http://169.254.170.2/v4/ok?redirect=other', 'http://127.0.0.1/v4/fake'):
            with patch.dict(os.environ, {'ECS_CONTAINER_METADATA_URI_V4': uri}), patch.object(carrier, 'build_opener') as opened:
                with self.assertRaises(PackError): carrier.metadata_task()
                opened.assert_not_called()
