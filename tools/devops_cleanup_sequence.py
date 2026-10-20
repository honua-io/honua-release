"""Pure chronology/custody consumer for the ORIGINAL six cleanup scenarios.

This is not an authority adapter. The release-owned caller must authenticate
the run, request, provider observations and original hash context independently.
Neither a local JSON file nor a passing source vector can supply that authority.
This layer deliberately cannot issue approvals, run tools or assert qualification.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from devops_provisioning_contract import CELLS, OPERATION, SHA, document, require, sha256, timestamp


# Events describe observed effects, not commands to perform. Repeated fresh
# attempts have separate identities; an original successful replay is never a
# new mutation event. The producer retains all other observations separately.
SEQUENCES = {
    CELLS[0]: ('apply-admitted', 'owned-resource-live', 'apply-terminal',
               'handoff-verified', 'terminal123-completed', 'destroy-admitted',
               'destroy-terminal', 'final-inventory'),
    CELLS[1]: ('apply-admitted', 'owned-resource-live', 'cancel-sent',
               'apply-terminal', 'native-restarted', 'plan-directory-lost',
               'cleanup-inspected', 'destroy-admitted', 'destroy-terminal', 'final-inventory'),
    CELLS[2]: ('apply-admitted', 'owned-resource-live', 'provider-cut-armed',
               'apply-terminal', 'provider-cut-restored', 'native-restarted',
               'plan-directory-lost', 'cleanup-inspected', 'destroy-admitted',
               'destroy-terminal', 'final-inventory'),
    CELLS[3]: ('apply-admitted', 'owned-resource-live', 'apply-terminal',
               'destroy-admitted', 'partial-destroy-observed', 'cancel-sent',
               'destroy-terminal', 'native-restarted', 'plan-directory-lost',
               'cleanup-inspected', 'retry-destroy-admitted', 'retry-destroy-terminal', 'final-inventory'),
    CELLS[4]: ('apply-admitted', 'owned-resource-live', 'apply-terminal',
               'destroy-admitted', 'partial-destroy-observed', 'provider-cut-armed',
               'destroy-terminal', 'provider-cut-restored', 'native-restarted',
               'plan-directory-lost', 'cleanup-inspected', 'retry-destroy-admitted',
               'retry-destroy-terminal', 'final-inventory'),
    CELLS[5]: ('external-storage-before', 'apply-admitted', 'owned-resource-live',
               'apply-terminal', 'destroy-admitted', 'destroy-terminal',
               'external-storage-after', 'final-inventory'),
}


@dataclass(frozen=True)
class SequenceContext:
    """Expected identities obtained by the authenticated release run recorder.

    original_hashes binds the exact finite private original set, not paths or
    hashes adopted from an untrusted sequence. event_originals binds each effect
    to its independently recorded evidence. Individual semantic consumers must
    additionally adjudicate those originals before acceptance.
    """
    cell: str
    run_id: str
    candidate_image: str
    operation_ids: Mapping[str, str]
    original_hashes: Mapping[str, str]
    event_originals: Mapping[str, str]
    started_at: str
    ended_at: str


def verify_sequence(original: bytes, originals: Mapping[str, bytes], context: SequenceContext):
    require(context.cell in CELLS, 'sequence-cell')
    require(set(originals) == set(context.original_hashes) and bool(originals), 'sequence-original-closure')
    require(all(type(name) is str and SHA.fullmatch(digest)
                and type(originals[name]) is bytes and sha256(originals[name]) == digest
                for name, digest in context.original_hashes.items()), 'sequence-original-hash')
    expected = SEQUENCES[context.cell]
    require(set(context.event_originals) == set(expected)
            and set(context.event_originals.values()) <= set(originals), 'sequence-event-original-closure')
    require(set(context.operation_ids) == {'apply', 'destroy'} | (
                {'retry-destroy'} if context.cell in (CELLS[3], CELLS[4]) else set())
            and all(type(value) is str and OPERATION.fullmatch(value)
                    for value in context.operation_ids.values())
            and context.operation_ids['apply'] != context.operation_ids['destroy'], 'sequence-operation-closure')
    # Retry may reconcile the original destroy intent. A distinct successor is
    # not automatically legitimate either: the full semantic judge must bind
    # its production-authenticated recovery relationship and fresh one-use plan.
    value = document(original)
    require(set(value) == {'schema', 'cell', 'runId', 'candidateImage', 'events'}
            and value['schema'] == 'honua.devops-cleanup-sequence/v1'
            and value['cell'] == context.cell and value['runId'] == context.run_id
            and value['candidateImage'] == context.candidate_image, 'sequence-context')
    events = value['events']
    require(type(events) is list and len(events) == len(expected), 'sequence-event-count')
    start, end = timestamp(context.started_at), timestamp(context.ended_at)
    require(start < end, 'sequence-window')
    previous_ns = 0
    previous_time = start
    current_operation = context.operation_ids['apply']
    for ordinal, (kind, event) in enumerate(zip(expected, events)):
        require(type(event) is dict and set(event) == {'ordinal', 'kind', 'operationId',
                'observedAt', 'monotonicNs', 'original', 'originalSha256'}, 'sequence-event-shape')
        if kind in ('destroy-admitted', 'retry-destroy-admitted'):
            current_operation = context.operation_ids[kind.removesuffix('-admitted')]
        require(type(event['ordinal']) is int and event['ordinal'] == ordinal
                and event['kind'] == kind and event['operationId'] == current_operation,
                'sequence-event-identity')
        observed = timestamp(event['observedAt'])
        require(type(event['monotonicNs']) is int and event['monotonicNs'] > previous_ns
                and previous_time <= observed <= end, 'sequence-event-order')
        name = context.event_originals[kind]
        require(event['original'] == name and event['originalSha256'] == context.original_hashes[name],
                'sequence-event-original')
        previous_ns, previous_time = event['monotonicNs'], observed
    return {'status': 'sequence-joined', 'cell': context.cell,
            'sequenceSha256': sha256(original), 'events': len(events),
            'candidateQualified': False, 'sixAwsCellsQualified': False,
            'terminal123Qualified': False}


def verify_matrix_sequences(sequences: Mapping[str, bytes], originals: Mapping[str, Mapping[str, bytes]],
                            contexts: Mapping[str, SequenceContext]):
    require(set(sequences) == set(originals) == set(contexts) == set(CELLS), 'sequence-exact-six-cells')
    require(len({context.run_id for context in contexts.values()}) == 1
            and len({context.candidate_image for context in contexts.values()}) == 1,
            'sequence-matrix-cohort')
    ids = [operation for context in contexts.values() for operation in set(context.operation_ids.values())]
    require(len(ids) == len(set(ids)), 'sequence-matrix-operation-reuse')
    results = []
    for cell in CELLS:
        require(contexts[cell].cell == cell, 'sequence-matrix-cell')
        results.append(verify_sequence(sequences[cell], originals[cell], contexts[cell]))
    # This is intentionally a custody/chronology result, never the semantic
    # six-cell judge. Missing underlying consumers cannot be papered over here.
    return {'status': 'sequences-joined', 'cells': results, 'candidateQualified': False,
            'sixAwsCellsQualified': False, 'terminal123Qualified': False}
