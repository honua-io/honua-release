"""Fail-closed controls for scenarios the current runner cannot support."""
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from agent_e2e_local_api import Seeded, Stack

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("w2g1_oracle", ROOT / "certification/agent-e2e/corpus/oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


@pytest.fixture
def stack(tmp_path):
    return Stack("http://127.0.0.1:1", "controller-only", None, "fake", "fake.yml", str(tmp_path), "sha256:" + "0" * 64)


@pytest.mark.parametrize("scenario_id", ["P01", "P02", "P12", "P24"])
def test_unsupported_fails_closed(scenario_id, stack):
    module = importlib.import_module("agent_e2e_local_scenarios." + scenario_id)
    assert module.id == scenario_id
    assert module.supported_variants == ()
    assert "unsupported" in module.__doc__.lower()
    expectations = json.loads((ROOT / "certification/agent-e2e/expectations/local" / (scenario_id + ".json")).read_text())
    assert expectations == {"A": {}, "B": {}}
    for variant in ("A", "B"):
        for call in (lambda: module.seed(stack, variant), lambda: module.fault_plan(variant),
                     lambda: module.observe(stack, Seeded({}), variant), lambda: module.expected(variant),
                     lambda: module.cleanup(stack, Seeded({}))):
            with pytest.raises(RuntimeError, match="unsupported"):
                call()


@pytest.mark.parametrize("scenario_id, assertion, correct, wrong, expected", [
    ("P01", "deployment.ready", {"deploymentId": "fixture", "healthStatus": 200, "authenticatedReadStatus": 200},
     {"deploymentId": "fixture", "healthStatus": 503, "authenticatedReadStatus": 200},
     {"deploymentId": "fixture", "healthStatus": 200, "authenticatedReadStatus": 200}),
    ("P02", "restart.observed", {"resourceId": "fixture", "states": ["running", "stopped", "running"]},
     {"resourceId": "fixture", "states": ["running", "running"]},
     {"resourceId": "fixture", "terminalState": "running", "requiredStates": ["stopped"]}),
    ("P12", "state.reopened", {"resourceId": "map", "versionId": "v1", "documentSha256": "fixture", "freshSessionId": "new"},
     {"resourceId": "map", "versionId": "v2", "documentSha256": "fixture", "freshSessionId": "new"},
     {"resourceId": "map", "versionId": "v1", "documentSha256": "fixture", "freshSessionId": "new"}),
    ("P24", "filter.exact", oracle.fixture_expectation("A", "filter.exact"), {"resourceIds": []},
     oracle.fixture_expectation("A", "filter.exact")),
])
def test_oracle_rejects_wrong_state(scenario_id, assertion, correct, wrong, expected):
    # Synthetic controls are not scenario expectations or observations.
    assert importlib.import_module("agent_e2e_local_scenarios." + scenario_id).supported_variants == ()
    oracle.verify(assertion, correct, expected)
    with pytest.raises(oracle.VerificationError):
        oracle.verify(assertion, wrong, expected)
