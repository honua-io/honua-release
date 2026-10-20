"""Grounding/status source controls; installed-candidate proof remains separate."""
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["validation", "journey"])
def test_grounding_status_original_observations_and_semantics(name):
    result = subprocess.run(["python3", "-B", f"certification/installed-clients/mcp_grounding_status_{name}_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_grounding_status_full_producer_uses_real_fixed_client_and_wire():
    result = subprocess.run(["node", "--test", "--test-concurrency=1",
                             "certification/installed-clients/mcp_grounding_status_execution_controls.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
