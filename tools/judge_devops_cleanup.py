"""Semantic consumers for the original DevOps cleanup qualification.

Originals and expected contexts come from the authenticated release recorder,
not a user-supplied verdict file. These functions neither execute infrastructure
nor authorize it. They join production evidence and the unchanged terminal-123
validator; they do not replace the six scenarios with a custody-only matrix.

The final matrix also needs the owned native process-scope/recovery consumers.
Until that composition is complete, every result here remains unqualified.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Mapping
from urllib.parse import urlsplit

from devops_aws_resource_fate import (OriginalRead, Resource, judge_fate,
                                     tag_census, verdict)
from devops_cleanup_attempt import AttemptContext, verify_attempt
from devops_mcp_observation_contract import field, operation_result, verify_call
from devops_provisioning_contract import (CELLS, SHA, canonical, document,
                                         require, sha256, timestamp)


def _unqualified():
    return {'candidateQualified': False, 'sixAwsCellsQualified': False,
            'terminal123Qualified': False}


def _custody(originals: Mapping[str, bytes], hashes: Mapping[str, str]):
    require(set(originals) == set(hashes) and bool(originals), 'judge-original-closure')
    require(all(type(raw) is bytes and type(hashes[name]) is str
                and SHA.fullmatch(hashes[name]) and sha256(raw) == hashes[name]
                for name, raw in originals.items()), 'judge-original-custody')


def _reference(row, kind, raw):
    require(type(row) is dict and set(row) == {'kind', 'reference', 'sha256', 'byteLength'}
            and row['kind'] == kind and row['sha256'] == sha256(raw)
            and row['reference'] == 'urn:sha256:' + sha256(raw)
            and type(row['byteLength']) is int and row['byteLength'] == len(raw),
            'judge-production-reference')


def _references(rows, originals):
    require(type(rows) is list and len(rows) == len(originals)
            and all(type(row) is dict and type(row.get('kind')) is str for row in rows)
            and len({row['kind'] for row in rows}) == len(rows)
            and {row['kind'] for row in rows} == set(originals), 'judge-reference-closure')
    for row in rows:
        _reference(row, row['kind'], originals[row['kind']])


def _schema_original(raw, schema_bytes):
    # An empty registry refuses external references instead of fetching a URL.
    from jsonschema import Draft202012Validator
    from referencing import Registry

    schema = document(schema_bytes)
    validator = Draft202012Validator(schema, registry=Registry())
    require(not any(validator.iter_errors(document(raw))), 'judge-original-schema')


def _metadata_projection(raw):
    """Actual ExactPlanMetadata DTO persisted by ProvisioningCleanupStore.

    This is a consumer of the production DTO, not an alternative plan schema.
    The canonical metadata has already passed verify_attempt independently.
    """
    value = document(raw)
    sources = {'source': {'TerraformRoot': 'terraform_root', 'IacRevision': 'iac_revision',
                         'IacTreeDigest': 'iac_tree_digest'},
               'toolchain': {'TerraformVersion': 'terraform_version',
                             'ProviderLockDigest': 'provider_lock_digest'},
               'backend': {'BackendConfigDigest': 'backend_config_digest', 'BackendKind': 'backend_kind',
                           'BackendIsRemote': 'is_remote', 'Workspace': 'workspace', 'ObjectKey': 'object_key',
                           'BackendRegion': 'region', 'BackendBucketArn': 'bucket_arn'},
               'identity': {'AccountId': 'account_id', 'Partition': 'partition', 'AssumedRoleArn': 'assumed_role_arn',
                            'RoleId': 'role_id', 'Issuer': 'issuer', 'CredentialKind': 'credential_kind',
                            'EvidenceMode': 'evidence_mode'},
               'inputs': {'InputDigest': 'input_digest'},
               'state_before': {'StateLineageBefore': 'lineage', 'StateSerialBefore': 'serial'},
               'plan': {'SavedPlanSha256': 'sha256'}, 'posture': {'ReleaseQualified': 'release_qualified'}}
    result = {target: value.get(section, {}).get(source)
              for section, pairs in sources.items() for target, source in pairs.items()}
    result.update({target: value.get(source) for target, source in
                   {'PlanMetadataDigest': 'plan_metadata_digest', 'Action': 'action',
                    'ActorId': 'actor_id', 'TargetId': 'target_id', 'CandidateDigest': 'candidate_digest',
                    'WorkloadIdentityContractDigest': 'workload_identity_contract_digest'}.items()})
    result.update(ExpiresAtUtc=value['expires_at_utc'],
                  BackendLockingKind=value['backend']['locking'].get('kind'),
                  BackendLockingDetail=value['backend']['locking'].get('detail'))
    return result


@dataclass(frozen=True)
class OriginContext:
    # All values are independently captured from the owned production host and
    # immutable IaC intake. A matching JSON declaration is not that authority.
    host_binding: str
    iac_root: str
    apply_script_sha256: str
    helper_sha256: str
    original_hashes: Mapping[str, str]


def verify_retained_origin(origin_bytes: bytes, originals: Mapping[str, bytes],
                           attempt: AttemptContext, context: OriginContext):
    """Rejoin durable origin after restart without requiring a live plan path."""
    _custody(originals, context.original_hashes)
    recovery_kinds = {'destroy-recovery'} if 'destroy-recovery' in originals else set()
    require(set(originals) == {'plan', 'plan-metadata', 'approval',
                              'cleanup-plan-schema', 'cleanup-exec-schema'} | recovery_kinds,
            'origin-original-kinds')
    require(sha256(originals['plan']) == attempt.original_hashes['saved-plan']
            and sha256(originals['plan-metadata']) == attempt.original_hashes['metadata']
            and sha256(originals['approval']) == attempt.original_hashes['approval'], 'origin-attempt-originals')
    value = document(origin_bytes)
    require(set(value) == {'SchemaVersion', 'ProvisioningOperationId', 'Stack', 'Environment',
            'HostBinding', 'PlanPath', 'IacRoot', 'ApplyScript', 'ApplyScriptSha256',
            'HelperSha256', 'ClaimDirectory', 'Metadata', 'EvidenceRefs'}
            | ({'Recovery'} if recovery_kinds else set()), 'origin-shape')
    require(value['SchemaVersion'] == 'honua.devops.cleanup-obligation/v1'
            and value['ProvisioningOperationId'] == attempt.plan.operation_id
            and value['Stack'] == 'aws-ecs' and value['Environment'] == attempt.environment,
            'origin-operation')
    require(type(context.host_binding) is str and SHA.fullmatch(context.host_binding)
            and value['HostBinding'] == context.host_binding, 'origin-host-ownership')
    require(type(context.iac_root) is str and context.iac_root.startswith('/')
            and not context.iac_root.endswith('/')
            and value['IacRoot'] == context.iac_root
            and value['ApplyScript'] == context.iac_root + '/scripts/terraform-exact-apply.sh'
            and value['ApplyScriptSha256'] == context.apply_script_sha256
            and value['HelperSha256'] == context.helper_sha256
            and all(type(digest) is str and SHA.fullmatch(digest)
                    for digest in (context.apply_script_sha256, context.helper_sha256)), 'origin-substrate')
    require(value['PlanPath'] == attempt.plan_path
            and value['ClaimDirectory'] == attempt.claim_directory
            and attempt.claim_directory.endswith('/cleanup-obligations/'
                + sha256(attempt.plan.operation_id.encode()) + '/claim'), 'origin-durable-coordinates')
    projection = _metadata_projection(originals['plan-metadata'])
    observed = value['Metadata']
    require(type(observed) is dict and set(observed) == set(projection), 'origin-metadata-shape')
    require(timestamp(observed['ExpiresAtUtc']) == timestamp(projection['ExpiresAtUtc']),
            'origin-metadata-expiry')
    require(canonical({key: item for key, item in observed.items() if key != 'ExpiresAtUtc'})
            == canonical({key: item for key, item in projection.items() if key != 'ExpiresAtUtc'}),
            'origin-metadata-original')
    _references(value['EvidenceRefs'], originals)
    if recovery_kinds:
        _reference(value['Recovery'], 'destroy-recovery', originals['destroy-recovery'])
        recovery = document(originals['destroy-recovery'])
        require(recovery.get('SchemaVersion') == 'honua.devops.destroy-recovery/v1'
                and recovery.get('ProvisioningOperationId') == attempt.plan.operation_id
                and recovery.get('PlanSha256') == sha256(originals['plan'])
                and recovery.get('PlanMetadataDigest') == projection['PlanMetadataDigest']
                and document(originals['approval']).get('recoveryContextSha256')
                    == sha256(originals['destroy-recovery']), 'origin-signed-recovery')
    else:
        require(type(projection['ActorId']) is str
                and not projection['ActorId'].startswith('honua-devops:destroy-recovery:')
                and document(originals['approval']).get('recoveryContextSha256') is None,
                'origin-missing-signed-recovery')
    _schema_original(originals['plan-metadata'], originals['cleanup-plan-schema'])
    return {'status': 'retained-origin-joined', 'operationId': attempt.plan.operation_id,
            'originSha256': sha256(origin_bytes), **_unqualified()}


def verify_cleanup_inspection(request_bytes: bytes, response_bytes: bytes, *, request_id,
                              arguments: dict, origin_bytes: bytes, before_restart: bytes,
                              originals: Mapping[str, bytes], attempt: AttemptContext,
                              origin_context: OriginContext):
    origin_kinds = {'cleanup-plan-schema', 'cleanup-exec-schema'} | (
        {'destroy-recovery'} if 'destroy-recovery' in origin_context.original_hashes else set())
    require(set(originals) == set(attempt.original_hashes) | origin_kinds,
            'inspection-original-closure')
    require(origin_bytes == before_restart, 'inspection-durable-origin-changed')
    origin = verify_retained_origin(origin_bytes, {
        'plan': originals['saved-plan'], 'plan-metadata': originals['metadata'],
        'approval': originals['approval'], **{name: originals[name] for name in
            origin_kinds}}, attempt, origin_context)
    attempt_result = verify_attempt({name: originals[name] for name in attempt.original_hashes}, attempt)
    require(arguments.get('action') == 'inspect-cleanup' and arguments.get('confirmed') is False
            and arguments.get('stack') == 'aws-ecs' and arguments.get('size') == 'small'
            and arguments.get('provisioningOperationId') == attempt.plan.operation_id
            and arguments.get('approvalReceiptJson') == '' and arguments.get('confirmation') == '',
            'inspection-fixed-request')
    require(attempt.terminal_at is not None, 'inspection-executor-not-terminal')
    expected_state = 'cleanup-indeterminate'
    if attempt_result['status'] == 'execution-joined':
        expected_state = ('cleanup-execution-acknowledged' if attempt.exit_code == 0
                          else 'cleanup-execution-failed')
    call = verify_call(request_bytes, response_bytes, request_id=request_id,
                       expected_arguments=arguments, allowed_statuses=(expected_state,))
    operation, _ = operation_result(document(response_bytes))
    inspection = field(operation, 'provisioningCleanup')
    require(type(inspection) is dict, 'inspection-structured-result')
    expected = {'provisioningOperationId': attempt.plan.operation_id, 'state': expected_state,
                'action': attempt.plan.action,
                'planMetadataDigest': document(originals['metadata'])['plan_metadata_digest'],
                'planSha256': sha256(originals['saved-plan']), 'approvalSha256': sha256(originals['approval']),
                'cleanupOutstanding': True, 'candidateQualified': False}
    for name, value in expected.items():
        require(type(field(inspection, name)) is type(value) and field(inspection, name) == value,
                'inspection-original-binding')
    receipt_hash = (sha256(originals['execution-receipt']) if 'execution-receipt' in originals else None)
    require(field(inspection, 'executionReceiptSha256') == receipt_hash, 'inspection-receipt-binding')
    if receipt_hash is not None:
        _schema_original(originals['execution-receipt'], originals['cleanup-exec-schema'])
    _references(field(inspection, 'evidenceRefs'), {
        'plan': originals['saved-plan'], 'plan-metadata': originals['metadata'], 'approval': originals['approval'],
        **{name: originals[name] for name in origin_kinds}})
    return {'status': 'cleanup-inspection-joined', 'origin': origin, 'call': call,
            'attempt': attempt_result, **_unqualified()}


def verify_state_chain(previous_originals: Mapping[str, bytes], previous: AttemptContext,
                       next_originals: Mapping[str, bytes], successor: AttemptContext):
    """Join actual remote-state coordinates; this does not invent a retry relation."""
    before = verify_attempt(previous_originals, previous)
    after = verify_attempt(next_originals, successor)
    require(previous.terminal_at is not None and previous.terminal_at <= successor.acquired_at,
            'state-chain-live-overlap')
    require(successor.plan.action == 'destroy'
            and previous.plan.operation_id != successor.plan.operation_id
            and previous.accepted_approval_sha256 != successor.accepted_approval_sha256,
            'state-chain-fresh-destroy-approval')
    first = document(previous_originals['metadata'])
    second = document(next_originals['metadata'])
    require(first['backend'] == second['backend']
            and previous.plan.account_id == successor.plan.account_id
            and previous.plan.target_id == successor.plan.target_id
            and previous.plan.candidate_image == successor.plan.candidate_image
            and previous.environment == successor.environment
            and first['source'] == second['source'] and first['toolchain'] == second['toolchain'],
            'state-chain-target-backend-source')
    # Each opaque actor has already matched its independently captured expected
    # value in verify_attempt. Owner/intent continuity is separately proven by
    # retained origin and the signed recovery relationship, never a name prefix.
    previous_state = (document(previous_originals['execution-receipt'])['state_after']
                      if 'execution-receipt' in previous_originals else first['state_before'])
    current = second['state_before']
    require(type(current.get('lineage')) is str and bool(current['lineage'])
            and type(current.get('serial')) is int and current['serial'] >= 0,
            'state-chain-current-remote-state')
    if previous_state['lineage'] is not None:
        require(previous_state['lineage'] == current['lineage'], 'state-chain-lineage')
    require(current['serial'] >= previous_state['serial'], 'state-chain-serial-regressed')
    if 'execution-receipt' in next_originals:
        final = document(next_originals['execution-receipt'])['state_after']
        require(final['lineage'] == current['lineage'] and final['serial'] >= current['serial'],
                'state-chain-result-regressed')
    return {'status': 'state-chain-joined', 'prior': before, 'successor': after,
            'originalDestroyIntentVerified': False, **_unqualified()}


@dataclass(frozen=True)
class RecoveryContext:
    root_operation_id: str
    request_digest: str
    original_hashes: Mapping[str, str]


def verify_destroy_recovery(originals: Mapping[str, bytes], context: RecoveryContext, *,
                            previous_originals: Mapping[str, bytes], previous: AttemptContext,
                            successor_originals: Mapping[str, bytes], successor: AttemptContext,
                            parent_origin_context: OriginContext,
                            successor_origin_context: OriginContext):
    """Consume protected DevOps destroy-recovery originals and fresh approval.

    Context hashes/accepted approvals and a head read while the owned root is
    reserved come from the authenticated recorder. This is no MAC verifier or
    compare-and-swap implementation and cannot grant execution authority.
    """
    _custody(originals, context.original_hashes)
    parent_kinds = set(parent_origin_context.original_hashes)
    require(set(originals) == parent_kinds | {'recovery-context', 'attempt-index', 'root-head',
            'parent-obligation', 'successor-obligation'}, 'recovery-original-closure')
    require(previous.plan.action == 'destroy' and previous.exit_code != 0,
            'recovery-parent-not-failed-destroy')
    chain = verify_state_chain(previous_originals, previous, successor_originals, successor)
    parent_origins = {key: originals[key] for key in parent_kinds}
    parent = verify_retained_origin(originals['parent-obligation'], parent_origins,
                                    previous, parent_origin_context)
    require(parent_origin_context.host_binding == successor_origin_context.host_binding
            and parent_origin_context.iac_root == successor_origin_context.iac_root
            and parent_origin_context.apply_script_sha256 == successor_origin_context.apply_script_sha256
            and parent_origin_context.helper_sha256 == successor_origin_context.helper_sha256,
            'recovery-same-owned-substrate')
    origin = document(originals['parent-obligation'])
    prior_metadata = document(previous_originals['metadata'])
    next_metadata = document(successor_originals['metadata'])
    value = document(originals['recovery-context'])
    prior_state = prior_metadata['state_before']
    minimum_serial = prior_state['serial']
    if 'execution-receipt' in previous_originals:
        after = document(previous_originals['execution-receipt'])['state_after']
        minimum_serial = max(minimum_serial, after['serial'])
        require(after['lineage'] is None or prior_state['lineage'] is None
                or after['lineage'] == prior_state['lineage'], 'recovery-parent-lineage-changed')
    parent_recovery = origin.get('Recovery')
    if parent_recovery is None:
        require(context.root_operation_id == previous.plan.operation_id, 'recovery-initial-root')
    else:
        require(document(originals['destroy-recovery']).get('RootProvisioningOperationId')
                == context.root_operation_id, 'recovery-retained-root')
    expected = {'SchemaVersion': 'honua.devops.destroy-recovery/v1',
        'RootProvisioningOperationId': context.root_operation_id,
        'ParentProvisioningOperationId': previous.plan.operation_id,
        'ParentObligationSha256': sha256(originals['parent-obligation']),
        'ParentRecoveryContextSha256': None if parent_recovery is None else parent_recovery['sha256'],
        'ParentPlanMetadataDigest': prior_metadata['plan_metadata_digest'],
        'ParentApprovalSha256': sha256(previous_originals['approval']),
        'ParentExecutionReceiptSha256': (sha256(previous_originals['execution-receipt'])
                                       if 'execution-receipt' in previous_originals else None),
        'ParentClaimSha256': sha256(previous_originals['claim']),
        'HostBinding': parent_origin_context.host_binding, 'Stack': 'aws-ecs',
        'Environment': previous.environment, 'ParentMetadata': origin['Metadata'],
        'ProvisioningOperationId': successor.plan.operation_id,
        'PlanSha256': sha256(successor_originals['saved-plan']),
        'PlanMetadataDigest': next_metadata['plan_metadata_digest'],
        'StateLineage': prior_state['lineage'], 'MinimumStateSerial': minimum_serial}
    require(set(value) == set(expected) | {'ParentOriginals'}
            and canonical({key: value[key] for key in expected}) == canonical(expected),
            'recovery-original-parent-context')
    retained = dict(parent_origins, **{'recovery-parent-obligation': originals['parent-obligation'],
                                     'recovery-parent-claim': previous_originals['claim']})
    if 'execution-receipt' in previous_originals:
        retained['recovery-parent-execution'] = previous_originals['execution-receipt']
    _references(value['ParentOriginals'], retained)
    actor = 'honua-devops:destroy-recovery:' + ':'.join(operation.rsplit(':', 1)[-1] for operation in
        (context.root_operation_id, previous.plan.operation_id, successor.plan.operation_id))
    require(next_metadata['actor_id'] == successor.plan.actor_id == actor,
            'recovery-signed-operation-actor')
    digest = sha256(originals['recovery-context'])
    require(document(successor_originals['approval']).get('recoveryContextSha256') == digest,
            'recovery-fresh-approved-context')
    # Every ordinary identity, owner, backend and toolchain coordinate remains
    # the same; an STS session suffix may refresh under that exact role.
    old_projection, new_projection = (_metadata_projection(raw) for raw in
        (previous_originals['metadata'], successor_originals['metadata']))
    varying = {'PlanMetadataDigest', 'SavedPlanSha256', 'ActorId', 'ExpiresAtUtc',
               'InputDigest', 'StateSerialBefore', 'AssumedRoleArn'}
    require(canonical({key: item for key, item in old_projection.items() if key not in varying})
            == canonical({key: item for key, item in new_projection.items() if key not in varying})
            and old_projection['AssumedRoleArn'].rsplit('/', 1)[0]
                == new_projection['AssumedRoleArn'].rsplit('/', 1)[0], 'recovery-owner-input-source')
    # The generated per-plan varfile moves, but every actual input byte stays.
    old_var = 'varfile:' + str(PurePosixPath(previous.plan_path).parent / 'small.auto.tfvars.json')
    new_var = 'varfile:' + str(PurePosixPath(successor.plan_path).parent / 'small.auto.tfvars.json')
    require(old_var in previous.plan.input_refs, 'recovery-generated-variable-original')
    inputs = {new_var if name == old_var else name: previous_originals[name]
              for name in previous.plan.input_refs}
    require(set(inputs) == set(successor.plan.input_refs)
            and all(successor_originals[name] == raw for name, raw in inputs.items()),
            'recovery-original-input-byte-closure')
    reference = {'kind': 'destroy-recovery', 'reference': 'urn:sha256:' + digest,
                 'sha256': digest, 'byteLength': len(originals['recovery-context'])}
    require(document(originals['attempt-index']) == reference, 'recovery-durable-attempt-index')
    head = document(originals['root-head'])
    require(set(head) == {'RootProvisioningOperationId', 'ParentProvisioningOperationId',
            'ProvisioningOperationId', 'RequestDigest', 'Context', 'Response'}
            and head['RootProvisioningOperationId'] == context.root_operation_id
            and head['ParentProvisioningOperationId'] == previous.plan.operation_id
            and head['ProvisioningOperationId'] == successor.plan.operation_id
            and type(context.request_digest) is str and SHA.fullmatch(context.request_digest)
            and head['RequestDigest'] == context.request_digest and head['Context'] == reference,
            'recovery-current-root-reservation')
    require(type(head['Response']) is dict, 'recovery-durable-plan-response')
    require(field(head['Response'], 'status') == 'terraform-destroy-plan-ready',
            'recovery-durable-plan-status')
    projected = field(head['Response'], 'provisioningRecovery')
    require(projected == {'rootProvisioningOperationId': context.root_operation_id,
        'parentProvisioningOperationId': previous.plan.operation_id,
        'provisioningOperationId': successor.plan.operation_id,
        'contextSha256': digest, 'evidenceRef': reference}, 'recovery-durable-response-context')
    approval_request = field(head['Response'], 'approvalRequest')
    require(type(approval_request) is dict and all(approval_request.get(key) == item for key, item in {
        'provisioningOperationId': successor.plan.operation_id,
        'planSha256': sha256(successor_originals['saved-plan']),
        'planMetadataDigest': next_metadata['plan_metadata_digest'], 'action': 'destroy',
        'stack': 'aws-ecs', 'environment': successor.environment,
        'receiptSchemaVersion': 'honua.devops.provision-approval/v1',
        'recoveryContextSha256': digest}.items()), 'recovery-durable-approval-request')
    successor_origins = {'plan': successor_originals['saved-plan'],
        'plan-metadata': successor_originals['metadata'], 'approval': successor_originals['approval'],
        'cleanup-plan-schema': originals['cleanup-plan-schema'],
        'cleanup-exec-schema': originals['cleanup-exec-schema'],
        'destroy-recovery': originals['recovery-context']}
    fresh = verify_retained_origin(originals['successor-obligation'], successor_origins,
                                   successor, successor_origin_context)
    return {'status': 'signed-destroy-recovery-originals-joined', 'parent': parent,
            'successorOrigin': fresh, 'stateChain': chain, 'recoveryContextSha256': digest,
            **_unqualified()}


def verify_inventory_history(*, cell: str, expected: Mapping[str, Resource],
                             pre_pages: tuple[OriginalRead, ...], live: Mapping[str, OriginalRead],
                             final: Mapping[str, OriginalRead], final_pages: tuple[OriginalRead, ...],
                             external_before: Mapping[str, OriginalRead], region: str, run_id: str,
                             partial: Mapping[str, OriginalRead] | None = None):
    require(cell in CELLS, 'history-cell')
    external = {arn for arn, resource in expected.items() if resource.ownership == 'externally-owned'}
    owned = set(expected) - external
    require(bool(owned) and set(external_before) == external, 'history-resource-ownership-closure')
    require(bool(external) == (cell == CELLS[5])
            and all(expected[arn].kind == 's3-bucket' for arn in external), 'history-external-storage-cell')
    pre = tag_census(pre_pages, region=region, run_id=run_id, cell_id=cell)
    require(set(pre) <= external, 'history-not-a-fresh-owned-cell')
    require(bool(live) and set(live) <= owned, 'history-live-identity-closure')
    observed = {arn: verdict(expected[arn], raw)['verdict'] for arn, raw in live.items()}
    require('live' in observed.values() and 'unverified' not in observed.values(),
            'history-no-owned-resource-observed-live')
    if cell not in (CELLS[1], CELLS[2]):
        # These scenarios require a completed apply. Partial-apply fault/cancel
        # scenarios instead interrupt after at least one resource exists; they
        # must not require every planned resource to have been created first.
        require(set(live) == owned and all(state in ('live', 'settled') for state in observed.values()),
                'history-completed-apply-resource-census')
    for arn, raw in external_before.items():
        require(verdict(expected[arn], raw)['verdict'] == 'live', 'history-external-storage-not-observed')
    require((partial is not None) == (cell in (CELLS[3], CELLS[4])), 'history-partial-destroy-scenario')
    if partial is not None:
        require(set(partial) == owned, 'history-partial-identity-closure')
        states = [verdict(expected[arn], raw)['verdict'] for arn, raw in partial.items()]
        require('unverified' not in states and 'live' in states
                and any(state in ('absent', 'settled') for state in states), 'history-no-partial-destroy-observed')
    result = judge_fate(expected, final, tag_pages=final_pages, region=region, run_id=run_id, cell_id=cell)
    return {'status': result['status'], 'inventory': result,
            'observedLiveOwnedArns': sorted(arn for arn, state in observed.items() if state == 'live'),
            'observedSettledOwnedArns': sorted(arn for arn, state in observed.items() if state == 'settled'),
            **_unqualified()}


@dataclass(frozen=True)
class TerminalContext:
    run_id: str
    run_attempt: str
    manifest_sha256: str
    record_sha256: str
    receipt_sha256: str
    endpoint: str
    applied_at: datetime
    destroy_started_at: datetime
    observed_at: datetime


def verify_terminal123(record_bytes: bytes, receipt_bytes: bytes, manifest_bytes: bytes,
                       context: TerminalContext):
    """Consume the original complete terminal journey, never a reduced substitute."""
    import yaml
    from e2e import cloud_journey

    _custody({'record': record_bytes, 'receipt': receipt_bytes, 'manifest': manifest_bytes},
             {'record': context.record_sha256, 'receipt': context.receipt_sha256,
              'manifest': context.manifest_sha256})
    parsed_endpoint = urlsplit(context.endpoint)
    require(parsed_endpoint.scheme == 'https' and parsed_endpoint.hostname
            and not parsed_endpoint.username and not parsed_endpoint.password
            and not parsed_endpoint.query and not parsed_endpoint.fragment, 'terminal-https-endpoint')
    record, receipt = document(record_bytes), document(receipt_bytes)
    require(record.get('receiptSha256') == sha256(receipt_bytes)
            and receipt.get('target', {}).get('baseUrl') == context.endpoint, 'terminal-original-cell-endpoint')
    # The complete supported workflow runs with its durable infrastructure.
    # The separately maintained redis-off refusal profile does not replace it.
    cell = 'aws-ecs/redis-on'
    require(cloud_journey.validate_attempt(record, receipt, cell, run_id=context.run_id,
        run_attempt=context.run_attempt, at=context.observed_at,
        pinned_manifest=yaml.safe_load(manifest_bytes), pinned_manifest_digest=context.manifest_sha256),
        'terminal-original123-failed')
    require(context.applied_at < context.destroy_started_at,
            'terminal-owned-cell-lifetime')
    generated = timestamp(receipt['generatedAt'])
    require(context.applied_at <= generated <= context.destroy_started_at,
            'terminal-receipt-outside-cell-lifetime')
    for stage in receipt['stages']:
        require(context.applied_at <= timestamp(stage['evidence']['observedAt'])
                <= generated, 'terminal-stage-outside-cell-lifetime')
    return {'status': 'original-terminal123-joined', 'recordSha256': sha256(record_bytes),
            'receiptSha256': sha256(receipt_bytes), 'manifestSha256': sha256(manifest_bytes),
            **_unqualified()}


@dataclass(frozen=True)
class HandoffContext:
    # Selected manifest/published proxy pins and the independently retained
    # authenticated apply response. None is learned from a proposed binding.
    endpoint: str
    candidate_reference: str
    proxy_package: str
    proxy_integrity: str
    secret_reference: str
    operator_contract_digest: str
    required_tools: tuple[str, ...]
    terraform_root_name: str
    server_identity_sha256: str
    apply_lineage: Mapping[str, object]
    verified_not_before: datetime
    verified_not_after: datetime
    original_hashes: Mapping[str, str]


@dataclass(frozen=True)
class ScopeContext:
    original_sha256: str
    # Captured by the authenticated fixed published-native recorder, never
    # copied from a proposed terminal record. Includes immutable launch pins.
    expected: Mapping[str, object]
    observed_not_before: datetime
    observed_not_after: datetime


def verify_owned_scope(original_bytes: bytes, context: ScopeContext, *, native_terminal=False):
    """Rejudge original kernel evidence through the maintained JS consumer.

    The fixed internal CLI accepts data only. It has no native/client/cloud
    entrypoint and cannot create a branded live witness. Node and this checkout
    are supplied by the maintained release workflow, outside document readers.
    """
    import subprocess
    from pathlib import Path

    _custody({'scope': original_bytes}, {'scope': context.original_sha256})
    envelope = document(original_bytes)
    require(set(envelope) == {'status', 'original'} and envelope['status'] in ('observed', 'draining'),
            'scope-original-envelope')
    require(isinstance(context.expected, Mapping) and set(context.expected) == {'launcherAdmission', 'nativeAdmission', 'nonce',
            'launchBinding', 'pidNamespace'}, 'scope-independent-context')
    require(type(context.expected['nativeAdmission']) is dict
            and type(context.expected['nativeAdmission'].get('pid')) is int
            and context.expected['nativeAdmission']['pid'] > 0, 'scope-native-context-type')
    original = envelope['original']
    require(type(original) is dict and type(original.get('capturedAt')) is str
            and context.observed_not_before <= timestamp(original.get('capturedAt'))
                <= context.observed_not_after, 'scope-observed-owned-lifetime')
    request = canonical({'original': original, 'expected': dict(context.expected)})
    require(len(request) <= 65 * 1024 * 1024, 'scope-consumer-input-bound')
    # No shell, caller-selected module, command arguments or process signal.
    # Observation/verification expiry must never terminate an admitted actor.
    result = subprocess.run(['node', str(Path(__file__).resolve().with_name('devops_scope_consumer.mjs'))],
                            input=request, capture_output=True, check=False)
    require(result.returncode == 0 and result.stderr == b'' and len(result.stdout) <= 4 * 1024 * 1024,
            'scope-canonical-consumer-refused')
    verdict = document(result.stdout)
    require(set(verdict) == {'status', 'closed', 'nativeTerminal', 'memberCount', 'liveOwned',
            'candidateQualified', 'sixAwsCellsQualified'}
            and verdict['status'] == 'scope-original-rejudged' and verdict['closed'] is True
            and type(verdict['nativeTerminal']) is bool
            and type(verdict['memberCount']) is int and verdict['memberCount'] >= 1
            and verdict['liveOwned'] == [] and verdict['candidateQualified'] is False
            and verdict['sixAwsCellsQualified'] is False, 'scope-owned-descendant-not-closed')
    if native_terminal:
        require(verdict['nativeTerminal'] is True and original.get('waitpidNoChildren') is True,
                'scope-native-terminal-unverified')
        children = original.get('reapedChildren')
        require(type(children) is list and bool(children)
                and all(type(row) is dict and set(row) == {'pid', 'waitStatus', 'exitCode'}
                    and type(row['pid']) is int and row['pid'] > 0
                    and type(row['waitStatus']) is int and 0 <= row['waitStatus'] <= 65535
                    and type(row['exitCode']) is int for row in children),
                'scope-terminal-reap-originals')
        import os
        require(all((os.WIFEXITED(row['waitStatus']) or os.WIFSIGNALED(row['waitStatus']))
                    and os.waitstatus_to_exitcode(row['waitStatus']) == row['exitCode'] for row in children)
                and context.expected['nativeAdmission']['pid'] in {row['pid'] for row in children},
                'scope-terminal-native-reaped')
    return {'status': 'owned-process-scope-original-joined',
            'originalSha256': context.original_sha256, 'nativeTerminal': verdict['nativeTerminal'],
            'memberCount': verdict['memberCount'], **_unqualified()}


def verify_install_handoff(originals: Mapping[str, bytes], attempt: AttemptContext,
                           context: HandoffContext, *, request_bytes: bytes,
                           response_bytes: bytes, request_id, arguments: dict):
    """Join the actual ECS handoff, immutable verification core and binding.

    This neither resolves a secret nor launches a proxy. Production verification
    itself reaps the owned proxy; a recorded nonzero exit is not a failed probe.
    The recorder must authenticate the native process and published artifacts
    separately before this source consumer can participate in a full matrix.
    """
    _custody(originals, context.original_hashes)
    require(set(originals) == set(attempt.original_hashes) | {
        'handoff', 'verification-evidence', 'verification-receipt', 'provision-binding',
        'handoff-schema', 'binding-schema', 'verification-schema'}, 'handoff-original-closure')
    joined = verify_attempt({key: originals[key] for key in attempt.original_hashes}, attempt)
    require(joined['status'] == 'execution-joined' and attempt.exit_code == 0
            and attempt.plan.action == 'apply', 'handoff-successful-owned-apply')
    endpoint = context.endpoint.rstrip('/')
    url = urlsplit(endpoint)
    require(url.scheme == 'https' and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment and url.path in ('', '/'), 'handoff-owned-endpoint')
    require(type(context.required_tools) is tuple and bool(context.required_tools)
            and all(type(name) is str and name for name in context.required_tools)
            and len(set(context.required_tools)) == len(context.required_tools)
            and all(type(value) is str and SHA.fullmatch(value) for value in
                    (context.operator_contract_digest, context.server_identity_sha256)),
            'handoff-independent-context')
    require(context.secret_reference.startswith(('aws-secretsmanager://', 'arn:aws:secretsmanager:'))
            and len(context.secret_reference) <= 1024
            and not any(char.isspace() or ord(char) < 32 for char in context.secret_reference),
            'handoff-aws-secret-locator')
    metadata, execution = (document(originals[key]) for key in ('metadata', 'execution-receipt'))
    approval = document(originals['approval'])
    apply_refs = {'plan': originals['saved-plan'], 'plan-metadata': originals['metadata'],
                  'approval': originals['approval'], 'apply': originals['execution-receipt']}
    lineage = dict(context.apply_lineage)
    for key, expected in {'provisioningOperationId': attempt.plan.operation_id,
            'planSha256': sha256(originals['saved-plan']),
            'planMetadataDigest': metadata['plan_metadata_digest'],
            'approvalReceiptId': approval['approvalReceiptId'],
            'approvalReceiptSha256': sha256(originals['approval']),
            'actuatorReceiptReference': 'urn:sha256:' + sha256(originals['execution-receipt'])}.items():
        require(lineage.get(key) == expected, 'handoff-original-apply-lineage')
    require(type(lineage.get('applyAuditEventId')) is str and len(lineage['applyAuditEventId']) == 32
            and all(char in '0123456789abcdef' for char in lineage['applyAuditEventId'])
            and all(lineage.get(key) is None for key in ('handoffReceiptSha256',
                'handoffVerificationReceiptId', 'handoffVerificationReceiptSha256',
                'rootProvisioningOperationId', 'serverOperationId', 'serverProposalId',
                'serverDecisionReference', 'serverExecutionId', 'releaseReceiptReference',
                'releaseReceiptSha256')), 'handoff-pre-handoff-lineage')
    _references(lineage.get('evidenceRefs'), apply_refs)
    proxy = {'package': context.proxy_package, 'integrity': context.proxy_integrity}
    operator = {'digest': context.operator_contract_digest, 'status': 'qualified',
                'endpoint': endpoint, 'adminKeySecretRef': context.secret_reference}
    handoff = document(originals['handoff'])
    _schema_original(originals['handoff'], originals['handoff-schema'])
    require(canonical(handoff) == canonical({'schemaVersion': 'honua.mcp-proxy.handoff/v1',
        'rootProvisioningOperationId': attempt.plan.operation_id,
        'provisioningLineage': lineage, 'candidateReference': context.candidate_reference,
        'endpointSource': 'operator-contract', 'adminKeySecretRefSource': 'operator-contract',
        'operatorContract': operator, 'proxyArtifact': proxy, 'command': 'npx',
        'args': ['-y', '--package', context.proxy_package, 'honua-mcp-proxy'],
        'env': {'HONUA_BASE_URL': endpoint, 'HONUA_MCP_REMOTE_URL': endpoint + '/mcp'},
        'secretRefs': {'HONUA_ADMIN_KEY': context.secret_reference},
        'capabilityContract': {'verification': {'method': 'MCP tools/list', 'failClosed': True},
            'required': [{'name': 'operator', 'activation': 'MCP tools/list view=full',
                'serverConfiguration': [], 'requiredToolPrefixes': [],
                'requiredTools': list(context.required_tools)}]}}), 'handoff-original-contract')
    core = document(originals['verification-evidence'])
    require(set(core) == {'schemaVersion', 'provisioningOperationId', 'handoffSha256',
        'candidateReference', 'proxyPackage', 'proxyIntegrity', 'secretReferenceSha256',
        'serverIdentity', 'serverEndpointIdentity', 'observedTools', 'observedRosterSha256',
        'verifierOutcome', 'childExitCode', 'childReaped', 'secretScanPassed', 'verifiedAtUtc'},
        'handoff-verification-core-shape')
    tools = core['observedTools']
    require(type(tools) is list and all(type(name) is str and name for name in tools)
            and tools == sorted(set(tools)) and set(context.required_tools) <= set(tools),
            'handoff-complete-observed-roster')
    fixed = {'schemaVersion': 'honua.devops.install-handoff-verification/v1',
        'provisioningOperationId': attempt.plan.operation_id,
        'handoffSha256': sha256(originals['handoff']), 'candidateReference': context.candidate_reference,
        'proxyPackage': context.proxy_package, 'proxyIntegrity': context.proxy_integrity,
        'secretReferenceSha256': sha256(context.secret_reference.encode()),
        'serverIdentity': context.server_identity_sha256, 'serverEndpointIdentity': sha256(endpoint.encode()),
        'observedRosterSha256': sha256('\n'.join(tools).encode()),
        'verifierOutcome': 'install-handoff-verified', 'childReaped': True, 'secretScanPassed': True}
    require(all(type(core[key]) is type(value) and core[key] == value for key, value in fixed.items())
            and type(core['childExitCode']) is int
            and attempt.terminal_at <= context.verified_not_before
            <= timestamp(core['verifiedAtUtc']) <= context.verified_not_after,
            'handoff-original-verification-result')
    # The core's original bytes, not reserialized evidence JSON, earn the URN.
    core_sha = sha256(originals['verification-evidence'])
    receipt = document(originals['verification-receipt'])
    require(receipt == {'receiptId': 'urn:sha256:' + core_sha,
        'receiptSha256': core_sha, 'evidence': core}, 'handoff-original-core-address')
    _schema_original(originals['verification-receipt'], originals['verification-schema'])
    binding = document(originals['provision-binding'])
    _schema_original(originals['provision-binding'], originals['binding-schema'])
    completed = dict(lineage, handoffReceiptSha256=sha256(originals['handoff']),
        handoffVerificationReceiptId=receipt['receiptId'], handoffVerificationReceiptSha256=core_sha,
        rootProvisioningOperationId=attempt.plan.operation_id,
        evidenceRefs=[*lineage['evidenceRefs'], *[{'kind': kind,
            'reference': 'urn:sha256:' + sha256(originals[kind]), 'sha256': sha256(originals[kind]),
            'byteLength': len(originals[kind])} for kind in ('handoff', 'verification-evidence')]])
    backend, identity, before, after = (metadata['backend'], metadata['identity'],
                                       execution['state_before'], execution['state_after'])
    expected_execution = {'planMetadataDigest': metadata['plan_metadata_digest'],
        'savedPlanSha256': sha256(originals['saved-plan']), 'terraformRoot': context.terraform_root_name,
        'iacRevision': metadata['source']['iac_revision'], 'iacTreeDigest': metadata['source']['iac_tree_digest'],
        'terraformVersion': metadata['toolchain']['terraform_version'],
        'providerLockDigest': metadata['toolchain']['provider_lock_digest'],
        'inputDigest': metadata['inputs']['input_digest'],
        'backend': {'backendConfigDigest': backend['backend_config_digest'], 'backendKind': backend['backend_kind'],
            'isRemote': backend['is_remote'], 'workspace': backend['workspace'],
            'objectKey': backend.get('object_key'), 'region': backend.get('region')},
        'executionIdentity': {key: execution['workload_identity'].get(source) for key, source in {
            'assumedRoleArn': 'assumed_role_arn', 'roleId': 'role_id', 'accountId': 'account_id',
            'partition': 'partition', 'issuer': 'issuer', 'credentialKind': 'credential_kind'}.items()},
        'state': {'lineageBefore': before.get('lineage'), 'serialBefore': before.get('serial'),
            'lineageAfter': after.get('lineage'), 'serialAfter': after.get('serial')},
        'operatorContractDigest': context.operator_contract_digest,
        'evidenceMode': identity['evidence_mode'], 'releaseQualified': metadata['posture']['release_qualified']}
    expected_execution['executionIdentity']['partition'] = (
        execution['workload_identity'].get('partition') or identity.get('partition'))
    expected_execution['executionIdentity']['issuer'] = identity.get('issuer')
    expected_teardown = {'kind': 'honua.iac.terraform-teardown/v1',
        'terraformRoot': execution['cleanup']['teardown_root'], 'action': execution['cleanup']['teardown_action'],
        'workspace': backend['workspace'], 'backendConfigDigest': backend['backend_config_digest'],
        'objectKey': backend.get('object_key'), 'stateLineage': after.get('lineage'),
        'stateSerial': after.get('serial'), 'accountId': identity['account_id'], 'region': backend.get('region')}
    require(execution['output_contract']['digest'] == context.operator_contract_digest,
            'handoff-applied-operator-contract')
    require(canonical(binding) == canonical({'schemaVersion': 'honua.devops.aws-ecs-provision-binding/v1',
        'lineage': completed, 'endpoint': endpoint, 'endpointSource': 'operator-contract',
        'adminKeySecretRefSource': 'operator-contract', 'candidateReference': context.candidate_reference,
        'proxyArtifact': proxy, 'secretReferenceSha256': sha256(context.secret_reference.encode()),
        'handoffSha256': sha256(originals['handoff']), 'verificationReceiptId': receipt['receiptId'],
        'verificationReceiptSha256': core_sha, 'operatorContract': {key: operator[key] for key in
            ('digest', 'status', 'endpoint')}, 'iacExecution': expected_execution,
        'execReceiptSha256': sha256(originals['execution-receipt']), 'execReceipt': execution,
        'teardownHandle': expected_teardown}), 'handoff-complete-provision-binding')
    request, response = document(request_bytes), document(response_bytes)
    require(type(request_id) in (str, int) and type(request.get('id')) is type(request_id)
            and type(response.get('id')) is type(request_id) and request['id'] == response['id'] == request_id
            and request.get('jsonrpc') == response.get('jsonrpc') == '2.0'
            and 'error' not in response and request.get('method') == 'tools/call'
            and request.get('params') == {'name': 'verify_install_handoff', 'arguments': arguments}
            and set(arguments) == {'handoffConfigPath', 'overwrite'}
            and type(arguments['handoffConfigPath']) is str and arguments['handoffConfigPath'].startswith('/')
            and type(arguments['overwrite']) is bool, 'handoff-owned-verification-call')
    result, status = operation_result(response)
    require(status == 'install-handoff-verified'
            and field(result, 'provisioningLineage') == completed, 'handoff-observed-completed-lineage')
    return {'status': 'install-handoff-originals-joined', 'operationId': attempt.plan.operation_id,
            'handoffSha256': sha256(originals['handoff']), 'verificationCoreSha256': core_sha,
            'bindingSha256': sha256(originals['provision-binding']), **_unqualified()}
