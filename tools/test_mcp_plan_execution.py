"""Release owns the executable canonical-client plan scenario, not the spec."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_javascript_plan_scenario():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_plan_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
