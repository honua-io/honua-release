import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/self_test_partitions.py"


def load_partition_runner():
    spec = importlib.util.spec_from_file_location("self_test_partitions", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest_jobs():
    return yaml.safe_load((ROOT / ".github/workflows/manifest-validate.yml").read_text())["jobs"]


def test_validate_waits_for_every_partition_even_on_failure(manifest_jobs):
    jobs = manifest_jobs
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


def test_partition_environments_preserve_signing_and_pins(manifest_jobs):
    children = manifest_jobs["self-tests"]
    child_commands = [step.get("run", "") for step in children["steps"]]
    assert child_commands.index("python3 tools/check_action_pins.py") < child_commands.index(
        "python -m pip install -r tools/requirements-validation.txt")
    assert 'bash tools/install_gitsign.sh "$RUNNER_TEMP/gitsign-bin"' in child_commands
    assert ("python tools/self_test_partitions.py run ${{ matrix.partition }} "
            "--output artifacts/self-tests/partition-${{ matrix.partition }}.json") in child_commands
    for job in (manifest_jobs["validate"], children):
        commands = [step.get("run", "") for step in job["steps"]]
        assert commands.index("python3 tools/check_action_pins.py") < commands.index(
            "python -m pip install -r tools/requirements-validation.txt")
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["fetch-depth"] == 0
        assert "continue-on-error" not in job
        assert not any("continue-on-error" in step for step in job["steps"])
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
        assert setup["with"]["cache"] == "pip"
        assert setup["with"]["cache-dependency-path"] == "tools/requirements-validation.txt"


def test_partition_scope_preserves_the_original_pytest_command():
    assert load_partition_runner().SELF_TEST_PATHS == (
        "tools/", "e2e/test_licensing.py", "e2e/test_upgrade_chaos.py", "e2e/test_cloud.py",
        "e2e/test_deterministic_refusal_profile.py", "e2e/test_cost_meter.py", "e2e/test_aws_eks.py",
        "e2e/test_large_import.py", "e2e/test_retire_cloud_cell.py", "e2e/test_ga_single_tenant.py",
    )


def test_plan_conserves_parametrized_tests_and_keeps_each_file_together():
    inventory = [f"tools/test_{file}.py::test_value[{case}]" for file in range(12) for case in range(3)]
    costs = {f"tools/test_{file}.py": float(file + 1) for file in range(12)}
    partitions = load_partition_runner().partition_inventory(inventory, costs)
    assert len(partitions) == 6
    assert sorted(node for group in partitions for node in group) == sorted(inventory)
    assert [sum(costs[file] for file in {node.split("::", 1)[0] for node in group})
            for group in partitions] == [13] * 6
    assert partitions == load_partition_runner().partition_inventory(list(reversed(inventory)), costs)
    for file in range(12):
        assert sum(any(node.startswith(f"tools/test_{file}.py::") for node in group)
                   for group in partitions) == 1


@pytest.mark.parametrize("inventory", [[], ["a::test_x", "a::test_x"]])
def test_plan_rejects_empty_or_duplicate_inventory(inventory):
    with pytest.raises(ValueError):
        load_partition_runner().partition_inventory(inventory, {})


@pytest.mark.parametrize("cost", [0, -1, True, "1", float("inf"), float("nan")])
def test_plan_rejects_invalid_scheduling_costs(cost):
    with pytest.raises(ValueError):
        load_partition_runner().partition_inventory(["tools/test_example.py::test_value"], {"tools/test_example.py": cost})


def receipts():
    module = load_partition_runner()
    inventory = [f"tools/test_{file}.py::test_value" for file in range(12)]
    return [{"partition": index, "inventory": inventory, "selected": selected,
             "executed": selected, "exitCode": 0}
            for index, selected in enumerate(module.partition_inventory(inventory, {}))]


def test_receipt_conservation_accepts_only_a_complete_execution():
    load_partition_runner().verify_receipts(receipts())


@pytest.mark.parametrize("damage,reason", [
    ("missing", "missing or duplicate"), ("duplicate", "missing or duplicate"),
    ("overlap", "exactly once"), ("omission", "exactly once"),
    ("collection-drift", "collection drift"), ("failed", "partition failed"),
    ("not-executed", "full selection"), ("empty", "nonempty and unique"),
    ("invalid-index", "missing or duplicate"),
])
def test_receipt_conservation_refuses_false_green(damage, reason):
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
    with pytest.raises(ValueError, match=reason):
        load_partition_runner().verify_receipts(values)


def fixture_runner(root):
    module = load_partition_runner()
    (root / ".github").mkdir()
    (root / ".github/self-test-costs.json").write_text(json.dumps({"tools/test_example.py": 20}))
    package = root / "fixture_namespace"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n")
    for relative in module.SELF_TEST_PATHS:
        path = root / ("tools/test_example.py" if relative == "tools/" else relative)
        path.parent.mkdir(exist_ok=True)
        path.write_text("from fixture_namespace import VALUE\ndef test_pass():\n    assert VALUE == 1\n")
    script = root / "tools/self_test_partitions.py"
    shutil.copyfile(RUNNER, script)
    return script


@pytest.mark.parametrize("mode", ["success", "failure", "subtest-failure", "setup-only"])
def test_workflow_entrypoint_conserves_real_outcomes(tmp_path, mode):
    script = fixture_runner(tmp_path)
    cases = [1, -1] if mode == "subtest-failure" else [1, 2]
    with (tmp_path / "tools/test_example.py").open("a") as stream:
        stream.write("import unittest\nclass TestSubtests(unittest.TestCase):\n"
                     f"    def test_values(self):\n        for value in {cases!r}:\n"
                     "            with self.subTest(value=value):\n                self.assertGreater(value, 0)\n")
    if mode in ("failure", "setup-only"):
        with (tmp_path / "tools/test_example.py").open("a") as stream:
            stream.write("def test_injected_failure():\n    assert False\n")
    else:
        with (tmp_path / "tools/test_example.py").open("a") as stream:
            stream.write("import pytest\n@pytest.mark.skip(reason='fixture skip')\ndef test_skip():\n    assert False\n")
    outputs = tmp_path / "receipts"
    outputs.mkdir()
    environment = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    if mode == "setup-only":
        environment["PYTEST_ADDOPTS"] = "--setup-only"
    outcomes = []
    for index in range(6):
        output = outputs / f"partition-{index}.json"
        outcomes.append(subprocess.run([sys.executable, str(script), "run", str(index), "--output", str(output)],
                                       cwd=tmp_path, env=environment, capture_output=True, text=True))
    expected = {"success": [0] * 6, "failure": [0, 0, 0, 0, 0, 1],
                "subtest-failure": [0, 0, 0, 0, 0, 1], "setup-only": [1] * 6}
    assert sorted(result.returncode for result in outcomes) == expected[mode], outcomes
    values = [json.loads(path.read_text()) for path in sorted(outputs.glob("*.json"))]
    assert values[0]["selected"]
    assert all(node.startswith("tools/test_example.py::") for node in values[0]["selected"])
    aggregate = subprocess.run([sys.executable, str(script), "verify", str(outputs)], cwd=tmp_path,
                               capture_output=True, text=True)
    assert (aggregate.returncode == 0) == (mode == "success"), aggregate.stderr
    if mode == "failure":
        failed = next(value for value in values if value["exitCode"] == 1)
        assert "tools/test_example.py::test_injected_failure" in failed["selected"]


def test_workflow_entrypoint_requires_the_scheduling_input(tmp_path):
    script = fixture_runner(tmp_path)
    (tmp_path / ".github/self-test-costs.json").unlink()
    result = subprocess.run([sys.executable, str(script), "run", "0", "--output", str(tmp_path / "receipt.json")],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "self-test-costs.json" in result.stderr
