"""Direct-cancel source controls; live candidate proof remains separate."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_direct_cancel_semantic_and_store_controls():
    for name in ("mcp_direct_cancel_validation_controls", "mcp_direct_cancel_state_controls"):
        result = subprocess.run(["python3", "-B", "-m", "unittest", "-v", name],
                                cwd=ROOT / "certification/installed-clients", capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr


def test_direct_cancel_complete_controlled_factory_and_wire():
    result = subprocess.run(["node", "--test", "--test-concurrency=1",
                             "certification/installed-clients/mcp_direct_cancel_execution_controls.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
