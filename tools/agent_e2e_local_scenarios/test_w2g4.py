"""Unsupported governance cases must remain visible and impossible to grade green.

There is no correct complete local fixture state for these adapters. In
particular, an HTTP 200 or an invented custody receipt cannot replace missing
native principals, original worker history, or clarification event custody.
These tests prove refusal, assertion coverage and negative controls; they do
not claim a passing scenario or exercise a Docker stack.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from agent_e2e_local_api import Observation, Seeded, Stack


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORACLE = load("w2g4_oracle", ROOT / "certification/agent-e2e/corpus/oracle.py")
IDS = ("P14", "P15", "P16", "P17", "P18", "P20")
VARIANTS = ("A", "B")
# Select only public development prompts. No held-out prompt enters test data.
PACK = {
    row["id"]: {
        "assertions": row["assertions"],
        "prompts": {variant: row["prompts"][variant] for variant in VARIANTS},
    }
    for row in json.loads((ROOT / "certification/agent-e2e/pack.v2.json").read_text())["scenarios"]
    if row["id"] in IDS
}


@pytest.fixture(params=IDS)
def scenario(request):
    return load("w2g4_" + request.param, Path(__file__).parent / (request.param + ".py"))


@pytest.fixture
def stack(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported scenario attempted I/O or process execution")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return Stack(
        base_url="http://127.0.0.1:8147", admin_key="fake-controller-only",
        approver_key="fake-separate-approver", compose_project="fixture-case",
        compose_file=str(tmp_path / "absent-compose.yml"), workdir=str(tmp_path),
        candidate_digest="sha256:" + "1" * 64,
        extra={"untrusted_success": {"httpStatus": 200, "pass": True}},
    )


@pytest.mark.parametrize("variant", VARIANTS)
def test_seed_repeatedly_refuses_without_side_effects(scenario, stack, variant):
    assert scenario.supported_variants == ()
    assert "Unsupported" in " ".join(scenario.__doc__.split())
    for _ in range(2):
        with pytest.raises(NotImplementedError, match=scenario.id):
            scenario.seed(stack, variant)
    assert list(Path(stack.workdir).iterdir()) == []
    assert scenario.fault_plan(variant) is None
    assert scenario.needs_approver() is (scenario.id in ("P14", "P17"))


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_pack_assertion_is_explicitly_unavailable(scenario, stack, variant):
    observed = scenario.observe(stack, Seeded({}), variant)
    assert [row.assertion_id for row in observed] == PACK[scenario.id]["assertions"]
    assert list(scenario.expected(variant)) == PACK[scenario.id]["assertions"]
    for row in observed:
        assert isinstance(row, Observation)
        assert row.observed is None
        assert row.evidence["requests"] == []
        assert row.evidence["unsupportedReason"] in " ".join(scenario.__doc__.split())
        with pytest.raises(ORACLE.VerificationError):
            ORACLE.verify(row.assertion_id, row.observed, scenario.expected(variant)[row.assertion_id])


@pytest.mark.parametrize("variant", VARIANTS)
def test_negative_control_cannot_turn_missing_facts_into_success(scenario, stack, variant):
    # Per-module negative control, for every assertion. Even a forged success
    # object cannot be graded against an unavailable frozen expectation.
    for assertion, expectation in scenario.expected(variant).items():
        for wrong in ({"pass": True}, {"requests": []}, {"status": "Approved"}):
            with pytest.raises(ORACLE.VerificationError):
                ORACLE.verify(assertion, wrong, expectation)


def test_no_cleanup_ownership_is_invented(scenario, stack):
    for _ in range(2):
        assert scenario.cleanup(stack, Seeded({})) is None
    with pytest.raises(ValueError, match="cannot own"):
        scenario.cleanup(stack, Seeded({"resourceId": "unowned-resource"}))
    assert list(Path(stack.workdir).iterdir()) == []


def test_frozen_unavailability_has_fixture_provenance(scenario):
    manifest = json.loads(scenario._EXPECTATIONS.read_text())
    assert manifest["schemaVersion"] == 1
    assert manifest["scenarioId"] == scenario.id
    assert manifest["supportedVariants"] == []
    assert manifest["unsupportedReason"] in " ".join(scenario.__doc__.split())
    assert set(manifest["variants"]) == set(VARIANTS)
    assert len(manifest["provenance"]["sources"]) == 2
    for variant, source in zip(VARIANTS, manifest["provenance"]["sources"]):
        assert source["path"] == f"certification/agent-e2e/corpus/oracles/town-{variant}.json"
        assert hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest() == source["sha256"]
        # No model-run IDs, invented permissions or native worker identities.
        assert set(manifest["variants"][variant]) == set(PACK[scenario.id]["assertions"])
        assert all(value is None for value in manifest["variants"][variant].values())


def test_expectations_are_not_mutable_process_cache(scenario):
    expected = scenario.expected("A")
    expected.clear()
    assert set(scenario.expected("A")) == set(PACK[scenario.id]["assertions"])


def test_no_model_visible_guidance_or_extra_authority(scenario, stack):
    # Scenario modules have no prompt/action/transport builder. The runner alone
    # selects the unchanged pack prompt; even stack.extra cannot inject facts.
    tree = ast.parse(Path(scenario.__file__).read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports <= {"__future__", "json", "pathlib", "typing", "agent_e2e_local_api"}
    public_functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
    }
    assert public_functions == {"seed", "fault_plan", "needs_approver", "observe", "expected", "cleanup"}
    stack.extra.update({"observations": {a: {"pass": True} for a in PACK[scenario.id]["assertions"]}})
    assert all(row.observed is None for row in scenario.observe(stack, Seeded({}), "A"))
    for prompt in PACK[scenario.id]["prompts"].values():
        assert all(word not in prompt for word in (
            "processId", "parameterName", "requesterPrincipalId", "proposalId", "admin_key",
        ))


def test_unsupported_variants_rejected_by_every_variant_method(scenario, stack):
    for variant in ("", "unknown", None):
        for call in (
            lambda: scenario.seed(stack, variant),
            lambda: scenario.fault_plan(variant),
            lambda: scenario.observe(stack, Seeded({}), variant),
            lambda: scenario.expected(variant),
        ):
            with pytest.raises(ValueError, match="development variants"):
                call()
