"""Refuse mutations outside the single reviewed Lambda inventory exception."""
import copy
import json

import pytest
import cloud_cell_inventory_policy as inventory

from cloud_cell_inventory_policy import (
    ADDRESS, POLICY, READ_ACTION, ROLE, SOURCE, confirm_policy, desired_policy, validate_plan,
)


def inputs():
    before = json.loads((SOURCE / "guardrails.before.json").read_text())
    after = desired_policy(before)
    values = {"name": POLICY, "role": ROLE}
    plan = {"resource_changes": [{"address": ADDRESS, "type": "aws_iam_role_policy", "mode": "managed",
        "change": {"actions": ["update"], "importing": {"id": ROLE + ":" + POLICY},
                   "before": {**values, "policy": json.dumps(before)},
                   "after": {**values, "policy": json.dumps(after)}}}]}
    return before, after, plan


def test_only_standing_version_inventory_is_excepted():
    before, after, plan = inputs()
    assert len(before["Statement"]) == len(after["Statement"])
    for old, new in zip(before["Statement"], after["Statement"]):
        if old["Sid"] == "DenyStandingTaggedResources":
            expected = copy.deepcopy(old)
            del expected["Action"]
            expected["NotAction"] = [READ_ACTION]
            assert new == expected and new["Effect"] == "Deny"
        else:
            assert old == new
    assert validate_plan(plan, before, after, before)["actions"] == ["update"]


def test_completed_update_can_be_reimported_without_resetting_policy():
    before, after, plan = inputs()
    change = plan["resource_changes"][0]["change"]
    change["actions"] = ["no-op"]
    change["before"] = copy.deepcopy(change["after"])
    assert validate_plan(plan, before, after, after)["actions"] == ["no-op"]


@pytest.mark.parametrize("actions", [["create"], ["delete"], ["delete", "create"], ["create", "delete"]])
def test_creation_deletion_and_replacement_refused(actions):
    before, after, plan = inputs()
    plan["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(ValueError, match="in-place"):
        validate_plan(plan, before, after, before)


@pytest.mark.parametrize("field,value", [("role", "other"), ("name", "other"), ("policy", '{"Statement":[]}')])
@pytest.mark.parametrize("side", ["before", "after"])
def test_wrong_identity_or_policy_refused(field, value, side):
    before, after, plan = inputs()
    plan["resource_changes"][0]["change"][side][field] = value
    with pytest.raises(ValueError):
        validate_plan(plan, before, after, before)


def test_undeclared_live_drift_refused():
    before, after, plan = inputs()
    drift = copy.deepcopy(before)
    drift["Statement"].pop()
    with pytest.raises(ValueError, match="live policy drift"):
        validate_plan(plan, before, after, drift)


@pytest.mark.parametrize("mutation", ["extra", "missing", "import", "address", "deferred", "drift"])
def test_only_exact_complete_import_plan_allowed(mutation):
    before, after, plan = inputs()
    row = plan["resource_changes"][0]
    if mutation == "extra":
        plan["resource_changes"].append(copy.deepcopy(row))
    elif mutation == "missing":
        plan["resource_changes"] = []
    elif mutation == "import":
        row["change"]["importing"] = {"id": "other:other"}
    elif mutation == "address":
        row["address"] = "aws_iam_role.other"
    elif mutation == "deferred":
        plan["deferred_changes"] = [{}]
    else:
        plan["resource_drift"] = [{}]
    with pytest.raises(ValueError):
        validate_plan(plan, before, after, before)


def test_readback_allows_original_then_exact_desired_revision():
    before, after, _ = inputs()
    observations = iter([before, before, after])
    sleeps = []
    assert confirm_policy(lambda: next(observations), before, after, sleep=sleeps.append)
    assert sleeps == [3, 3]


def test_readback_never_accepts_stale_policy_and_stops_at_bound():
    before, after, _ = inputs()
    observations, sleeps = [], []
    def observe():
        observations.append(True)
        return before
    assert not confirm_policy(observe, before, after, sleep=sleeps.append, attempts=3)
    assert len(observations) == 3 and sleeps == [3, 3]


def test_readback_refuses_unreviewed_policy_immediately():
    before, after, _ = inputs()
    sleeps = []
    with pytest.raises(ValueError, match="unreviewed policy"):
        confirm_policy(lambda: {}, before, after, sleep=sleeps.append)
    assert sleeps == []


def test_readback_does_not_hide_api_failure():
    before, after, _ = inputs()
    def observe():
        raise RuntimeError("inventory denied")
    with pytest.raises(RuntimeError, match="inventory denied"):
        confirm_policy(observe, before, after, sleep=lambda _: None)


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_interrupted_receipt_transition_preserves_uncertainty(tmp_path, monkeypatch, operation):
    path = tmp_path / "receipt.json"
    previous = {"applyAttempted": True, "applied": None, "observedDesired": None}
    inventory.write_receipt(path, previous)
    def interrupt(*_):
        raise InterruptedError("cancelled")
    monkeypatch.setattr(inventory.os, operation, interrupt)
    with pytest.raises(InterruptedError):
        inventory.write_receipt(path, {"applied": True, "observedDesired": True})
    assert json.loads(path.read_text()) == previous


def test_complete_receipt_transition_replaces_only_after_write(tmp_path):
    path = tmp_path / "receipt.json"
    inventory.write_receipt(path, {"applied": None})
    inventory.write_receipt(path, {"applied": True, "observedDesired": True})
    assert json.loads(path.read_text()) == {"applied": True, "observedDesired": True}
    assert not path.with_name(path.name + ".pending").exists()
