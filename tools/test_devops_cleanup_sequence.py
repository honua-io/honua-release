"""Manufactured custody vectors only: never six-cell/provider execution proof."""
from dataclasses import replace
import copy
import pytest

from devops_provisioning_contract import CELLS, Refusal, canonical, sha256
from devops_cleanup_sequence import SEQUENCES, SequenceContext, verify_sequence, verify_matrix_sequences


def vector(cell, index=0):
    names = SEQUENCES[cell]
    originals = {name: canonical({'sourceControlOnly': True, 'kind': name, 'cell': cell}) for name in names}
    operations = {name: 'urn:honua:provisioning:' + format(index * 3 + ordinal + 1, '032x')
                  for ordinal, name in enumerate(('apply', 'destroy', 'retry-destroy')
                  if cell in (CELLS[3], CELLS[4]) else ('apply', 'destroy'))}
    context = SequenceContext(cell, 'owned-run', 'example.invalid/honua@sha256:' + 'a' * 64,
        operations, {name: sha256(raw) for name, raw in originals.items()},
        {name: name for name in names}, '2026-10-20T00:00:00Z', '2026-10-20T00:59:00Z')
    events = []
    operation = operations['apply']
    for ordinal, name in enumerate(names):
        if name in ('destroy-admitted', 'retry-destroy-admitted'):
            operation = operations[name.removesuffix('-admitted')]
        events.append({'ordinal': ordinal, 'kind': name, 'operationId': operation,
            'observedAt': '2026-10-20T00:' + format(ordinal + 1, '02d') + ':00Z',
            'monotonicNs': ordinal + 1, 'original': name, 'originalSha256': sha256(originals[name])})
    value = {'schema': 'honua.devops-cleanup-sequence/v1', 'cell': cell,
             'runId': context.run_id, 'candidateImage': context.candidate_image, 'events': events}
    return value, originals, context


@pytest.mark.parametrize('cell', CELLS)
def test_all_original_sequences_positive_is_only_custody(cell):
    value, originals, context = vector(cell)
    result = verify_sequence(canonical(value), originals, context)
    assert result['status'] == 'sequence-joined'
    assert result['candidateQualified'] is result['sixAwsCellsQualified'] is result['terminal123Qualified'] is False


@pytest.mark.parametrize('cell', CELLS)
@pytest.mark.parametrize('mutation', ['missing-event', 'extra-event', 'reorder', 'wrong-operation',
    'nonmonotonic', 'bool-clock', 'outside-window', 'wrong-original', 'extra-original',
    'modified-original', 'wrong-run', 'wrong-image', 'unrecorded-context-event'])
def test_each_cell_refuses_custody_or_sequence_substitution(cell, mutation):
    value, originals, context = vector(cell)
    if mutation == 'missing-event': value['events'].pop(1)
    if mutation == 'extra-event': value['events'].append(copy.deepcopy(value['events'][-1]))
    if mutation == 'reorder': value['events'][1], value['events'][2] = value['events'][2], value['events'][1]
    if mutation == 'wrong-operation': value['events'][-1]['operationId'] = context.operation_ids['apply']
    if mutation == 'nonmonotonic': value['events'][1]['monotonicNs'] = value['events'][0]['monotonicNs']
    if mutation == 'bool-clock': value['events'][0]['monotonicNs'] = True
    if mutation == 'outside-window': value['events'][-1]['observedAt'] = '2026-10-21T00:00:00Z'
    if mutation == 'wrong-original': value['events'][0]['originalSha256'] = 'f' * 64
    if mutation == 'extra-original': originals['unrequested'] = b'unrequested'
    if mutation == 'modified-original': originals[next(iter(originals))] += b' '
    if mutation == 'wrong-run': value['runId'] = 'foreign'
    if mutation == 'wrong-image': value['candidateImage'] = 'foreign@sha256:' + 'b' * 64
    if mutation == 'unrecorded-context-event': context = replace(context, event_originals={})
    with pytest.raises(Refusal): verify_sequence(canonical(value), originals, context)


def test_matrix_requires_all_six_and_no_cross_cell_operation_reuse():
    values = {}; originals = {}; contexts = {}
    for index, cell in enumerate(CELLS):
        value, originals[cell], contexts[cell] = vector(cell, index)
        values[cell] = canonical(value)
    result = verify_matrix_sequences(values, originals, contexts)
    assert len(result['cells']) == 6 and result['sixAwsCellsQualified'] is False
    with pytest.raises(Refusal, match='exact-six'):
        verify_matrix_sequences(dict(list(values.items())[:-1]), originals, contexts)
    contexts[CELLS[1]] = replace(contexts[CELLS[1]], operation_ids=contexts[CELLS[0]].operation_ids)
    with pytest.raises(Refusal, match='operation-reuse'):
        verify_matrix_sequences(values, originals, contexts)


@pytest.mark.parametrize('cell',[CELLS[3],CELLS[4]])
def test_retry_custody_does_not_require_an_unrelated_new_intent(cell):
    value, originals, context = vector(cell)
    operations = dict(context.operation_ids, **{'retry-destroy':context.operation_ids['destroy']})
    context = replace(context, operation_ids=operations)
    for event in value['events']:
        if event['kind'] in ('retry-destroy-admitted','retry-destroy-terminal','final-inventory'):
            event['operationId'] = operations['destroy']
    result = verify_sequence(canonical(value), originals, context)
    assert result['status']=='sequence-joined' and result['sixAwsCellsQualified'] is False
