import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/self_test_partitions.py"


def runner():
    spec = importlib.util.spec_from_file_location("self_test_partitions", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_waits_for_every_partition_even_on_failure():
    workflow = yaml.safe_load((ROOT / ".github/workflows/manifest-validate.yml").read_text())
    jobs = workflow["jobs"]
    children = jobs["self-tests"]
    assert children["strategy"]["matrix"]["partition"] == list(range(6))
    assert children["strategy"]["max-parallel"] == 6
    assert children["strategy"]["fail-fast"] is False
    gate = jobs["validate"]
    assert gate["needs"] == ["self-tests"]
    assert gate["if"] == "${{ always() }}"
    admission = gate["steps"][0]
    assert admission["env"] == {"PARTITION_RESULT": "${{ needs.self-tests.result }}"}
    for result in ("success", "failure", "cancelled", "skipped", ""):
        completed = subprocess.run(["bash", "-e", "-c", admission["run"]],
                                   env={**os.environ, "PARTITION_RESULT": result})
        assert (completed.returncode == 0) == (result == "success")
    commands = [step.get("run", "") for step in gate["steps"]]
    assert "python tools/self_test_partitions.py verify artifacts/self-tests" in commands
    assert not any("pytest tools/" in command for command in commands)
    child_commands = [step.get("run", "") for step in children["steps"]]
    assert child_commands.index("python3 tools/check_action_pins.py") < child_commands.index(
        "python -m pip install -r tools/requirements-validation.txt")
    assert 'bash tools/install_gitsign.sh "$RUNNER_TEMP/gitsign-bin"' in child_commands
    assert ("python tools/self_test_partitions.py run ${{ matrix.partition }} "
            "--output artifacts/self-tests/partition-${{ matrix.partition }}.json") in child_commands
    for job in (gate, children):
        assert "continue-on-error" not in job
        assert not any("continue-on-error" in step for step in job["steps"])
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
        assert setup["with"]["cache"] == "pip"
        assert setup["with"]["cache-dependency-path"] == "tools/requirements-validation.txt"


def test_partition_scope_preserves_the_original_pytest_command():
    assert runner().SELF_TEST_PATHS == (
        "tools/", "e2e/test_licensing.py", "e2e/test_upgrade_chaos.py", "e2e/test_cloud.py",
        "e2e/test_deterministic_refusal_profile.py", "e2e/test_cost_meter.py", "e2e/test_aws_eks.py",
        "e2e/test_large_import.py", "e2e/test_retire_cloud_cell.py", "e2e/test_ga_single_tenant.py",
    )


def test_plan_conserves_parametrized_tests_and_keeps_each_file_together():
    inventory = [f"tools/test_{file}.py::test_value[{case}]" for file in range(12) for case in range(3)]
    costs = {f"tools/test_{file}.py": float(file + 1) for file in range(12)}
    partitions = runner().partition_inventory(inventory, costs)
    assert len(partitions) == 6
    assert sorted(node for group in partitions for node in group) == sorted(inventory)
    assert partitions == runner().partition_inventory(list(reversed(inventory)), costs)
    for file in range(12):
        assert sum(any(node.startswith(f"tools/test_{file}.py::") for node in group)
                   for group in partitions) == 1


@pytest.mark.parametrize("inventory", [[], ["a::test_x", "a::test_x"]])
def test_plan_rejects_empty_or_duplicate_inventory(inventory):
    with pytest.raises(ValueError):
        runner().partition_inventory(inventory, {})


@pytest.mark.parametrize("cost", [0, -1, "1", float("inf"), float("nan")])
def test_plan_rejects_invalid_scheduling_costs(cost):
    with pytest.raises(ValueError):
        runner().partition_inventory(["tools/test_example.py::test_value"], {"tools/test_example.py": cost})


def receipts():
    module = runner()
    inventory = [f"tools/test_{file}.py::test_value" for file in range(12)]
    return [{"partition": index, "inventory": inventory, "selected": selected,
             "executed": selected, "exitCode": 0}
            for index, selected in enumerate(module.partition_inventory(inventory, {}))]


def test_receipt_conservation_accepts_only_a_complete_execution():
    runner().verify_receipts(receipts())


@pytest.mark.parametrize("damage", ["missing", "duplicate", "overlap", "omission", "collection-drift",
                                   "failed", "not-executed", "empty", "invalid-index"])
def test_receipt_conservation_refuses_false_green(damage):
    values = receipts()
    if damage == "missing":
        values.pop()
    elif damage == "duplicate":
        values[-1] = values[0]
    elif damage == "overlap":
        values[1]["selected"].append(values[0]["selected"][0])
    elif damage == "omission":
        values[0]["selected"].pop()
    elif damage == "collection-drift":
        values[0]["inventory"] = values[0]["inventory"][:-1]
    elif damage == "failed":
        values[0]["exitCode"] = 1
    elif damage == "not-executed":
        values[0]["executed"] = []
    elif damage == "empty":
        for value in values:
            value.update(inventory=[], selected=[], executed=[])
    else:
        values[0]["partition"] = -1
    with pytest.raises(ValueError):
        runner().verify_receipts(values)


def test_real_pytest_failure_reddens_partition_and_aggregate(tmp_path):
    module = runner()
    for relative in module.SELF_TEST_PATHS:
        path = tmp_path / ("tools/test_example.py" if relative == "tools/" else relative)
        path.parent.mkdir(exist_ok=True)
        path.write_text("def test_pass():\n    assert True\n")
    (tmp_path / "tools/test_example.py").write_text("def test_injected_failure():\n    assert False\n")
    outputs = tmp_path / "receipts"
    outputs.mkdir()
    outcomes = []
    for index in range(6):
        output = outputs / f"partition-{index}.json"
        code = (f"import sys;sys.path.insert(0,{str(RUNNER.parent)!r});"
                "from pathlib import Path;from self_test_partitions import run_partition;"
                f"sys.exit(run_partition(Path({str(tmp_path)!r}),{index},Path({str(output)!r})))")
        outcomes.append(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True))
    assert sorted(result.returncode for result in outcomes) == [0, 0, 0, 0, 0, 1], outcomes
    values = [json.loads(path.read_text()) for path in sorted(outputs.glob("*.json"))]
    failed = next(value for value in values if value["exitCode"] == 1)
    assert "tools/test_example.py::test_injected_failure" in failed["selected"]
    with pytest.raises(ValueError, match="failed"):
        module.verify_receipts(values)
