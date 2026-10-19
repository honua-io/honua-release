"""Non-case cost reports cannot hide bootstrap/shared charges or qualify cases."""
import copy
import json

import pytest

from agent_e2e_budget import validate as validate_budget
from agent_e2e_gate import Invalid, digest, validate_operation_reports


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return {'path':path.name, 'sha256':digest(path.read_bytes())}


@pytest.fixture
def accounting(tmp_path):
    candidate = 'sha256:'+'a'*64
    rows = [{'id':'bootstrap-'+kind, 'operationId':'bootstrap', 'purpose':'bootstrap',
             'kind':kind, 'amountUsd':'10', 'basisDigest':'sha256:'+'b'*64, 'state':'reserved', 'actualUsd':None}
            for kind in ('infrastructure','cleanup')]
    ledger = {'schemaVersion':2, 'candidateKey':candidate, 'ceilingUsd':'500', 'cleanupReserveUsd':'50',
              'entries':[], 'operations':rows}
    observation = write(tmp_path/'cost.json', {'fixture':'independently retained observation'})
    report = {'schemaVersion':1, 'candidateKey':candidate, 'operationId':'bootstrap', 'purpose':'bootstrap',
              'status':'liability-retained', 'reservations':copy.deepcopy(rows), 'observations':[observation]}
    return ledger, report, tmp_path


def check(accounting):
    ledger, report, root = accounting
    ref = write(root/'report.json', report)
    validate_budget(ledger)
    validate_operation_reports(ledger, [ref], root)


def test_unknown_usage_retains_full_liability_without_fake_attempts(accounting):
    check(accounting)
    ledger, _, _ = accounting
    assert ledger['entries'] == []
    assert validate_budget(ledger)['liabilityUsd'] == '20'


@pytest.mark.parametrize('change', ['omit-charge','reduce-price','invent-settlement','wrong-candidate',
                                  'wrong-purpose','unknown-operation','no-observation','altered-observation'])
def test_reports_cannot_hide_or_relabel_operations(accounting, change):
    _, report, root = accounting
    if change == 'omit-charge':
        report['reservations'].pop()
    elif change == 'reduce-price':
        report['reservations'][0]['amountUsd'] = '0.1'
    elif change == 'invent-settlement':
        report['status'] = 'settled'
    elif change == 'wrong-candidate':
        report['candidateKey'] = 'sha256:'+'c'*64
    elif change == 'wrong-purpose':
        report['purpose'] = 'telemetry'
    elif change == 'unknown-operation':
        report['operationId'] = 'new-campaign'
    elif change == 'no-observation':
        report['observations'] = []
    else:
        (root/'cost.json').write_text('{}')
    with pytest.raises(Invalid):
        check(accounting)


def test_operation_reports_are_complete_and_unique(accounting):
    ledger, report, root = accounting
    ref = write(root/'report.json', report)
    with pytest.raises(Invalid, match='missing'):
        validate_operation_reports(ledger, [], root)
    with pytest.raises(Invalid, match='duplicate'):
        validate_operation_reports(ledger, [ref,ref], root)
    ledger['operations'].append({**ledger['operations'][0], 'id':'retained-storage', 'operationId':'storage', 'purpose':'retention'})
    with pytest.raises(Invalid, match='omit budget history'):
        validate_operation_reports(ledger, [ref], root)


def test_legacy_budget_and_missing_bootstrap_cannot_qualify(accounting):
    ledger, report, root = accounting
    ref = write(root/'report.json', report)
    ledger['schemaVersion'] = 1
    with pytest.raises(Invalid, match='operation-aware'):
        validate_operation_reports(ledger, [ref], root)
    ledger['schemaVersion'] = 2
    ledger['operations'] = []
    with pytest.raises(Invalid, match='bootstrap'):
        validate_operation_reports(ledger, [ref], root)


def test_settlement_requires_updated_report_and_keeps_all_history(accounting):
    ledger, report, _ = accounting
    for row in ledger['operations']:
        row.update(state='settled', actualUsd='0.5')
    with pytest.raises(Invalid, match='retained charges'):
        check(accounting)
    report['reservations'] = copy.deepcopy(ledger['operations'])
    report['status'] = 'settled'
    check(accounting)
    assert validate_budget(ledger)['liabilityUsd'] == '1.0'
