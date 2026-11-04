"""What the build/test gate judges on each component's pinned commit, read once from the certification YAMLs.

A component's check-runs split into the core verdict and named exclusions: env-gated live-integration lanes
(env-gated-checks.yaml), aggregate roll-ups (rollup-checks.yaml), dependency-security signals
(security-checks.yaml), non-build governance jobs (governance-checks.yaml) and checks filed on the commit that
do not judge it (advisory-checks.yaml). The commit proves its builds and tests ran through either a full-matrix
Actions run (full-matrix-checks.yaml) or its required lanes (required-checks.yaml).

Loading validates every file: a malformed entry raises instead of being skipped, so a typo cannot quietly
drop a requirement or an exclusion. A missing file declares nothing.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

import yaml

from required_lanes import REQUIRED_PATH, load_required_lanes

CERTIFICATION = Path(__file__).resolve().parent
ORG = "honua-io"
WORKFLOW_PREFIX = ".github/workflows/"
SHARD_INVENTORY_KEYS = frozenset({"path", "jobName", "testStep"})

Lane = tuple[str, str]


@dataclass(frozen=True)
class ComponentPolicy:
    component: str = ""
    env_gated_names: frozenset[str] = frozenset()
    env_gated_events: frozenset[Lane] = frozenset()
    rollup_names: frozenset[str] = frozenset()
    security_names: frozenset[str] = frozenset()
    governance_names: frozenset[str] = frozenset()
    advisory_names: frozenset[str] = frozenset()
    advisory_workflows: frozenset[str] = frozenset()
    full_matrix: Mapping | None = None
    required_lanes: frozenset[Lane] = frozenset()

    def is_advisory(self, run: dict) -> bool:
        # A workflow exclusion applies only when enrichment recorded the path, so a missing metadata read
        # keeps the run core and cannot hide a red.
        path = str(run.get("_workflow_path") or "").strip()
        return str(run.get("name", "")) in self.advisory_names or (bool(path) and path in self.advisory_workflows)


@dataclass(frozen=True)
class PolicyFiles:
    env_gated: Path = CERTIFICATION / "env-gated-checks.yaml"
    rollup: Path = CERTIFICATION / "rollup-checks.yaml"
    security: Path = CERTIFICATION / "security-checks.yaml"
    governance: Path = CERTIFICATION / "governance-checks.yaml"
    advisory: Path = CERTIFICATION / "advisory-checks.yaml"
    full_matrix: Path = CERTIFICATION / "full-matrix-checks.yaml"
    required: Path = REQUIRED_PATH


@dataclass(frozen=True)
class BuildTestPolicy:
    components: Mapping[str, ComponentPolicy] = field(default_factory=dict)

    def component(self, name: str) -> ComponentPolicy:
        return self.components.get(name) or ComponentPolicy(component=name)

    @classmethod
    def load(cls, files: PolicyFiles = PolicyFiles()) -> BuildTestPolicy:
        fields: dict[str, dict[str, object]] = defaultdict(dict)
        for component, entries in _component_entries(files.env_gated, ("name", "workflow")):
            fields[component]["env_gated_names"] = _values(entries, "name")
            fields[component]["env_gated_events"] = _workflow_events(component, entries)
        for attribute, path in (("rollup_names", files.rollup), ("security_names", files.security),
                                ("governance_names", files.governance)):
            for component, entries in _component_entries(path, ("name",)):
                fields[component][attribute] = _values(entries, "name")
        for component, entries in _component_entries(files.advisory, ("name", "workflow")):
            fields[component]["advisory_names"] = _values(entries, "name")
            fields[component]["advisory_workflows"] = _values(entries, "workflow")
        for component, raw in _read_mapping(files.full_matrix).items():
            fields[component]["full_matrix"] = _full_matrix_policy(component, raw)
        for component, lanes in load_required_lanes(files.required).items():
            fields[component]["required_lanes"] = lanes
        return cls({name: ComponentPolicy(component=name, **values) for name, values in fields.items()})


def _read_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping of component to entries")
    return {str(component): value for component, value in data.items()}


def _component_entries(path: Path, identifying_keys: tuple[str, ...]) -> Iterator[tuple[str, list[dict]]]:
    """Each component's entries; every entry is a mapping naming at least one of `identifying_keys`."""
    for component, entries in _read_mapping(path).items():
        entries = entries or []
        if not isinstance(entries, list):
            raise ValueError(f"{path.name}: {component} needs a list of entries")
        for entry in entries:
            if not isinstance(entry, dict) or not any(_text(entry, key) for key in identifying_keys):
                raise ValueError(f"{path.name}: every {component} entry needs one of {list(identifying_keys)}")
        yield component, entries


def _text(entry: dict, key: str) -> str:
    return str(entry.get(key) or "").strip()


def _values(entries: list[dict], key: str) -> frozenset[str]:
    return frozenset(value for value in (_text(entry, key) for entry in entries) if value)


def _workflow_events(component: str, entries: list[dict]) -> frozenset[Lane]:
    """(workflow path, event) pairs: a run is env-gated only when both match, so other events stay core."""
    pairs: set[Lane] = set()
    for entry in entries:
        workflow = _text(entry, "workflow")
        if not workflow:
            continue
        events = [str(event).strip() for event in entry.get("events") or [] if str(event).strip()]
        if not workflow.startswith(WORKFLOW_PREFIX) or not events:
            raise ValueError(f"env-gated {component}: {workflow!r} needs a {WORKFLOW_PREFIX} path and events")
        pairs |= {(workflow, event) for event in events}
    return frozenset(pairs)


def _full_matrix_policy(component: str, raw: object) -> dict:
    """The full-matrix run a component's pin requires, in the shape full_matrix_evidence verifies."""
    policy = raw if isinstance(raw, dict) else {}
    workflow = _text(policy, "workflow")
    events = frozenset(str(event).strip() for event in policy.get("events") or [] if str(event).strip())
    required = frozenset(_text(entry, "name") for entry in policy.get("requiredChecks") or []
                         if isinstance(entry, dict) and _text(entry, "name"))
    if not (workflow and events and required):
        raise ValueError(f"full-matrix policy for {component} needs workflow, events and requiredChecks")
    inventory = policy.get("shardInventory")
    if not isinstance(inventory, dict) or set(inventory) != SHARD_INVENTORY_KEYS:
        raise ValueError("full-matrix policy needs an explicit pinned shard inventory")
    return {"workflow": workflow, "events": events, "requiredChecks": required,
            "repository": f"{ORG}/{component}", "shardInventory": inventory}
