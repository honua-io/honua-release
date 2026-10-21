"""Pure original-byte joins for the existing AWS cleanup proof; no actuator or issuer.

The trusted expected values are supplied by the independently authenticated release
context. A matching document is not a signature, publication, runtime or candidate
qualification. No file paths, subprocesses, network or credentials are consumed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Mapping

CELLS = ('ordinary-approved-destroy', 'cancel-apply-after-resource', 'provider-failure-apply',
         'cancel-destroy', 'transient-destroy-restart-retry', 'externally-owned-storage-retained')
SHA = re.compile(r'[0-9a-f]{64}\Z')
OPERATION = re.compile(r'urn:honua:provisioning:[0-9a-f]{32}\Z')
MAX_JSON = 8 * 1024 * 1024


class Refusal(ValueError):
    """Safe fixed-code refusal; never interpolates raw receipt contents."""


def require(condition, category):
    if not condition:
        raise Refusal(category)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def document(value: bytes):
    require(type(value) is bytes and 0 < len(value) <= MAX_JSON, 'original-json-bound')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'original-json-duplicate')
            result[key] = item
        return result
    try:
        result = json.loads(value, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refusal('original-json-number')))
    except (UnicodeError, json.JSONDecodeError):
        raise Refusal('original-json-shape') from None
    require(type(result) is dict, 'original-json-object')
    return result


def timestamp(value):
    require(type(value) is str and (value.endswith('Z') or value.endswith('+00:00')), 'timestamp-shape')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0, 'timestamp-shape')
        return parsed
    except ValueError:
        raise Refusal('timestamp-shape') from None


@dataclass(frozen=True)
class PlanContext:
    """Authenticated release/owned request context, never inferred from the plan."""
    candidate_image: str
    iac_revision: str
    iac_tree_digest: str
    terraform_version: str
    provider_lock_digest: str
    backend: Mapping
    account_id: str
    assumed_role_arn: str
    actor_id: str
    target_id: str
    operation_id: str
    action: str
    # Exact safe names and original hashes captured by the trusted request recorder.
    # Values can include secrets, so the public result carries hashes only.
    input_refs: tuple[str, ...]
    created_not_before: datetime
    expires_not_after: datetime


def verify_plan_originals(metadata_bytes: bytes, saved_plan: bytes,
                          inputs: Mapping[str, bytes], context: PlanContext):
    """Join exact original inputs/image -> canonical metadata -> saved binary plan.

    ``inputs`` is the finite mapping of canonical input_refs to their ORIGINAL
    bytes; no local pathname is opened. It includes all TF_VAR and auto-tfvars
    sources captured by the normal substrate. No missing/extra inputs are allowed.
    The production DevOps variable file is uniquely identified by its explicit
    trusted ``varfile:`` reference and must carry the expected immutable image.
    """
    require(context.action in ('apply', 'destroy') and OPERATION.fullmatch(context.operation_id),
            'expected-operation-action')
    require(type(saved_plan) is bytes and 0 < len(saved_plan) <= 512 * 1024 * 1024,
            'saved-plan-bound')
    require(type(context.candidate_image) is str
            and re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', context.candidate_image),
            'expected-candidate-image')
    require(len(set(context.input_refs)) == len(context.input_refs)
            and set(inputs) == set(context.input_refs) and bool(inputs), 'input-finite-closure')
    entries = []
    candidate_matches = []
    for name in sorted(inputs):
        require(type(name) is str and re.fullmatch(r'(varfile|autofile|env):[^\r\n=]+', name),
                'input-reference-shape')
        value = inputs[name]
        require(type(value) is bytes and len(value) <= MAX_JSON, 'input-byte-bound')
        entries.append(name + '=' + sha256(value))
        if name.startswith('varfile:'):
            variables = document(value)
            if 'honua_image' in variables:
                candidate_matches.append(variables['honua_image'])
    require(candidate_matches == [context.candidate_image], 'candidate-input-binding')
    metadata = document(metadata_bytes)
    require(all(type(metadata.get(key)) is dict for key in
                ('source', 'toolchain', 'backend', 'identity', 'inputs', 'plan', 'posture')),
            'metadata-section-shape')
    digest = metadata.get('plan_metadata_digest')
    require(type(digest) is str and SHA.fullmatch(digest), 'metadata-digest-shape')
    unsigned = {key: value for key, value in metadata.items() if key != 'plan_metadata_digest'}
    require(sha256(canonical(unsigned)) == digest, 'metadata-original-digest')
    require(metadata.get('kind') == 'honua.iac.exact-plan' and metadata.get('schema_version') == 'v1',
            'metadata-contract')
    require(metadata.get('action') == context.action and metadata.get('actor_id') == context.actor_id
            and metadata.get('target_id') == context.target_id, 'metadata-request-identity')
    require(metadata.get('source', {}).get('iac_revision') == context.iac_revision
            and metadata['source'].get('iac_tree_digest') == context.iac_tree_digest,
            'metadata-source-identity')
    require(metadata.get('toolchain') == {'terraform_version': context.terraform_version,
                                        'provider_lock_digest': context.provider_lock_digest},
            'metadata-toolchain')
    require(metadata.get('backend') == dict(context.backend), 'metadata-backend-identity')
    require(context.backend.get('is_remote') is True
            and context.backend.get('locking', {}).get('kind') not in (None, 'none')
            and context.backend.get('backend_access_role_arn'), 'metadata-remote-locking')
    identity = metadata.get('identity', {})
    require(identity.get('account_id') == context.account_id
            and identity.get('assumed_role_arn') == context.assumed_role_arn
            and identity.get('credential_kind') == 'sts-assumed-role'
            and identity.get('evidence_mode') == 'live', 'metadata-workload-identity')
    entries.sort()  # IaC sorts full reference=hash entries, not references alone.
    input_refs = [entry.split('=', 1)[0] for entry in entries]
    require(metadata.get('inputs') == {'input_digest': sha256('\n'.join(entries).encode()),
                                      'input_refs': input_refs}, 'metadata-input-closure')
    require(metadata.get('plan', {}).get('sha256') == sha256(saved_plan), 'metadata-saved-plan')
    posture = metadata.get('posture', {})
    require(posture.get('release_qualified') is True and posture.get('local_state_allowed') is False
            and posture.get('dirty_source_allowed') is False, 'metadata-release-posture')
    created = timestamp(metadata.get('created_at_utc'))
    expires = timestamp(metadata.get('expires_at_utc'))
    require(context.created_not_before <= created < expires <= context.expires_not_after,
            'metadata-time-context')
    return {'planMetadataDigest': digest, 'savedPlanSha256': sha256(saved_plan),
            'inputDigest': metadata['inputs']['input_digest'], 'candidateImage': context.candidate_image,
            'candidateQualified': False, 'sixAwsCellsQualified': False}


def verify_execution_original(receipt_bytes: bytes, metadata_bytes: bytes, *,
                              claim_directory: str, plan_path: str, expected_exit: int):
    """Rejudge the original canonical receipt, including failed executions.

    The metadata must first pass verify_plan_originals. A nonzero original is
    retained as failure evidence; it never means resources were destroyed.
    """
    receipt = document(receipt_bytes)
    plan = document(metadata_bytes)
    require(type(expected_exit) is int and 0 <= expected_exit <= 255, 'expected-exit-shape')
    require(receipt.get('kind') == 'honua.iac.exec-receipt'
            and receipt.get('schema_version') == 'v1', 'execution-contract')
    require(type(receipt.get('exit_status')) is int and receipt['exit_status'] == expected_exit
            and receipt.get('status') == ('succeeded' if expected_exit == 0 else 'failed'),
            'execution-result')
    require(all(type(receipt.get(key)) is dict for key in
                ('workload_identity', 'backend_step', 'state_before', 'state_after', 'cleanup', 'output_contract')),
            'execution-section-shape')
    pairs = [('plan_metadata_digest', plan.get('plan_metadata_digest')),
             ('approved_digest', plan.get('plan_metadata_digest')),
             ('saved_plan_sha256', plan.get('plan', {}).get('sha256'))]
    pairs += [(key, plan.get(key)) for key in ('action', 'actor_id', 'target_id', 'candidate_digest')]
    require(all(receipt.get(key) == value for key, value in pairs), 'execution-plan-binding')
    identity = plan.get('identity', {})
    expected_identity = {key: identity.get(key) for key in
                         ('assumed_role_arn', 'role_id', 'account_id', 'partition', 'issuer', 'credential_kind')}
    expected_identity['contract_digest'] = plan.get('workload_identity_contract_digest')
    require(receipt['workload_identity'] == expected_identity, 'execution-workload-binding')
    backend = plan.get('backend', {})
    require(receipt['backend_step'] == {key: backend.get(key) for key in
                ('backend_config_digest', 'backend_kind', 'workspace', 'object_key', 'bucket_arn', 'locking')},
            'execution-backend-binding')
    for state in (receipt['state_before'], receipt['state_after']):
        require(set(state) == {'lineage', 'serial'}
                and (state['lineage'] is None or type(state['lineage']) is str)
                and type(state['serial']) is int and state['serial'] >= 0, 'execution-state-shape')
    require(receipt['state_before'] == plan.get('state_before'), 'execution-prior-state')
    require(receipt['cleanup'] == {'claim_dir': claim_directory, 'saved_plan': plan_path,
                'teardown_root': plan.get('source', {}).get('terraform_root'), 'teardown_action': 'destroy'},
            'execution-cleanup-coordinates')
    require(receipt['output_contract'].get('output_name') == 'operator_contract_digest',
            'execution-output-contract')
    completed = timestamp(receipt.get('completed_at_utc'))
    require(completed >= timestamp(plan.get('created_at_utc')), 'execution-time')
    return {'originalReceiptSha256': sha256(receipt_bytes), 'exitStatus': expected_exit,
            'status': receipt['status'], 'stateAfter': receipt['state_after'],
            'cleanupOutstanding': True, 'candidateQualified': False, 'sixAwsCellsQualified': False}


def verify_claim_original(claim_bytes: bytes, receipt_bytes: bytes | None, *,
                          claim_directory: str, executor_id: str,
                          acquired_at: datetime, executor_terminal_at: datetime | None):
    """Canonical claim status is execution evidence, never resource-absence proof."""
    claim = document(claim_bytes)
    require(set(claim) == {'schema_version', 'kind', 'claim_dir', 'state', 'acquired_at_utc',
                           'completed_at_utc', 'phase', 'executor_id', 'receipt_digest'}, 'claim-shape')
    require(claim['schema_version'] == 'v1' and claim['kind'] == 'honua.iac.claim-status',
            'claim-contract')
    require(claim['claim_dir'] == claim_directory and claim['executor_id'] == executor_id
            and timestamp(claim['acquired_at_utc']) == acquired_at, 'claim-original-identity')
    require(claim['phase'] in ('preflight', 'mutation-started', 'terraform-acknowledged',
                               'receipt-committed', 'reconciliation-required'), 'claim-phase')
    require(claim['state'] in ('held', 'completed', 'reconciliation-required'), 'claim-state')
    if executor_terminal_at is None:
        return {'status': 'live-or-indeterminate', 'cleanupOutstanding': True,
                'candidateQualified': False, 'sixAwsCellsQualified': False}
    require(executor_terminal_at >= acquired_at, 'claim-executor-order')
    if claim['phase'] != 'receipt-committed' or claim['completed_at_utc'] is None or receipt_bytes is None:
        return {'status': 'indeterminate', 'cleanupOutstanding': True,
                'candidateQualified': False, 'sixAwsCellsQualified': False}
    require(claim['receipt_digest'] == sha256(receipt_bytes), 'claim-original-receipt')
    require(acquired_at <= timestamp(claim['completed_at_utc']) <= executor_terminal_at,
            'claim-completion-order')
    return {'status': 'execution-acknowledged', 'originalReceiptSha256': sha256(receipt_bytes),
            'cleanupOutstanding': True, 'candidateQualified': False, 'sixAwsCellsQualified': False}


def verify_approval_original(approval_bytes: bytes, metadata_bytes: bytes, *,
                             context: PlanContext, stack: str, environment: str,
                             accepted_original_sha256: str, issuer: str, key_id: str,
                             issued_by_principal: str, executor_principal: str,
                             mutation_started_at: datetime):
    """Join an independently authenticated production-accepted KMS approval.

    accepted_original_sha256/issuer/key identity must come from the authenticated
    production approval verifier/audit, NOT this document or a caller JSON flag.
    This function neither issues nor independently validates a MAC. The coordinator
    must not call it without that original verifier provenance.
    """
    require(SHA.fullmatch(accepted_original_sha256)
            and sha256(approval_bytes) == accepted_original_sha256, 'approval-accepted-original')
    require(type(issued_by_principal) is str and bool(issued_by_principal)
            and type(executor_principal) is str and bool(executor_principal)
            and issued_by_principal != executor_principal, 'approval-separated-principal')
    approval = document(approval_bytes)
    plan = document(metadata_bytes)
    require(approval.get('schemaVersion') == 'honua.devops.provision-approval/v1'
            and approval.get('signingMode') == 'kms-mac' and approval.get('decision') == 'approved',
            'approval-evidentiary-mode')
    require(approval.get('issuer') == issuer and approval.get('keyId') == key_id
            and approval.get('provisioningOperationId') == context.operation_id,
            'approval-issuer-operation')
    require(approval.get('planSha256') == plan.get('plan', {}).get('sha256')
            and approval.get('planMetadataDigest') == plan.get('plan_metadata_digest')
            and approval.get('action') == context.action and approval.get('stack') == stack
            and approval.get('environment') == environment, 'approval-exact-plan-action')
    issued = timestamp(approval.get('issuedAtUtc'))
    expires = timestamp(approval.get('expiresAtUtc'))
    require(issued <= mutation_started_at < expires
            and 0 < (expires - issued).total_seconds() <= 3600, 'approval-mutation-time')
    require(type(approval.get('approvalReceiptId')) is str and bool(approval['approvalReceiptId'])
            and type(approval.get('signature')) is str and bool(approval['signature']), 'approval-identity')
    return {'approvalReceiptId': approval['approvalReceiptId'], 'approvalSha256': sha256(approval_bytes),
            'provisioningOperationId': context.operation_id, 'candidateQualified': False,
            'sixAwsCellsQualified': False}


def verify_saved_plan_review(review_bytes: bytes, *, candidate_image: str,
                             expected_workloads: Mapping[str, str],
                             recorded_saved_plan_sha256: str, saved_plan: bytes):
    """Optional supplemental fixed-tool `terraform show -json` diagnostic join.

    Production emits a text review; this JSON diagnostic is not required for the
    original six cells and cannot replace missing production evidence.

    The coordinator must independently authenticate the read command/tool/source
    and record its input saved-plan digest. JSON alone cannot prove that origin.
    Expected workload address -> container name comes from the reviewed preset.
    """
    require(sha256(saved_plan) == recorded_saved_plan_sha256, 'plan-review-original-input')
    require(bool(expected_workloads) and all(type(key) is str and type(value) is str
                                           for key, value in expected_workloads.items()),
            'plan-review-expected-workloads')
    review = document(review_bytes)
    require(type(review.get('planned_values')) is dict, 'plan-review-values')
    found = {}
    addresses = set()
    def visit(module):
        require(type(module) is dict and type(module.get('resources', [])) is list
                and type(module.get('child_modules', [])) is list, 'plan-review-module')
        for resource in module.get('resources', []):
            require(type(resource) is dict and type(resource.get('address')) is str,
                    'plan-review-resource')
            address = resource['address']
            require(address not in addresses, 'plan-review-duplicate-resource')
            addresses.add(address)
            if resource.get('type') != 'aws_ecs_task_definition':
                continue
            require(address in expected_workloads and type(resource.get('values')) is dict,
                    'plan-review-unexpected-workload')
            raw = resource['values'].get('container_definitions')
            require(type(raw) is str, 'plan-review-container-definition')
            # Reuse duplicate/nonfinite JSON refusal while retaining the native list shape.
            wrapper = document(b'{"containers":' + raw.encode() + b'}')
            containers = wrapper['containers']
            require(type(containers) is list and bool(containers), 'plan-review-container-list')
            names = set()
            for container in containers:
                require(type(container) is dict and type(container.get('name')) is str
                        and container['name'] not in names, 'plan-review-container-name')
                names.add(container['name'])
                if container['name'] == expected_workloads[address]:
                    require(container.get('image') == candidate_image, 'plan-review-candidate-image')
                    found[address] = container['name']
            require(address in found, 'plan-review-missing-container')
        for child in module.get('child_modules', []):
            visit(child)
    visit(review['planned_values'].get('root_module'))
    require(found == dict(expected_workloads), 'plan-review-workload-closure')
    return {'reviewSha256': sha256(review_bytes), 'savedPlanSha256': sha256(saved_plan),
            'candidateImage': candidate_image, 'workloads': sorted(found),
            'candidateQualified': False, 'sixAwsCellsQualified': False}
