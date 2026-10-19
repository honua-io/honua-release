"""Independent canonical-client location semantics join the existing test floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_location_oracle():
    result = subprocess.run(["python3", "certification/installed-clients/mcp_location_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_location_wire_execution():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_location_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
