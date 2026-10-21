"""Required floor executes authored worker-result publication producer controls."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_publish_result_semantic_controls():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_publish_result_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_publish_result_source_binding_controls():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_publish_result_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
