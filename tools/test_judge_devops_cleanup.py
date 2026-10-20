"""Adversarial source vectors, never signed-approval or live AWS evidence."""
from dataclasses import replace
from datetime import timedelta
import copy
import json

import pytest
from jsonschema import ValidationError

from devops_aws_resource_fate import OriginalRead, Resource, expected_command
from devops_provisioning_contract import CELLS, Refusal, canonical, document, sha256, timestamp
from judge_devops_cleanup import (HandoffContext, OriginContext, RecoveryContext, TerminalContext,
    verify_cleanup_inspection, verify_destroy_recovery, verify_install_handoff, verify_inventory_history,
    verify_retained_origin, verify_state_chain, verify_terminal123)
from test_devops_cleanup_attempt import attempt_vector
from test_devops_provisioning_contract import original
from test_eks_deployment_qualification import cell as existing_journey_cell


def retained_vector():
    originals, attempt = attempt_vector()
    claim_path = '/owned/cleanup-obligations/' + sha256(attempt.plan.operation_id.encode()) + '/claim'
    execution = document(originals['execution-receipt'])
    execution['cleanup']['claim_dir'] = claim_path
    originals['execution-receipt'] = canonical(execution)
    claim = document(originals['claim'])
    claim.update(claim_dir=claim_path, receipt_digest=sha256(originals['execution-receipt']))
    originals['claim'] = canonical(claim)
    attempt = replace(attempt, claim_directory=claim_path,
                      original_hashes={name: sha256(raw) for name, raw in originals.items()})
    schema = canonical({'type': 'object', 'required': ['schema_version'],
                        'properties': {'schema_version': {'const': 'v1'}}})
    origin_originals = {'plan': originals['saved-plan'], 'plan-metadata': originals['metadata'],
                       'approval': originals['approval'], 'cleanup-plan-schema': schema,
                       'cleanup-exec-schema': schema}
    refs = [{'kind': kind, 'reference': 'urn:sha256:' + sha256(raw), 'sha256': sha256(raw),
             'byteLength': len(raw)} for kind, raw in origin_originals.items()]
    # Literal production DTO shape; no call to the consumer's projection helper.
    metadata = document(originals['metadata'])
    dto = {'PlanMetadataDigest': metadata['plan_metadata_digest'], 'Action': 'apply',
        'ExpiresAtUtc': '2026-10-20T00:30:00+00:00', 'TerraformRoot': '/owned/iac/examples/aws-ecs',
        'IacRevision': '1' * 40, 'IacTreeDigest': '2' * 64, 'TerraformVersion': '1.13.5',
        'ProviderLockDigest': '3' * 64, 'BackendConfigDigest': 'b' * 64, 'BackendKind': 's3',
        'BackendIsRemote': True, 'Workspace': 'default', 'ObjectKey': 'owned/cell.tfstate',
        'BackendRegion': 'us-west-2', 'AccountId': '123456789012', 'Partition': None,
        'AssumedRoleArn': 'arn:aws:sts::123456789012:assumed-role/exec/owned',
        'RoleId': None, 'Issuer': None, 'CredentialKind': 'sts-assumed-role', 'EvidenceMode': 'live',
        'InputDigest': metadata['inputs']['input_digest'], 'StateLineageBefore': 'original-lineage',
        'StateSerialBefore': 4, 'SavedPlanSha256': sha256(originals['saved-plan']), 'ReleaseQualified': True,
        'ActorId': attempt.plan.actor_id, 'TargetId': 'aws-ecs:it', 'CandidateDigest': None,
        'WorkloadIdentityContractDigest': None, 'BackendBucketArn': 'arn:aws:s3:::owned-state',
        'BackendLockingKind': 's3-native', 'BackendLockingDetail': 'use_lockfile'}
    value = {'SchemaVersion': 'honua.devops.cleanup-obligation/v1',
        'ProvisioningOperationId': attempt.plan.operation_id, 'Stack': 'aws-ecs', 'Environment': 'it',
        'HostBinding': 'c' * 64, 'PlanPath': attempt.plan_path, 'IacRoot': '/owned/iac',
        'ApplyScript': '/owned/iac/scripts/terraform-exact-apply.sh',
        'ApplyScriptSha256': 'd' * 64, 'HelperSha256': 'e' * 64,
        'ClaimDirectory': claim_path, 'Metadata': dto, 'EvidenceRefs': refs}
    context = OriginContext('c' * 64, '/owned/iac', 'd' * 64, 'e' * 64,
                            {name: sha256(raw) for name, raw in origin_originals.items()})
    return value, origin_originals, originals, attempt, context


def test_original_retained_origin_joins_without_requiring_the_plan_directory():
    value, refs, _, attempt, context = retained_vector()
    result = verify_retained_origin(canonical(value), refs, attempt, context)
    assert result['status'] == 'retained-origin-joined'
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


@pytest.mark.parametrize('change', ['owner', 'operation', 'plan-path', 'claim-path', 'script',
    'helper', 'metadata-action', 'metadata-serial-bool', 'metadata-expiry', 'extra-dto',
    'reference-kind', 'reference-bytes', 'reference-length-bool', 'extra-original', 'changed-original'])
def test_retained_origin_refuses_substitution_even_with_plausible_normalized_metadata(change):
    value, refs, _, attempt, context = retained_vector()
    if change == 'owner': value['HostBinding'] = 'f' * 64
    if change == 'operation': value['ProvisioningOperationId'] = 'urn:honua:provisioning:' + '9' * 32
    if change == 'plan-path': value['PlanPath'] = '/other/old-plan'
    if change == 'claim-path': value['ClaimDirectory'] = '/other/claim'
    if change == 'script': value['ApplyScriptSha256'] = 'f' * 64
    if change == 'helper': value['HelperSha256'] = None
    if change == 'metadata-action': value['Metadata']['Action'] = 'destroy'
    if change == 'metadata-serial-bool': value['Metadata']['StateSerialBefore'] = True
    if change == 'metadata-expiry': value['Metadata']['ExpiresAtUtc'] = '2026-10-20T00:31:00Z'
    if change == 'extra-dto': value['Metadata']['accepted'] = True
    if change == 'reference-kind': value['EvidenceRefs'][0]['kind'] = 'other'
    if change == 'reference-bytes': value['EvidenceRefs'][0]['sha256'] = 'f' * 64
    if change == 'reference-length-bool': value['EvidenceRefs'][0]['byteLength'] = True
    if change == 'extra-original': refs['other'] = b'other'
    if change == 'changed-original': refs['approval'] += b' '
    with pytest.raises(Refusal):
        verify_retained_origin(canonical(value), refs, attempt, context)


def inspection_vector():
    value, refs, originals, attempt, context = retained_vector()
    all_originals = {**originals, **{name: refs[name] for name in
                                    ('cleanup-plan-schema', 'cleanup-exec-schema')}}
    args = {'stack': 'aws-ecs', 'size': 'small', 'action': 'inspect-cleanup', 'variablesJson': '{}',
        'confirmed': False, 'confirmation': '', 'approvalReceiptJson': '', 'idempotencyKey': 'source-vector',
        'provisioningOperationId': attempt.plan.operation_id}
    request = {'jsonrpc': '2.0', 'id': 'source-1', 'method': 'tools/call',
               'params': {'name': 'provision_infrastructure', 'arguments': copy.deepcopy(args)}}
    evidence = {'ProvisioningOperationId': attempt.plan.operation_id, 'State': 'cleanup-execution-acknowledged',
        'Action': 'apply', 'PlanMetadataDigest': document(originals['metadata'])['plan_metadata_digest'],
        'PlanSha256': sha256(originals['saved-plan']), 'ApprovalSha256': sha256(originals['approval']),
        'ExecutionReceiptSha256': sha256(originals['execution-receipt']), 'EvidenceRefs': value['EvidenceRefs'],
        'CleanupOutstanding': True, 'CandidateQualified': False}
    response = {'jsonrpc': '2.0', 'id': 'source-1', 'result': {'structuredContent': {
        'Status': 'cleanup-execution-acknowledged', 'provisioningCleanup': evidence}}}
    kwargs = {'request_id': 'source-1', 'arguments': args, 'origin_bytes': canonical(value),
        'before_restart': canonical(value), 'originals': all_originals, 'attempt': attempt,
        'origin_context': context}
    return request, response, kwargs


def test_real_inspect_shape_rejoins_all_originals_and_never_means_resource_absence():
    request, response, kwargs = inspection_vector()
    result = verify_cleanup_inspection(canonical(request), canonical(response), **kwargs)
    assert result['status'] == 'cleanup-inspection-joined'
    assert result['attempt']['cleanupOutstanding'] is True
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


@pytest.mark.parametrize('change', ['changed-origin', 'wrong-id', 'other-tool', 'confirmed',
    'live', 'receipt-substitution', 'cleanup-complete', 'candidate-qualified', 'ambiguous-alias'])
def test_inspection_cannot_turn_an_acknowledgement_into_a_cleanup_or_readiness_verdict(change):
    request, response, kwargs = inspection_vector()
    evidence = response['result']['structuredContent']['provisioningCleanup']
    if change == 'changed-origin': kwargs['before_restart'] += b' '
    if change == 'wrong-id': response['id'] = 'other'
    if change == 'other-tool': request['params']['name'] = 'other'
    if change == 'confirmed': kwargs['arguments']['confirmed'] = True
    if change == 'live': kwargs['attempt'] = replace(kwargs['attempt'], terminal_at=None)
    if change == 'receipt-substitution': evidence['ExecutionReceiptSha256'] = 'f' * 64
    if change == 'cleanup-complete': evidence['CleanupOutstanding'] = False
    if change == 'candidate-qualified': evidence['CandidateQualified'] = True
    if change == 'ambiguous-alias': evidence['candidateQualified'] = False
    with pytest.raises(Refusal):
        verify_cleanup_inspection(canonical(request), canonical(response), **kwargs)


def destroy_vector(previous_originals, previous):
    originals = copy.deepcopy(previous_originals)
    operation = 'urn:honua:provisioning:' + '5' * 32
    actor = 'honua-devops:' + operation
    metadata = document(originals['metadata'])
    metadata.update(action='destroy', actor_id=actor,
                    state_before=document(originals['execution-receipt'])['state_after'])
    originals['saved-plan'] = b'different exact destroy binary plan'
    metadata['plan']['sha256'] = sha256(originals['saved-plan'])
    originals['metadata'] = original(metadata)
    metadata = document(originals['metadata'])
    approval = document(originals['approval'])
    approval.update(provisioningOperationId=operation, action='destroy', approvalReceiptId='new-source-only',
                    planSha256=metadata['plan']['sha256'], planMetadataDigest=metadata['plan_metadata_digest'])
    originals['approval'] = canonical(approval)
    receipt = document(originals['execution-receipt'])
    receipt.update(action='destroy', actor_id=actor, saved_plan_sha256=metadata['plan']['sha256'],
        plan_metadata_digest=metadata['plan_metadata_digest'], approved_digest=metadata['plan_metadata_digest'],
        state_before=metadata['state_before'], state_after={'lineage': 'original-lineage', 'serial': 6},
        completed_at_utc='2026-10-20T00:20:00Z')
    originals['execution-receipt'] = canonical(receipt)
    claim = document(originals['claim'])
    claim.update(acquired_at_utc='2026-10-20T00:12:00Z', completed_at_utc='2026-10-20T00:20:00Z',
                 receipt_digest=sha256(originals['execution-receipt']))
    originals['claim'] = canonical(claim)
    context = replace(previous, plan=replace(previous.plan, operation_id=operation, actor_id=actor, action='destroy'),
        acquired_at=timestamp('2026-10-20T00:12:00Z'), terminal_at=timestamp('2026-10-20T00:21:00Z'),
        mutation_started_at=timestamp('2026-10-20T00:13:00Z'),
        accepted_approval_sha256=sha256(originals['approval']),
        original_hashes={name: sha256(raw) for name, raw in originals.items()})
    return originals, context


def test_state_chain_allows_distinct_production_operation_actors_but_does_not_invent_retry_intent():
    originals, context = attempt_vector()
    successor, next_context = destroy_vector(originals, context)
    assert context.plan.actor_id != next_context.plan.actor_id
    result = verify_state_chain(originals, context, successor, next_context)
    assert result['status'] == 'state-chain-joined'
    assert result['originalDestroyIntentVerified'] is False
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def test_state_chain_uses_the_authenticated_opaque_actor_not_an_ordinary_operation_name_template():
    originals, context = attempt_vector()
    successor, next_context = destroy_vector(originals, context)
    actor = 'honua-devops:destroy-recovery:' + '4' * 32 + ':' + '4' * 32 + ':' + '5' * 32
    metadata = document(successor['metadata'])
    metadata['actor_id'] = actor
    successor['metadata'] = original(metadata)
    digest = document(successor['metadata'])['plan_metadata_digest']
    approval = document(successor['approval'])
    approval['planMetadataDigest'] = digest
    successor['approval'] = canonical(approval)
    execution = document(successor['execution-receipt'])
    execution.update(actor_id=actor, plan_metadata_digest=digest, approved_digest=digest)
    successor['execution-receipt'] = canonical(execution)
    claim = document(successor['claim'])
    claim['receipt_digest'] = sha256(successor['execution-receipt'])
    successor['claim'] = canonical(claim)
    next_context = replace(next_context, plan=replace(next_context.plan, actor_id=actor),
        accepted_approval_sha256=sha256(successor['approval']),
        original_hashes={name: sha256(raw) for name, raw in successor.items()})
    result = verify_state_chain(originals, context, successor, next_context)
    assert result['status'] == 'state-chain-joined'
    assert result['originalDestroyIntentVerified'] is result['candidateQualified'] is False


def test_state_chain_refuses_an_actor_that_does_not_match_the_independent_expected_context():
    originals, context = attempt_vector()
    successor, next_context = destroy_vector(originals, context)
    next_context = replace(next_context, plan=replace(next_context.plan, actor_id='unrelated-actor'))
    with pytest.raises(Refusal, match='metadata-request-identity'):
        verify_state_chain(originals, context, successor, next_context)


@pytest.mark.parametrize('change', ['overlap', 'same-approval', 'wrong-image', 'wrong-target', 'live-parent'])
def test_state_chain_refuses_overlap_or_other_coordinates(change):
    originals, context = attempt_vector()
    successor, next_context = destroy_vector(originals, context)
    if change == 'overlap': context = replace(context, terminal_at=timestamp('2026-10-20T00:12:30Z'))
    if change == 'same-approval': context = replace(context, accepted_approval_sha256=next_context.accepted_approval_sha256)
    if change == 'wrong-image': next_context = replace(next_context, plan=replace(next_context.plan, candidate_image='other@sha256:' + 'f' * 64))
    if change == 'wrong-target': next_context = replace(next_context, plan=replace(next_context.plan, target_id='foreign'))
    if change == 'live-parent': context = replace(context, terminal_at=None)
    with pytest.raises(Refusal):
        verify_state_chain(originals, context, successor, next_context)


def inventory_vector(cell=CELLS[0]):
    resource = Resource('arn:aws:ec2:us-west-2:123456789012:volume/vol-0123456789abcdef0',
        'ec2-volume', 'vol-0123456789abcdef0', '123456789012', 'us-west-2', 'owned')
    live = OriginalRead(expected_command(resource), 0,
        canonical({'Volumes': [{'VolumeId': resource.identity, 'State': 'available'}]}), b'')
    absent = OriginalRead(expected_command(resource), 0, canonical({'Volumes': []}), b'')
    tag_args = ('aws', 'resourcegroupstaggingapi', 'get-resources', '--region', 'us-west-2',
        '--tag-filters', 'Key=honua-release:run-id,Values=source-vector-run',
        'Key=honua-release:cell,Values=' + cell, '--no-paginate', '--output', 'json')
    pages = (OriginalRead(tag_args, 0, canonical({'ResourceTagMappingList': []}), b''),)
    return {'cell': cell, 'expected': {resource.arn: resource}, 'pre_pages': pages,
        'live': {resource.arn: live}, 'final': {resource.arn: absent}, 'final_pages': pages,
        'external_before': {}, 'region': 'us-west-2', 'run_id': 'source-vector-run'}


def test_resource_history_requires_a_real_live_original_and_independent_final_absence():
    result = verify_inventory_history(**inventory_vector())
    assert result['status'] == 'destroyed' and result['inventory']['survivors'] == []
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def second_resource(value):
    resource = Resource('arn:aws:ec2:us-west-2:123456789012:volume/vol-1123456789abcdef0',
        'ec2-volume', 'vol-1123456789abcdef0', '123456789012', 'us-west-2', 'owned')
    value['expected'][resource.arn] = resource
    value['final'][resource.arn] = OriginalRead(expected_command(resource), 0,
                                              canonical({'Volumes': []}), b'')
    return resource


def test_completed_apply_requires_the_entire_owned_resource_observation_census():
    value = inventory_vector()
    second_resource(value)
    with pytest.raises(Refusal, match='completed-apply-resource-census'):
        verify_inventory_history(**value)


@pytest.mark.parametrize('cell', [CELLS[1], CELLS[2]])
def test_partial_apply_may_stop_after_one_resource_without_claiming_other_resources_were_created(cell):
    value = inventory_vector(cell)
    uncreated = second_resource(value)
    result = verify_inventory_history(**value)
    assert result['status'] == 'destroyed'
    assert uncreated.arn not in result['observedLiveOwnedArns']
    assert len(result['observedLiveOwnedArns']) == 1
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def test_inspection_refuses_unrecorded_extra_original_even_when_other_joins_match():
    request, response, kwargs = inspection_vector()
    kwargs['originals']['unrecorded'] = b'not captured by the owned recorder'
    with pytest.raises(Refusal, match='inspection-original-closure'):
        verify_cleanup_inspection(canonical(request), canonical(response), **kwargs)


@pytest.mark.parametrize('change,expected', [('survivor', 'cleanup-failed'), ('api-error', 'inventory-indeterminate')])
def test_survivor_and_api_failure_have_distinct_non_green_verdicts(change, expected):
    value = inventory_vector()
    arn = next(iter(value['expected']))
    value['final'][arn] = (value['live'][arn] if change == 'survivor' else
        replace(value['final'][arn], exit_code=255, stderr=b'AccessDenied'))
    result = verify_inventory_history(**value)
    assert result['status'] == expected
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def test_external_bucket_is_bound_to_owner_and_region_and_must_remain_live():
    value = inventory_vector(CELLS[5])
    bucket = Resource('arn:aws:s3:::retained-source-vector', 's3-bucket', 'retained-source-vector',
                      '123456789012', 'us-west-2', 'externally-owned')
    read = OriginalRead(expected_command(bucket), 0, canonical({'LocationConstraint': 'us-west-2'}), b'')
    value['expected'][bucket.arn] = bucket
    value['external_before'][bucket.arn] = read
    value['final'][bucket.arn] = read
    result = verify_inventory_history(**value)
    assert result['inventory']['retainedExternalResources'] == [bucket.arn]
    value['final'][bucket.arn] = replace(read, stdout=b'', exit_code=254,
        stderr=b'An error occurred (NoSuchBucket) when calling the GetBucketLocation operation: gone')
    with pytest.raises(Refusal, match='external-owned-resource-lost'):
        verify_inventory_history(**value)


def terminal_vector(existing_journey_cell):
    from e2e import cloud_journey

    manifest, _, raw, _, _ = existing_journey_cell
    receipt = json.loads(next(iter(raw['journeyReceiptBytes'].values())))
    receipt['target'].update(id='aws-ecs/redis-on', kind='aws-ecs')
    receipt['server']['image'] = cloud_journey.candidate_image('aws-ecs/redis-on', manifest)
    for stage in receipt['stages']:
        stage['evidence']['source'] = 'live-aws-ecs'
    receipt_bytes = canonical(receipt)
    record = copy.deepcopy(raw['report']['journeyAttempts'][0])
    record.update(cell='aws-ecs/redis-on', receiptSha256=sha256(receipt_bytes))
    record_bytes = canonical(record)
    manifest_bytes = raw['executedManifestYaml'].encode()
    generated = timestamp(receipt['generatedAt'])
    context = TerminalContext('123', '1', sha256(manifest_bytes), sha256(record_bytes), sha256(receipt_bytes),
        receipt['target']['baseUrl'], generated - timedelta(minutes=1), generated + timedelta(minutes=1), generated)
    return record_bytes, receipt_bytes, manifest_bytes, context


def test_complete_original_journey_uses_the_real_unchanged_validator(existing_journey_cell):
    result = verify_terminal123(*terminal_vector(existing_journey_cell))
    assert result['status'] == 'original-terminal123-joined'
    assert result['terminal123Qualified'] is result['candidateQualified'] is False


@pytest.mark.parametrize('change', ['missing-stage', 'blocked-stage', 'wrong-client', 'wrong-server',
    'other-run', 'other-endpoint', 'outside-lifetime', 'changed-manifest', 'changed-receipt'])
def test_terminal123_cannot_be_reduced_relabelled_or_rebound(existing_journey_cell, change):
    record_bytes, receipt_bytes, manifest_bytes, context = terminal_vector(existing_journey_cell)
    receipt = document(receipt_bytes)
    record = document(record_bytes)
    if change == 'missing-stage': receipt['stages'].pop(7)
    if change == 'blocked-stage': receipt['stages'][7]['status'] = 'blocked'
    if change == 'wrong-client': receipt['clientArtifacts']['honua-sdk-js']['version'] = '99.0.0'
    if change == 'wrong-server': receipt['server']['sourceSha'] = 'f' * 40
    if change == 'other-run': record['runId'] = 'other'
    if change == 'other-endpoint': context = replace(context, endpoint='https://other.invalid')
    if change == 'outside-lifetime': context = replace(context, destroy_started_at=context.applied_at)
    if change == 'changed-manifest': manifest_bytes += b'\n'
    receipt_bytes = canonical(receipt)
    record['receiptSha256'] = sha256(receipt_bytes)
    record_bytes = canonical(record)
    context = replace(context, record_sha256=sha256(record_bytes), receipt_sha256=sha256(receipt_bytes))
    if change == 'changed-receipt': receipt_bytes += b' '
    with pytest.raises((Refusal, ValueError, ValidationError)):
        verify_terminal123(record_bytes, receipt_bytes, manifest_bytes, context)


def handoff_vector():
    originals, attempt = attempt_vector()
    metadata = document(originals['metadata'])
    metadata['identity']['partition'] = 'aws'
    originals['metadata'] = original(metadata)
    metadata = document(originals['metadata'])
    approval = document(originals['approval'])
    approval['planMetadataDigest'] = metadata['plan_metadata_digest']
    originals['approval'] = canonical(approval)
    execution = document(originals['execution-receipt'])
    execution.update(plan_metadata_digest=metadata['plan_metadata_digest'],
                     approved_digest=metadata['plan_metadata_digest'])
    execution['workload_identity']['partition'] = 'aws'
    execution['output_contract']['digest'] = 'a' * 64
    originals['execution-receipt'] = canonical(execution)
    claim = document(originals['claim']); claim['receipt_digest'] = sha256(originals['execution-receipt'])
    originals['claim'] = canonical(claim)
    attempt = replace(attempt, accepted_approval_sha256=sha256(originals['approval']),
                      original_hashes={name: sha256(raw) for name, raw in originals.items()})
    metadata = document(originals['metadata'])
    approval = document(originals['approval'])
    refs = [{'kind': name, 'reference': 'urn:sha256:' + sha256(raw), 'sha256': sha256(raw),
             'byteLength': len(raw)} for name, raw in {
        'plan': originals['saved-plan'], 'plan-metadata': originals['metadata'],
        'approval': originals['approval'], 'apply': originals['execution-receipt']}.items()]
    lineage = {'provisioningOperationId': attempt.plan.operation_id,
        'planSha256': sha256(originals['saved-plan']), 'planMetadataDigest': metadata['plan_metadata_digest'],
        'approvalReceiptId': approval['approvalReceiptId'], 'approvalReceiptSha256': sha256(originals['approval']),
        'applyAuditEventId': '9' * 32, 'actuatorReceiptReference': 'urn:sha256:' + sha256(originals['execution-receipt']),
        'handoffReceiptSha256': None, 'handoffVerificationReceiptId': None,
        'handoffVerificationReceiptSha256': None, 'rootProvisioningOperationId': None,
        'serverOperationId': None, 'serverProposalId': None, 'serverDecisionReference': None,
        'serverExecutionId': None, 'releaseReceiptReference': None, 'releaseReceiptSha256': None,
        'evidenceRefs': refs}
    endpoint = 'https://owned.example.invalid'
    secret = 'arn:aws:secretsmanager:us-west-2:123456789012:secret:owned-secret-locator'
    proxy = {'package': '@honua/mcp-server@0.2.6', 'integrity': 'sha512-c291cmNlLXZlY3Rvci1vbmx5'}
    operator = {'digest': 'a' * 64, 'status': 'qualified', 'endpoint': endpoint, 'adminKeySecretRef': secret}
    handoff = {'schemaVersion': 'honua.mcp-proxy.handoff/v1',
        'rootProvisioningOperationId': attempt.plan.operation_id, 'provisioningLineage': copy.deepcopy(lineage),
        'candidateReference': 'source-vector-only-candidate', 'endpointSource': 'operator-contract',
        'adminKeySecretRefSource': 'operator-contract', 'operatorContract': operator, 'proxyArtifact': proxy,
        'command': 'npx', 'args': ['-y', '--package', proxy['package'], 'honua-mcp-proxy'],
        'env': {'HONUA_BASE_URL': endpoint, 'HONUA_MCP_REMOTE_URL': endpoint + '/mcp'},
        'secretRefs': {'HONUA_ADMIN_KEY': secret}, 'capabilityContract': {
            'verification': {'method': 'MCP tools/list', 'failClosed': True}, 'required': [
                {'name': 'operator', 'activation': 'MCP tools/list view=full', 'serverConfiguration': [],
                 'requiredToolPrefixes': [], 'requiredTools': ['source-tool-a', 'source-tool-b']}]}}
    originals['handoff'] = canonical(handoff)
    core = {'schemaVersion': 'honua.devops.install-handoff-verification/v1',
        'provisioningOperationId': attempt.plan.operation_id, 'handoffSha256': sha256(originals['handoff']),
        'candidateReference': 'source-vector-only-candidate', 'proxyPackage': proxy['package'],
        'proxyIntegrity': proxy['integrity'], 'secretReferenceSha256': sha256(secret.encode()),
        'serverIdentity': 'b' * 64, 'serverEndpointIdentity': sha256(endpoint.encode()),
        'observedTools': ['source-tool-a', 'source-tool-b', 'source-tool-c'],
        'observedRosterSha256': sha256(b'source-tool-a\nsource-tool-b\nsource-tool-c'),
        'verifierOutcome': 'install-handoff-verified', 'childExitCode': 137, 'childReaped': True,
        'secretScanPassed': True, 'verifiedAtUtc': '2026-10-20T00:15:00Z'}
    # Deliberately noncanonical: the original core, not a reconstructed object,
    # is what the actual producer puts into its content-addressed evidence store.
    originals['verification-evidence'] = json.dumps(core, indent=1).encode()
    core_sha = sha256(originals['verification-evidence'])
    receipt = {'receiptId': 'urn:sha256:' + core_sha, 'receiptSha256': core_sha, 'evidence': core}
    originals['verification-receipt'] = canonical(receipt)
    completed = copy.deepcopy(lineage)
    completed.update(handoffReceiptSha256=sha256(originals['handoff']),
        handoffVerificationReceiptId=receipt['receiptId'], handoffVerificationReceiptSha256=core_sha,
        rootProvisioningOperationId=attempt.plan.operation_id)
    for kind in ('handoff', 'verification-evidence'):
        raw = originals[kind]
        completed['evidenceRefs'].append({'kind': kind, 'reference': 'urn:sha256:' + sha256(raw),
                                        'sha256': sha256(raw), 'byteLength': len(raw)})
    binding = {'schemaVersion': 'honua.devops.aws-ecs-provision-binding/v1', 'lineage': completed,
        'endpoint': endpoint, 'endpointSource': 'operator-contract', 'adminKeySecretRefSource': 'operator-contract',
        'candidateReference': 'source-vector-only-candidate', 'proxyArtifact': proxy,
        'secretReferenceSha256': sha256(secret.encode()), 'handoffSha256': sha256(originals['handoff']),
        'verificationReceiptId': receipt['receiptId'], 'verificationReceiptSha256': core_sha,
        'operatorContract': {key: operator[key] for key in ('digest', 'status', 'endpoint')},
        'execReceiptSha256': sha256(originals['execution-receipt']), 'execReceipt': execution,
        'iacExecution': {'planMetadataDigest': metadata['plan_metadata_digest'],
            'savedPlanSha256': sha256(originals['saved-plan']), 'terraformRoot': 'examples/aws-ecs',
            'iacRevision': '1' * 40, 'iacTreeDigest': '2' * 64, 'terraformVersion': '1.13.5',
            'providerLockDigest': '3' * 64, 'inputDigest': metadata['inputs']['input_digest'],
            'backend': {'backendConfigDigest': 'b' * 64, 'backendKind': 's3', 'isRemote': True,
                'workspace': 'default', 'objectKey': 'owned/cell.tfstate', 'region': 'us-west-2'},
            'executionIdentity': {'assumedRoleArn': 'arn:aws:sts::123456789012:assumed-role/exec/owned',
                'roleId': None, 'accountId': '123456789012', 'partition': 'aws', 'issuer': None,
                'credentialKind': 'sts-assumed-role'},
            'state': {'lineageBefore': 'original-lineage', 'serialBefore': 4,
                      'lineageAfter': 'original-lineage', 'serialAfter': 5},
            'operatorContractDigest': 'a' * 64, 'evidenceMode': 'live', 'releaseQualified': True},
        'teardownHandle': {'kind': 'honua.iac.terraform-teardown/v1',
            'terraformRoot': '/owned/iac/examples/aws-ecs', 'action': 'destroy', 'workspace': 'default',
            'backendConfigDigest': 'b' * 64, 'objectKey': 'owned/cell.tfstate',
            'stateLineage': 'original-lineage', 'stateSerial': 5,
            'accountId': '123456789012', 'region': 'us-west-2'}}
    originals['provision-binding'] = canonical(binding)
    originals['handoff-schema'] = canonical({'type': 'object', 'required': ['provisioningLineage', 'env']})
    originals['binding-schema'] = canonical({'type': 'object', 'required': ['lineage', 'teardownHandle']})
    originals['verification-schema'] = canonical({'type': 'object', 'required': ['receiptId', 'evidence']})
    context = HandoffContext(endpoint, 'source-vector-only-candidate', proxy['package'], proxy['integrity'],
        secret, 'a' * 64, ('source-tool-a', 'source-tool-b'), 'examples/aws-ecs', 'b' * 64, lineage,
        timestamp('2026-10-20T00:12:00Z'), timestamp('2026-10-20T00:16:00Z'),
        {name: sha256(raw) for name, raw in originals.items()})
    arguments = {'handoffConfigPath': '/owned/handoff/honua-mcp-proxy.handoff.json', 'overwrite': False}
    request = {'jsonrpc': '2.0', 'id': 'owned-handoff', 'method': 'tools/call',
        'params': {'name': 'verify_install_handoff', 'arguments': copy.deepcopy(arguments)}}
    response = {'jsonrpc': '2.0', 'id': 'owned-handoff', 'result': {'structuredContent': {
        'Status': 'install-handoff-verified', 'provisioningLineage': copy.deepcopy(completed)}}}
    return originals, attempt, context, request, response, arguments


def test_actual_handoff_dto_joins_noncanonical_core_and_normally_reaped_proxy_exit():
    originals, attempt, context, request, response, arguments = handoff_vector()
    result = verify_install_handoff(originals, attempt, context, request_bytes=canonical(request),
        response_bytes=canonical(response), request_id='owned-handoff', arguments=arguments)
    assert result['status'] == 'install-handoff-originals-joined'
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is result['terminal123Qualified'] is False


@pytest.mark.parametrize('change', ['extra-original', 'wrong-custody', 'endpoint', 'candidate',
    'proxy-integrity', 'secret-locator', 'apply-lineage', 'secret-env', 'override', 'fail-closed-int',
    'self-referencing-handoff', 'core-reserialization', 'core-handoff', 'core-roster', 'core-identity',
    'core-child', 'core-scan', 'core-time', 'core-exit-bool', 'binding-owner', 'binding-state',
    'binding-teardown', 'binding-receipt', 'binding-reference', 'binding-extra', 'receipt-schema',
    'wrong-tool', 'wrong-id', 'rpc-error', 'response-lineage'])
def test_coherently_rehashed_handoff_substitutions_do_not_qualify(change):
    originals, attempt, context, request, response, arguments = handoff_vector()
    if change == 'extra-original': originals['extra'] = b'unrequested'
    if change == 'endpoint': context = replace(context, endpoint='https://foreign.example.invalid')
    if change == 'candidate': context = replace(context, candidate_reference='foreign')
    if change == 'proxy-integrity': context = replace(context, proxy_integrity='sha512-Zm9yZWlnbg==')
    if change == 'secret-locator': context = replace(context, secret_reference=context.secret_reference + '-foreign')
    if change == 'apply-lineage': context = replace(context, apply_lineage=dict(context.apply_lineage, applyAuditEventId='f' * 32))
    handoff = document(originals['handoff'])
    if change == 'secret-env': handoff['env']['HONUA_ADMIN_KEY'] = 'unrequested-secret-material'
    if change == 'override': handoff['endpointSource'] = 'caller-override'
    if change == 'fail-closed-int': handoff['capabilityContract']['verification']['failClosed'] = 1
    if change == 'self-referencing-handoff': handoff['provisioningLineage']['evidenceRefs'].append({
        'kind': 'handoff', 'reference': 'urn:sha256:' + 'f' * 64, 'sha256': 'f' * 64, 'byteLength': 1})
    if change in ('secret-env', 'override', 'fail-closed-int', 'self-referencing-handoff'):
        originals['handoff'] = canonical(handoff)
    core = document(originals['verification-evidence'])
    if change == 'core-reserialization': originals['verification-evidence'] = canonical(core)
    if change == 'core-handoff': core['handoffSha256'] = 'f' * 64
    if change == 'core-roster': core['observedTools'].remove('source-tool-b')
    if change == 'core-identity': core['serverIdentity'] = 'f' * 64
    if change == 'core-child': core['childReaped'] = False
    if change == 'core-scan': core['secretScanPassed'] = False
    if change == 'core-time': core['verifiedAtUtc'] = '2026-10-20T00:17:00Z'
    if change == 'core-exit-bool': core['childExitCode'] = False
    if change.startswith('core-') and change != 'core-reserialization':
        originals['verification-evidence'] = canonical(core)
    binding = document(originals['provision-binding'])
    if change == 'binding-owner': binding['iacExecution']['executionIdentity']['accountId'] = '999999999999'
    if change == 'binding-state': binding['iacExecution']['state']['serialAfter'] = 6
    if change == 'binding-teardown': binding['teardownHandle']['objectKey'] = 'foreign/state'
    if change == 'binding-receipt': binding['execReceipt']['state_after']['serial'] = 6
    if change == 'binding-reference': binding['lineage']['evidenceRefs'][-1]['byteLength'] += 1
    if change == 'binding-extra': binding['qualified'] = True
    if change.startswith('binding-'): originals['provision-binding'] = canonical(binding)
    if change == 'receipt-schema': originals['verification-schema'] = canonical({'type': 'null'})
    if change == 'wrong-tool': request['params']['name'] = 'provision_infrastructure'
    if change == 'wrong-id': response['id'] = 'foreign'
    if change == 'rpc-error': response['error'] = {'code': -32603, 'message': 'source-vector'}
    if change == 'response-lineage': response['result']['structuredContent']['provisioningLineage']['applyAuditEventId'] = 'f' * 32
    context = replace(context, original_hashes={name: sha256(raw) for name, raw in originals.items()})
    if change == 'wrong-custody': context = replace(context, original_hashes=dict(context.original_hashes, handoff='f' * 64))
    with pytest.raises(Refusal):
        verify_install_handoff(originals, attempt, context, request_bytes=canonical(request),
            response_bytes=canonical(response), request_id='owned-handoff', arguments=arguments)


def recovery_vector(missing_receipt=False):
    origin, origin_refs, previous_raw, previous, origin_context = retained_vector()
    metadata = document(previous_raw['metadata']); metadata['action'] = 'destroy'
    old_input = previous.plan.input_refs[0]
    variable = '/owned/plans/original/small.auto.tfvars.json'
    previous_raw['varfile:' + variable] = previous_raw.pop(old_input)
    metadata['inputs'] = {'input_refs': ['varfile:' + variable],
        'input_digest': sha256(('varfile:' + variable + '=' + sha256(previous_raw['varfile:' + variable])).encode())}
    previous_raw['metadata'] = original(metadata); metadata = document(previous_raw['metadata'])
    approval = document(previous_raw['approval'])
    approval.update(action='destroy', planMetadataDigest=metadata['plan_metadata_digest'])
    previous_raw['approval'] = canonical(approval)
    execution = document(previous_raw['execution-receipt'])
    execution.update(action='destroy', plan_metadata_digest=metadata['plan_metadata_digest'],
        approved_digest=metadata['plan_metadata_digest'], status='failed', exit_status=1)
    previous_raw['execution-receipt'] = canonical(execution)
    claim = document(previous_raw['claim']); claim['receipt_digest'] = sha256(previous_raw['execution-receipt'])
    previous_raw['claim'] = canonical(claim)
    previous = replace(previous, plan=replace(previous.plan, action='destroy', input_refs=('varfile:' + variable,)),
        exit_code=1, accepted_approval_sha256=sha256(previous_raw['approval']),
        original_hashes={name: sha256(raw) for name, raw in previous_raw.items()})
    origin['Metadata'].update(Action='destroy', PlanMetadataDigest=metadata['plan_metadata_digest'],
                              InputDigest=metadata['inputs']['input_digest'])
    origin_refs.update({'plan-metadata': previous_raw['metadata'], 'approval': previous_raw['approval']})
    def references(values):
        return [{'kind': name, 'reference': 'urn:sha256:' + sha256(raw), 'sha256': sha256(raw),
                 'byteLength': len(raw)} for name, raw in values.items()]
    origin['EvidenceRefs'] = references(origin_refs)
    origin_context = replace(origin_context, original_hashes={name: sha256(raw) for name, raw in origin_refs.items()})
    next_raw, successor = destroy_vector(previous_raw, previous)
    next_plan_path = '/owned/plans/successor/honua.tfplan'
    next_variable = '/owned/plans/successor/small.auto.tfvars.json'
    actor = 'honua-devops:destroy-recovery:' + '4' * 32 + ':' + '4' * 32 + ':' + '5' * 32
    next_raw['varfile:' + next_variable] = next_raw.pop('varfile:' + variable)
    next_metadata = document(next_raw['metadata']); next_metadata['actor_id'] = actor
    next_metadata['inputs'] = {'input_refs': ['varfile:' + next_variable],
        'input_digest': sha256(('varfile:' + next_variable + '=' + sha256(next_raw['varfile:' + next_variable])).encode())}
    next_raw['metadata'] = original(next_metadata); next_metadata = document(next_raw['metadata'])
    next_claim_path = '/owned/cleanup-obligations/' + sha256(successor.plan.operation_id.encode()) + '/claim'
    next_execution = document(next_raw['execution-receipt'])
    next_execution.update(actor_id=actor, plan_metadata_digest=next_metadata['plan_metadata_digest'],
        approved_digest=next_metadata['plan_metadata_digest'], exit_status=0, status='succeeded')
    next_execution['cleanup'].update(claim_dir=next_claim_path, saved_plan=next_plan_path)
    next_raw['execution-receipt'] = canonical(next_execution)
    next_claim = document(next_raw['claim'])
    next_claim.update(claim_dir=next_claim_path, receipt_digest=sha256(next_raw['execution-receipt']))
    next_raw['claim'] = canonical(next_claim)
    next_approval = document(next_raw['approval'])
    next_approval['planMetadataDigest'] = next_metadata['plan_metadata_digest']
    if missing_receipt:
        del previous_raw['execution-receipt']
        claim.update(state='reconciliation-required', phase='reconciliation-required',
                     completed_at_utc=None, receipt_digest=None)
        previous_raw['claim'] = canonical(claim)
        previous = replace(previous, exit_code=None,
            original_hashes={name: sha256(raw) for name, raw in previous_raw.items()})
    parent_obligation = canonical(origin)
    parent_originals = dict(origin_refs, **{'recovery-parent-obligation': parent_obligation,
                                          'recovery-parent-claim': previous_raw['claim']})
    if not missing_receipt: parent_originals['recovery-parent-execution'] = previous_raw['execution-receipt']
    value = {'SchemaVersion': 'honua.devops.destroy-recovery/v1',
        'RootProvisioningOperationId': previous.plan.operation_id,
        'ParentProvisioningOperationId': previous.plan.operation_id,
        'ParentObligationSha256': sha256(parent_obligation), 'ParentRecoveryContextSha256': None,
        'ParentPlanMetadataDigest': metadata['plan_metadata_digest'],
        'ParentApprovalSha256': sha256(previous_raw['approval']),
        'ParentExecutionReceiptSha256': None if missing_receipt else sha256(previous_raw['execution-receipt']),
        'ParentClaimSha256': sha256(previous_raw['claim']), 'HostBinding': origin_context.host_binding,
        'Stack': 'aws-ecs', 'Environment': 'it', 'ParentMetadata': copy.deepcopy(origin['Metadata']),
        'ProvisioningOperationId': successor.plan.operation_id,
        'PlanSha256': sha256(next_raw['saved-plan']), 'PlanMetadataDigest': next_metadata['plan_metadata_digest'],
        'StateLineage': 'original-lineage', 'MinimumStateSerial': 4 if missing_receipt else 5,
        'ParentOriginals': references(parent_originals)}
    recovery_raw = canonical(value)
    reference = references({'destroy-recovery': recovery_raw})[0]
    next_approval['recoveryContextSha256'] = sha256(recovery_raw)
    next_raw['approval'] = canonical(next_approval)
    successor = replace(successor, plan=replace(successor.plan, actor_id=actor,
        input_refs=('varfile:' + next_variable,)), exit_code=0, plan_path=next_plan_path,
        claim_directory=next_claim_path, accepted_approval_sha256=sha256(next_raw['approval']),
        original_hashes={name: sha256(raw) for name, raw in next_raw.items()})
    successor_refs = dict(origin_refs, **{'plan': next_raw['saved-plan'], 'plan-metadata': next_raw['metadata'],
                                        'approval': next_raw['approval'], 'destroy-recovery': recovery_raw})
    fresh = copy.deepcopy(origin)
    fresh.update(ProvisioningOperationId=successor.plan.operation_id, PlanPath=next_plan_path,
        ClaimDirectory=next_claim_path, EvidenceRefs=references(successor_refs), Recovery=reference)
    fresh['Metadata'].update(PlanMetadataDigest=next_metadata['plan_metadata_digest'],
        SavedPlanSha256=sha256(next_raw['saved-plan']), ActorId=actor, StateSerialBefore=5,
        InputDigest=next_metadata['inputs']['input_digest'])
    fresh_context = replace(origin_context, original_hashes={name: sha256(raw) for name, raw in successor_refs.items()})
    head = {'RootProvisioningOperationId': previous.plan.operation_id,
        'ParentProvisioningOperationId': previous.plan.operation_id,
        'ProvisioningOperationId': successor.plan.operation_id, 'RequestDigest': 'a' * 64,
        'Context': reference, 'Response': {'Status': 'terraform-destroy-plan-ready',
            'provisioningRecovery': {'rootProvisioningOperationId': previous.plan.operation_id,
                'parentProvisioningOperationId': previous.plan.operation_id,
                'provisioningOperationId': successor.plan.operation_id,
                'contextSha256': sha256(recovery_raw), 'evidenceRef': reference},
            'approvalRequest': {'provisioningOperationId': successor.plan.operation_id,
                'planSha256': sha256(next_raw['saved-plan']),
                'planMetadataDigest': next_metadata['plan_metadata_digest'], 'action': 'destroy',
                'stack': 'aws-ecs', 'environment': 'it',
                'receiptSchemaVersion': 'honua.devops.provision-approval/v1',
                'recoveryContextSha256': sha256(recovery_raw)}}}
    originals = dict(origin_refs, **{'recovery-context': recovery_raw, 'parent-obligation': parent_obligation,
        'successor-obligation': canonical(fresh), 'attempt-index': canonical(reference), 'root-head': canonical(head)})
    context = RecoveryContext(previous.plan.operation_id, 'a' * 64,
        {name: sha256(raw) for name, raw in originals.items()})
    kwargs = {'previous_originals': previous_raw, 'previous': previous,
        'successor_originals': next_raw, 'successor': successor,
        'parent_origin_context': origin_context, 'successor_origin_context': fresh_context}
    return originals, context, kwargs


@pytest.mark.parametrize('missing_receipt', [False, True])
def test_actual_signed_destroy_recovery_keeps_missing_original_receipt_indeterminate(missing_receipt):
    originals, context, kwargs = recovery_vector(missing_receipt)
    result = verify_destroy_recovery(originals, context, **kwargs)
    assert result['status'] == 'signed-destroy-recovery-originals-joined'
    assert result['stateChain']['prior']['status'] == ('indeterminate' if missing_receipt else 'execution-joined')
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


@pytest.mark.parametrize('change', ['wrong-custody', 'extra-original', 'parent-live', 'successful-parent',
    'parent-role', 'parent-host', 'parent-claim', 'parent-receipt', 'parent-original-length',
    'wrong-root', 'wrong-parent', 'serial-backslide', 'plan-digest', 'ordinary-actor',
    'missing-approval-context', 'fresh-var-bytes', 'missing-index', 'wrong-index',
    'superseded-head', 'head-context', 'head-request', 'response-context', 'response-approval',
    'response-status', 'missing-origin-recovery', 'missing-origin-reference', 'origin-host'])
def test_recovery_refuses_foreign_or_unsigned_relationship_without_inventing_intent(change):
    originals, context, kwargs = recovery_vector()
    if change == 'parent-live': kwargs['previous'] = replace(kwargs['previous'], terminal_at=None)
    if change == 'successful-parent': kwargs['previous'] = replace(kwargs['previous'], exit_code=0)
    value = document(originals['recovery-context'])
    if change == 'parent-role': value['ParentMetadata']['RoleId'] = 'foreign-role'
    if change == 'parent-host': value['HostBinding'] = 'f' * 64
    if change == 'parent-claim': value['ParentClaimSha256'] = 'f' * 64
    if change == 'parent-receipt': value['ParentExecutionReceiptSha256'] = None
    if change == 'parent-original-length': value['ParentOriginals'][0]['byteLength'] += 1
    if change == 'wrong-root': value['RootProvisioningOperationId'] = 'urn:honua:provisioning:' + '9' * 32
    if change == 'wrong-parent': value['ParentProvisioningOperationId'] = value['ProvisioningOperationId']
    if change == 'serial-backslide': value['MinimumStateSerial'] = 4
    if change == 'plan-digest': value['PlanMetadataDigest'] = 'f' * 64
    if canonical(value) != originals['recovery-context']: originals['recovery-context'] = canonical(value)
    if change == 'ordinary-actor':
        kwargs['successor'] = replace(kwargs['successor'], plan=replace(kwargs['successor'].plan, actor_id='ordinary'))
    if change == 'missing-approval-context':
        approval = document(kwargs['successor_originals']['approval']); approval.pop('recoveryContextSha256')
        kwargs['successor_originals']['approval'] = canonical(approval)
        kwargs['successor'] = replace(kwargs['successor'], accepted_approval_sha256=sha256(canonical(approval)),
            original_hashes={name: sha256(raw) for name, raw in kwargs['successor_originals'].items()})
    if change == 'fresh-var-bytes':
        name = kwargs['successor'].plan.input_refs[0]
        kwargs['successor_originals'][name] += b' '
        kwargs['successor'] = replace(kwargs['successor'],
            original_hashes={name: sha256(raw) for name, raw in kwargs['successor_originals'].items()})
    if change == 'missing-index': del originals['attempt-index']
    if change == 'wrong-index': originals['attempt-index'] = canonical({'kind': 'destroy-recovery', 'sha256': 'f' * 64})
    head = document(originals['root-head'])
    if change == 'superseded-head': head['ProvisioningOperationId'] = 'urn:honua:provisioning:' + '9' * 32
    if change == 'head-context': head['Context']['byteLength'] += 1
    if change == 'head-request': head['RequestDigest'] = 'f' * 64
    if change == 'response-context': head['Response']['provisioningRecovery']['contextSha256'] = 'f' * 64
    if change == 'response-approval': head['Response']['approvalRequest']['recoveryContextSha256'] = 'f' * 64
    if change == 'response-status': head['Response']['Status'] = 'infrastructure-destroyed'
    originals['root-head'] = canonical(head)
    origin = document(originals['successor-obligation'])
    if change == 'missing-origin-recovery': origin.pop('Recovery')
    if change == 'missing-origin-reference': origin['EvidenceRefs'] = [row for row in origin['EvidenceRefs'] if row['kind'] != 'destroy-recovery']
    if change == 'origin-host': origin['HostBinding'] = 'f' * 64
    originals['successor-obligation'] = canonical(origin)
    if change == 'extra-original': originals['unrequested'] = b'original'
    context = replace(context, original_hashes={name: sha256(raw) for name, raw in originals.items()})
    if change == 'wrong-custody': context = replace(context, original_hashes=dict(context.original_hashes,
                                                                               **{'root-head': 'f' * 64}))
    with pytest.raises(Refusal): verify_destroy_recovery(originals, context, **kwargs)
