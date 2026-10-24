"""Synthetic immutable installation controls; no cloud key or active runtime."""
import base64
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import installed_mcp_configuration as mcp
from agent_e2e_pack import canonical_digest, PackError
from fixture_handoff import encoded


class InstalledMcpBase(unittest.TestCase):
    def setUp(self):
        self.candidate='sha256:'+'a'*64; self.expiry=2000006000
        self.operation={'operationId':'installation','liabilityBasisDigest':'sha256:'+'b'*64,
            'reservationIds':{'infrastructure':'original-infra','cleanup':'original-cleanup'}}
        ceilings={k:False for k in mcp.STUDIO};ceilings.update(datasetImportScopes=[],managedProcessIds=[])
        self.cfg={'region':'us-east-1','secretArn':'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-fixed-slot-mcp-AbCdEf',
            'endpoint':'https://api.pilot.demo.honua.io/mcp','workflowView':'workspace','runtimeId':'runtime','slotId':'fixed-slot',
            'targetId':'actual-target','nativeBootstrapOperationId':'original-bootstrap','originalFundingExpiresEpoch':self.expiry,'mutationCeilings':ceilings}
        self.issued={'apiKeyId':'11111111-1111-1111-1111-111111111111',
            'secretArn':'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-unit-agent/native-agent/agent-AbCdEf',
            'versionId':'c'*64,'permissions':['admin:read','agent:workspace','agent:runtime-task:runtime']}
        self.issued['effectivePermissions']={'id':self.issued['apiKeyId'],'canAuthenticate':True,'permissions':self.issued['permissions']}
        original={'candidateKey':self.candidate,'operation':{'operationId':'original-bootstrap'},'endpoint':self.cfg['endpoint'].removesuffix('/mcp'),
            'targetId':'actual-target','secretArns':{'agent':self.issued['secretArn']},'installedRuntime':{'runtimeId':'runtime','mutationCeilings':ceilings}}
        self.original=original
        self.bootstrap_definition='arn:aws:ecs:us-east-1:123456789012:task-definition/native-bootstrap:1'
        completed={'schema':'honua.private-native-bootstrap/v1','candidateKey':self.candidate,'operation':original['operation'],
            'keys':{'agent':self.issued},'target':{'targetId':'actual-target'},'configurationDigest':canonical_digest(original),
            'native':{'taskDefinitionArn':self.bootstrap_definition,'imageUri':'actual-reviewed-bootstrap@sha256:'+'d'*64}}
        prefix=canonical_digest({'kind':'native-bootstrap','candidateKey':self.candidate,'operationId':'original-bootstrap'})
        self.records={prefix+'/completed':{'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':completed},
            prefix+'/key-agent':{'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':self.issued}}
        self.durable=MagicMock();self.durable.store.candidate=self.candidate
        self.durable.state={'ledger':{'interactiveAccounting':{'originalExpiresEpoch':self.expiry}},'scenarioOperations':self.records}
        self.controller_task='arn:aws:ecs:us-east-1:123456789012:task/honua-pilot-unit-agent/'+'f'*32
        self.ecs=MagicMock();self.ecs.describe_tasks.return_value={'tasks':[{'taskArn':self.controller_task,'taskDefinitionArn':'actual-controller:1'}]}
        self.controller_definition={'containerDefinitions':[{'name':'campaign-controller','secrets':[{'name':'HONUA_AGENT_REQUESTER_KEY',
            'valueFrom':self.issued['secretArn']+':::'+self.issued['versionId']}]}]}
        def definition(**kwargs):
            if kwargs['taskDefinition']==self.bootstrap_definition:
                return {'taskDefinition':{'taskDefinitionArn':self.bootstrap_definition,'containerDefinitions':[{
                    'image':completed['native']['imageUri'],'environment':[{'name':'PILOT_NATIVE_BOOTSTRAP_CONFIG','value':json.dumps(self.original)}]}]}}
            return {'taskDefinition':self.controller_definition}
        self.ecs.describe_task_definition.side_effect=definition
        self.credential='private-synthetic-agent-credential-'+'k'*48
        self.principal={'actorId':'actual-opaque-task-actor','studioOwnerId':self.issued['apiKeyId'],
            'authenticationKind':'api-key','isHumanSession':False,'tenantId':'actual-tenant'}
        def http(config,credential,method,path):
            self.assertEqual(credential,self.credential);self.assertEqual(method,'GET')
            body={'targetId':'actual-target','principal':self.principal} if path.endswith('/target') else {'success':True,'data':{
                'id':self.issued['apiKeyId'],'status':'active','canAuthenticate':True,'permissions':self.issued['permissions']}}
            raw=encoded(body)
            return {'httpStatus':200,'body':body,'bodyBase64':base64.b64encode(raw).decode(),'bodyBytes':len(raw),'bodySha256':mcp.canonical_digest(body)}
        self.http=MagicMock(side_effect=http)
        self.versions={};self.secrets=MagicMock()
        def put(**kwargs):
            value={'ARN':kwargs['SecretId'],'VersionId':kwargs['ClientRequestToken'],'SecretString':kwargs['SecretString']}
            self.versions[value['VersionId']]=value
            return {k:value[k] for k in ('ARN','VersionId')}
        self.put=put;self.secrets.put_secret_value.side_effect=put
        self.secrets.get_secret_value.side_effect=lambda **kwargs:self.versions[kwargs['VersionId']]

    def run_base(self):
        with patch.object(mcp,'authorize'),patch.object(mcp,'validate_retained_funding'),patch.dict(os.environ,{
            'PILOT_CONTROLLER_TASK_ARN':self.controller_task,'HONUA_AGENT_REQUESTER_KEY':self.credential}):
            return mcp.provision(self.durable,self.operation,self.cfg,secrets=self.secrets,ecs=self.ecs,http=self.http,clock=lambda:2000000000)

    def test_base_is_immutable_and_explicitly_pending_with_no_turn_or_control_credential(self):
        result=self.run_base();binding=result['binding']
        payload=json.loads(self.secrets.put_secret_value.call_args.kwargs['SecretString'])
        self.assertEqual(set(payload),{'endpoint','credentialKind','credential','workflowView','bindingMode','runtimeId','slotId'})
        self.assertEqual(payload['bindingMode'],'native-execution/v1')
        self.assertFalse(binding['activation']);self.assertFalse(binding['qualification']);self.assertEqual(binding['status'],'registration-pending')
        self.assertEqual(binding['principal']['actorId'],'actual-opaque-task-actor')
        self.assertEqual(result,self.run_base());self.assertEqual(self.secrets.put_secret_value.call_count,1)
        self.assertNotIn(self.credential,json.dumps(self.durable.state))
        self.assertEqual(canonical_digest(self.records[result['reference']['key']]),result['reference']['sha256'])

    def test_lost_secret_ack_reads_only_original_version_and_keeps_original_completion(self):
        def lost(**kwargs):self.put(**kwargs);raise TimeoutError('write acknowledgement lost')
        self.secrets.put_secret_value.side_effect=lost
        with self.assertRaises(TimeoutError):self.run_base()
        result=self.run_base()
        self.assertFalse(result['binding']['activation']);self.assertEqual(self.secrets.put_secret_value.call_count,1)
        self.secrets.get_secret_value.assert_called_once()

    def test_changed_original_funding_expiry_refuses_before_any_observation(self):
        self.cfg['originalFundingExpiresEpoch']+=1
        with self.assertRaisesRegex(PackError,'original funding expiry'):self.run_base()
        self.http.assert_not_called();self.secrets.put_secret_value.assert_not_called()

    def test_control_key_cannot_be_copied_as_task_credential(self):
        self.issued['permissions'].append('agent:runtime-control:runtime')
        with self.assertRaisesRegex(PackError,'control or approval'):self.run_base()
        self.secrets.put_secret_value.assert_not_called()

    def test_runtime_task_marker_is_required_even_with_all_mutations_disabled(self):
        self.issued['permissions'].remove('agent:runtime-task:runtime')
        with self.assertRaisesRegex(PackError,'control or approval'):self.run_base()
        self.secrets.put_secret_value.assert_not_called()

    def test_wrong_immutable_injection_never_writes(self):
        self.controller_definition['containerDefinitions'][0]['secrets'][0]['valueFrom']+='wrong'
        with self.assertRaisesRegex(PackError,'exact original immutable'):self.run_base()
        self.secrets.put_secret_value.assert_not_called()

    def test_original_source_endpoint_cannot_be_rebound_to_alias(self):
        self.cfg['endpoint']='https://agent.pilot.demo.honua.io/mcp'
        with self.assertRaisesRegex(PackError,'original bootstrap configuration'):self.run_base()
        self.secrets.put_secret_value.assert_not_called()

    def test_native_principal_is_observed_not_derived_from_declared_key_id(self):
        self.principal['studioOwnerId']='22222222-2222-2222-2222-222222222222'
        with self.assertRaisesRegex(PackError,'actual matching machine principal'):self.run_base()
        self.secrets.put_secret_value.assert_not_called()

    def test_same_allocation_cannot_be_reminted_with_another_operation(self):
        self.run_base();self.operation['operationId']='replacement-operation'
        with self.assertRaisesRegex(PackError,'different immutable MCP intent'):self.run_base()
        self.assertEqual(self.secrets.put_secret_value.call_count,1)


if __name__=='__main__':unittest.main()
