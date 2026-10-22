"""Original-byte plan join controls; manufactured source vectors grant no AWS proof."""
from datetime import datetime, timezone
import copy
from dataclasses import replace
import json
import pytest
import devops_provisioning_contract as contract


def vector():
    image = 'example.invalid/honua@sha256:' + 'a' * 64
    backend = {'is_remote': True, 'backend_kind': 's3', 'backend_config_digest': 'b' * 64,
               'workspace': 'default', 'object_key': 'owned/cell.tfstate', 'region': 'us-west-2',
               'bucket_id': 'owned-state', 'bucket_arn': 'arn:aws:s3:::owned-state',
               'locking': {'kind': 's3-native', 'detail': 'use_lockfile'},
               'encryption': {'kind': 'sse-kms'},
               'backend_access_role_arn': 'arn:aws:iam::123456789012:role/backend'}
    inputs = {'varfile:/owned/request/terraform.tfvars.json': contract.canonical({'honua_image': image,
              'region': 'us-west-2', 'db_password': 'source-vector-only-not-a-credential'})}
    context = contract.PlanContext(image, '1' * 40, '2' * 64, '1.13.5', '3' * 64, backend,
              '123456789012', 'arn:aws:sts::123456789012:assumed-role/exec/owned',
              'honua-devops:urn:honua:provisioning:' + '4' * 32, 'aws-ecs:it',
              'urn:honua:provisioning:' + '4' * 32, 'apply', tuple(inputs),
              datetime(2026,10,20,0,0,tzinfo=timezone.utc), datetime(2026,10,20,1,0,tzinfo=timezone.utc))
    plan = b'original saved binary plan source vector'
    metadata = {'schema_version': 'v1', 'kind': 'honua.iac.exact-plan', 'action': 'apply',
                'created_at_utc': '2026-10-20T00:01:00Z', 'expires_at_utc': '2026-10-20T00:30:00Z',
                'actor_id': context.actor_id, 'target_id': context.target_id, 'candidate_digest': None,
                'source': {'iac_revision': context.iac_revision, 'iac_tree_digest': context.iac_tree_digest},
                'toolchain': {'terraform_version': context.terraform_version,
                              'provider_lock_digest': context.provider_lock_digest}, 'backend': backend,
                'identity': {'account_id': context.account_id, 'assumed_role_arn': context.assumed_role_arn,
                             'credential_kind': 'sts-assumed-role', 'evidence_mode': 'live'},
                'inputs': {'input_digest': contract.sha256('\n'.join(name+'='+contract.sha256(value)
                           for name,value in sorted(inputs.items())).encode()), 'input_refs': sorted(inputs)},
                'plan': {'path':'honua.tfplan','sha256':contract.sha256(plan)},
                'posture': {'release_qualified':True,'local_state_allowed':False,'dirty_source_allowed':False}}
    return metadata,plan,inputs,context


def original(metadata):
    value=copy.deepcopy(metadata);value.pop('plan_metadata_digest',None)
    value['plan_metadata_digest']=contract.sha256(contract.canonical(value))
    return contract.canonical(value)


def test_original_inputs_saved_plan_and_optional_candidate_field():
    metadata,plan,inputs,context=vector()
    result=contract.verify_plan_originals(original(metadata),plan,inputs,context)
    assert result['candidateImage']==context.candidate_image
    assert result['candidateQualified'] is False and result['sixAwsCellsQualified'] is False
    assert 'db_password' not in json.dumps(result)


@pytest.mark.parametrize('mutation', ['image','input-extra','input-missing','plan-bytes','source','provider',
    'backend','account','role','actor','target','action','time','local','unlocked','section'])
def test_coherently_rehashed_wrong_originals_do_not_match_trusted_context(mutation):
    metadata,plan,inputs,context=vector()
    if mutation=='image':
        key=next(iter(inputs));inputs[key]=contract.canonical({'honua_image':'other@sha256:'+'f'*64})
        metadata['inputs']['input_digest']=contract.sha256((key+'='+contract.sha256(inputs[key])).encode())
    if mutation=='input-extra': inputs['env:unexpected']=b'unexpected'
    if mutation=='input-missing': inputs.clear()
    if mutation=='plan-bytes': plan+=b'changed'
    if mutation=='source': metadata['source']['iac_revision']='9'*40
    if mutation=='provider': metadata['toolchain']['provider_lock_digest']='9'*64
    if mutation=='backend': metadata['backend']=dict(metadata['backend'],object_key='foreign/state')
    if mutation=='account': metadata['identity']['account_id']='999999999999'
    if mutation=='role': metadata['identity']['assumed_role_arn']='foreign'
    if mutation=='actor': metadata['actor_id']='foreign'
    if mutation=='target': metadata['target_id']='foreign'
    if mutation=='action': metadata['action']='destroy'
    if mutation=='time': metadata['expires_at_utc']='2026-10-21T00:30:00Z'
    if mutation=='local': metadata['posture']['local_state_allowed']=True
    if mutation=='unlocked': metadata['backend']=dict(metadata['backend'],locking={'kind':'none'})
    if mutation=='section': metadata['identity']=[]
    with pytest.raises(contract.Refusal): contract.verify_plan_originals(original(metadata),plan,inputs,context)


def test_duplicate_original_json_keys_refused_before_digest_acceptance():
    metadata,plan,inputs,context=vector()
    raw=original(metadata).replace(b'"action":"apply"',b'"action":"destroy","action":"apply"')
    with pytest.raises(contract.Refusal,match='duplicate'):
        contract.verify_plan_originals(raw,plan,inputs,context)


def execution_vector(exit_status=1):
    metadata,plan,inputs,context=vector()
    metadata['source']['terraform_root']='/owned/iac/examples/aws-ecs'
    metadata['state_before']={'lineage':'original-lineage','serial':4}
    original_metadata=original(metadata);metadata=contract.document(original_metadata)
    receipt={'schema_version':'v1','kind':'honua.iac.exec-receipt','action':'apply',
             'completed_at_utc':'2026-10-20T00:10:00Z','exit_status':exit_status,
             'status':'succeeded' if exit_status==0 else 'failed',
             'plan_metadata_digest':metadata['plan_metadata_digest'],
             'approved_digest':metadata['plan_metadata_digest'],
             'saved_plan_sha256':metadata['plan']['sha256'], 'actor_id':context.actor_id,
             'target_id':context.target_id,'candidate_digest':None,
             'workload_identity':{key:metadata['identity'].get(key) for key in
                 ('assumed_role_arn','role_id','account_id','partition','issuer','credential_kind')}
                 | {'contract_digest':metadata.get('workload_identity_contract_digest')},
             'backend_step':{key:metadata['backend'].get(key) for key in
                            ('backend_config_digest','backend_kind','workspace','object_key','bucket_arn','locking')},
             'state_before':metadata['state_before'],'state_after':{'lineage':'original-lineage','serial':5},
             'output_contract':{'output_name':'operator_contract_digest','digest':None},
             'cleanup':{'claim_dir':'/owned/claims/original','saved_plan':'/owned/plans/original/honua.tfplan',
                        'teardown_root':metadata['source']['terraform_root'],'teardown_action':'destroy'}}
    return receipt,original_metadata,context


@pytest.mark.parametrize('exit_status',[0,1,130])
def test_actual_receipt_schema_join_never_means_cleanup_is_complete(exit_status):
    receipt,metadata,context=execution_vector(exit_status)
    result=contract.verify_execution_original(contract.canonical(receipt),metadata,
           claim_directory='/owned/claims/original',plan_path='/owned/plans/original/honua.tfplan',expected_exit=exit_status)
    assert result['exitStatus']==exit_status and result['cleanupOutstanding'] is True
    assert result['candidateQualified'] is False


@pytest.mark.parametrize('mutation',['cleanup-claim','cleanup-plan','action','approved','backend','role','state','status','exit-bool'])
def test_execution_original_substitutions_refuse(mutation):
    receipt,metadata,context=execution_vector()
    if mutation=='cleanup-claim': receipt['cleanup']['claim_dir']='/foreign/claim'
    if mutation=='cleanup-plan': receipt['cleanup']['saved_plan']='/foreign/plan'
    if mutation=='action': receipt['action']='destroy'
    if mutation=='approved': receipt['approved_digest']='f'*64
    if mutation=='backend': receipt['backend_step']['object_key']='foreign'
    if mutation=='role': receipt['workload_identity']['assumed_role_arn']='foreign'
    if mutation=='state': receipt['state_after']['secret']='not-permitted-in-output'
    if mutation=='status': receipt['status']='succeeded'
    if mutation=='exit-bool': receipt['exit_status']=True
    with pytest.raises(contract.Refusal):
        contract.verify_execution_original(contract.canonical(receipt),metadata,
            claim_directory='/owned/claims/original',plan_path='/owned/plans/original/honua.tfplan',expected_exit=1)


def test_canonical_claim_completed_projection_does_not_fake_resource_absence():
    receipt,metadata,context=execution_vector();raw=contract.canonical(receipt)
    claim={'schema_version':'v1','kind':'honua.iac.claim-status','claim_dir':'/owned/claims/original',
           'state':'reconciliation-required','acquired_at_utc':'2026-10-20T00:02:00Z',
           'completed_at_utc':'2026-10-20T00:10:00Z','phase':'receipt-committed',
           'executor_id':'original-executor','receipt_digest':contract.sha256(raw)}
    kwargs={'claim_directory':claim['claim_dir'],'executor_id':claim['executor_id'],
            'acquired_at':contract.timestamp(claim['acquired_at_utc']),
            'executor_terminal_at':contract.timestamp('2026-10-20T00:11:00Z')}
    result=contract.verify_claim_original(contract.canonical(claim),raw,**kwargs)
    assert result['status']=='execution-acknowledged' and result['cleanupOutstanding'] is True
    assert contract.verify_claim_original(contract.canonical(claim),raw,**dict(kwargs,executor_terminal_at=None))['status']=='live-or-indeterminate'
    claim['phase']='reconciliation-required'
    assert contract.verify_claim_original(contract.canonical(claim),raw,**kwargs)['status']=='indeterminate'
    claim['executor_id']='foreign'
    with pytest.raises(contract.Refusal,match='identity'):
        contract.verify_claim_original(contract.canonical(claim),raw,**kwargs)


def test_saved_plan_review_matches_exact_reviewed_container_and_nested_module():
    metadata,plan,inputs,context=vector();address='module.honua.aws_ecs_task_definition.main'
    review={'planned_values':{'root_module':{'child_modules':[{'resources':[{'address':address,
             'type':'aws_ecs_task_definition','values':{'container_definitions':json.dumps([
                {'name':'honua','image':context.candidate_image},{'name':'proxy','image':'reviewed-proxy:1'}])}}]}]}}}
    kwargs={'candidate_image':context.candidate_image,'expected_workloads':{address:'honua'},
            'recorded_saved_plan_sha256':contract.sha256(plan),'saved_plan':plan}
    result=contract.verify_saved_plan_review(contract.canonical(review),**kwargs)
    assert result['workloads']==[address] and result['candidateQualified'] is False
    resource=review['planned_values']['root_module']['child_modules'][0]['resources'][0]
    resource['values']['container_definitions']=json.dumps([{'name':'honua','image':'foreign@sha256:'+'f'*64}])
    with pytest.raises(contract.Refusal,match='candidate-image'):
        contract.verify_saved_plan_review(contract.canonical(review),**kwargs)


@pytest.mark.parametrize('mutation',[None,'action','plan','operation','issuer','mode','decision','expiry','same-principal','unaccepted-original'])
def test_approval_requires_independent_original_acceptance_and_all_plan_semantics(mutation):
    metadata,plan,inputs,context=vector();metadata=original(metadata);parsed=contract.document(metadata)
    approval={'schemaVersion':'honua.devops.provision-approval/v1','approvalReceiptId':'source-vector-approval',
              'issuer':'trusted-issuer','keyId':'a'*16,'provisioningOperationId':context.operation_id,
              'planSha256':contract.sha256(plan),'planMetadataDigest':parsed['plan_metadata_digest'],
              'action':'apply','stack':'aws-ecs','environment':'it','decision':'approved','signingMode':'kms-mac',
              'issuedAtUtc':'2026-10-20T00:02:00.0000000+00:00',
              'expiresAtUtc':'2026-10-20T00:20:00.0000000+00:00',
              'signature':'SOURCE_VECTOR_NOT_A_REAL_MAC'}
    if mutation=='action': approval['action']='destroy'
    if mutation=='plan': approval['planSha256']='f'*64
    if mutation=='operation': approval['provisioningOperationId']='urn:honua:provisioning:'+'f'*32
    if mutation=='issuer': approval['issuer']='foreign'
    if mutation=='mode': approval['signingMode']='local-hmac-dev'
    if mutation=='decision': approval['decision']='rejected'
    if mutation=='expiry': approval['expiresAtUtc']='2026-10-20T00:03:00Z'
    raw=contract.canonical(approval)
    kwargs={'context':context,'stack':'aws-ecs','environment':'it',
            'accepted_original_sha256':contract.sha256(raw),'issuer':'trusted-issuer','key_id':'a'*16,
            'issued_by_principal':'approver-role','executor_principal':'executor-role',
            'mutation_started_at':contract.timestamp('2026-10-20T00:04:00Z')}
    if mutation=='same-principal': kwargs['issued_by_principal']='executor-role'
    if mutation=='unaccepted-original': kwargs['accepted_original_sha256']='f'*64
    if mutation is None:
        result=contract.verify_approval_original(raw,metadata,**kwargs)
        assert result['candidateQualified'] is False and result['sixAwsCellsQualified'] is False
    else:
        with pytest.raises(contract.Refusal):contract.verify_approval_original(raw,metadata,**kwargs)


def test_iac_full_entry_sort_with_overlapping_input_filenames(tmp_path):
    import hashlib
    import os
    from pathlib import Path
    import subprocess
    metadata,plan,inputs,context=vector()
    inputs={'varfile:vars.tfvars':next(iter(inputs.values())),
            'varfile:vars.tfvars.json':contract.canonical({'region':'us-west-2'})}
    context=replace(context,input_refs=tuple(inputs))
    fixture=Path(__file__).parent/'fixtures/devops-cleanup-inputs/tf-exec-contract.sh'
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()=='da86f5025725e488ba28b9122866c60f0c416ed362f6deff9aeb997931c7e018'
    assert hashlib.sha1(b'blob '+str(fixture.stat().st_size).encode()+b'\x00'+fixture.read_bytes()).hexdigest()=='8d02ba80178a99b33a8247ce20cedeefadcc70a6'
    for name,value in inputs.items(): (tmp_path/name.split(':',1)[1]).write_bytes(value)
    # Only the canonical read/hash function executes; no Terraform/AWS command is called.
    environment={'PATH':os.defpath,'HOME':str(tmp_path)}
    observed=subprocess.run(['bash','-c','source "$1"; input_digest_doc "$2" vars.tfvars vars.tfvars.json',
                             'owned-input-digest',str(fixture),str(tmp_path)],cwd=tmp_path,
                             env=environment,capture_output=True,check=True,timeout=10)
    actual=contract.document(observed.stdout)
    assert actual['input_refs']==['varfile:vars.tfvars.json','varfile:vars.tfvars']
    assert actual['input_refs']!=sorted(inputs)
    metadata['inputs']=actual
    result=contract.verify_plan_originals(original(metadata),plan,inputs,context)
    assert result['candidateQualified'] is False
    wrong_entries=[name+'='+contract.sha256(inputs[name]) for name in sorted(inputs)]
    metadata['inputs']={'input_digest':contract.sha256('\n'.join(wrong_entries).encode()),
                        'input_refs':sorted(inputs)}
    with pytest.raises(contract.Refusal,match='input-closure'):
        contract.verify_plan_originals(original(metadata),plan,inputs,context)
