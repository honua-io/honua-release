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
