"""The build/test gate's policy files load once into one model, and a malformed entry is refused."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "certification"))
import check_build_test as bt  # noqa: E402
from build_test_policy import BuildTestPolicy, ComponentPolicy, PolicyFiles  # noqa: E402

SERVER_MATRIX = """\
lib:
  workflow: .github/workflows/ci.yml
  events: [schedule]
  shardInventory: {path: .github/ci-shards.json, jobName: "Tests ({shard_name})", testStep: Run}
  requiredChecks:
    - name: Build
"""


def _load(tmp_path: Path, **documents: str) -> BuildTestPolicy:
    """Load a policy from the given file bodies, keyed by PolicyFiles field; every other file is absent."""
    paths = {}
    for field in PolicyFiles.__dataclass_fields__:
        path = tmp_path / f"{field}.yaml"
        if field in documents:
            path.write_text(documents[field], encoding="utf-8")
        paths[field] = path
    return BuildTestPolicy.load(PolicyFiles(**paths))


def test_absent_files_declare_nothing(tmp_path):
    policy = _load(tmp_path)
    assert policy.components == {}
    assert policy.component("lib") == ComponentPolicy(component="lib")


def test_each_file_fills_its_part_of_one_component_policy(tmp_path):
    policy = _load(
        tmp_path,
        env_gated='lib:\n  - name: Live\n  - workflow: .github/workflows/live.yml\n    events: [schedule]\n',
        rollup="lib:\n  - name: Gate\n",
        security="lib:\n  - name: Dependabot\n",
        governance="lib:\n  - name: Attest\n",
        advisory="lib:\n  - name: Observer\n  - workflow: .github/workflows/observe.yml\n",
        required="lib:\n  .github/workflows/ci.yml: [build]\n",
    ).component("lib")
    assert policy == ComponentPolicy(
        component="lib", env_gated_names=frozenset({"Live"}),
        env_gated_events=frozenset({(".github/workflows/live.yml", "schedule")}),
        rollup_names=frozenset({"Gate"}), security_names=frozenset({"Dependabot"}),
        governance_names=frozenset({"Attest"}), advisory_names=frozenset({"Observer"}),
        advisory_workflows=frozenset({".github/workflows/observe.yml"}),
        required_lanes=frozenset({(".github/workflows/ci.yml", "build")}))


def test_a_complete_full_matrix_entry_loads_in_the_verifier_shape(tmp_path):
    matrix = _load(tmp_path, full_matrix=SERVER_MATRIX).component("lib").full_matrix
    assert matrix["repository"] == "honua-io/lib"
    assert matrix["events"] == frozenset({"schedule"}) and matrix["requiredChecks"] == frozenset({"Build"})


@pytest.mark.parametrize("missing", ["  workflow: .github/workflows/ci.yml\n", "  events: [schedule]\n",
                                     "  requiredChecks:\n    - name: Build\n"])
def test_an_incomplete_full_matrix_entry_is_refused_not_dropped(tmp_path, missing):
    document = SERVER_MATRIX.replace(missing, "")
    with pytest.raises(ValueError, match="needs workflow, events and requiredChecks"):
        _load(tmp_path, full_matrix=document)


@pytest.mark.parametrize("field, document", [
    ("rollup", "lib:\n  - why: no name\n"),
    ("governance", "lib:\n  - Attest\n"),
    ("security", "lib: {name: Dependabot}\n"),
    ("advisory", "lib:\n  - why: neither a name nor a workflow\n"),
    ("env_gated", "lib:\n  - workflow: .github/workflows/live.yml\n"),
    ("env_gated", "lib:\n  - workflow: live.yml\n    events: [schedule]\n"),
    ("env_gated", "- lib\n"),
])
def test_a_malformed_exclusion_entry_is_refused(tmp_path, field, document):
    with pytest.raises(ValueError):
        _load(tmp_path, **{field: document})


def test_an_advisory_workflow_needs_the_enriched_path():
    policy = ComponentPolicy(advisory_workflows=frozenset({".github/workflows/observe.yml"}))
    assert policy.is_advisory({"name": "observe", "_workflow_path": ".github/workflows/observe.yml"})
    assert not policy.is_advisory({"name": "observe"})


def test_the_cli_policy_paths_all_reach_the_loader(tmp_path):
    flags = ("env-gated", "rollup", "security", "governance", "advisory", "full-matrix", "required")
    args = bt._parse_args([argument for flag in flags for argument in (f"--{flag}", str(tmp_path / flag))])
    files = bt._policy_files(args)
    assert {str(getattr(files, field)) for field in PolicyFiles.__dataclass_fields__} == {
        str(tmp_path / flag) for flag in flags}
