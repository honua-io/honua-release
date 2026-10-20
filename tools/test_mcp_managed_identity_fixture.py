"""The shared normal managed identity fixture joins maintained source controls."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_normal_managed_identity_provisioning_readback_revoke_and_failure_controls():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_managed_identity_fixture_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
