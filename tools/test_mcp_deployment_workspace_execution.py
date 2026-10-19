"""Original deployment/workspace primary execution joins the maintained source floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_deployment_workspace_semantics_and_independent_observer():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_deployment_workspace_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_deployment_workspace_complete_dependency_and_proof_binding():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_deployment_workspace_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=360)
    assert result.returncode == 0, result.stdout + result.stderr
