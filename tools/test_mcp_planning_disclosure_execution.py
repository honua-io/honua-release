"""Planning source controls; actual model/candidate qualification remains separate."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_planning_disclosure_full_wire_and_coherent_semantic_refusals():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_planning_disclosure_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_planning_bridge_rejudges_both_complete_original_vectors():
    result = subprocess.run(["node", "--test", "--test-concurrency=1",
                             "certification/installed-clients/mcp_planning_disclosure.test.mjs",
                             "certification/installed-clients/mcp_planning_execution_controls.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
