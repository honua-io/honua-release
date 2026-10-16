"""Adversarial tests for reservations, not a retrospective billing calculator."""
from decimal import Decimal
import json
from pathlib import Path
import pytest

from agent_e2e_budget import BudgetError, update, validate


KEY = "sha256:" + "1" * 64
BASIS = "sha256:" + "2" * 64


def reserve(path, rid, amount, *, campaign="c1", kind="model"):
    return update(path, "reserve", candidate_key=KEY, reservation_id=rid,
                  amount=amount, campaign_id=campaign, case_id="P01:A:local-docker",
                  attempt=1, kind=kind, basis_digest=BASIS)


def test_candidate_cannot_reset_spend_with_new_campaign(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 440)
    update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=440)
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError, match="reserve"):
        reserve(path, "r2", 11, campaign="c2")
    assert len(json.loads(path.read_text())["entries"]) == 1


def test_cleanup_can_spend_reserved_funds_but_model_cannot(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "compute", 450, kind="infrastructure")
    with pytest.raises(BudgetError):
        reserve(path, "model", 1)
    assert Decimal(reserve(path, "cleanup", 50, kind="cleanup")["remainingUsd"]) == 0
    with pytest.raises(BudgetError):
        reserve(path, "extra-cleanup", 1, kind="cleanup")


def test_actual_overrun_is_retained_and_blocks_later_reservations(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 1)
    with pytest.raises(BudgetError, match="exceeded"):
        update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=2)
    assert json.loads(path.read_text())["entries"][0]["actualUsd"] == "2"
    with pytest.raises(BudgetError, match="exceeded"):
        reserve(path, "r2", 1)


@pytest.mark.parametrize("value", [True, -1, "NaN", "Infinity", "-Infinity"])
def test_invalid_cost_cannot_enter_ledger(tmp_path, value):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError):
        reserve(path, "bad", value)
    assert json.loads(path.read_text())["entries"] == []


def test_reservation_is_not_reusable_and_unsettled_liability_counts(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    reserve(path, "r1", 400)
    with pytest.raises(BudgetError):
        reserve(path, "r1", 1)
    with pytest.raises(BudgetError):
        reserve(path, "r2", 51)
    update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=10)
    reserve(path, "r2", 400)
    with pytest.raises(BudgetError):
        update(path, "settle", candidate_key=KEY, reservation_id="r1", amount=0)


def test_wrong_candidate_and_concurrent_writer_refused(tmp_path):
    path = tmp_path / "ledger.json"
    update(path, "init", candidate_key=KEY)
    with pytest.raises(BudgetError):
        update(path, "init", candidate_key="sha256:" + "3" * 64)
    path.with_suffix(".json.lock").touch()
    with pytest.raises(BudgetError, match="in use"):
        reserve(path, "r1", 1)


def reserve_operation(path, rid='bootstrap-infra', amount='400', **overrides):
    values = {'candidate_key':KEY, 'reservation_id':rid, 'operation_id':'bootstrap',
              'purpose':'bootstrap', 'kind':'infrastructure', 'amount':amount, 'basis_digest':BASIS}
    return update(path, 'reserve-operation', **{**values, **overrides})


def test_shared_operations_and_attempts_share_one_ceiling(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path)
    reserve(path, 'attempt', '50')
    with pytest.raises(BudgetError, match='reserve'):
        reserve(path, 'another', '0.01', campaign='new-campaign')
    assert Decimal(reserve_operation(path, 'cleanup', '50', kind='cleanup')['remainingUsd']) == 0
    update(path, 'init', candidate_key=KEY)
    assert len(json.loads(path.read_text())['operations']) == 2


def test_operation_overrun_is_retained_and_blocks_attempt(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path, amount='1')
    with pytest.raises(BudgetError, match='exceeded'):
        update(path, 'settle', candidate_key=KEY, reservation_id='bootstrap-infra', amount='2')
    assert json.loads(path.read_text())['operations'][0]['actualUsd'] == '2'
    with pytest.raises(BudgetError, match='exceeded'):
        reserve(path, 'model', '1')


@pytest.mark.parametrize('override', [{'kind':'model'}, {'purpose':'test-case'}, {'operation_id':'../case'}, {'operation_id':''}])
def test_operations_cannot_disguise_model_attempts(tmp_path, override):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    with pytest.raises(BudgetError, match='operation'):
        reserve_operation(path, **override)
    assert json.loads(path.read_text())['operations'] == []


def test_reservation_ids_are_unique_across_scopes(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve(path, 'shared-id', 1)
    with pytest.raises(BudgetError, match='duplicate'):
        reserve_operation(path, rid='shared-id')


def test_legacy_ledger_cannot_silently_acquire_operation_charges(tmp_path):
    path = tmp_path/'ledger.json'
    path.write_text(json.dumps({'schemaVersion':1, 'candidateKey':KEY, 'ceilingUsd':'500',
        'cleanupReserveUsd':'50', 'entries':[]}))
    original = path.read_bytes()
    with pytest.raises(BudgetError, match='migration'):
        reserve_operation(path)
    assert path.read_bytes() == original


def test_overrun_blocks_new_work_but_later_cleanup_charge_is_retained(tmp_path):
    path = tmp_path/'ledger.json'
    update(path, 'init', candidate_key=KEY)
    reserve_operation(path, rid='infra', amount='1')
    reserve_operation(path, rid='cleanup', amount='1', kind='cleanup')
    for rid, actual in [('infra','2'), ('cleanup','3')]:
        with pytest.raises(BudgetError, match='exceeded'):
            update(path, 'settle', candidate_key=KEY, reservation_id=rid, amount=actual)
    rows = json.loads(path.read_text())['operations']
    assert [row['actualUsd'] for row in rows] == ['2','3']
    with pytest.raises(BudgetError, match='exceeded'):
        reserve(path, 'model', '1')
