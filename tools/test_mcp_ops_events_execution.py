"""Findings/events source controls; candidate/client execution remains separate."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["state", "config", "setup", "validation", "data_read", "journey"])
def test_owned_findings_events_observers_authoring_and_semantic_controls(name):
    result = subprocess.run(["python3", "-B", f"certification/installed-clients/mcp_ops_events_{name}_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_findings_events_bridge_rejudges_complete_sdk_and_inspector_vectors():
    result = subprocess.run(["node", "--test", "--test-concurrency=1",
                             "certification/installed-clients/mcp_ops_events_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr


def test_findings_events_full_producer_orchestration_and_fail_closed_cleanup():
    result = subprocess.run(["node", "--test", "--test-concurrency=1",
                             "certification/installed-clients/mcp_ops_events_execution_controls.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
