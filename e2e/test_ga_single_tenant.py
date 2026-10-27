"""Founder ruling ga-cells-single-tenant-2026-10-27: single-tenant GA journey, one declared N/A check."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_journey
import ga_single_tenant as gst

CELL = "aws-ecs/redis-on"
KEY = "ab" * 32


def stage(number, *checks, status="pass", blocked_by=None):
    return {"number": number, "status": status, "blockedBy": blocked_by or [],
            "evidence": {"source": "live-aws-ecs"},
            "checks": [{"id": f"{number}.probe", "status": "pass"}, *checks]}


def single_tenant_receipt(cell=CELL):
    """The shape strict run 40435209970 recorded on aws-ecs/redis-on with the journey single-tenant:
    stages 1-8 pass except 8.tenant-isolation, blocked by the tracked second-tenant gap."""
    stages = [stage(n) for n in range(1, 8)]
    stages.append(stage(8, {"id": "8.execution", "status": "pass"}, {"id": "8.rbac-denial", "status": "pass"},
                        {"id": gst.CHECK, "status": "blocked", "blockedBy": [gst.BLOCKER]},
                        status="blocked", blocked_by=[gst.BLOCKER]))
    return {"status": "blocked", "mode": "live", "roster": {"status": "pass"},
            "target": {"id": cell, "kind": cell.split("/")[0]}, "stages": stages}


def ruling_file(tmp_path, **changes):
    ruling = {"owner": "Mike McDougall (founder)", "cells": sorted(gst.GA_CELLS),
              "not_applicable_checks": [gst.CHECK], **changes}
    ruling = {k: v for k, v in ruling.items() if v is not None}
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"rulings": {gst.RULING: ruling}}))
    return path


def test_committed_ruling_lists_exactly_the_ga_ecs_cells():
    assert gst.ruled_cells() == {"aws-ecs/redis-off", "aws-ecs/redis-on"}
    ruling = json.loads(gst.DECISION_OVERRIDES.read_text())["rulings"][gst.RULING]
    assert "founder" in ruling["owner"].lower() and ruling["authority"].startswith("https://github.com/")


def test_green_single_tenant_receipt_with_only_the_declared_check_is_admitted():
    assert gst.admits(single_tenant_receipt(), CELL)
    assert gst.declaration(CELL) == {"profile": "ga-single-tenant", "ruling": gst.RULING,
                                     "cell": CELL, "notApplicable": ["8.tenant-isolation"]}


def test_green_qualify_attempt_admits_it_on_the_ga_cell(monkeypatch):
    monkeypatch.setattr(cloud_journey, "validate_attempt", lambda *a, **k: False)
    assert cloud_journey.qualify_attempt({}, single_tenant_receipt(), CELL, raw_path=Path("r"),
                                         run_id="1", run_attempt="1")


@pytest.mark.parametrize("cell", ["aws-serverless/redis-on", "aws-eks/redis-on"])
def test_red_preview_cells_are_never_admitted(cell, monkeypatch):
    monkeypatch.setattr(cloud_journey, "validate_attempt", lambda *a, **k: False)
    receipt = single_tenant_receipt(cell)
    for item in receipt["stages"]:
        item["evidence"]["source"] = "live-" + cell.split("/")[0]
    assert not gst.admits(receipt, cell)
    assert not cloud_journey.qualify_attempt({}, receipt, cell, raw_path=Path("r"), run_id="1", run_attempt="1")


@pytest.mark.parametrize("ruling", [
    None,                                                        # missing ruling
    {"owner": "release coordinator"},                            # not a founder ruling
    {"cells": ["aws-ecs/redis-on", "aws-serverless/redis-on"]},  # used outside the GA cloud cells
    {"cells": []},
    {"not_applicable_checks": ["8.tenant-isolation", "8.rbac-denial"]},
])
def test_red_a_missing_or_widened_ruling_admits_nothing_and_keeps_tenant_mode(tmp_path, ruling):
    path = tmp_path / "none.json" if ruling is None else ruling_file(tmp_path, **ruling)
    if ruling is None:
        path.write_text(json.dumps({"rulings": {}}))
    assert gst.ruled_cells(path) == frozenset()
    assert not gst.admits(single_tenant_receipt(), CELL, path)
    assert gst.journey_tenant_key(CELL, KEY, path) == KEY


@pytest.mark.parametrize("defect", ["stage7-fails", "other-check-blocked", "other-blocker",
                                    "approval-check-blocked", "roster", "build-mode", "wrong-cell", "passing-raw"])
def test_red_anything_beyond_the_single_declared_check_is_not_admitted(defect):
    receipt = single_tenant_receipt()
    if defect == "stage7-fails":  # the two-tenant 404 of run 40497820221
        receipt["stages"][6]["status"] = "fail"
        receipt["stages"][6]["checks"].append({"id": "7.durable-proposal", "status": "fail"})
    elif defect == "other-check-blocked":
        receipt["stages"][2]["checks"][0]["status"] = "blocked"
    elif defect == "other-blocker":
        for item in (receipt["stages"][7], receipt["stages"][7]["checks"][-1]):
            item["blockedBy"] = ["https://github.com/honua-io/honua-release/issues/123"]
    elif defect == "approval-check-blocked":
        receipt["stages"][7]["checks"][1]["status"] = "blocked"
    elif defect == "roster":
        receipt["roster"]["status"] = "blocked"
    elif defect == "build-mode":
        receipt["mode"] = "build"
    elif defect == "wrong-cell":
        receipt["target"]["id"] = "aws-ecs/redis-off"
    elif defect == "passing-raw":
        receipt["status"] = "pass"
    assert not gst.admits(receipt, CELL)


def test_journey_runs_single_tenant_on_ruled_cells_but_the_issuer_key_is_kept_elsewhere():
    assert gst.journey_tenant_key(CELL, KEY) is None
    assert gst.journey_tenant_key("aws-ecs/redis-off", KEY) is None
    assert gst.journey_tenant_key("aws-serverless/redis-on", KEY) == KEY
    assert gst.journey_tenant_key("aws-eks/redis-on", KEY) == KEY


def test_attempt_builds_a_single_tenant_target_on_a_ruled_cell(monkeypatch, tmp_path):
    """The journey target carries no tenant fixture; the redaction set still covers the sealed key."""
    built = {}

    class Targets:
        def kind_of(self, cell):
            return cell.split("/")[0]

        def build(self, template, *, cell, endpoint, tenant_fixture):
            built["tenant_fixture"] = tenant_fixture
            raise RuntimeError("stop after the target is built")

    class Driver:
        def load(self, path):
            return {}

    monkeypatch.setattr(cloud_journey, "drivers", lambda: (Driver(), type("A", (), {"DEFAULT_TARGET": "t"})()))
    monkeypatch.setattr(cloud_journey, "cloud_targets", lambda: Targets())
    monkeypatch.setattr(cloud_journey, "manifest", lambda: {})
    monkeypatch.setattr(cloud_journey, "cell_dir", lambda cell: tmp_path)
    for cell, expected in ((CELL, False), ("aws-serverless/redis-on", True)):
        with pytest.raises(RuntimeError, match="stop after"):
            cloud_journey.attempt(cell, 1, "https://cell.example", "admin", tenant_signing_key=KEY)
        assert built["tenant_fixture"] is expected


def test_the_receipt_fixture_mirrors_the_recorded_run_shape():
    receipt = single_tenant_receipt()
    blocked = [(s["number"], c["id"]) for s in receipt["stages"] for c in s["checks"] if c["status"] != "pass"]
    assert blocked == [(8, "8.tenant-isolation")]
    assert copy.deepcopy(receipt) == receipt
