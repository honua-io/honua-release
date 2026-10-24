"""Whole attempt source vectors are not genuine signed approvals or AWS proof."""
from dataclasses import replace
import pytest
from devops_provisioning_contract import canonical, document, sha256, timestamp, Refusal
from devops_cleanup_attempt import AttemptContext, verify_attempt
from test_devops_provisioning_contract import execution_vector, vector


def attempt_vector(exit_code=0):
    receipt, metadata, plan_context = execution_vector(exit_code)
    _, saved_plan, inputs, _ = vector()
    approval = canonical({'schemaVersion':'honua.devops.provision-approval/v1',
        'signingMode':'kms-mac', 'decision':'approved', 'issuer':'source-vector-issuer',
        'keyId':'source-vector-key', 'provisioningOperationId':plan_context.operation_id,
        'planSha256':document(metadata)['plan']['sha256'],
        'planMetadataDigest':document(metadata)['plan_metadata_digest'],
        'action':'apply', 'stack':'aws-ecs', 'environment':'it',
        'issuedAtUtc':'2026-10-20T00:01:30Z', 'expiresAtUtc':'2026-10-20T00:20:00Z',
        'approvalReceiptId':'source-vector-only', 'signature':'not-a-real-mac-source-test-only'})
    raw_receipt = canonical(receipt)
    claim = canonical({'schema_version':'v1', 'kind':'honua.iac.claim-status',
        'claim_dir':'/owned/claims/original', 'state':'completed',
        'acquired_at_utc':'2026-10-20T00:02:00Z', 'completed_at_utc':'2026-10-20T00:10:00Z',
        'phase':'receipt-committed', 'executor_id':'owned-executor',
        'receipt_digest':sha256(raw_receipt)})
    originals = dict(inputs, metadata=metadata, **{'saved-plan':saved_plan, 'approval':approval,
                     'claim':claim, 'execution-receipt':raw_receipt})
    context = AttemptContext(plan_context, '/owned/claims/original',
        '/owned/plans/original/honua.tfplan', 'owned-executor',
        timestamp('2026-10-20T00:02:00Z'), timestamp('2026-10-20T00:11:00Z'),
        timestamp('2026-10-20T00:03:00Z'), sha256(approval), 'source-vector-issuer',
        'source-vector-key', 'issuer-principal', 'executor-principal', 'it',
        {name:sha256(raw) for name,raw in originals.items()}, exit_code)
    return originals, context


@pytest.mark.parametrize('exit_code',[0,1,130])
def test_complete_attempt_joins_all_consumers_and_keeps_cleanup_outstanding(exit_code):
    originals, context = attempt_vector(exit_code)
    result = verify_attempt(originals, context)
    assert result['status'] == 'execution-joined'
    assert result['execution']['exitStatus'] == exit_code
    assert result['cleanupOutstanding'] is True
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is False


def test_cancelled_missing_receipt_is_retained_indeterminate_never_success():
    originals, context = attempt_vector()
    del originals['execution-receipt']
    claim = document(originals['claim'])
    claim.update(state='reconciliation-required', phase='reconciliation-required',
                 completed_at_utc=None, receipt_digest=None)
    originals['claim'] = canonical(claim)
    context = replace(context, exit_code=None,
                      original_hashes={name:sha256(raw) for name,raw in originals.items()})
    result = verify_attempt(originals, context)
    assert result['status'] == 'indeterminate' and result['execution'] is None
    assert result['cleanupOutstanding'] is True


@pytest.mark.parametrize('mutation',['extra','missing','tamper','foreign-approval',
    'same-principal','wrong-executor','wrong-claim-path','wrong-plan-path',
    'live-executor','terminal-before-receipt','expired-plan','mutation-before-claim'])
def test_whole_attempt_refuses_custody_and_semantic_substitution(mutation):
    originals, context = attempt_vector()
    if mutation=='extra': originals['unrequested']=b'new'
    if mutation=='missing': del originals['approval']
    if mutation=='tamper': originals['approval']+=b' '
    if mutation=='foreign-approval': context=replace(context,accepted_approval_sha256='f'*64)
    if mutation=='same-principal': context=replace(context,issued_by_principal=context.executor_principal)
    if mutation=='wrong-executor': context=replace(context,executor_id='foreign')
    if mutation=='wrong-claim-path': context=replace(context,claim_directory='/foreign')
    if mutation=='wrong-plan-path': context=replace(context,plan_path='/foreign')
    if mutation=='live-executor': context=replace(context,terminal_at=None)
    if mutation=='terminal-before-receipt': context=replace(context,terminal_at=timestamp('2026-10-20T00:04:00Z'))
    if mutation=='expired-plan': context=replace(context,mutation_started_at=timestamp('2026-10-20T00:31:00Z'),terminal_at=timestamp('2026-10-20T00:40:00Z'))
    if mutation=='mutation-before-claim': context=replace(context,mutation_started_at=timestamp('2026-10-20T00:01:00Z'))
    with pytest.raises(Refusal): verify_attempt(originals,context)
