"""Challenge the knowledge/semantic bindings independently of runtime fixtures."""
import copy

import pytest

import agent_e2e_gate as gate
from agent_e2e_pack import PackError
from test_agent_e2e_gate import campaign, change_receipt, validate, write


def test_attestation_cannot_hide_knowledge_interface_or_semantic_drift(campaign):
    receipt = gate.read_json(campaign/'receipts/0.json')
    wrong = copy.deepcopy(receipt['knowledge'])
    wrong['sourceRevision'] = 'f'*40
    change_receipt(campaign, lambda r: r.__setitem__('knowledge', wrong))
    with pytest.raises(gate.Invalid, match='knowledge binding'):
        validate(campaign)
    change_receipt(campaign, lambda r: r.__setitem__('knowledge', receipt['knowledge']))

    ref = write(campaign/'evidence/no-interfaces.json', {'caseId':receipt['caseId'], 'attempt':1, 'interfaces':[]})
    change_receipt(campaign, lambda r: r.__setitem__('runtimeInterfaces', ref))
    with pytest.raises(gate.Invalid, match='no observed runtime interfaces'):
        validate(campaign)
    change_receipt(campaign, lambda r: r.__setitem__('runtimeInterfaces', receipt['runtimeInterfaces']))

    report = gate.read_json(campaign/'evidence/semantics.json')
    report['configurations'][0]['checks'].pop()
    ref = write(campaign/'evidence/semantics.json', report)
    trusted = gate.read_json(campaign/'trusted.json')
    trusted['semanticReport'] = ref
    write(campaign/'trusted.json', trusted)
    with pytest.raises(PackError, match='coverage incomplete'):
        validate(campaign)


def test_development_failure_retains_cost_without_becoming_qualification_history(campaign):
    registry = gate.read_json(campaign/'registry.json')
    prior = {**registry['attempts'][0], 'campaignId':'development-earlier', 'status':'fail',
             'failureAttribution':'model', 'campaignMode':'development'}
    registry['attempts'].append(prior)
    write(campaign/'registry.json', registry)
    ledger = gate.read_json(campaign/'ledger.json')
    ledger['entries'].append({**ledger['entries'][0], 'id':'development-charge',
                              'campaignId':'development-earlier', 'campaignMode':'development',
                              'amountUsd':'2', 'actualUsd':'2'})
    write(campaign/'ledger.json', ledger)
    trusted = gate.read_json(campaign/'trusted.json')
    trusted['attemptRegistryDigest'] = gate.digest((campaign/'registry.json').read_bytes())
    trusted['budgetLedgerDigest'] = gate.digest((campaign/'ledger.json').read_bytes())
    write(campaign/'trusted.json', trusted)
    result = validate(campaign)
    assert result['casesPassed'] == 252
    assert result['totalCostUsd'] == '7.56'
    assert result['candidateLiabilityUsd'] == '9.58'  # Includes retained bootstrap costs as well as development.


@pytest.mark.parametrize('replace', ['configuration', 'rawResponses', 'observation'])
def test_semantic_target_relabel_or_cross_target_replay_fails(campaign, replace):
    report = gate.read_json(campaign/'evidence/semantics.json')
    source, destination = report['configurations'][:2]
    if replace == 'configuration':
        copied = copy.deepcopy(source)
        copied['target'] = destination['target']
        report['configurations'][1] = copied
    else:
        destination['checks'][0][replace] = source['checks'][0][replace]
    ref = write(campaign/'evidence/semantics.json', report)
    trusted = gate.read_json(campaign/'trusted.json')
    trusted['semanticReport'] = ref
    write(campaign/'trusted.json', trusted)
    with pytest.raises(PackError, match='target/candidate|another deployment'):
        validate(campaign)
