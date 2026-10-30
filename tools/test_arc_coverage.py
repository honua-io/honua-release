"""Live-model arc coverage is evidence, not scenario membership."""

import json
from pathlib import Path

import pytest

import arc_coverage


ROOT = Path(__file__).resolve().parents[1]
WHEN = "2026-10-30T01:00:00Z"
LATER = "2026-10-30T02:00:00Z"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _attempt(**extra):
    document = {
        "schemaVersion": "live-model-attempt.v1",
        "liveModel": True,
        "attempt": 1,
        "scenarioId": "P11",
        "target": "aws-ecs",
        "runId": "run-1",
        "completedAt": WHEN,
        "outcome": "pass",
    }
    document.update(extra)
    return document


def _cell(table, arc, target):
    return next(cell for cell in table["cells"] if cell["arc"] == arc and cell["target"] == target)


def _table(*documents, tmp_path):
    for index, document in enumerate(documents):
        _write(tmp_path / f"{index}.json", document)
    return arc_coverage.build_table([tmp_path])


def test_missing_evidence_is_twenty_four_unqualified_cells(tmp_path):
    out = tmp_path / "arc-coverage.v1.json"
    assert arc_coverage.main(["--evidence", str(tmp_path / "missing"), "--out", str(out)]) == 0
    table = json.loads(out.read_text())
    assert table["schemaVersion"] == "arc-coverage.v1"
    assert [(cell["arc"], cell["target"]) for cell in table["cells"]] == [
        (arc, target) for arc in arc_coverage.ARCS for target in arc_coverage.TARGETS]
    assert len(table["cells"]) == 24
    assert all(cell["coverage"] == "unqualified" for cell in table["cells"])


def test_first_live_attempt_fills_only_its_cell_and_ignores_a_later_attempt(tmp_path):
    table = _table({
        "schemaVersion": "arc-coverage-evidence.v1",
        "attempts": [
            _attempt(runId="first", completedAt=WHEN, outcome="fail", status="fail"),
            _attempt(attempt=2, runId="second", completedAt=LATER, outcome="pass"),
            _attempt(scenarioId="P03", target="local-docker", runId="data", outcome="pass", status="pass"),
            _attempt(liveModel=False, scenarioId="P01", target="aws-eks", runId="scripted"),
        ],
    }, tmp_path=tmp_path)
    gp = _cell(table, "gp", "aws-ecs")["coverage"]
    assert gp["scenarioIds"] == ["P11"]
    assert gp["lastRun"] == {"runId": "first", "completedAt": WHEN}
    assert gp["firstAttemptOutcome"] == "fail"
    assert _cell(table, "data", "local-docker")["coverage"]["scenarioIds"] == ["P03"]
    assert _cell(table, "bootstrap", "aws-eks")["coverage"] == "unqualified"
    assert _cell(table, "gp", "aws-eks")["coverage"] == "unqualified"


def test_later_first_attempt_is_the_last_run(tmp_path):
    table = _table(
        _attempt(runId="early", completedAt=WHEN, outcome="fail", status="fail"),
        _attempt(runId="late", completedAt=LATER, outcome="pass", status="pass"),
        tmp_path=tmp_path,
    )
    coverage = _cell(table, "gp", "aws-ecs")["coverage"]
    assert coverage["scenarioIds"] == ["P11"]
    assert coverage["lastRun"]["runId"] == "late"
    assert coverage["firstAttemptOutcome"] == "pass"


@pytest.mark.parametrize("scenario", ["P12", "P13", "L04", "L05", "L06", "P21", "P22", "P23"])
def test_restore_restart_and_cleanup_do_not_cover_operate(tmp_path, scenario):
    table = _table(_attempt(scenarioId=scenario, outcome="pass", status="pass",
                            operateDemonstrated=True), tmp_path=tmp_path)
    assert all(cell["coverage"] == "unqualified" for cell in table["cells"])


def test_explicit_operate_is_covered_and_a_false_gp_flag_is_not(tmp_path):
    table = _table(
        _attempt(scenarioId="O01", arc="operate", target="aws-lambda", outcome="pass", status="pass",
                 executionOutcome="completed", operateDemonstrated=True),
        _attempt(scenarioId="P11", target="aws-eks", outcome="pass", status="pass",
                 functionalGpExecuted=False, executionOutcome="completed"),
        tmp_path=tmp_path,
    )
    assert _cell(table, "operate", "aws-lambda")["coverage"]["scenarioIds"] == ["O01"]
    assert _cell(table, "gp", "aws-eks")["coverage"] == "unqualified"


def test_serving_refusal_journey_does_not_cover_gp_or_operate(tmp_path):
    table = _table({
        "source": "terminal-model-canary",
        "gate": "nightly-model-journey",
        "runId": "900",
        "generatedAt": WHEN,
        "cells": [{
            "cell": "aws-ecs/redis-off",
            "drivers": ["genuine-model"],
            "status": "pass",
            "attempts": [{
                "number": 1, "status": "pass", "driver": "genuine-model",
                "profile": "redis-off-serving-refusal", "executionOutcome": "refused",
                "functionalGpExecuted": False, "completedAt": WHEN,
            }, {
                "number": 2, "status": "pass", "driver": "genuine-model",
                "profile": "full-workflow", "executionOutcome": "completed",
                "functionalGpExecuted": True, "completedAt": LATER,
            }],
        }],
    }, tmp_path=tmp_path)
    assert _cell(table, "gp", "aws-ecs")["coverage"] == "unqualified"
    assert _cell(table, "operate", "aws-ecs")["coverage"] == "unqualified"


def test_full_workflow_journey_covers_gp_only_as_bounded_gp(tmp_path):
    table = _table({
        "source": "terminal-model-canary",
        "gate": "genuine-model-journey",
        "runId": "901",
        "cells": [{
            "cell": "aws-ecs/redis-on",
            "drivers": ["genuine-model"],
            "status": "pass",
            "attempts": [{
                "number": 1, "status": "pass", "driver": "genuine-model",
                "profile": "full-workflow", "executionOutcome": "completed",
                "functionalGpExecuted": True, "completedAt": WHEN,
            }],
        }, {
            "cell": "aws-batch/redis-on",
            "mode": "genuine-model",
            "status": "pass",
            "functionalGpExecuted": True,
            "executionOutcome": "completed",
        }],
    }, tmp_path=tmp_path)
    gp = _cell(table, "gp", "aws-ecs")["coverage"]
    assert gp == {
        "scenarioIds": ["bounded-gp"],
        "lastRun": {"runId": "901", "completedAt": WHEN},
        "firstAttemptOutcome": "pass",
    }
    assert _cell(table, "bootstrap", "aws-ecs")["coverage"] == "unqualified"
    assert _cell(table, "operate", "aws-ecs")["coverage"] == "unqualified"
    assert all(cell["coverage"] == "unqualified" or cell["target"] == "aws-ecs" for cell in table["cells"])


def test_passed_stages_fill_their_arcs_and_gp_still_needs_execution(tmp_path):
    table = _table({
        "gate": "genuine-model-journey",
        "runId": "902",
        "cells": [{
            "cell": "aws-eks/redis-on",
            "mode": "genuine-model",
            "status": "pass",
            "functionalGpExecuted": False,
            "executionOutcome": "refused",
            "profile": "redis-off-serving-refusal",
            "attempts": [{
                "attempt": 1, "status": "pass", "completedAt": WHEN,
                "stages": [
                    {"id": "publish-service", "status": "pass"},
                    {"id": "bounded-gp", "status": "pass"},
                    {"id": "durable-compositions", "status": "pass"},
                ],
            }],
        }],
    }, tmp_path=tmp_path)
    assert _cell(table, "data", "aws-eks")["coverage"]["scenarioIds"] == ["publish-service"]
    assert _cell(table, "publish", "aws-eks")["coverage"]["scenarioIds"] == ["publish-service"]
    assert _cell(table, "gp", "aws-eks")["coverage"] == "unqualified"
    assert _cell(table, "operate", "aws-eks")["coverage"] == "unqualified"


def test_unknown_target_has_no_column_and_malformed_evidence_fails(tmp_path):
    table = _table(_attempt(target="aws-batch"), tmp_path=tmp_path)
    assert all(cell["coverage"] == "unqualified" for cell in table["cells"])
    _write(tmp_path / "broken.json", "{")
    out = tmp_path / "out.json"
    assert arc_coverage.main(["--evidence", str(tmp_path), "--out", str(out)]) == 1
    assert not out.exists()


def test_release_train_publishes_the_table_outside_the_nightly_mint():
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-train.yml").read_text())
    steps = workflow["jobs"]["report"]["steps"]
    publish = next(step for step in steps if step.get("name") == "Publish the arc-by-target model coverage table")
    upload = next(step for step in steps if step.get("name") == "Upload the arc-by-target coverage table")
    assert "python tools/arc_coverage.py" in publish["run"]
    assert "--out out/certified-candidate/arc-coverage.v1.json" in publish["run"]
    assert upload["uses"] == "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"  # v7.0.2
    assert upload["with"]["name"] == "arc-coverage"
    assert upload["with"]["if-no-files-found"] == "error"
    binding = next(step for step in steps if "mint_nightly_lock.py --declare-evidence" in step.get("run", ""))
    nightly = binding["run"].split('if [ "${NIGHTLY:-}" = "true" ]; then\n', 1)[1].split("\nfi", 1)[0]
    assert "arc_coverage.py" not in nightly
    assert "mint_nightly_lock.py" in nightly


def test_raw_canary_receipt_keeps_the_stages_the_folded_report_drops(tmp_path):
    """journey-gate-report artifacts retain model-canary receipts next to the folded row."""
    _write(tmp_path / "gate-report-journey.json", {
        "source": "terminal-model-canary",
        "gate": "genuine-model-journey",
        "runId": "777",
        "generatedAt": WHEN,
        "cells": [{
            "cell": "aws-ecs/redis-on",
            "drivers": ["genuine-model"],
            "status": "pass",
            "attempts": [{
                "number": 1, "status": "pass", "driver": "genuine-model",
                "profile": "full-workflow", "executionOutcome": "completed",
                "functionalGpExecuted": True, "completedAt": WHEN,
            }],
        }],
    })
    _write(tmp_path / "model-canary-1.json", {
        "receiptSchema": "terminal-model-canary-receipt-v1",
        "mode": "genuine-model",
        "cell": "aws-ecs/redis-on",
        "attempt": 1,
        "status": "pass",
        "profile": "full-workflow",
        "executionOutcome": "completed",
        "functionalGpExecuted": True,
        "completedAt": WHEN,
        "stages": [
            {"id": "installed-client-handoff", "status": "pass"},
            {"id": "credential-preflight", "status": "pass"},
            {"id": "publish-service", "status": "pass"},
            {"id": "style-render", "status": "pass"},
            {"id": "bounded-gp", "status": "pass"},
            {"id": "durable-compositions", "status": "pass"},
            {"id": "separate-principal-approval", "status": "pass"},
        ],
    })
    _write(tmp_path / "model-canary-2.json", {
        "receiptSchema": "terminal-model-canary-receipt-v1",
        "mode": "genuine-model",
        "cell": "aws-ecs/redis-on",
        "attempt": 2,
        "status": "pass",
        "profile": "full-workflow",
        "executionOutcome": "completed",
        "functionalGpExecuted": True,
        "completedAt": LATER,
        "stages": [{"id": "installed-client-handoff", "status": "pass"}],
    })
    refusal = tmp_path / "redis-off"
    _write(refusal / "gate-report-journey.json", {
        "source": "terminal-model-canary", "gate": "nightly-model-journey", "runId": "778",
        "cells": [{
            "cell": "aws-ecs/redis-off", "drivers": ["genuine-model"], "status": "pass",
            "attempts": [{"number": 1, "status": "pass", "profile": "redis-off-serving-refusal",
                          "executionOutcome": "refused", "functionalGpExecuted": False, "completedAt": WHEN}],
        }],
    })
    _write(refusal / "model-canary-1.json", {
        "receiptSchema": "terminal-model-redis-off-receipt-v1",
        "mode": "genuine-model",
        "cell": "aws-ecs/redis-off",
        "attempt": 1,
        "status": "pass",
        "profile": "redis-off-serving-refusal",
        "executionOutcome": "refused",
        "functionalGpExecuted": False,
        "completedAt": WHEN,
        "stages": [
            {"id": "publish-service", "status": "pass"},
            {"id": "bounded-gp", "status": "pass"},
        ],
    })
    table = arc_coverage.build_table([tmp_path])
    assert _cell(table, "bootstrap", "aws-ecs")["coverage"]["scenarioIds"] == [
        "credential-preflight", "installed-client-handoff"]
    assert _cell(table, "bootstrap", "aws-ecs")["coverage"]["lastRun"] == {"runId": "777", "completedAt": WHEN}
    assert _cell(table, "data", "aws-ecs")["coverage"]["scenarioIds"] == ["publish-service"]
    assert _cell(table, "publish", "aws-ecs")["coverage"]["scenarioIds"] == [
        "publish-service", "separate-principal-approval"]
    assert _cell(table, "style-render", "aws-ecs")["coverage"]["scenarioIds"] == ["style-render"]
    assert _cell(table, "gp", "aws-ecs")["coverage"]["scenarioIds"] == ["bounded-gp"]
    assert _cell(table, "gp", "aws-ecs")["coverage"]["firstAttemptOutcome"] == "pass"
    assert _cell(table, "operate", "aws-ecs")["coverage"] == "unqualified"
    assert _cell(table, "data", "aws-ecs")["coverage"]["scenarioIds"] == ["publish-service"]
    # The redis-off receipt passed publish. Its GP stage is serving plus refusal, so GP stays empty there.
    assert "publish-service" in _cell(table, "publish", "aws-ecs")["coverage"]["scenarioIds"]
    assert _cell(table, "gp", "aws-ecs")["coverage"]["scenarioIds"] == ["bounded-gp"]


def test_refusal_and_a_false_flag_are_not_gp_or_operate_credit():
    refusal = {"cell": "aws-ecs/redis-off", "functionalGpExecuted": False,
               "profile": "redis-off-serving-refusal", "executionOutcome": "refused", "status": "pass",
               "attempts": [{"attempt": 1, "status": "pass"}]}
    label, gp, operate, excluded, claimed = arc_coverage.journey_cell_credit(refusal)
    assert (label, gp, operate, excluded, claimed) == ("aws-ecs/redis-off", False, False, True, False)
    refusal["coversGp"] = True
    assert arc_coverage.journey_cell_credit(refusal)[4] is True
    executed = {"cell": "aws-ecs/redis-on", "status": "pass", "functionalGpExecuted": True,
                "executionOutcome": "completed", "coversOperate": True,
                "attempts": [{"attempt": 1, "status": "pass"}]}
    label, gp, operate, excluded, claimed = arc_coverage.journey_cell_credit(executed)
    assert (label, gp, operate, excluded, claimed) == ("aws-ecs/redis-on", True, False, False, True)
