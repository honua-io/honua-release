"""Join original production plan/approval/executor evidence for one owned intent.

Pure parsing only. Trusted context comes from the release-owned authenticated
recorder and production approval audit, never a caller-written acceptance flag.
Cancellation may leave no final receipt: that remains explicitly indeterminate.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from devops_provisioning_contract import (PlanContext, SHA, document, require, sha256,
    timestamp, verify_plan_originals, verify_approval_original, verify_execution_original, verify_claim_original)


@dataclass(frozen=True)
class AttemptContext:
    plan: PlanContext
    claim_directory: str
    plan_path: str
    executor_id: str
    acquired_at: datetime
    terminal_at: datetime | None
    mutation_started_at: datetime
    accepted_approval_sha256: str
    issuer: str
    key_id: str
    issued_by_principal: str
    executor_principal: str
    environment: str
    # These are hashes independently captured from the actual owned recorder,
    # not adopted from this function's input files.
    original_hashes: Mapping[str, str]
    exit_code: int | None


def verify_attempt(originals: Mapping[str, bytes], context: AttemptContext):
    required = {'metadata', 'saved-plan', 'approval', 'claim'} | set(context.plan.input_refs)
    if context.exit_code is not None:
        required.add('execution-receipt')
    require(set(originals) == set(context.original_hashes) == required, 'attempt-original-closure')
    require(all(type(digest) is str and SHA.fullmatch(digest)
                and type(originals[name]) is bytes and sha256(originals[name]) == digest
                for name, digest in context.original_hashes.items()), 'attempt-original-custody')
    require(context.acquired_at <= context.mutation_started_at
            and (context.terminal_at is None or context.mutation_started_at <= context.terminal_at),
            'attempt-lifetime')
    plan = verify_plan_originals(originals['metadata'], originals['saved-plan'],
        {name: originals[name] for name in context.plan.input_refs}, context.plan)
    metadata = document(originals['metadata'])
    require(timestamp(metadata['created_at_utc']) <= context.mutation_started_at
            < timestamp(metadata['expires_at_utc']), 'attempt-plan-expired-at-mutation')
    approval = verify_approval_original(originals['approval'], originals['metadata'],
        context=context.plan, stack='aws-ecs', environment=context.environment,
        accepted_original_sha256=context.accepted_approval_sha256,
        issuer=context.issuer, key_id=context.key_id,
        issued_by_principal=context.issued_by_principal, executor_principal=context.executor_principal,
        mutation_started_at=context.mutation_started_at)
    receipt = originals.get('execution-receipt')
    execution = None
    if receipt is not None:
        require(context.terminal_at is not None, 'attempt-receipt-executor-still-live')
        execution = verify_execution_original(receipt, originals['metadata'],
            claim_directory=context.claim_directory, plan_path=context.plan_path,
            expected_exit=context.exit_code)
        require(context.mutation_started_at <= timestamp(document(receipt)['completed_at_utc'])
                <= context.terminal_at, 'attempt-execution-lifetime')
    claim = verify_claim_original(originals['claim'], receipt,
        claim_directory=context.claim_directory, executor_id=context.executor_id,
        acquired_at=context.acquired_at, executor_terminal_at=context.terminal_at)
    # A durable provisional claim is useful recovery evidence; it cannot be
    # promoted to a completed execution or absent resources by this join.
    status = ('execution-joined' if execution is not None
              and claim['status'] == 'execution-acknowledged' else 'indeterminate')
    return {'status': status, 'operationId': context.plan.operation_id,
            'action': context.plan.action, 'plan': plan, 'approval': approval,
            'execution': execution, 'claim': claim, 'cleanupOutstanding': True,
            'candidateQualified': False, 'sixAwsCellsQualified': False,
            'terminal123Qualified': False}
