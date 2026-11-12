import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import sys

import pytest

PARTITION_COUNT = 6
SELF_TEST_PATHS = (
    "tools/", "e2e/test_licensing.py", "e2e/test_upgrade_chaos.py", "e2e/test_cloud.py",
    "e2e/test_deterministic_refusal_profile.py", "e2e/test_cost_meter.py", "e2e/test_aws_eks.py",
    "e2e/test_large_import.py", "e2e/test_retire_cloud_cell.py", "e2e/test_ga_single_tenant.py",
)


def require_unique_inventory(inventory):
    if not inventory or len(inventory) != len(set(inventory)):
        raise ValueError("self-test inventory must be nonempty and unique")


def partition_inventory(inventory, file_seconds):
    require_unique_inventory(inventory)
    files = defaultdict(list)
    for node in sorted(inventory):
        files[node.split("::", 1)[0]].append(node)
    costs = {file: file_seconds.get(file, 1.0) for file in files}
    if any(type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0
           for cost in costs.values()):
        raise ValueError("self-test scheduling costs must be positive finite seconds")
    groups = [[] for _ in range(PARTITION_COUNT)]
    loads = [0.0] * PARTITION_COUNT
    for file in sorted(files, key=lambda file: (-costs[file], file)):
        index = min(range(PARTITION_COUNT), key=lambda index: (loads[index], index))
        groups[index].extend(files[file])
        loads[index] += costs[file]
    return [sorted(group) for group in groups]


class PartitionSelection:
    def __init__(self, partition, file_seconds):
        self.partition = partition
        self.file_seconds = file_seconds
        self.inventory = []
        self.selected = []
        self.executed = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        self.inventory = sorted(item.nodeid for item in items)
        self.selected = partition_inventory(self.inventory, self.file_seconds)[self.partition]
        selected = set(self.selected)
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=deselected)

    def pytest_runtest_logreport(self, report):
        is_test_call = report.when == "call" and not hasattr(report, "context")
        if is_test_call or (report.when == "setup" and report.skipped):
            self.executed.append(report.nodeid)


def require_complete_execution(selected, executed):
    if not selected or Counter(executed) != Counter(selected):
        raise ValueError("self-test partition did not execute its full selection")


def run_partition(root, partition, output):
    if partition not in range(PARTITION_COUNT):
        raise ValueError("invalid self-test partition")
    costs_path = root / ".github/self-test-costs.json"
    costs = json.loads(costs_path.read_text())
    selection = PartitionSelection(partition, costs)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    previous_directory = Path.cwd()
    previous_import_path = list(sys.path)
    try:
        os.chdir(root)
        sys.path.insert(0, str(root))
        exit_code = int(pytest.main([*SELF_TEST_PATHS, "-q", f"--junitxml={output.with_suffix('.xml')}"],
                                   plugins=[selection]))
    finally:
        os.chdir(previous_directory)
        sys.path[:] = previous_import_path
    if exit_code == 0:
        try:
            require_complete_execution(selection.selected, selection.executed)
        except ValueError as error:
            print(error, file=sys.stderr)
            exit_code = 1
    output.write_text(json.dumps({"partition": partition, "inventory": selection.inventory,
                                  "selected": selection.selected, "executed": sorted(selection.executed),
                                  "exitCode": exit_code}, indent=2) + "\n")
    return exit_code


def verify_receipts(receipts):
    indexes = sorted(receipt["partition"] for receipt in receipts)
    if len(receipts) != PARTITION_COUNT or indexes != list(range(PARTITION_COUNT)):
        raise ValueError("missing or duplicate self-test partitions")
    inventory = receipts[0]["inventory"]
    require_unique_inventory(inventory)
    selected = []
    for receipt in receipts:
        if receipt["exitCode"] != 0:
            raise ValueError("self-test partition failed")
        if receipt["inventory"] != inventory:
            raise ValueError("self-test collection drift between partitions")
        require_complete_execution(receipt["selected"], receipt["executed"])
        selected.extend(receipt["selected"])
    if Counter(selected) != Counter(inventory):
        raise ValueError("self-test selections must cover the inventory exactly once")


def main():
    parser = argparse.ArgumentParser(description="Run and conserve the six manifest self-test partitions.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("partition", type=int, choices=range(PARTITION_COUNT))
    run.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        return run_partition(Path(__file__).resolve().parents[1], args.partition, args.output)
    verify_receipts([json.loads(path.read_text()) for path in sorted(args.directory.glob("partition-*.json"))])
    print("All six self-test partitions executed the complete inventory exactly once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
